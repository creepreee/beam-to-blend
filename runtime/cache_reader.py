from __future__ import annotations

"""Reader for the BVC binary vertex cache.

Pure CPython + numpy, no ``bpy`` — so it can be tested outside Blender and used
by the mesh updater. Uses memory-mapping so frame position blocks are paged in
on demand rather than loaded all at once (the whole point of the cache).
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

from importer import binary


@dataclass
class CachedObject:
    name: str
    stable: bool
    vertex_count: int
    face_count: int
    topology_hash: str
    index_count: int
    _base_index_offset: int
    _frame_vertex_offset: int
    _uv_offset: int = 0
    _material_offset: int = 0
    _loop_uv_offset: int = 0


class CacheReader:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._mmap = np.memmap(self.path, dtype=np.uint8, mode="r")
        raw = self._mmap.tobytes() if self._mmap.size < (1 << 20) else bytes(
            self._mmap[: binary.HEADER_SIZE]
        )
        self.header = binary.unpack_header(raw)
        self._is_v3 = self.header.get("version", 0) >= 3
        self._is_v5 = self.header.get("version", 0) >= 5
        self._dyn_entry_size = binary.DYN_ENTRY_SIZE_V2 if not self._is_v3 else binary.DYN_ENTRY_SIZE
        self._load_object_table()
        self._dynamic_names: List[str] = [o.name for o in self._objects if not o.stable]
        self._global_material_names: List[str] = []
        if self._is_v3:
            self._load_material_name_table()

    @property
    def frame_count(self) -> int:
        return self.header["frame_count"]

    def object_names(self) -> List[str]:
        return [o.name for o in self._objects]

    def stable_objects(self) -> List[CachedObject]:
        return [o for o in self._objects if o.stable]

    def dynamic_objects(self) -> List[CachedObject]:
        return [o for o in self._objects if not o.stable]

    def get_object(self, name: str) -> CachedObject:
        return self._by_name[name]

    def close(self) -> None:
        mm = getattr(self, "_mmap", None)
        if mm is not None:
            mm._mmap.close()
            self._mmap = None

    def __enter__(self) -> "CacheReader":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _load_object_table(self) -> None:
        start = self.header["object_table_offset"]
        end = self.header["base_mesh_offset"]
        buf = bytes(self._mmap[start:end])
        objects: List[CachedObject] = []
        offset = 0
        for _ in range(self.header["object_count"]):
            entry, offset = binary.unpack_object_entry(
                buf, offset, is_v3=self._is_v3, is_v5=self._is_v5,
            )
            objects.append(
                CachedObject(
                    name=entry["name"],
                    stable=entry["stable"],
                    vertex_count=entry["vertex_count"],
                    face_count=entry["face_count"],
                    topology_hash=entry["topology_hash"],
                    index_count=entry["index_count"],
                    _base_index_offset=entry["base_index_offset"],
                    _frame_vertex_offset=entry["frame_vertex_offset"],
                    _uv_offset=entry.get("uv_offset", 0),
                    _material_offset=entry.get("material_offset", 0),
                    _loop_uv_offset=entry.get("loop_uv_offset", 0),
                )
            )
        self._objects = objects
        self._by_name: Dict[str, CachedObject] = {o.name: o for o in objects}

    def _load_material_name_table(self) -> None:
        mat_off = self.header.get("material_table_offset", 0)
        if mat_off == 0:
            return
        buf = bytes(self._mmap[mat_off:mat_off + 4])
        (count,) = binary.struct.unpack_from("<I", buf, 0)
        est_size = 4 + sum(2 + 32 for _ in range(count))
        full_buf = bytes(self._mmap[mat_off:mat_off + est_size])
        names, _ = binary.unpack_material_name_table(full_buf, 0)
        self._global_material_names = names

    def _require_v3(self) -> None:
        if not self._is_v3:
            raise ValueError(
                "This cache was built with an older format (v2). "
                "Please rebuild the cache to access UV/material data."
            )

    def _resolve_material_name(self, gid: int) -> str:
        if gid < len(self._global_material_names):
            return self._global_material_names[gid]
        return "<unknown>"

    # --- geometry: stable objects --------------------------------------
    def base_indices(self, name: str) -> np.ndarray:
        obj = self._by_name[name]
        if not obj.stable:
            raise ValueError(f"{name!r} has no cached base mesh")
        if obj.index_count == 0:
            return np.empty((0, 3), dtype=np.int32)
        start = obj._base_index_offset
        n = obj.index_count * 3
        flat = np.frombuffer(
            self._mmap, dtype=np.int32, count=n, offset=start
        )
        return flat.reshape(-1, 3)

    def _frame_block_offset(self, frame: int) -> int:
        dir_off = self.header["frame_directory_offset"]
        (off,) = np.frombuffer(
            self._mmap, dtype=np.uint64, count=1, offset=dir_off + frame * 8
        )
        return int(off)

    def frame_positions(self, name: str, frame: int) -> np.ndarray:
        if not 0 <= frame < self.frame_count:
            raise IndexError(f"frame {frame} out of range 0..{self.frame_count-1}")
        obj = self._by_name[name]
        if not obj.stable:
            raise ValueError(f"{name!r} is not a stable/cached object")
        block = self._frame_block_offset(frame)
        byte_off = block + obj._frame_vertex_offset * 4
        n = obj.vertex_count * 3
        flat = np.frombuffer(
            self._mmap, dtype=np.float32, count=n, offset=byte_off
        )
        return flat.reshape(-1, 3)

    # --- per-frame vehicle transforms (BVC v4) --------------------------
    # Stored as 12 float32 per frame: position(3) + orientation matrix(9,
    # row-major, columns = right/fwd/up).  Reconstructed in the builder from
    # the captured forward/up direction vectors (refNode-offset-free).
    _TRANSFORM_FLOATS = 12
    _TRANSFORM_BYTES = _TRANSFORM_FLOATS * 4

    def frame_transform(self, frame: int) -> np.ndarray:
        """Returns [px, py, pz, rxx, rxy, rxz, fxx, fxy, fxz, uxx, uxy, uxz]
        for *frame* in Blender space.

        position(3) + 3x3 orientation matrix(9, row-major, columns =
        right/fwd/up).  Returns None if no transform data."""
        tdo = self.header.get("transform_data_offset", 0)
        if tdo == 0 or not (0 <= frame < self.frame_count):
            return None
        off = tdo + frame * self._TRANSFORM_BYTES
        raw = np.frombuffer(self._mmap, dtype=np.float32, count=self._TRANSFORM_FLOATS, offset=off)
        return raw.copy()

    # --- UV + material: stable objects ---------------------------------
    def base_uvs(self, name: str) -> np.ndarray:
        self._require_v3()
        obj = self._by_name[name]
        if not obj.stable:
            raise ValueError(f"{name!r} is not stable")
        off = obj._uv_offset
        if off == 0:
            return np.zeros((obj.vertex_count, 2), dtype=np.float32)
        n = obj.vertex_count * 2
        flat = np.frombuffer(self._mmap, dtype=np.float32, count=n, offset=off)
        return flat.reshape(-1, 2)

    def base_loop_uvs(self, name: str) -> Optional[np.ndarray]:
        """Per-face-corner UVs as ``(face_count, 3, 2)`` f32, or None (pre-v5).

        Loop UVs are what let a seam survive a position-only weld: two corners
        of adjacent faces can sit on the SAME merged vertex and still carry
        different UVs.  Callers should prefer this over :meth:`base_uvs` and
        fall back to the per-vertex block when it returns None.
        """
        obj = self._by_name[name]
        if not obj.stable:
            raise ValueError(f"{name!r} is not stable")
        off = obj._loop_uv_offset
        if off == 0 or obj.face_count == 0:
            return None
        n = obj.face_count * 6
        flat = np.frombuffer(self._mmap, dtype=np.float32, count=n, offset=off)
        return flat.reshape(-1, 3, 2)

    def base_material_names(self, name: str) -> List[str]:
        self._require_v3()
        obj = self._by_name[name]
        if not obj.stable:
            raise ValueError(f"{name!r} is not stable")
        off = obj._material_offset
        if off == 0:
            return []
        buf = bytes(self._mmap[off:off + 2])
        (local_count,) = binary.struct.unpack_from("<H", buf, 0)
        est = 2 + local_count * 4 + obj.face_count * 2
        full = bytes(self._mmap[off:off + est])
        global_ids, _per_face, _ = binary.unpack_material_block(full, 0)
        return [self._resolve_material_name(gid) for gid in global_ids]

    def base_material_ids(self, name: str) -> np.ndarray:
        self._require_v3()
        obj = self._by_name[name]
        if not obj.stable:
            raise ValueError(f"{name!r} is not stable")
        off = obj._material_offset
        if off == 0 or obj.face_count == 0:
            return np.empty(0, dtype=np.uint16)
        buf = bytes(self._mmap[off:off + 2])
        (local_count,) = binary.struct.unpack_from("<H", buf, 0)
        local_start = off + 2 + local_count * 4
        n = obj.face_count
        flat = np.frombuffer(self._mmap, dtype=np.uint16, count=n, offset=local_start)
        return flat

    # --- geometry: dynamic objects -------------------------------------
    def frame_dynamic_geometry(self, name: str, frame: int) -> Tuple[np.ndarray, np.ndarray]:
        """Full mesh data (positions Nx3 f32, indices Mx3 i32) for a dynamic
        object at a given frame.  Returns empty arrays if the object does not
        exist in this frame."""
        if not 0 <= frame < self.frame_count:
            raise IndexError(f"frame {frame} out of range 0..{self.frame_count-1}")
        obj = self._by_name[name]
        if obj.stable:
            raise ValueError(f"{name!r} is stable, use frame_positions()")

        dyn_off = self.header["dynamic_directory_offset"]
        if dyn_off == 0:
            raise ValueError("no dynamic data in this cache")

        dynamic_count = len(self._dynamic_names)
        try:
            di = self._dynamic_names.index(name)
        except ValueError:
            raise ValueError(f"{name!r} not found in dynamic objects")

        entry_off = dyn_off + (frame * dynamic_count + di) * self._dyn_entry_size
        raw = bytes(self._mmap[entry_off : entry_off + self._dyn_entry_size])
        entry = binary.unpack_dynamic_entry(raw, 0, is_v3=self._is_v3)

        vc = entry["vertex_count"]
        ic = entry["index_count"]

        if vc == 0:
            return (np.empty((0, 3), dtype=np.float32), np.empty((0, 3), dtype=np.int32))

        pos_off = entry["positions_offset"]
        idx_off = entry["indices_offset"]

        positions = np.frombuffer(
            self._mmap, dtype=np.float32, count=vc * 3, offset=pos_off
        ).reshape(-1, 3)
        indices = np.frombuffer(
            self._mmap, dtype=np.int32, count=ic * 3, offset=idx_off
        ).reshape(-1, 3)

        return (positions, indices)

    # --- UV + material: dynamic objects ---------------------------------
    def _read_dynamic_entry(self, name: str, frame: int) -> dict:
        obj = self._by_name[name]
        dyn_off = self.header["dynamic_directory_offset"]
        dynamic_count = len(self._dynamic_names)
        di = self._dynamic_names.index(name)
        entry_off = dyn_off + (frame * dynamic_count + di) * self._dyn_entry_size
        raw = bytes(self._mmap[entry_off : entry_off + self._dyn_entry_size])
        return binary.unpack_dynamic_entry(raw, 0, is_v3=self._is_v3)

    def frame_dynamic_uvs(self, name: str, frame: int) -> np.ndarray:
        self._require_v3()
        entry = self._read_dynamic_entry(name, frame)
        off = entry.get("uv_offset", 0)
        vc = entry["vertex_count"]
        if off == 0 or vc == 0:
            return np.zeros((vc, 2), dtype=np.float32)
        mat_off = entry.get("material_offset", 0)
        if mat_off != 0 and off == mat_off:
            return np.zeros((vc, 2), dtype=np.float32)
        flat = np.frombuffer(self._mmap, dtype=np.float32, count=vc * 2, offset=off)
        return flat.reshape(-1, 2)

    def frame_dynamic_material_names(self, name: str, frame: int) -> List[str]:
        self._require_v3()
        entry = self._read_dynamic_entry(name, frame)
        off = entry.get("material_offset", 0)
        if off == 0:
            return []
        uv_off = entry.get("uv_offset", 0)
        if uv_off != 0 and off == uv_off:
            return []
        buf = bytes(self._mmap[off:off + 2])
        (local_count,) = binary.struct.unpack_from("<H", buf, 0)
        est = 2 + local_count * 4 + entry["index_count"] * 2
        full = bytes(self._mmap[off:off + est])
        global_ids, _per_face, _ = binary.unpack_material_block(full, 0)
        return [self._resolve_material_name(gid) for gid in global_ids]

    def frame_dynamic_material_ids(self, name: str, frame: int) -> np.ndarray:
        self._require_v3()
        entry = self._read_dynamic_entry(name, frame)
        off = entry.get("material_offset", 0)
        vc = entry["vertex_count"]
        ic = entry["index_count"]
        if off == 0 or ic == 0:
            return np.empty(0, dtype=np.uint16)
        uv_off = entry.get("uv_offset", 0)
        if uv_off != 0 and off == uv_off:
            return np.empty(0, dtype=np.uint16)
        buf = bytes(self._mmap[off:off + 2])
        (local_count,) = binary.struct.unpack_from("<H", buf, 0)
        local_start = off + 2 + local_count * 4
        flat = np.frombuffer(self._mmap, dtype=np.uint16, count=ic, offset=local_start)
        return flat
