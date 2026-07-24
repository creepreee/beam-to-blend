from __future__ import annotations

"""Regression test for the GLB material-name-table ordering bug (2026-07-24).

``build_from_gltf`` used to pack the global material name table BEFORE the
per-object loop registered any names, so the on-disk table held only
``__no_material__`` while per-object blocks referenced global ids 1..N.  At
import every name resolved to ``<unknown>``, collapsing all material slots into
one and breaking per-object material binding + texture assignment.

This test builds a two-object GLB where each object's faces reference distinct
named materials, builds a BVC, and asserts the material names round-trip
correctly (no ``<unknown>``).
"""

import json
import struct
from pathlib import Path
from typing import List, Tuple

import numpy as np

from importer.cache_builder import CacheBuilder
from runtime.cache_reader import CacheReader


def _pad4(b: bytes, fill: bytes = b"\x00") -> bytes:
    return b + fill * ((-len(b)) % 4)


def _build_glb_with_materials(
    objects: List[Tuple[str, np.ndarray, np.ndarray, List[str], List[int]]],
    material_names: List[str],
) -> bytes:
    """Build a GLB where each object has multiple material-split primitives.

    objects: (name, positions (N,3) f32, tris (M,3) u32, per-object material
    name list, per-face material-name-index list).
    material_names: global material list (indices used by primitives).
    """
    bin_parts: List[bytes] = []
    buffer_views: List[dict] = []
    accessors: List[dict] = []
    meshes: List[dict] = []
    nodes: List[dict] = []
    offset = 0

    def add_view(raw: bytes) -> int:
        nonlocal offset
        raw = _pad4(raw)
        buffer_views.append({"buffer": 0, "byteOffset": offset, "byteLength": len(raw)})
        bin_parts.append(raw)
        offset += len(raw)
        return len(buffer_views) - 1

    gltf_materials = [{"name": n} for n in material_names]

    for name, positions, tris, obj_mats, face_mat_ids in objects:
        positions = np.asarray(positions, dtype=np.float32)
        pos_view = add_view(positions.tobytes())
        accessors.append({
            "bufferView": pos_view, "componentType": 5126,
            "count": int(positions.shape[0]), "type": "VEC3",
            "min": positions.min(axis=0).tolist(),
            "max": positions.max(axis=0).tolist(),
        })
        pos_accessor = len(accessors) - 1

        # One primitive per material used by this object (BeamNG-style split).
        tris = np.asarray(tris, dtype=np.uint32)
        face_mat_ids = np.asarray(face_mat_ids)
        prims = []
        for local_mat in obj_mats:
            gmat = material_names.index(local_mat)
            sel = tris[face_mat_ids == gmat]
            if sel.shape[0] == 0:
                continue
            idx_view = add_view(sel.reshape(-1).astype(np.uint32).tobytes())
            accessors.append({
                "bufferView": idx_view, "componentType": 5125,
                "count": int(sel.size), "type": "SCALAR",
            })
            prims.append({
                "attributes": {"POSITION": pos_accessor},
                "indices": len(accessors) - 1,
                "mode": 4,
                "material": gmat,
            })

        meshes.append({"name": f"{name}_mesh", "primitives": prims})
        nodes.append({"name": name, "mesh": len(meshes) - 1})

    bin_chunk = b"".join(bin_parts)
    gltf = {
        "asset": {"version": "2.0"},
        "scenes": [{"nodes": list(range(len(nodes)))}],
        "scene": 0,
        "nodes": nodes,
        "meshes": meshes,
        "materials": gltf_materials,
        "accessors": accessors,
        "bufferViews": buffer_views,
        "buffers": [{"byteLength": len(bin_chunk)}],
    }
    json_chunk = _pad4(json.dumps(gltf).encode("utf-8"), fill=b" ")
    bin_chunk = _pad4(bin_chunk)
    body = (
        struct.pack("<I4s", len(json_chunk), b"JSON") + json_chunk
        + struct.pack("<I4s", len(bin_chunk), b"BIN\x00") + bin_chunk
    )
    return struct.pack("<4sII", b"glTF", 2, 12 + len(body)) + body


def _make_box(z: float) -> Tuple[np.ndarray, np.ndarray]:
    """A 4-vertex, 2-triangle patch at height ``z``."""
    positions = np.array([
        [0, 0, z], [1, 0, z], [1, 1, z], [0, 1, z],
    ], dtype=np.float32)
    tris = np.array([[0, 1, 2], [0, 2, 3]], dtype=np.uint32)
    return positions, tris


def test_glb_material_names_roundtrip(tmp_path):
    # Global material palette; object A uses paint+glass, object B uses leather.
    mats = ["paint", "glass", "leather"]

    posA, trisA = _make_box(0.0)
    # 2 faces: face0 -> paint(0), face1 -> glass(1)
    objA = ("body", posA, trisA, ["paint", "glass"], [0, 1])

    posB, trisB = _make_box(1.0)
    # 2 faces both -> leather(2)
    objB = ("seat", posB, trisB, ["leather"], [2, 2])

    glb = _build_glb_with_materials([objA, objB], mats)

    seq_dir = tmp_path / "seq"
    seq_dir.mkdir()
    for i in range(2):  # two identical frames
        (seq_dir / f"frame_{i:05d}.glb").write_bytes(glb)

    out = tmp_path / "seq.bvc"
    CacheBuilder(seq_dir, out).build(weld=False, workers=1)

    r = CacheReader(str(out))
    try:
        body_mats = r.base_material_names("body")
        seat_mats = r.base_material_names("seat")
    finally:
        r.close()

    # THE FIX: names resolve to the real palette, never "<unknown>".
    assert "<unknown>" not in body_mats
    assert "<unknown>" not in seat_mats
    assert set(body_mats) == {"paint", "glass"}
    assert set(seat_mats) == {"leather"}
