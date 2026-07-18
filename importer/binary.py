from __future__ import annotations

"""BVC (BeamNG Vertex Cache) binary format definition.

The cache stores, for a sequence of frames:
  * each stable object's base mesh (indices + vertex count) exactly once, and
  * a per-frame stream of vertex positions for the stable objects.

V3 adds UV coordinates, material names, and per-face material ids to the
base mesh (stable objects) and per-frame dynamic data.

Dynamic (topology-changing) objects store per-frame full mesh data
(positions + indices + uvs + materials) in a separate section after the frame
blocks.

Everything is little-endian.

V3 file layout
---------------
    [Header]
    [Object table]           one entry per object, stable objects first
    [Base mesh blocks]       indices for each stable object, in table order
    [UV blocks]              (N,2) f32 per stable object, in table order — NEW v3
    [Material name table]    global dedup of all material names — NEW v3
    [Material blocks]        per stable object: local_names + per-face ids — NEW v3
    [Frame directory]        frame_count * uint64 absolute offsets
    [Frame blocks]           frame_count blocks; each is the concatenated
                             positions (vertex_count*3 float32) of every stable
                             object, in object-table order
    [Dynamic directory]      (optional) one entry per (frame, dynamic object)
    [Dynamic data]           (optional) concatenated per-frame mesh data

Header (little-endian, v3)
    char[4]  magic   = "BVC1"
    uint32   version = 3
    uint32   frame_count
    uint32   object_count           (total, incl. dynamic)
    uint32   stable_count
    uint64   stable_vertex_total    (sum of stable vertex counts = floats/3 per frame)
    uint64   object_table_offset
    uint64   base_mesh_offset
    uint64   uv_blocks_offset       (0 if v2 / no UV data)
    uint64   material_table_offset  (0 if v2 / no material data)
    uint64   material_blocks_offset (0 if v2 / no material data)
    uint64   frame_directory_offset
    uint64   frame_blocks_offset
    uint64   dynamic_directory_offset  (0 if no dynamic objects)
    uint64   dynamic_data_offset        (0 if no dynamic objects)

Object table entry (v3)
    uint16   name_length
    char[]   name (utf-8, name_length bytes)
    uint8    flags            bit0: cacheable/stable
    uint32   vertex_count
    uint32   face_count
    char[64] topology_hash    (hex, ascii, zero-padded)
    uint64   base_index_offset (abs offset of this object's index block; 0 if dynamic)
    uint32   index_count       (number of triangles; 0 if dynamic)
    uint64   frame_vertex_offset (float offset within a frame block where this
                                  object's positions start; for stable objects)
    uint64   uv_offset         (abs offset of this object's (N,2) f32 UV block; 0 if none) — NEW v3
    uint64   material_offset   (abs offset of this object's material block; 0 if none) — NEW v3

Dynamic directory entry  (one per (frame, dynamic_object), in frame-major order)
    uint32   vertex_count
    uint32   index_count          (number of triangles)
    uint64   positions_offset     (absolute file offset into dynamic data section)
    uint64   indices_offset       (absolute file offset into dynamic data section)
    uint64   uv_offset            (absolute file offset; 0 if none) — NEW v3
    uint64   material_offset      (absolute file offset; 0 if none) — NEW v3

Material name table (at material_table_offset)
    uint32   count                  (number of unique names)
    for each name:
        uint16   length
        char[]   utf-8 (length bytes)

Material block (one per object, at uv_offset)
    uint16   local_count            (number of material names for this object)
    uint32[] global_ids             (local_count * uint32 indices into the global name table)
    uint16[] per_face_ids           (face_count * uint16 local indices)
"""

import struct

MAGIC = b"BVC1"
VERSION = 4

FLAG_STABLE = 1 << 0
FLAG_WELDED = 1 << 1  # diagnostic: this object's vertices were welded at build time

_HEADER = struct.Struct("<4sIIIIQQQQQQQQQQQ")
_HEADER_V3 = struct.Struct("<4sIIIIQQQQQQQQQQ")
_HEADER_V2 = struct.Struct("<4sIIIIQQQQQQQ")

_HASH_LEN = 64

_OBJ_TAIL = struct.Struct("<BII64sQIQ")

_DYN_ENTRY = struct.Struct("<IIQQQQ")
_DYN_ENTRY_V2 = struct.Struct("<IIQQ")
DYN_ENTRY_SIZE = _DYN_ENTRY.size
DYN_ENTRY_SIZE_V2 = _DYN_ENTRY_V2.size


