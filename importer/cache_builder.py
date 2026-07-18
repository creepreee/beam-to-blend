from __future__ import annotations

"""Builds a BVC binary vertex cache from a scanned BeamNG sequence.

Memory-light: frames are read one at a time. The base mesh (triangle indices)
for each stable object is written once from the first frame; every frame then
contributes only its stable objects' vertex positions to the frame stream.
Dynamic objects get per-frame full mesh data in a post-frame-block section.
"""

import os
import struct
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

from . import binary
from .gltf_reader import GLBSequenceReader
from .scanner import (
    ObjectSignature,
    SequenceManifest,
    _edge_count,
    _face_count,
)
from .topology import TopologyHasher


class CacheBuildError(Exception):
    pass


class CaptureBuildError(CacheBuildError):
    """Raised when building from a capture file fails."""
    pass


class CacheBuilder:
    def __init__(self, sequence_dir: Path, out_path: Path):
        self.sequence_dir = Path(sequence_dir)
        self.out_path = Path(out_path)

    def build(
        self,
        manifest: Optional[SequenceManifest] = None,
        weld: bool = False,
        workers: int = 1,
    ) -> SequenceManifest:
        import traceback

        total_frames = 0
        try:
            return self._build_impl(manifest, weld, workers)
        except CacheBuildError:
            raise
        except Exception as exc:
            details = traceback.format_exc()
            sys.stderr.write(f"[BeamNG] BUILD CACHE ERROR:\n{details}\n")
            sys.stderr.flush()
            raise CacheBuildError(f"build failed: {exc}") from exc

    def _build_impl(
        self,
        manifest: Optional[SequenceManifest],
        weld: bool,
        workers: int,
    ) -> SequenceManifest:
        from pathlib import Path
        from .parallel_reader import read_frames_parallel

        frames = sorted(self.sequence_dir.glob("*.glb"))
        if not frames:
            raise CacheBuildError(f"no .glb frames in {self.sequence_dir}")

        self.sequence_dir = Path(self.sequence_dir)
        total_frames = len(frames)
        reader = GLBSequenceReader()

        # --- Frame 0: baseline + base meshes + initial manifest ---------
        sys.stderr.write(f"[BeamNG]   reading frame 0 base mesh from {frames[0].name} ..." + chr(10))
        sys.stdout.flush()
        first_doc = reader.read(frames[0], want_materials=True)
        first = first_doc.by_name()

        if manifest is None:
            manifest = SequenceManifest(
                sequence_name=self.sequence_dir.name, frame_count=total_frames
            )
            for obj_name, obj in first.items():
                topo_hash = TopologyHasher.hash_indices(obj.vertex_count, obj.indices)
                manifest.objects[obj_name] = ObjectSignature(
                    name=obj_name,
                    vertex_count=obj.vertex_count,
                    edge_count=_edge_count(obj.indices, obj.face_count),
                    face_count=obj.face_count,
                    topology_hash=topo_hash,
                )
        # else: manifest was provided (e.g. from a prior scan)

        # Baseline topology hashes for drift detection
        baseline_hash: Dict[str, str] = {
            n: s.topology_hash for n, s in manifest.objects.items()
        }

        # Mutable lists — objects move from stable → dynamic as drift is detected
        current_stable: List[str] = sorted(
            n for n, s in manifest.objects.items() if s.cacheable
        )
        current_dynamic: List[str] = sorted(
            n for n, s in manifest.objects.items() if not s.cacheable
        )
        sigs = manifest.objects

        print(f"[BeamNG] Building cache: {len(current_stable)} provisional stable, "
              f"{len(current_dynamic)} dynamic, "
              f"{total_frames} frames -> {self.out_path}")
        sys.stdout.flush()

        for name in current_stable:
            if name not in first:
                raise CacheBuildError(f"stable object {name!r} missing in frame 0")

        # --- Store per-object UV + material data from frame 0 ------------
        frame0_uvs: Dict[str, np.ndarray] = {}
        frame0_matnames: Dict[str, List[str]] = {}
        frame0_matids: Dict[str, np.ndarray] = {}
        for name in first:
            obj = first[name]
            frame0_uvs[name] = obj.uvs
            frame0_matnames[name] = obj.material_names
            frame0_matids[name] = obj.face_material_ids

        # --- Build global material name table ----------------------------
        all_material_names: List[str] = []
        name_index: Dict[str, int] = {}
        def _ensure_name(n: str) -> int:
            idx = name_index.get(n)
            if idx is None:
                idx = len(all_material_names)
                name_index[n] = idx
                all_material_names.append(n)
            return idx

        for name in first:
            matnames = frame0_matnames.get(name)
            if matnames:
                for mn in matnames:
                    _ensure_name(mn)
        # Pre-register fallback name so objects without materials get a valid
        # global id that exists in the material name table.
        _ensure_name("__no_material__")

        # --- Compute weld tables (opt-in) -------------------------------
        weld_tables: Dict[str, tuple] = {}
        welded_uv_map: Dict[str, np.ndarray] = {}
        if weld:
            sys.stderr.write("[BeamNG]   computing weld tables ..." + chr(10))
            sys.stderr.flush()
            welded_before = welded_after = 0
            for name in current_stable:
                base_pos = first[name].positions
                rounded = np.round(base_pos, WELD_DECIMALS)
                _uniq, rep_idx, inv = np.unique(
                    rounded, axis=0, return_index=True, return_inverse=True
                )
                rep_idx = np.ascontiguousarray(rep_idx, dtype=np.int64)
                inv = np.ascontiguousarray(inv.reshape(-1), dtype=np.int64)
                weld_tables[name] = (rep_idx, inv)
                if frame0_uvs.get(name) is not None:
                    welded_uv_map[name] = np.ascontiguousarray(
                        frame0_uvs[name][rep_idx], dtype=np.float32
                    )
                welded_before += base_pos.shape[0]
                welded_after += rep_idx.shape[0]
            if welded_before:
                pct = 100.0 * (welded_before - welded_after) / welded_before
                print(f"[BeamNG]   weld: {welded_before} -> {welded_after} verts "
                      f"({pct:.1f}% removed) across {len(current_stable)} objects")
                sys.stdout.flush()

        def vcount(name: str) -> int:
            t = weld_tables.get(name)
            return int(t[0].shape[0]) if t is not None else sigs[name].vertex_count

        # --- Layout: computed from INITIAL stable set (maximum size) -----
        frame_vertex_offset: Dict[str, int] = {}
        cursor = 0
        for name in current_stable:
            frame_vertex_offset[name] = cursor
            cursor += vcount(name) * 3
        max_stable_vertex_total = sum(vcount(n) for n in current_stable)

        all_names_initial = list(sigs.keys())
        initial_table_size = sum(
            2 + len(n.encode("utf-8")) + binary._OBJ_TAIL.size + 16  # +16 for QQ uv/material offsets
            for n in all_names_initial
        )
        padded_table_size = initial_table_size + 4096
        object_table_offset = binary.HEADER_SIZE
        base_mesh_offset = object_table_offset + padded_table_size

        base_index_offset: Dict[str, int] = {}
        index_count: Dict[str, int] = {}
        off = base_mesh_offset
        base_index_bytes: Dict[str, bytes] = {}
        welded_face_counts: Dict[str, int] = {}
        for name in current_stable:
            idx = first[name].indices
            if idx is None:
                raise CacheBuildError(f"stable object {name!r} has no indices")
            t = weld_tables.get(name)
            if t is not None:
                inv = t[1]
                idx32 = np.ascontiguousarray(inv[idx.reshape(-1)].reshape(idx.shape),
                                             dtype=np.int32)
                a, b, c = idx32[:, 0], idx32[:, 1], idx32[:, 2]
                valid = ~((a == b) | (b == c) | (a == c))
                n_degen = int((~valid).sum())
                if n_degen:
                    idx32 = np.ascontiguousarray(idx32[valid], dtype=np.int32)
                    print(f"[BeamNG]   welded {name}: removed {n_degen} degenerate triangle(s)")
                    sys.stdout.flush()
            else:
                idx32 = np.ascontiguousarray(idx, dtype=np.int32)
            raw = idx32.tobytes()
            base_index_bytes[name] = raw
            base_index_offset[name] = off
            index_count[name] = int(idx32.shape[0])
            welded_face_counts[name] = int(idx32.shape[0])
            off += len(raw)

        uv_blocks_offset = off
        uv_block_bytes: Dict[str, bytes] = {}
        for name in current_stable:
            uvs = welded_uv_map.get(name, frame0_uvs.get(name))
            if uvs is not None:
                uv_data = np.ascontiguousarray(uvs, dtype=np.float32).tobytes()
            else:
                uv_data = np.zeros((vcount(name), 2), dtype=np.float32).tobytes()
            uv_block_bytes[name] = uv_data
            off += len(uv_data)

        material_table_offset = off
        mat_table_bytes = binary.pack_material_name_table(all_material_names)
        off += len(mat_table_bytes)

        material_blocks_offset = off
        mat_block_bytes: Dict[str, bytes] = {}
        for name in current_stable:
            matnames = frame0_matnames.get(name)
            matids = frame0_matids.get(name)
            if matnames and matids is not None:
                global_ids = [_ensure_name(mn) for mn in matnames]
                face_count = welded_face_counts.get(name, 0)
                if face_count > 0 and len(matids) != face_count:
                    # Weld may have removed degenerate faces; filter matids
                    t = weld_tables.get(name)
                    if t is not None:
                        idx = first[name].indices
                        inv = t[1]
                        idx32 = np.ascontiguousarray(inv[idx.reshape(-1)].reshape(idx.shape), dtype=np.int32)
                        a, b, c = idx32[:, 0], idx32[:, 1], idx32[:, 2]
                        valid = ~((a == b) | (b == c) | (a == c))
                        matids = np.ascontiguousarray(matids[valid], dtype=np.uint16)
                per_face = matids.tolist() if matids is not None else []
            else:
                global_ids = [_ensure_name("__no_material__")]
                per_face = [0] * welded_face_counts.get(name, 0)
            mb = binary.pack_material_block(global_ids, per_face)
            mat_block_bytes[name] = mb
            off += len(mb)

        frame_directory_offset = off
        frame_blocks_offset = frame_directory_offset + total_frames * 8
        frame_block_size = max_stable_vertex_total * 3 * 4

        dynamic_frame_data: List[Dict[str, tuple]] = []

        # --- Write output file (single pass) -----------------------------
        _ensure_writable(self.out_path)
        out_str = os.fspath(self.out_path)
        sys.stderr.write(f"[BeamNG]   writing {out_str} ...\n")
        sys.stderr.flush()

        try:
            fh = open(out_str, "w+b")
        except OSError as exc:
            sys.stderr.write(f"[BeamNG]   FAILED open({out_str!r}): {exc}\n")
            sys.stderr.write(traceback.format_exc() + "\n")
            sys.stderr.flush()
            _raise_path_error(self.out_path, self.out_path.parent, exc)

        with fh:
            # Header placeholder (will patch after frame loop)
            fh.write(
                binary.pack_header(
                    frame_count=total_frames,
                    object_count=len(all_names_initial),
                    stable_count=len(current_stable),
                    stable_vertex_total=max_stable_vertex_total,
                    object_table_offset=object_table_offset,
                    base_mesh_offset=base_mesh_offset,
                    uv_blocks_offset=uv_blocks_offset,
                    material_table_offset=material_table_offset,
                    material_blocks_offset=material_blocks_offset,
                    frame_directory_offset=frame_directory_offset,
                    frame_blocks_offset=frame_blocks_offset,
                )
            )

            # Object table placeholder (zero-padded, rewritten after frame loop)
            fh.write(b"\x00" * padded_table_size)

            # Base mesh blocks
            for name in current_stable:
                fh.write(base_index_bytes[name])

            # UV blocks
            for name in current_stable:
                fh.write(uv_block_bytes[name])

            # Material name table
            fh.write(mat_table_bytes)

            # Material blocks
            for name in current_stable:
                fh.write(mat_block_bytes[name])

            # Frame directory (fixed stride based on max set)
            for i in range(total_frames):
                fh.write(
                    np.uint64(frame_blocks_offset + i * frame_block_size).tobytes()
                )

            # --- Single pass: classify + write positions -----------------
            if workers > 1:
                sys.stderr.write(
                    f"[BeamNG]   reading frames with {workers} worker processes ...\n"
                )
                sys.stderr.flush()

            for fi, record in read_frames_parallel(
                frames, reader.remap_cache,
                want_positions=True, want_indices=True,
                workers=workers,
            ):
                frame_path = frames[fi]
                sys.stderr.write(f"[BeamNG]   frame {fi + 1}/{total_frames} ({frame_path.name}) ...\n")
                sys.stdout.flush()

                # --- Topology check (skip frame 0 — already baseline) ---
                if fi > 0:
                    write_pos = fh.tell()
                    for obj_name in list(current_stable):
                        entry = record.get(obj_name)
                        if entry is None:
                            sigs[obj_name].cacheable = False
                            sigs[obj_name].fallback_reason = f"missing in {frame_path.name}"
                            current_stable.remove(obj_name)
                            current_dynamic.append(obj_name)
                            _backfill_dynamic(
                                fh, obj_name, fi,
                                frame_blocks_offset, frame_block_size,
                                frame_vertex_offset, vcount,
                                base_index_bytes,
                                dynamic_frame_data,
                            )
                            continue
                        vcount_obj, _pos, indices, _uv, _mats, _matids = entry
                        topo_hash = TopologyHasher.hash_indices(vcount_obj, indices)
                        if topo_hash != baseline_hash[obj_name]:
                            sigs[obj_name].cacheable = False
                            sigs[obj_name].fallback_reason = (
                                f"topology changed in {frame_path.name} "
                                f"({sigs[obj_name].vertex_count} verts -> {vcount_obj})"
                            )
                            current_stable.remove(obj_name)
                            current_dynamic.append(obj_name)
                            _backfill_dynamic(
                                fh, obj_name, fi,
                                frame_blocks_offset, frame_block_size,
                                frame_vertex_offset, vcount,
                                base_index_bytes,
                                dynamic_frame_data,
                            )
                    fh.seek(write_pos)

                    # Newly appearing objects → dynamic
                    for obj_name, entry in record.items():
                        if obj_name not in manifest.objects:
                            vcount_obj, _pos, indices, _uv, _mats, _matids = entry
                            fc = _face_count(indices, vcount_obj)
                            sig = ObjectSignature(
                                name=obj_name,
                                vertex_count=vcount_obj,
                                edge_count=_edge_count(indices, fc),
                                face_count=fc,
                                topology_hash=TopologyHasher.hash_indices(vcount_obj, indices),
                                cacheable=False,
                                fallback_reason=f"first appears in {frame_path.name}",
                            )
                            manifest.objects[obj_name] = sig
                            current_dynamic.append(obj_name)

                # --- Write positions for stable objects in slot order ------
                for name in frame_vertex_offset:
                    if name in current_stable:
                        entry = record.get(name)
                        if entry is None:
                            slot_bytes = vcount(name) * 3 * 4
                            fh.write(b"\x00" * slot_bytes)
                            continue
                        vcount_obj, positions, _idx, _uv, _mats, _matids = entry
                        pos = np.ascontiguousarray(self._gltf_to_blender(positions), dtype=np.float32)
                        t = weld_tables.get(name)
                        if t is not None:
                            rep_idx, inv = t
                            welded_pos = np.ascontiguousarray(pos[rep_idx], dtype=np.float32)
                            err = float(np.abs(welded_pos[inv] - pos).max())
                            if err > WELD_VERIFY_EPS:
                                raise CacheBuildError(
                                    f"weld group separated for {name!r} in "
                                    f"{frame_path.name}: max err {err:.6f} > {WELD_VERIFY_EPS}"
                                )
                            fh.write(welded_pos.tobytes())
                        else:
                            fh.write(pos.tobytes())
                    else:
                        fh.write(b"\x00" * (vcount(name) * 3 * 4))

                # --- Collect dynamic data for this frame ------------------
                fd: Dict[str, tuple] = {}
                for name in current_dynamic:
                    entry = record.get(name)
                    if entry is not None:
                        _vc, positions, indices, _uv, _mats, _matids = entry
                        pos = np.ascontiguousarray(self._gltf_to_blender(positions), dtype=np.float32).copy()
                        idx = np.ascontiguousarray(indices, dtype=np.int32).copy()
                        a, b, c = idx[:, 0], idx[:, 1], idx[:, 2]
                        valid = ~((a == b) | (b == c) | (a == c))
                        n_degen = int((~valid).sum())
                        if n_degen:
                            idx = np.ascontiguousarray(idx[valid], dtype=np.int32)
                            if _matids is not None:
                                _matids = np.ascontiguousarray(_matids[valid], dtype=np.uint16)
                        # Also gather dynamic UV/material data if present
                        dyn_uv = _uv if _uv is not None else None
                        dyn_matnames = _mats if _mats is not None else None
                        dyn_matids = _matids if _matids is not None else None
                        fd[name] = (pos, idx, dyn_uv, dyn_matnames, dyn_matids)
                dynamic_frame_data.append(fd)

            # --- After frame loop: rewrite object table with final classification ---
            all_names_final = list(manifest.objects.keys())
            current_stable = sorted(
                n for n, s in manifest.objects.items() if s.cacheable
            )
            current_dynamic = sorted(
                n for n, s in manifest.objects.items() if not s.cacheable
            )
            final_ordered = current_stable + [
                n for n in current_dynamic if n not in set(current_stable)
            ]

            # Compute UV + material offsets for each object
            uv_off: Dict[str, int] = {}
            mat_off: Dict[str, int] = {}
            uv_cursor = uv_blocks_offset
            mat_cursor = material_blocks_offset
            for name in current_stable:
                uv_off[name] = uv_cursor
                uv_cursor += len(uv_block_bytes.get(name, b""))
                mat_off[name] = mat_cursor
                mat_cursor += len(mat_block_bytes.get(name, b""))

            fh.seek(object_table_offset)
            for name in final_ordered:
                sig = manifest.objects[name]
                is_stable = sig.cacheable
                fh.write(
                    binary.pack_object_entry(
                        name=name,
                        stable=is_stable,
                        vertex_count=vcount(name) if is_stable else sig.vertex_count,
                        face_count=welded_face_counts.get(name, sig.face_count),
                        topology_hash=sig.topology_hash,
                        base_index_offset=base_index_offset.get(name, 0),
                        index_count=index_count.get(name, 0),
                        frame_vertex_offset=frame_vertex_offset.get(name, 0),
                        welded=name in weld_tables,
                        uv_offset=uv_off.get(name, 0),
                        material_offset=mat_off.get(name, 0),
                    )
                )

            # --- Dynamic data section (after frame blocks) ---------------
            if current_dynamic:
                fh.seek(0, os.SEEK_END)
                dynamic_directory_offset = fh.tell()

                dir_entries: List[dict] = []
                data_segments: List[bytes] = []
                for fi in range(total_frames):
                    fd = dynamic_frame_data[fi]
                    for name in current_dynamic:
                        if name in fd:
                            pos, idx, dyn_uv, dyn_matnames, dyn_matids = fd[name]
                            pos_bytes = np.ascontiguousarray(pos, dtype=np.float32).tobytes()
                            idx_bytes = np.ascontiguousarray(idx, dtype=np.int32).tobytes()
                            uv_bytes = b""
                            mat_block = b""
                            if dyn_uv is not None:
                                uv_bytes = np.ascontiguousarray(dyn_uv, dtype=np.float32).tobytes()
                            if dyn_matnames is not None and dyn_matids is not None:
                                global_ids = [_ensure_name(mn) for mn in dyn_matnames]
                                per_face = dyn_matids.tolist()
                                mat_block = binary.pack_material_block(global_ids, per_face)
                            dir_entries.append({
                                "vc": pos.shape[0],
                                "ic": idx.shape[0],
                                "pos_bytes": pos_bytes,
                                "idx_bytes": idx_bytes,
                                "uv_bytes": uv_bytes,
                                "mat_block": mat_block,
                            })
                            data_segments.append(pos_bytes + idx_bytes + uv_bytes + mat_block)
                        else:
                            dir_entries.append({"vc": 0, "ic": 0, "pos_bytes": b"", "idx_bytes": b"",
                                                "uv_bytes": b"", "mat_block": b""})

                dir_size = len(dir_entries) * binary.DYN_ENTRY_SIZE
                dynamic_data_offset_val = dynamic_directory_offset + dir_size
                data_cursor = dynamic_data_offset_val
                for de in dir_entries:
                    pos_off = data_cursor
                    idx_off = data_cursor + len(de["pos_bytes"])
                    uv_off_dyn = data_cursor + len(de["pos_bytes"]) + len(de["idx_bytes"])
                    mat_off_dyn = uv_off_dyn + len(de["uv_bytes"])
                    fh.write(binary.pack_dynamic_entry(
                        de["vc"], de["ic"], pos_off, idx_off, uv_off_dyn, mat_off_dyn,
                    ))
                    data_cursor += len(de["pos_bytes"]) + len(de["idx_bytes"]) + len(de["uv_bytes"]) + len(de["mat_block"])
                for blob in data_segments:
                    fh.write(blob)
            else:
                dynamic_directory_offset = 0
                dynamic_data_offset_val = 0

            # Patch header with corrected counts + dynamic offsets
            fh.seek(0)
            fh.write(
                binary.pack_header(
                    frame_count=total_frames,
                    object_count=len(all_names_final),
                    stable_count=len(current_stable),
                    stable_vertex_total=max_stable_vertex_total,
                    object_table_offset=object_table_offset,
                    base_mesh_offset=base_mesh_offset,
                    uv_blocks_offset=uv_blocks_offset,
                    material_table_offset=material_table_offset,
                    material_blocks_offset=material_blocks_offset,
                    frame_directory_offset=frame_directory_offset,
                    frame_blocks_offset=frame_blocks_offset,
                    dynamic_directory_offset=dynamic_directory_offset,
                    dynamic_data_offset=dynamic_data_offset_val,
                )
            )

        manifest.stable_objects = current_stable
        manifest.dynamic_objects = current_dynamic

        size_mb = self.out_path.stat().st_size / 1e6
        print(f"[BeamNG] cache written: {size_mb:.1f} MB ({total_frames} frames, "
              f"{len(manifest.stable_objects)} stable, {len(manifest.dynamic_objects)} dynamic)")
        sys.stdout.flush()
        return manifest

    @staticmethod
    def _gltf_to_blender(pos: np.ndarray) -> np.ndarray:
        """Convert glTF Y-up positions to Blender Z-up at BVC write time.

        glTF: X=right, Y=up, Z=toward-viewer (right-handed)
        Blender: X=right, Y=forward, Z=up (right-handed)
        Mapping: Bx=X, By=-Z, Bz=Y
        """
        out = np.empty_like(pos)
        out[:, 0] = pos[:, 0]
        out[:, 1] = -pos[:, 2]
        out[:, 2] = pos[:, 1]
        return out

    @staticmethod
    def _world_to_blender(pos: np.ndarray) -> np.ndarray:
        """Convert BeamNG world-space positions to Blender space.

        BeamNG world (left-handed): X=right, Y=forward, Z=up
        Blender (right-handed): X=right, Y=forward, Z=up
        The handedness flip maps: Bx=-Wy, By=-Wx, Bz=Wz
        This ensures the car faces -X (as in BeamNG viewport) and
        vertical rolls (pitch) appear correctly in Blender.
        """
        out = np.empty_like(pos)
        out[:, 0] = -pos[:, 1]
        out[:, 1] = -pos[:, 0]
        out[:, 2] = pos[:, 2]
        return out

    @staticmethod
    def _quat_rotate(q: np.ndarray, pos: np.ndarray) -> np.ndarray:
        """Rotate *pos* (N×3) by quaternion *q* (xyzw).  Pure numpy, no scipy."""
        return pos @ CacheBuilder._quat_to_matrix(q).T

    @staticmethod
    def _quat_to_matrix(q: np.ndarray) -> np.ndarray:
        """Quaternion (xyzw) -> 3×3 rotation matrix (float64)."""
        x, y, z, w = float(q[0]), float(q[1]), float(q[2]), float(q[3])
        return np.array([
            [1 - 2*(y*y + z*z),     2*(x*y - w*z),     2*(x*z + w*y)],
            [    2*(x*y + w*z),  1 - 2*(x*x + z*z),     2*(y*z - w*x)],
            [    2*(x*z - w*y),     2*(y*z + w*x),  1 - 2*(x*x + y*y)],
        ], dtype=np.float64)

    @staticmethod
    def _pool_to_blender(pos: np.ndarray) -> np.ndarray:
        """GPU pool space (Y-up: X=length, Y=up, Z=width) -> Blender/physics
        space (X=right, Y=forward, Z=up, right-handed).

        Axis mapping — pool Y-up → physics Z-up:
          physics_X (right)  = pool_Z (width)
          physics_Y (forward) = pool_X (length)
          physics_Z (up)     = pool_Y (up)

        This ensures vehicle rotations (about physics axes) correctly map to
        the car's local axes: a pitch (rotation about physics X/right) rotates
        about the car's width axis, not its length axis.
        """
        out = np.empty_like(pos)
        out[:, 0] = pos[:, 2]
        out[:, 1] = pos[:, 0]
        out[:, 2] = pos[:, 1]
        return out

    # ------------------------------------------------------------------
    # Capture-file entry point  (BMC v1 path — replaces scanner + GLB)
    # ------------------------------------------------------------------

    def build_from_capture(
        self,
        capture_bmc_path: str,
    ) -> SequenceManifest:
        """Build a BVC directly from a BMC v1 ``capture.bmc`` file.

        Uses the new shared-pool reader (BmcReader). No weld, no clamp,
        no repair, no dynamic fallback.  Primitive grouping by flexmesh
        index is handled in the reader.  All objects are presumed stable.
        """
        import traceback
        try:
            return self._build_from_capture_impl(capture_bmc_path)
        except (CacheBuildError, CaptureBuildError):
            raise
        except Exception as exc:
            details = traceback.format_exc()
            sys.stderr.write(f"[BeamNG] BUILD FROM CAPTURE ERROR:\n{details}\n")
            sys.stderr.flush()
            raise CaptureBuildError(f"build from capture failed: {exc}") from exc

    def _build_from_capture_impl(
        self,
        capture_bmc_path: str,
    ) -> SequenceManifest:
        """Build BVC from BMC v1 capture.bmc.

        All objects are stable (shared pool guarantees topology invariance).
        The reader groups primitives by flexmesh index; each group becomes
        one BVC object.  Positions are converted from GPU pool space (Y-up)
        to Blender space (Z-up) via _pool_to_blender.
        """
        from .capture_reader import BmcReader

        with BmcReader(capture_bmc_path) as reader:
            total_frames = reader.frame_count()
            obj_names = reader.object_names()
            obj_count = len(obj_names)

            print(
                f"[BeamNG] Building cache from BMC v1: "
                f"{obj_count} objects, {total_frames} frames -> {self.out_path}",
                flush=True,
            )

            # --- Gather per-object data from reader ------------------------
            base_indices: Dict[str, np.ndarray] = {}
            base_uvs: Dict[str, np.ndarray] = {}
            base_matnames: Dict[str, List[str]] = {}
            vcounts: Dict[str, int] = {}
            face_counts: Dict[str, int] = {}
            index_range: Dict[str, np.ndarray] = {}

            manifest = SequenceManifest(
                sequence_name=self.out_path.stem, frame_count=total_frames
            )

            for name in obj_names:
                info = reader.object_info(name)
                idx = reader.base_indices(name)
                uvs = reader.base_uvs(name)

                base_indices[name] = np.ascontiguousarray(idx, dtype=np.int32)
                base_uvs[name] = np.ascontiguousarray(uvs, dtype=np.float32) if uvs is not None else np.zeros((info.vertex_count, 2), dtype=np.float32)
                base_matnames[name] = []  # material names from BMC primitive table not propagated yet
                vcounts[name] = info.vertex_count
                face_counts[name] = info.index_count
                index_range[name] = info.index_range

                topo_hash = TopologyHasher.hash_indices(info.vertex_count, idx)
                manifest.objects[name] = ObjectSignature(
                    name=name,
                    vertex_count=info.vertex_count,
                    edge_count=_edge_count(idx, info.index_count),
                    face_count=info.index_count,
                    topology_hash=topo_hash,
                    cacheable=True,
                )

            current_stable = obj_names
            current_dynamic: List[str] = []

            # --- Layout computation ----------------------------------------
            frame_vertex_offset: Dict[str, int] = {}
            cursor = 0
            for name in current_stable:
                frame_vertex_offset[name] = cursor
                cursor += vcounts[name] * 3
            max_stable_vertex_total = sum(vcounts[n] for n in current_stable)

            all_material_names: List[str] = ["__no_material__"]
            name_index: Dict[str, int] = {"__no_material__": 0}

            def _ensure_name(n: str) -> int:
                idx = name_index.get(n)
                if idx is None:
                    idx = len(all_material_names)
                    name_index[n] = idx
                    all_material_names.append(n)
                return idx

            initial_table_size = sum(
                2 + len(n.encode("utf-8")) + binary._OBJ_TAIL.size + 16
                for n in current_stable
            )
            padded_table_size = initial_table_size + 4096
            object_table_offset = binary.HEADER_SIZE
            base_mesh_offset = object_table_offset + padded_table_size

            base_index_bytes: Dict[str, bytes] = {}
            base_index_offset: Dict[str, int] = {}
            index_count: Dict[str, int] = {}
            off = base_mesh_offset
            for name in current_stable:
                ib = np.ascontiguousarray(base_indices[name], dtype=np.int32).tobytes()
                base_index_bytes[name] = ib
                base_index_offset[name] = off
                index_count[name] = face_counts[name]
                off += len(ib)

            uv_block_bytes: Dict[str, bytes] = {}
            uv_off: Dict[str, int] = {}
            for name in current_stable:
                uv = base_uvs.get(name)
                ub = (
                    np.ascontiguousarray(uv, dtype=np.float32).tobytes()
                    if uv is not None and uv.size > 0
                    else b""
                )
                uv_block_bytes[name] = ub
                uv_off[name] = off
                off += len(ub)

            uv_blocks_offset = off

            mat_table_bytes = binary.pack_material_name_table(all_material_names)
            material_table_offset = off
            off += len(mat_table_bytes)

            mat_block_bytes: Dict[str, bytes] = {}
            mat_off: Dict[str, int] = {}
            for name in current_stable:
                mnames = base_matnames.get(name, ["__no_material__"])
                global_ids = [_ensure_name(mn) for mn in mnames]
                mids = np.zeros(face_counts[name], dtype=np.uint16)
                mb = binary.pack_material_block(global_ids, mids)
                mat_block_bytes[name] = mb
                mat_off[name] = off
                off += len(mb)

            material_blocks_offset = off

            frame_block_size = max_stable_vertex_total * 3 * 4
            frame_directory_offset = off
            off += total_frames * 8
            frame_blocks_offset = off

            # --- Write BVC file -------------------------------------------
            print(f"[BeamNG]   writing {self.out_path} ...", flush=True)
            self.out_path.parent.mkdir(parents=True, exist_ok=True)

            if self.out_path.exists():
                try:
                    self.out_path.unlink()
                except OSError:
                    pass

            with open(self.out_path, "wb") as fh:
                fh.write(
                    binary.pack_header(
                        frame_count=total_frames,
                        object_count=len(current_stable),
                        stable_count=len(current_stable),
                        stable_vertex_total=max_stable_vertex_total,
                        object_table_offset=object_table_offset,
                        base_mesh_offset=base_mesh_offset,
                        uv_blocks_offset=uv_blocks_offset,
                        material_table_offset=material_table_offset,
                        material_blocks_offset=material_blocks_offset,
                        frame_directory_offset=frame_directory_offset,
                        frame_blocks_offset=frame_blocks_offset,
                        dynamic_directory_offset=0,
                        dynamic_data_offset=0,
                        transform_data_offset=0,
                    )
                )
                fh.write(b"\x00" * padded_table_size)

                for name in current_stable:
                    fh.write(base_index_bytes[name])
                for name in current_stable:
                    fh.write(uv_block_bytes[name])

                fh.write(mat_table_bytes)
                for name in current_stable:
                    fh.write(mat_block_bytes[name])

                for i in range(total_frames):
                    fh.write(struct.pack("<Q", frame_blocks_offset + i * frame_block_size))

                # Per-frame: read shared pool positions, index into each object's range
                has_transform = reader.header.has_transform

                # Basis-change quaternion: physics Y-up → Blender Z-up.
                # Axis permutation (z,x,y): physics X→Blender Y, Y→Blender Z, Z→Blender X.
                # Represented as 120° rotation around (1,1,1)/√3.
                per_frame_transforms: list[np.ndarray] = []

                for fi in range(total_frames):
                    shared_pos = reader._read_shared_positions(fi)
                    pos_blender = self._pool_to_blender(shared_pos.astype(np.float64))

                    p_blender = np.zeros(3, dtype=np.float32)
                    q_blender = np.array([0.0, 0.0, 0.0, 1.0], dtype=np.float32)

                    if has_transform:
                        vtx = reader.frame_vehicle_transform(fi)
                        # Transform is already in physics Z-up space.
                        # Physics Z-up (X=right, Y=forward, Z=up) is the same as
                        # Blender Z-up — no axis conversion needed.
                        # Only vertex positions need _pool_to_blender (pool Y-up -> Z-up).
                        p_blender = vtx[:3].astype(np.float32)
                        q_blender = vtx[3:7].astype(np.float32)

                    for name in current_stable:
                        obj_idx = index_range[name]
                        pos = np.ascontiguousarray(pos_blender[obj_idx], dtype=np.float32)
                        fh.write(pos.tobytes())

                    written = sum(vcounts[n] * 12 for n in current_stable)
                    pad = frame_block_size - written
                    if pad > 0:
                        fh.write(b"\x00" * pad)

                    if fi == 0:
                        print(
                            f"[BeamNG]   frame 0 written "
                            f"({len(current_stable)} objects, "
                            f"{max_stable_vertex_total} verts)",
                            flush=True,
                        )

                    per_frame_transforms.append(np.concatenate([p_blender, q_blender]))

                # Write transform data section (after frame blocks)
                transform_data_offset = 0
                if has_transform and per_frame_transforms:
                    transform_data_offset = fh.tell()
                    for tf in per_frame_transforms:
                        fh.write(tf.tobytes())

                # Patch header and write object table
                fh.seek(0)
                fh.write(
                    binary.pack_header(
                        frame_count=total_frames,
                        object_count=len(current_stable),
                        stable_count=len(current_stable),
                        stable_vertex_total=max_stable_vertex_total,
                        object_table_offset=object_table_offset,
                        base_mesh_offset=base_mesh_offset,
                        uv_blocks_offset=uv_blocks_offset,
                        material_table_offset=material_table_offset,
                        material_blocks_offset=material_blocks_offset,
                        frame_directory_offset=frame_directory_offset,
                        frame_blocks_offset=frame_blocks_offset,
                        dynamic_directory_offset=0,
                        dynamic_data_offset=0,
                        transform_data_offset=transform_data_offset,
                    )
                )

                fh.seek(object_table_offset)
                for name in current_stable:
                    sig = manifest.objects[name]
                    fh.write(
                        binary.pack_object_entry(
                            name=name,
                            stable=True,
                            vertex_count=vcounts[name],
                            face_count=face_counts[name],
                            topology_hash=sig.topology_hash,
                            base_index_offset=base_index_offset[name],
                            index_count=index_count[name],
                            frame_vertex_offset=frame_vertex_offset[name],
                            welded=False,
                            uv_offset=uv_off[name],
                            material_offset=mat_off[name],
                        )
                    )

            manifest.stable_objects = current_stable
            manifest.dynamic_objects = current_dynamic

            size_mb = self.out_path.stat().st_size / 1e6
            print(
                f"[BeamNG] cache written from capture: {size_mb:.1f} MB "
                f"({total_frames} frames, {len(current_stable)} objects)",
                flush=True,
            )

        return manifest

