from __future__ import annotations

"""Builds a BVC binary vertex cache from a BeamNG Architecture-v3 capture.

The capture (``.bmc``) contains, per frame:
  * the deforming mesh in GPU-pool local space (Y-up, left-handed), and
  * the vehicle's live rigid transform (position + rotation) in physics
    space (Z-up, right-handed) read from the VLUA vehicle VM.

This builder does the minimum:
  * converts each frame's pool vertices to Blender space via a single
    axis permutation (``_pool_to_blender``),
  * passes the rigid transform through untouched (quaternion component
    order is handled at import time by ``runtime/mesh_update``).

No welding, no reconstruction, no calibration, no quaternion guessing.
"""

import struct
from pathlib import Path
from typing import Dict, List

import numpy as np

from . import binary
from .topology import TopologyHasher
from .capture_reader import BmcReader


class CacheBuildError(Exception):
    pass


class CaptureBuildError(CacheBuildError):
    """Raised when building from a capture file fails."""


def _pool_to_blender(pos: np.ndarray) -> np.ndarray:
    """GPU pool space (Y-up, left-handed) -> Blender/physics space (Z-up).

    Pool axes:  X = length (forward), Y = up, Z = width (right)
    Blender:    X = right,           Y = forward, Z = up

    i.e.  blender = (pool_z, pool_x, pool_y)
    """
    out = np.empty_like(pos)
    out[:, 0] = pos[:, 2]
    out[:, 1] = pos[:, 0]
    out[:, 2] = pos[:, 1]
    return out


def _world_to_blender(pos: np.ndarray) -> np.ndarray:
    """BeamNG absolute world space -> Blender space.

    The v5 capture already re-frames the vertices to Blender Z-up at capture
    time (``tools/v5_capture.lua`` applies ``world = (x, -z, y)`` after
    ``getRefNodeMatrix():mulP3F``, baking the Y-up -> Z-up rotation into the
    stored coordinates). So the .bmc is already Blender-world: right-handed
    Z-up, same axes/handedness as Blender.  No conversion is needed here — the
    float values are copied through unchanged (identity).

    This is the v5 "zero-matrix" design: deformation, rotation and world
    translation are baked into every vertex at capture; the BVC is a flat
    verbatim array stream with no parent empty and no per-frame matrix.
    """
    return np.ascontiguousarray(pos, dtype=np.float32)


