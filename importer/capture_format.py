from __future__ import annotations

"""BMC v1 binary format — Beam Motion Capture format.

Fixed-size-frame design for direct seeking without an index table.
All integers are little-endian.

Header (40 bytes):
  [0..4)   magic       b"BMC1"
  [4..8)   version     uint32 = 1
  [8..12)  vertex_count  uint32  — shared pool vertex count
  [12..16) index_count    uint32  — shared index buffer count
  [16..20) primitive_count  uint32
  [20..24) material_count   uint32
  [24..28) flags         uint32  — bit 0: has UVs, bit 1: has normals, bit 2: has transform
  [28..32) frame_size    uint32  — bytes per frame (constant across all frames)
  [32..40) static_size   uint64  — total bytes of static data after header

Frame N begins at byte: 40 + static_size + N * frame_size
"""

import struct
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

HEADER_SIZE = 40

# Flag bits
FLAG_HAS_UVS = 1 << 0
FLAG_HAS_NORMALS = 1 << 1
FLAG_HAS_TRANSFORM = 1 << 2

# Per-frame layout when transform is present: timestamp(8) + vertices(vertex_count*12) + transform(28)
FRAME_TRANSFORM_FLOATS = 7  # px, py, pz, qx, qy, qz, qw
FRAME_TRANSFORM_BYTES = FRAME_TRANSFORM_FLOATS * 4


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class PrimitiveEntry:
    name: str
    start_index: int
    index_count: int
    material_id: int = -1
    flexmesh_index: int = -1


@dataclass
class MaterialEntry:
    name: str


@dataclass
class BmcHeader:
    version: int
    vertex_count: int
    index_count: int
    primitive_count: int
    material_count: int
    flags: int
    frame_size: int
    static_size: int

    @property
    def has_uvs(self) -> bool:
        return bool(self.flags & FLAG_HAS_UVS)

    @property
    def has_normals(self) -> bool:
        return bool(self.flags & FLAG_HAS_NORMALS)

    @property
    def has_transform(self) -> bool:
        return bool(self.flags & FLAG_HAS_TRANSFORM)

    @property
    def frame_positions_bytes(self) -> int:
        return self.vertex_count * 12

    @property
    def frame_timestamp_bytes(self) -> int:
        return 8

    @property
    def frame_transform_bytes(self) -> int:
        return FRAME_TRANSFORM_BYTES if self.has_transform else 0


@dataclass
class BmcCapture:
    header: BmcHeader
    index_data: np.ndarray  # uint32[N]
    uv_data: Optional[np.ndarray] = None  # float32[vertex_count x 2]
    normal_data: Optional[np.ndarray] = None  # float32[vertex_count x 3]
    primitives: List[PrimitiveEntry] = field(default_factory=list)
    materials: List[MaterialEntry] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Pack / unpack
# ---------------------------------------------------------------------------


def pack_header(
    vertex_count: int,
    index_count: int,
    primitive_count: int,
    material_count: int,
    flags: int,
    frame_size: int,
    static_size: int,
) -> bytes:
    return struct.pack(
        "<4sIIIIIIIQ",
        b"BMC1",
        1,  # version
        vertex_count,
        index_count,
        primitive_count,
        material_count,
        flags,
        frame_size,
        static_size,
    )


def unpack_header(data: bytes) -> BmcHeader:
    magic, version, vc, ic, pc, mc, flags, frame_size, static_size = struct.unpack(
        "<4sIIIIIIIQ", data[:HEADER_SIZE]
    )
    if magic != b"BMC1":
        raise ValueError(f"Bad BMC magic: {magic!r}")
    return BmcHeader(
        version=version,
        vertex_count=vc,
        index_count=ic,
        primitive_count=pc,
        material_count=mc,
        flags=flags,
        frame_size=frame_size,
        static_size=static_size,
    )


def pack_primitive_table(primitives: List[PrimitiveEntry]) -> bytes:
    parts = []
    for p in primitives:
        name_bytes = p.name.encode("utf-8")
        if len(name_bytes) > 65535:
            raise ValueError(f"Primitive name too long: {p.name}")
        parts.append(struct.pack("<H", len(name_bytes)))
        parts.append(name_bytes)
        parts.append(struct.pack("<IiI", p.start_index, p.index_count, p.material_id))
        parts.append(struct.pack("<i", p.flexmesh_index))
    return b"".join(parts)


def _unpack_primitive_table(data: bytes, count: int) -> List[PrimitiveEntry]:
    primitives = []
    offset = 0
    for _ in range(count):
        (name_len,) = struct.unpack_from("<H", data, offset)
        offset += 2
        name = data[offset : offset + name_len].decode("utf-8")
        offset += name_len
        start_index, index_count, material_id = struct.unpack_from("<IiI", data, offset)
        offset += 12
        (flexmesh_index,) = struct.unpack_from("<i", data, offset)
        offset += 4
        primitives.append(PrimitiveEntry(name=name, start_index=start_index, index_count=index_count, material_id=material_id, flexmesh_index=flexmesh_index))
    return primitives


def pack_material_table(materials: List[MaterialEntry]) -> bytes:
    parts = []
    for m in materials:
        name_bytes = m.name.encode("utf-8")
        if len(name_bytes) > 65535:
            raise ValueError(f"Material name too long: {m.name}")
        parts.append(struct.pack("<H", len(name_bytes)))
        parts.append(name_bytes)
    return b"".join(parts)


def _unpack_material_table(data: bytes, count: int) -> List[MaterialEntry]:
    materials = []
    offset = 0
    for _ in range(count):
        (name_len,) = struct.unpack_from("<H", data, offset)
        offset += 2
        name = data[offset : offset + name_len].decode("utf-8")
        offset += name_len
        materials.append(MaterialEntry(name=name))
    return materials


