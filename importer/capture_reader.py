from __future__ import annotations

"""BMC v1 reader — reads capture.bmc for the simplified CacheBuilder.

Groups primitives by flexmesh name prefix (same logic as
``_merge_capture_by_flexmesh`` in the old builder) and provides
per-object, per-frame positions by indexing into the shared pool.
No weld, no clamp, no repair, no dynamic fallback.

Usage::

    with BmcReader("capture.bmc") as reader:
        obj_names = reader.object_names()
        idx = reader.base_indices("body")       # uint32 Nx3
        uvs = reader.base_uvs("body")           # float32 Nx2
        pos = reader.frame_positions(5, "body") # float32 Nx3
"""

import struct
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

from . import capture_format as bmc


@dataclass
class ObjectInfo:
    """One merged object in the capture."""
    name: str
    vertex_count: int
    index_count: int
    index_range: np.ndarray  # int64[N] — shared-pool vertex indices contributing to this object
    remap: np.ndarray  # int64[N] — shared_pool_index -> compact_local_index
    base_indices: np.ndarray  # int32[Mx3] — triangle indices in compact local space


class BmcReader:
    """Reads a BMC v1 capture.bmc file.

    Context-manager. Opens the file once; provides per-object and per-frame
    access into the shared pool data.
    """

    def __init__(self, path: str):
        self._path = path
        self._file = open(path, "rb")
        self.header = bmc.read_bmc_header(path)
        self._read_static()

        self._objects: Dict[str, ObjectInfo] = {}
        self._object_names: List[str] = []
        self._build_objects()

    def _read_static(self):
        self._file.seek(bmc.HEADER_SIZE)
        static = self._file.read(self.header.static_size)

        offset = 0
        idx_bytes = self.header.index_count * 4
        self._index_data = np.frombuffer(
            static[offset: offset + idx_bytes], dtype=np.uint32
        ).copy()
        offset += idx_bytes

        self._uv_data = None
        if self.header.has_uvs:
            uv_bytes = self.header.vertex_count * 8
            self._uv_data = np.frombuffer(
                static[offset: offset + uv_bytes], dtype=np.float32
            ).reshape(-1, 2).copy()
            offset += uv_bytes

        self._normal_data = None
        if self.header.has_normals:
            normal_bytes = self.header.vertex_count * 12
            self._normal_data = np.frombuffer(
                static[offset: offset + normal_bytes], dtype=np.float32
            ).reshape(-1, 3).copy()
            offset += normal_bytes

        # Parse primitive table
        self._primitives = bmc._unpack_primitive_table(
            static[offset:], self.header.primitive_count
        )

    def _build_objects(self):
        """Group primitives by flexmesh name prefix, dedup indices from shared pool."""
        groups: Dict[int, List[int]] = {}
        standalone: List[int] = []

        for i, p in enumerate(self._primitives):
            if p.flexmesh_index >= 0:
                groups.setdefault(p.flexmesh_index, []).append(i)
            else:
                standalone.append(i)

        def _dedup_object(member_indices: List[int], name: str) -> ObjectInfo:
            all_shared_idx = []
            for mi in member_indices:
                p = self._primitives[mi]
                shared = self._index_data[p.start_index:p.start_index + p.index_count].ravel().astype(np.int64)
                all_shared_idx.append(shared)

            concat = np.concatenate(all_shared_idx) if len(all_shared_idx) > 1 else all_shared_idx[0]

            # Deduplicate — multiple material-split primitives may reference same vertices
            unique_idx, inverse = np.unique(concat, return_inverse=True)
            n_unique = len(unique_idx)

            # Remap indices to compact local space
            compact_idx = inverse.reshape(-1, 3).astype(np.int32)

            # Remove degenerate triangles
            a, b, c = compact_idx[:, 0], compact_idx[:, 1], compact_idx[:, 2]
            valid = (a != b) & (b != c) & (a != c)
            n_degen = int((~valid).sum())
            if n_degen:
                compact_idx = compact_idx[valid]

            return ObjectInfo(
                name=name,
                vertex_count=n_unique,
                index_count=compact_idx.shape[0],
                index_range=unique_idx,
                remap=np.arange(n_unique, dtype=np.int64),  # direct: local -> shared pool index
                base_indices=compact_idx,
            )

        # Process flexmesh groups
        for fmi in sorted(groups.keys()):
            members = groups[fmi]
            name = self._primitives[members[0]].name
            obj = _dedup_object(members, name)
            self._objects[name] = obj
            self._object_names.append(name)

        # Process standalone primitives (props, etc.) — each gets its own object
        name_counter: Dict[str, int] = {}
        for idx in standalone:
            p = self._primitives[idx]
            name = p.name
            count = name_counter.get(name, 0)
            name_counter[name] = count + 1
            if count > 0:
                name = f'{name}_{count}'
            obj = _dedup_object([idx], name)
            self._objects[name] = obj
            self._object_names.append(name)

    # --- Public API ---

    def object_count(self) -> int:
        return len(self._object_names)

    def frame_count(self) -> int:
        return bmc.frame_count(
            self._file.seek(0, 2),
            self.header,
        )

    def object_names(self) -> List[str]:
        return list(self._object_names)

    def object_info(self, name: str) -> ObjectInfo:
        return self._objects[name]

    def base_indices(self, name: str) -> np.ndarray:
        return self._objects[name].base_indices.copy()

    def base_uvs(self, name: str) -> Optional[np.ndarray]:
        obj = self._objects[name]
        if self._uv_data is not None:
            return self._uv_data[obj.index_range].copy()
        return None

    def frame_positions(self, frame_index: int, name: str) -> np.ndarray:
        obj = self._objects[name]
        pos = self._read_shared_positions(frame_index)
        return pos[obj.index_range].copy()

    def frame_vehicle_transform(self, frame_index: int) -> np.ndarray:
        """Returns [px, py, pz, fx, fy, fz, ux, uy, uz] for frame N (v4).

        position + forward dir + up dir.  The transform is stored after vertex
        positions when FLAG_HAS_TRANSFORM is set in the BMC header.  Raises
        ValueError if not present.
        """
        if not self.header.has_transform:
            raise ValueError("BMC has no vehicle transform data")
        off = bmc.frame_offset(self.header, frame_index) + 8 + self.header.vertex_count * 12
        self._file.seek(off)
        raw = self._file.read(bmc.FRAME_TRANSFORM_BYTES)
        return np.frombuffer(raw, dtype=np.float32).copy()

    def _read_shared_positions(self, frame_index: int) -> np.ndarray:
        """Read the full shared pool positions for *frame_index*."""
        off = bmc.frame_offset(self.header, frame_index) + 8  # skip timestamp
        vc = self.header.vertex_count
        self._file.seek(off)
        raw = self._file.read(vc * 12)
        return np.frombuffer(raw, dtype=np.float32).reshape(-1, 3)

    # --- Resource management ---

    def close(self):
        if self._file and not self._file.closed:
            self._file.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