def pack_header(
    frame_count: int,
    object_count: int,
    stable_count: int,
    stable_vertex_total: int,
    object_table_offset: int,
    base_mesh_offset: int,
    uv_blocks_offset: int = 0,
    material_table_offset: int = 0,
    material_blocks_offset: int = 0,
    frame_directory_offset: int = 0,
    frame_blocks_offset: int = 0,
    dynamic_directory_offset: int = 0,
    dynamic_data_offset: int = 0,
    transform_data_offset: int = 0,
) -> bytes:
    return _HEADER.pack(
        MAGIC,
        VERSION,
        frame_count,
        object_count,
        stable_count,
        stable_vertex_total,
        object_table_offset,
        base_mesh_offset,
        uv_blocks_offset,
        material_table_offset,
        material_blocks_offset,
        frame_directory_offset,
        frame_blocks_offset,
        dynamic_directory_offset,
        dynamic_data_offset,
        transform_data_offset,
    )


def unpack_header(data: bytes) -> dict:
    magic = data[0:4]
    if magic != MAGIC:
        raise ValueError(f"not a BVC file (magic={magic!r})")
    version = int(data[4])
    if version < 2 or version > VERSION:
        raise ValueError(f"unsupported BVC version {version}")
    if version == 2:
        (magic_v2, ver_v2, fc, oc, sc, svt, oto, bmo,
         fdo, fbo, ddo, ddo2) = _HEADER_V2.unpack_from(data, 0)
        return {
            "version": version,
            "frame_count": fc,
            "object_count": oc,
            "stable_count": sc,
            "stable_vertex_total": svt,
            "object_table_offset": oto,
            "base_mesh_offset": bmo,
            "uv_blocks_offset": 0,
            "material_table_offset": 0,
            "material_blocks_offset": 0,
            "frame_directory_offset": fdo,
            "frame_blocks_offset": fbo,
            "dynamic_directory_offset": ddo,
            "dynamic_data_offset": ddo2,
            "transform_data_offset": 0,
        }
    if version == 3:
        (
            magic_v3, ver_v3, fc, oc, sc, svt, oto, bmo,
            uvo, mto, mbo, fdo, fbo, ddo, ddo2,
        ) = _HEADER_V3.unpack_from(data, 0)
        return {
            "version": version,
            "frame_count": fc,
            "object_count": oc,
            "stable_count": sc,
            "stable_vertex_total": svt,
            "object_table_offset": oto,
            "base_mesh_offset": bmo,
            "uv_blocks_offset": uvo,
            "material_table_offset": mto,
            "material_blocks_offset": mbo,
            "frame_directory_offset": fdo,
            "frame_blocks_offset": fbo,
            "dynamic_directory_offset": ddo,
            "dynamic_data_offset": ddo2,
            "transform_data_offset": 0,
        }
    (
        magic_v4, ver_v4, fc, oc, sc, svt, oto, bmo,
        uvo, mto, mbo, fdo, fbo, ddo, ddo2, tdo,
    ) = _HEADER.unpack_from(data, 0)
    return {
        "version": version,
        "frame_count": fc,
        "object_count": oc,
        "stable_count": sc,
        "stable_vertex_total": svt,
        "object_table_offset": oto,
        "base_mesh_offset": bmo,
        "uv_blocks_offset": uvo,
        "material_table_offset": mto,
        "material_blocks_offset": mbo,
        "frame_directory_offset": fdo,
        "frame_blocks_offset": fbo,
        "dynamic_directory_offset": ddo,
        "dynamic_data_offset": ddo2,
        "transform_data_offset": tdo,
    }


HEADER_SIZE = _HEADER.size


def pack_object_entry(
    name: str,
    stable: bool,
    vertex_count: int,
    face_count: int,
    topology_hash: str,
    base_index_offset: int,
    index_count: int,
    frame_vertex_offset: int,
    welded: bool = False,
    uv_offset: int = 0,
    material_offset: int = 0,
) -> bytes:
    name_bytes = name.encode("utf-8")
    flags = FLAG_STABLE if stable else 0
    if welded:
        flags |= FLAG_WELDED
    hash_bytes = topology_hash.encode("ascii")[:_HASH_LEN].ljust(_HASH_LEN, b"\x00")
    return (
        struct.pack("<H", len(name_bytes))
        + name_bytes
        + _OBJ_TAIL.pack(
            flags,
            vertex_count,
            face_count,
            hash_bytes,
            base_index_offset,
            index_count,
            frame_vertex_offset,
        )
        + struct.pack("<QQ", uv_offset, material_offset)
    )


