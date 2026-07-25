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
FLAG_HAS_NODES = 1 << 3  # v5: refNode-triangle rigid transform
FLAG_WORLD_SPACE = 1 << 4  # v5: vertices already in absolute world space (no pool->Blender)
FLAG_HAS_PROPS = 1 << 5  # v6: trailing prop section (rigid propmeshes, frozen pose)

# --- Prop section (v6) -------------------------------------------------------
# BeamNG vehicles expose two mesh families: FLEXMESHES (the deforming body,
# captured into the shared pool + per-frame stream) and PROPMESHES (rigid parts
# animated by prop.position/prop.rotation — steering wheel, pedals, gauge
# needles, indicator stalk).  The stock exporter (util/export.lua:924-928)
# walks BOTH; the BMC capture historically walked only flexmeshes, so those
# rigid parts were missing from BMC-built caches.
#
# Props are stored in a NEW static section appended AFTER the material table,
# gated by FLAG_HAS_PROPS.  CRITICAL INVARIANT: this section grows
# ``static_size`` ONLY — it never touches ``vertex_count`` or ``frame_size``,
# so the per-frame flexmesh pool block and the ``frame_vehicle_transform``
# offset inside each frame are byte-for-byte unchanged.  (A prior attempt that
# folded prop verts into ``vertex_count`` shifted that offset and launched the
# car into the air; this layout makes that failure structurally impossible.)
# Old readers that don't know the flag simply ignore the trailing bytes.
#
# Data is stored RAW as the engine returns it (prop-local verts + a
# vehicle-space position + rotation quaternion) so the coordinate bake lives in
# Python and can be re-tuned by rebuilding the BVC from the SAME .bmc.
#
# Prop section layout (only present when FLAG_HAS_PROPS):
#   [0..4)  prop_count        uint32
#   per prop:
#     [2]   name_len          uint16
#     [..]  name              utf-8 bytes
#     [4]   vertices_count    uint32
#     [4]   index_count       uint32
#     [4]   material_id       int32
#     [4]   has_uv            uint32  (1/0)
#     [12]  position          f32 * 3   (vehicle-space, RAW)
#     [16]  rotation          f32 * 4   (quaternion xyzw, RAW)
#     [..]  vertices          f32 * 3 * vertices_count  (prop-local, RAW)
#     [..]  indices           uint32 * index_count
#     [..]  uvs               f32 * 2 * vertices_count   (only if has_uv)

# Per-frame layout (v5): timestamp(8) + vertices + transform block.
# Transform block = 9 f32 = (px,py,pz) translation + forward(3) + up(3),
# all already in Blender Z-up space (physics->Blender swap {x,z,-y}).  The
# builder reconstructs an orthonormal basis R=[right|fwd|up] and drives a
# parent empty, mirroring export.lua's rigid-motion intent.
FRAME_TRANSFORM_FLOATS = 9
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
class PropEntry:
    """One rigid propmesh, captured once (frozen pose).

    All spatial data is stored RAW in the engine's own space — ``position`` and
    ``rotation`` are the vehicle-space placement the engine reports for this
    prop, ``vertices`` are prop-local.  The Python builder is responsible for
    baking these into the same Blender space the flexmesh pool uses, so the
    coordinate convention can be re-tuned without a fresh capture.
    """
    name: str
    material_id: int
    position: "np.ndarray"          # f32[3] — vehicle-space, raw
    rotation: "np.ndarray"          # f32[4] — quaternion xyzw, raw
    vertices: "np.ndarray"          # f32[N,3] — prop-local, raw
    indices: "np.ndarray"           # uint32[M] — flat triangle indices
    uvs: Optional["np.ndarray"] = None  # f32[N,2] or None


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
    def has_world_space(self) -> bool:
        return bool(self.flags & FLAG_WORLD_SPACE)

    @property
    def has_props(self) -> bool:
        return bool(self.flags & FLAG_HAS_PROPS)

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


def pack_prop_section(props: List[PropEntry]) -> bytes:
    """Serialize the trailing prop section (see the layout note at module top)."""
    parts: List[bytes] = [struct.pack("<I", len(props))]
    for p in props:
        name_bytes = p.name.encode("utf-8")
        if len(name_bytes) > 65535:
            raise ValueError(f"Prop name too long: {p.name}")
        verts = np.ascontiguousarray(p.vertices, dtype=np.float32).reshape(-1, 3)
        idx = np.ascontiguousarray(p.indices, dtype=np.uint32).ravel()
        has_uv = 1 if p.uvs is not None else 0
        parts.append(struct.pack("<H", len(name_bytes)))
        parts.append(name_bytes)
        parts.append(struct.pack("<IiiI", verts.shape[0], int(idx.shape[0]),
                                 int(p.material_id), has_uv))
        parts.append(np.ascontiguousarray(p.position, dtype=np.float32).tobytes())
        parts.append(np.ascontiguousarray(p.rotation, dtype=np.float32).tobytes())
        parts.append(verts.tobytes())
        parts.append(idx.tobytes())
        if has_uv:
            uv = np.ascontiguousarray(p.uvs, dtype=np.float32).reshape(-1, 2)
            parts.append(uv.tobytes())
    return b"".join(parts)


def unpack_prop_section(data: bytes) -> List[PropEntry]:
    """Parse the trailing prop section produced by :func:`pack_prop_section`."""
    props: List[PropEntry] = []
    if len(data) < 4:
        return props
    (count,) = struct.unpack_from("<I", data, 0)
    offset = 4
    for _ in range(count):
        (name_len,) = struct.unpack_from("<H", data, offset)
        offset += 2
        name = data[offset:offset + name_len].decode("utf-8")
        offset += name_len
        vcount, icount, material_id, has_uv = struct.unpack_from("<IiiI", data, offset)
        offset += 16
        position = np.frombuffer(data, dtype=np.float32, count=3, offset=offset).copy()
        offset += 12
        rotation = np.frombuffer(data, dtype=np.float32, count=4, offset=offset).copy()
        offset += 16
        verts = np.frombuffer(
            data, dtype=np.float32, count=vcount * 3, offset=offset
        ).reshape(-1, 3).copy()
        offset += vcount * 12
        indices = np.frombuffer(
            data, dtype=np.uint32, count=icount, offset=offset
        ).copy()
        offset += icount * 4
        uvs = None
        if has_uv:
            uvs = np.frombuffer(
                data, dtype=np.float32, count=vcount * 2, offset=offset
            ).reshape(-1, 2).copy()
            offset += vcount * 8
        props.append(PropEntry(
            name=name, material_id=material_id, position=position,
            rotation=rotation, vertices=verts, indices=indices, uvs=uvs,
        ))
    return props


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