# ---------------------------------------------------------------------------
# Compute sizes
# ---------------------------------------------------------------------------


def compute_static_size(
    index_count: int,
    vertex_count: int,
    flags: int,
    primitives: List[PrimitiveEntry],
    materials: List[MaterialEntry],
) -> int:
    size = index_count * 4  # index_data
    if flags & FLAG_HAS_UVS:
        size += vertex_count * 8  # uv_data (float32 x 2)
    if flags & FLAG_HAS_NORMALS:
        size += vertex_count * 12  # normal_data (float32 x 3)
    # Primitive table
    for p in primitives:
        size += 2 + len(p.name.encode("utf-8")) + 12 + 4  # len + name + (uint32, uint32, int32) + flexmesh_index
    # Material table
    for m in materials:
        size += 2 + len(m.name.encode("utf-8"))
    return size


def compute_frame_size(vertex_count: int, has_transform: bool) -> int:
    size = 8 + vertex_count * 12  # timestamp + positions
    if has_transform:
        size += FRAME_TRANSFORM_BYTES
    return size


# ---------------------------------------------------------------------------
# Full file read / write
# ---------------------------------------------------------------------------


def write_bmc(path: str, capture: BmcCapture) -> None:
    header = capture.header
    static_size = compute_static_size(
        header.index_count,
        header.vertex_count,
        header.flags,
        capture.primitives,
        capture.materials,
    )
    frame_size = compute_frame_size(header.vertex_count, header.has_transform)

    header_packed = pack_header(
        vertex_count=header.vertex_count,
        index_count=header.index_count,
        primitive_count=len(capture.primitives),
        material_count=len(capture.materials),
        flags=header.flags,
        frame_size=frame_size,
        static_size=static_size,
    )

    with open(path, "wb") as f:
        f.write(header_packed)

        # Static section
        f.write(np.ascontiguousarray(capture.index_data, dtype=np.uint32).tobytes())
        if header.has_uvs and capture.uv_data is not None:
            f.write(np.ascontiguousarray(capture.uv_data, dtype=np.float32).tobytes())
        if header.has_normals and capture.normal_data is not None:
            f.write(np.ascontiguousarray(capture.normal_data, dtype=np.float32).tobytes())
        f.write(pack_primitive_table(capture.primitives))
        f.write(pack_material_table(capture.materials))

    # Return path for convenience
    return path


def read_bmc_header(path: str) -> BmcHeader:
    with open(path, "rb") as f:
        header_data = f.read(HEADER_SIZE)
    return unpack_header(header_data)


def read_bmc(path: str) -> BmcCapture:
    with open(path, "rb") as f:
        header = unpack_header(f.read(HEADER_SIZE))
        static = f.read(header.static_size)

    offset = 0

    # Index data
    index_bytes = header.index_count * 4
    index_data = np.frombuffer(static[offset : offset + index_bytes], dtype=np.uint32).copy()
    offset += index_bytes

    # UV data
    uv_data = None
    if header.has_uvs:
        uv_bytes = header.vertex_count * 8
        uv_data = np.frombuffer(static[offset : offset + uv_bytes], dtype=np.float32).reshape(-1, 2).copy()
        offset += uv_bytes

    # Normal data
    normal_data = None
    if header.has_normals:
        normal_bytes = header.vertex_count * 12
        normal_data = np.frombuffer(static[offset : offset + normal_bytes], dtype=np.float32).reshape(-1, 3).copy()
        offset += normal_bytes

    # Primitive table
    primitives = _unpack_primitive_table(static[offset:], header.primitive_count)
    # Advance past primitives
    for p in primitives:
        offset += 2 + len(p.name.encode("utf-8")) + 12 + 4

    # Material table
    materials = _unpack_material_table(static[offset:], header.material_count)

    return BmcCapture(
        header=header,
        index_data=index_data,
        uv_data=uv_data,
        normal_data=normal_data,
        primitives=primitives,
        materials=materials,
    )


# ---------------------------------------------------------------------------
# Frame access
# ---------------------------------------------------------------------------


def frame_offset(header: BmcHeader, frame_index: int) -> int:
    return HEADER_SIZE + header.static_size + frame_index * header.frame_size


def read_frame_timestamp(path: str, header: BmcHeader, frame_index: int) -> float:
    offset = frame_offset(header, frame_index)
    with open(path, "rb") as f:
        f.seek(offset)
        (ts,) = struct.unpack("<d", f.read(8))
    return ts


def read_frame_positions(path: str, header: BmcHeader, frame_index: int) -> np.ndarray:
    offset = frame_offset(header, frame_index) + 8  # skip timestamp
    count = header.vertex_count
    with open(path, "rb") as f:
        f.seek(offset)
        raw = f.read(count * 12)
    return np.frombuffer(raw, dtype=np.float32).reshape(-1, 3).copy()


def read_frame_transform(path: str, header: BmcHeader, frame_index: int) -> Optional[np.ndarray]:
    if not header.has_transform:
        return None
    offset = frame_offset(header, frame_index) + 8 + header.vertex_count * 12
    with open(path, "rb") as f:
        f.seek(offset)
        raw = f.read(FRAME_TRANSFORM_BYTES)
    return np.frombuffer(raw, dtype=np.float32).copy()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def frame_count(file_size: int, header: BmcHeader) -> int:
    frame_region = file_size - HEADER_SIZE - header.static_size
    return frame_region // header.frame_size if header.frame_size > 0 else 0