def unpack_object_entry(data: bytes, offset: int, is_v3: bool = True) -> tuple[dict, int]:
    (name_len,) = struct.unpack_from("<H", data, offset)
    offset += 2
    name = data[offset : offset + name_len].decode("utf-8")
    offset += name_len
    (
        flags,
        vertex_count,
        face_count,
        hash_bytes,
        base_index_offset,
        index_count,
        frame_vertex_offset,
    ) = _OBJ_TAIL.unpack_from(data, offset)
    offset += _OBJ_TAIL.size
    uv_offset = 0
    material_offset = 0
    if is_v3:
        uv_offset, material_offset = struct.unpack_from("<QQ", data, offset)
        offset += 16
    return (
        {
            "name": name,
            "stable": bool(flags & FLAG_STABLE),
            "welded": bool(flags & FLAG_WELDED),
            "vertex_count": vertex_count,
            "face_count": face_count,
            "topology_hash": hash_bytes.rstrip(b"\x00").decode("ascii"),
            "base_index_offset": base_index_offset,
            "index_count": index_count,
            "frame_vertex_offset": frame_vertex_offset,
            "uv_offset": uv_offset,
            "material_offset": material_offset,
        },
        offset,
    )


def pack_dynamic_entry(
    vertex_count: int,
    index_count: int,
    positions_offset: int,
    indices_offset: int,
    uv_offset: int = 0,
    material_offset: int = 0,
) -> bytes:
    return _DYN_ENTRY.pack(
        vertex_count, index_count, positions_offset, indices_offset,
        uv_offset, material_offset,
    )


def unpack_dynamic_entry(data: bytes, offset: int, is_v3: bool = True) -> dict:
    if is_v3:
        vc, ic, pos_off, idx_off, uv_off, mat_off = _DYN_ENTRY.unpack_from(data, offset)
    else:
        vc, ic, pos_off, idx_off = _DYN_ENTRY_V2.unpack_from(data, offset)
        uv_off = 0
        mat_off = 0
    return {
        "vertex_count": vc,
        "index_count": ic,
        "positions_offset": pos_off,
        "indices_offset": idx_off,
        "uv_offset": uv_off,
        "material_offset": mat_off,
    }


# --- Material name table helpers ------------------------------------------

def pack_material_name_table(names: list[str]) -> bytes:
    """Pack a global-dedup list of material names.

    Format:
        uint32 count
        for each name: uint16 length + utf-8 bytes
    """
    name_bytes_list = [n.encode("utf-8") for n in names]
    parts = [struct.pack("<I", len(names))]
    for nb in name_bytes_list:
        parts.append(struct.pack("<H", len(nb)))
        parts.append(nb)
    return b"".join(parts)


def unpack_material_name_table(data: bytes, offset: int = 0) -> tuple[list[str], int]:
    (count,) = struct.unpack_from("<I", data, offset)
    offset += 4
    names: list[str] = []
    for _ in range(count):
        (length,) = struct.unpack_from("<H", data, offset)
        offset += 2
        names.append(data[offset : offset + length].decode("utf-8"))
        offset += length
    return names, offset


def pack_material_block(
    global_name_ids: list[int],
    per_face_ids: list[int],
) -> bytes:
    """Pack one object's material block.

    Format:
        uint16 local_count (number of unique materials this object uses)
        uint32[] global_ids (local_count * uint32 indices into global name table)
        uint16[] per_face_ids (face_count * uint16 local indices)
    """
    local_count = len(global_name_ids)
    face_count = len(per_face_ids)
    parts = [
        struct.pack("<H", local_count),
        struct.pack(f"<{local_count}I", *global_name_ids),
        struct.pack(f"<{face_count}H", *per_face_ids),
    ]
    return b"".join(parts)


def unpack_material_block(
    data: bytes, offset: int = 0,
) -> tuple[list[int], list[int], int]:
    """Unpack one object's material block.

    Returns (global_name_ids, per_face_ids, new_offset).
    """
    (local_count,) = struct.unpack_from("<H", data, offset)
    offset += 2
    global_ids = list(struct.unpack_from(f"<{local_count}I", data, offset))
    offset += local_count * 4
    remaining = (len(data) - offset) // 2
    per_face = list(struct.unpack_from(f"<{remaining}H", data, offset))
    offset += remaining * 2
    return global_ids, per_face, offset
