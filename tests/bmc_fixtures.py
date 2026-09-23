from __future__ import annotations

"""Synthetic BMC capture fixtures for headless Blender tests.

Builds a real ``.bmc`` file (BMC v1: shared pool + per-frame positions +
rigid transform) so the BMC pipeline can be exercised end-to-end without
BeamNG.  Coordinates are written in the v5 capture's world space (Blender
Z-up) and FLAG_WORLD_SPACE tells the builder to copy them through.
"""

import os
from typing import List, Tuple

import numpy as np

from importer import capture_format as bmc


def _box(cx, cy, cz, sx, sy, sz):
    """Axis-aligned box centred at (cx, cy, cz) with the given edge lengths."""
    hx, hy, hz = sx / 2.0, sy / 2.0, sz / 2.0
    pos = np.array([
        [cx - hx, cy - hy, cz - hz], [cx + hx, cy - hy, cz - hz],
        [cx + hx, cy + hy, cz - hz], [cx - hx, cy + hy, cz - hz],
        [cx - hx, cy - hy, cz + hz], [cx + hx, cy - hy, cz + hz],
        [cx + hx, cy + hy, cz + hz], [cx - hx, cy + hy, cz + hz],
    ], dtype=np.float32)
    idx = np.array([
        [0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7],
        [0, 1, 5], [0, 5, 4], [1, 2, 6], [1, 6, 5],
        [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7],
    ], dtype=np.uint32)
    return pos, idx


def write_bmc_sequence(path: str,
                       objects: List[Tuple[str, List[np.ndarray], np.ndarray]],
                       transforms: List[np.ndarray] = None) -> str:
    """Write a BMC v1 capture from per-frame vertex arrays.

    objects: [(name, [frame0_pos, frame1_pos, ...], indices (M,3) uint32)]
    transforms: optional per-frame 9-float blocks
        (px,py,pz, fx,fy,fz, ux,uy,uz) in Blender Z-up world space.

    Every object must have the same vertex count in every frame (BMC frames
    are fixed-size).  All objects' vertices are concatenated into the shared
    pool in object order.
    """
    if transforms is None:
        transforms = []
    has_transform = len(transforms) > 0
    frame_count = len(objects[0][1])
    for name, frames, _idx in objects:
        assert len(frames) == frame_count, f"{name}: frame count mismatch"

    vcounts = [len(frames[0]) for _n, frames, _i in objects]
    vertex_count = sum(vcounts)

    # Shared index buffer + primitive table (one primitive per object).
    index_parts = []
    primitives = []
    pool_offset = 0
    for name, _frames, idx in objects:
        remapped = idx.astype(np.uint32).ravel() + pool_offset
        index_parts.append(remapped)
        primitives.append(bmc.PrimitiveEntry(
            name=name,
            start_index=sum(len(p) for p in index_parts[:-1]),
            index_count=len(remapped),
            material_id=0,
            flexmesh_index=-1,
        ))
        pool_offset += len(frames[0])
    index_data = np.concatenate(index_parts)

    # Frame blocks: timestamp + shared pool positions (+ transform).
    frame_blocks = []
    for f in range(frame_count):
        block = np.empty((vertex_count, 3), dtype=np.float32)
        off = 0
        for name, frames, _idx in objects:
            n = len(frames[f])
            block[off:off + n] = frames[f]
            off += n
        buf = bytearray()
        buf += np.array([0.0], dtype=np.float64).tobytes()   # timestamp
        buf += block.tobytes()
        if has_transform:
            buf += np.asarray(transforms[f], dtype=np.float32).tobytes()
        frame_blocks.append(bytes(buf))

    flags = 0
    if has_transform:
        flags |= bmc.FLAG_HAS_TRANSFORM
    flags |= bmc.FLAG_WORLD_SPACE

    static_size = bmc.compute_static_size(
        len(index_data), vertex_count, flags, primitives,
        [bmc.MaterialEntry(name="default")],
    )
    header = bmc.BmcHeader(
        version=1,
        vertex_count=vertex_count,
        index_count=len(index_data),
        primitive_count=len(primitives),
        material_count=1,
        flags=flags,
        frame_size=len(frame_blocks[0]),
        static_size=static_size,
    )
    capture = bmc.BmcCapture(
        header=header,
        index_data=index_data,
        primitives=primitives,
        materials=[bmc.MaterialEntry(name="default")],
    )
    with open(path, "wb") as fh:
        fh.write(bmc.pack_header(
            vertex_count, len(index_data), len(primitives), 1,
            flags, header.frame_size, static_size))
        fh.write(np.ascontiguousarray(index_data, dtype=np.uint32).tobytes())
        fh.write(bmc.pack_primitive_table(primitives))
        fh.write(bmc.pack_material_table(capture.materials))
        for block in frame_blocks:
            fh.write(block)
    return path


def moving_car_sequence(path: str, n_frames: int = 24) -> str:
    """A body sliding +X with two wheels riding along (one bobbing)."""
    import math
    objects = []
    transforms = []
    body_frames, wfl_frames, wfr_frames = [], [], []
    for f in range(n_frames):
        bx = -1.0 + 0.08 * f
        bob = 0.02 * math.sin(2.0 * math.pi * f / 12.0)
        body_pos, body_idx = _box(bx, 0.0, 0.6, 1.0, 0.5, 0.35)
        wfl_pos, wfl_idx = _box(bx - 0.55, -0.32, 0.15 + bob, 0.28, 0.28, 0.28)
        wfr_pos, wfr_idx = _box(bx - 0.55, 0.32, 0.15 - bob, 0.28, 0.28, 0.28)
        body_frames.append(body_pos)
        wfl_frames.append(wfl_pos)
        wfr_frames.append(wfr_pos)
        transforms.append(np.array(
            [bx, 0.0, 0.0,  1.0, 0.0, 0.0,  0.0, 1.0, 0.0], dtype=np.float32))
    objects = [
        ("body", body_frames, body_idx),
        ("wheel_fl", wfl_frames, wfl_idx),
        ("wheel_fr", wfr_frames, wfl_idx),
    ]
    return write_bmc_sequence(path, objects, transforms)