def _backfill_dynamic(
    fh, name: str, change_frame: int,
    frame_blocks_offset: int, frame_block_size: int,
    frame_vertex_offset: Dict[str, int],
    vcount_fn,
    base_index_bytes: Dict[str, bytes],
    dynamic_frame_data: List[Dict[str, tuple]],
) -> None:
    """When an object becomes dynamic at *change_frame*, backfill earlier
    frames' data from the already-written frame blocks into the dynamic
    data section so every frame has full mesh data for this object."""
    saved = fh.tell()
    slot_offset = frame_vertex_offset[name]
    slot_bytes = vcount_fn(name) * 3 * 4
    for fr in range(change_frame):
        byte_off = frame_blocks_offset + fr * frame_block_size + slot_offset * 4
        fh.seek(byte_off)
        raw = fh.read(slot_bytes)
        pos = np.frombuffer(raw, dtype=np.float32).reshape(-1, 3).copy()
        idx = np.frombuffer(base_index_bytes[name], dtype=np.int32).reshape(-1, 3).copy()
        dynamic_frame_data[fr][name] = (pos, idx, None, None, None)
    fh.seek(saved)


def _ensure_writable(path: Path) -> None:
    """Validate that the directory for *path* exists, or raise
    ``CacheBuildError`` with a clear message."""
    parent = path.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _raise_path_error(path, parent, exc)


def _raise_path_error(path: Path, parent: Path, exc: Exception) -> None:
    import traceback
    tb = traceback.format_exc()
    msg = (
        f"cannot write cache to {path}\n"
        f"  parent directory: {parent}\n"
        f"  error: {exc}\n"
        f"  traceback:\n{tb}\n\n"
        "This usually means the target drive is not available (e.g. a\n"
        "CD/DVD drive, unplugged USB drive, or disconnected network\n"
        "share). Choose a path on your main drive (C:) or set the\n"
        "Cache File field to a writable location."
    )
    raise CacheBuildError(msg)
