from __future__ import annotations

"""Helpers to build minimal valid GLB byte strings for tests.

Keeps the GLB-reader tests self-contained (no reliance on large external
sample files). Produces spec-compliant glTF 2.0 GLB with a single embedded
buffer.
"""

import json
import struct
from typing import List, Optional, Tuple

import numpy as np


def _pad4(b: bytes, fill: bytes = b"\x00") -> bytes:
    pad = (-len(b)) % 4
    return b + fill * pad


def build_glb(
    objects: List[Tuple[str, np.ndarray, Optional[np.ndarray]]],
) -> bytes:
    """Build a GLB from ``(name, positions (N,3) float32, indices (M,3) int|None)``.

    Each object becomes one node -> one mesh -> one triangle primitive.
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
        buffer_views.append(
            {"buffer": 0, "byteOffset": offset, "byteLength": len(raw)}
        )
        bin_parts.append(raw)
        offset += len(raw)
        return len(buffer_views) - 1

    for name, positions, indices in objects:
        positions = np.asarray(positions, dtype=np.float32)
        pos_view = add_view(positions.tobytes())
        pos_min = positions.min(axis=0).tolist()
        pos_max = positions.max(axis=0).tolist()
        accessors.append(
            {
                "bufferView": pos_view,
                "componentType": 5126,  # FLOAT
                "count": int(positions.shape[0]),
                "type": "VEC3",
                "min": pos_min,
                "max": pos_max,
            }
        )
        pos_accessor = len(accessors) - 1

        attributes = {"POSITION": pos_accessor}
        prim: dict = {"attributes": attributes, "mode": 4}

        if indices is not None:
            indices = np.asarray(indices, dtype=np.uint32).reshape(-1)
            idx_view = add_view(indices.tobytes())
            accessors.append(
                {
                    "bufferView": idx_view,
                    "componentType": 5125,  # UNSIGNED_INT
                    "count": int(indices.shape[0]),
                    "type": "SCALAR",
                }
            )
            prim["indices"] = len(accessors) - 1

        meshes.append({"name": f"{name}_mesh", "primitives": [prim]})
        nodes.append({"name": name, "mesh": len(meshes) - 1})

    bin_chunk = b"".join(bin_parts)
    gltf = {
        "asset": {"version": "2.0"},
        "scenes": [{"nodes": list(range(len(nodes)))}],
        "scene": 0,
        "nodes": nodes,
        "meshes": meshes,
        "accessors": accessors,
        "bufferViews": buffer_views,
        "buffers": [{"byteLength": len(bin_chunk)}],
    }

    json_chunk = _pad4(json.dumps(gltf).encode("utf-8"), fill=b" ")
    bin_chunk = _pad4(bin_chunk)

    body = (
        struct.pack("<I4s", len(json_chunk), b"JSON")
        + json_chunk
        + struct.pack("<I4s", len(bin_chunk), b"BIN\x00")
        + bin_chunk
    )
    header = struct.pack("<4sII", b"glTF", 2, 12 + len(body))
    return header + body


def build_shared_pool_glb(
    pool: np.ndarray,
    objects: List[Tuple[str, np.ndarray]],
) -> bytes:
    """Build a GLB where every object indexes into ONE shared POSITION accessor.

    Mirrors the real BeamNG layout: ``pool`` is an (N,3) float32 vertex buffer;
    each object is ``(name, indices)`` where indices reference rows of ``pool``.
    Every object's mesh has a single primitive using the same POSITION accessor.
    """
    pool = np.asarray(pool, dtype=np.float32)

    bin_parts: List[bytes] = []
    buffer_views: List[dict] = []
    accessors: List[dict] = []
    meshes: List[dict] = []
    nodes: List[dict] = []
    offset = 0

    def add_view(raw: bytes) -> int:
        nonlocal offset
        raw = _pad4(raw)
        buffer_views.append(
            {"buffer": 0, "byteOffset": offset, "byteLength": len(raw)}
        )
        bin_parts.append(raw)
        offset += len(raw)
        return len(buffer_views) - 1

    # One shared POSITION accessor for the whole pool.
    pos_view = add_view(pool.tobytes())
    accessors.append(
        {
            "bufferView": pos_view,
            "componentType": 5126,
            "count": int(pool.shape[0]),
            "type": "VEC3",
            "min": pool.min(axis=0).tolist(),
            "max": pool.max(axis=0).tolist(),
        }
    )
    shared_pos_accessor = 0

    for name, indices in objects:
        indices = np.asarray(indices, dtype=np.uint32).reshape(-1)
        idx_view = add_view(indices.tobytes())
        accessors.append(
            {
                "bufferView": idx_view,
                "componentType": 5125,
                "count": int(indices.shape[0]),
                "type": "SCALAR",
            }
        )
        prim = {
            "attributes": {"POSITION": shared_pos_accessor},
            "indices": len(accessors) - 1,
            "mode": 4,
        }
        meshes.append({"name": f"{name}_mesh", "primitives": [prim]})
        nodes.append({"name": name, "mesh": len(meshes) - 1})

    bin_chunk = b"".join(bin_parts)
    gltf = {
        "asset": {"version": "2.0"},
        "scenes": [{"nodes": list(range(len(nodes)))}],
        "scene": 0,
        "nodes": nodes,
        "meshes": meshes,
        "accessors": accessors,
        "bufferViews": buffer_views,
        "buffers": [{"byteLength": len(bin_chunk)}],
    }
    json_chunk = _pad4(json.dumps(gltf).encode("utf-8"), fill=b" ")
    bin_chunk = _pad4(bin_chunk)
    body = (
        struct.pack("<I4s", len(json_chunk), b"JSON")
        + json_chunk
        + struct.pack("<I4s", len(bin_chunk), b"BIN\x00")
        + bin_chunk
    )
    header = struct.pack("<4sII", b"glTF", 2, 12 + len(body))
    return header + body


def simple_quad(name: str = "quad", z: float = 0.0) -> Tuple[str, np.ndarray, np.ndarray]:
    """A 4-vertex, 2-triangle quad. ``z`` lets tests move vertices per 'frame'."""
    positions = np.array(
        [[0, 0, z], [1, 0, z], [1, 1, z], [0, 1, z]], dtype=np.float32
    )
    indices = np.array([[0, 1, 2], [0, 2, 3]], dtype=np.uint32)
    return name, positions, indices