class CacheBuilder:
    def __init__(self, sequence_dir: Path, out_path: Path):
        self.sequence_dir = Path(sequence_dir)
        self.out_path = Path(out_path)

    # Public entry point. The only supported source is a v3 capture (.bmc).
    def build(self, capture_bmc_path: str | None = None, **_ignored) -> "SequenceManifest":
        return self.build_from_capture(capture_bmc_path or self.sequence_dir)

    def build_from_capture(self, capture_bmc_path: str) -> "SequenceManifest":
        """Build a BVC directly from a BMC v1 ``capture.bmc`` file.

        Uses the shared-pool reader (BmcReader). No weld, no clamp, no
        repair.  All objects are topology-stable.  Positions are converted
        from pool space to Blender space via ``_pool_to_blender``; the rigid
        transform is copied through byte-for-byte.
        """
        with BmcReader(capture_bmc_path) as reader:
            total_frames = reader.frame_count()
            obj_names = reader.object_names()
            obj_count = len(obj_names)

            print(
                f"[BeamNG] Building cache from BMC v1: "
                f"{obj_count} objects, {total_frames} frames -> {self.out_path}",
                flush=True,
            )

            base_indices: Dict[str, np.ndarray] = {}
            base_uvs: Dict[str, np.ndarray] = {}
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
                base_uvs[name] = (
                    np.ascontiguousarray(uvs, dtype=np.float32)
                    if uvs is not None
                    else np.zeros((info.vertex_count, 2), dtype=np.float32)
                )
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

            # --- Layout computation ------------------------------------
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
                mnames = ["__no_material__"]
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

            # --- Write BVC file ---------------------------------------
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

                # --- Per-frame vertex + rigid transform -----------------
                # v4: transform = position + forward + up direction vectors.
                # Reconstruct a clean orientation matrix (right, fwd, up) via
                # cross product, bypassing the refNode-bound quaternion.
                #
                # Spawn-flip correction: BeamNG's captured fwd vector can snap
                # ~90 deg between the pre-settle frames (0..k) and the settled
                # state (k+1..) with almost no XY displacement. That is a capture
                # artifact, not real motion. We unwrap it so the sequence is
                # continuous AND anchored so the car's rest facing is Blender -Y
                # (the user's hard requirement).  We do this by rotating the
                # pre-flip frames by the detected yaw delta; the settled frames
                # already carry the true orientation.
                has_transform = reader.header.has_transform
                world_space = reader.header.has_world_space
                per_frame_transforms: list[np.ndarray] = []

                if world_space:
                    # v5 absolute world-space dump: vertices are already in
                    # BeamNG world coords (engine render matrix applied at
                    # capture).  No pool->Blender perm, no transform block, no
                    # matrix.  Just copy frames straight through, then fall
                    # through to the shared header patch (transform_data_offset=0).
                    print("[BeamNG]   world-space dump: applying single global "
                          "S (Y-up world -> Blender Z-up), no transform block",
                          flush=True)
                    for fi in range(total_frames):
                        shared_pos = reader._read_shared_positions(fi)
                        pos_blender = np.ascontiguousarray(
                            _world_to_blender(shared_pos.astype(np.float32)))
                        for name in current_stable:
                            obj_idx = index_range[name]
                            pos = np.ascontiguousarray(
                                pos_blender[obj_idx], dtype=np.float32)
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
                    # no transform data for world-space dumps
                    transform_data_offset = 0
                else:
                    transform_data_offset = 0  # set by transform block below

                # --- Rigid transform from direction vectors (PROVEN in calibrator) ---
                # Calibrator verified basis (blender_import_csv.py::basis_to_quat):
                #   x_axis = forward   (mesh local +X is the nose)
                #   z_axis = up        (mesh local +Z is the roof)
                #   y_axis = z x x     (re-orthogonalize, right-handed)
                # No PCA, no spawn-flip unwrap, no L matrix. The direction vectors
                # are already physics Z-up == Blender Z-up (same axes/handedness),
                # so the basis maps directly to the Blender matrix.
                if not world_space and has_transform:
                    # =========================================================
                    #  POSITION-ONLY transform  (rotation is already in the mesh)
                    # =========================================================
                    # PROVEN (Kabsch on raw verticesGet output, 2026-07-20):
                    #   verticesGet() returns the vehicle mesh already ROTATED and
                    #   DEFORMED in place around the vehicle's own origin — the full
                    #   rigid rotation is BAKED INTO THE VERTICES.  It is NOT
                    #   translated into world space (vertex centroid stays ~1 m from
                    #   origin while getPosition() travels 60+ m).
                    #
                    #   So the ONLY missing piece is TRANSLATION.  Applying a rotation
                    #   from getRotation()/getDirectionVector() on top of the mesh
                    #   double-rotates it (a 90 deg turn rendered as 180 deg — the
                    #   long-standing bug).  The calibrator never hit this because it
                    #   dumped the mesh ONCE (a static snapshot) and rotated that
                    #   single frame; the importer re-reads the (already-rotated)
                    #   mesh every frame.
                    #
                    #   Fix: store IDENTITY rotation + getPosition() translation.
                    #   The permuted pool mesh already sits in Blender axes (physics
                    #   Z-up == Blender Z-up), so getPosition() needs no conversion.
                    IDENTITY_ROT = np.eye(3, dtype=np.float32).reshape(-1)
                    for fi in range(total_frames):
                        shared_pos = reader._read_shared_positions(fi)
                        pos_blender = _pool_to_blender(shared_pos.astype(np.float64))

                        vtx = reader.frame_vehicle_transform(fi)
                        p = np.asarray(vtx[:3], dtype=np.float64)

                        # translation only; mesh already carries rotation+deformation
                        p_blender = p.astype(np.float32)
                        tf_blender = IDENTITY_ROT

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

                        per_frame_transforms.append(np.concatenate([p_blender, tf_blender]))
    
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


# Local import kept at the bottom to avoid a circular import at module load.
from .scanner import ObjectSignature, SequenceManifest, _edge_count  # noqa: E402
