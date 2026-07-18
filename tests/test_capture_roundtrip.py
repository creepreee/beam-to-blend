from __future__ import annotations

"""Round-trip test for the BMC v1 → BVC pipeline.

Writes a synthetic BMC v1 capture, builds BVC via CacheBuilder.build_from_capture,
reads back via CacheReader, and asserts positions/indices/UVs match.
"""

import numpy as np
import struct

from importer import capture_format as bmc
from importer.capture_reader import BmcReader
from importer.cache_builder import CacheBuilder
from runtime.cache_reader import CacheReader


_OBJECTS = [
    # (name, vertex_count, triangle_count, material_name)
    ("body", 8, 4, "paint"),
    ("hood", 6, 2, "chrome"),
    ("wheel", 5, 3, "rubber"),
]
_FRAMES = 10
_HAS_UVS = True


def _write_bmc(path: str):
    """Write a synthetic BMC v1 file."""
    total_verts = sum(vc for _, vc, _, _ in _OBJECTS)
    total_idx = sum(tc * 3 for _, _, tc, _ in _OBJECTS)

    rng = np.random.default_rng(7)

    # Shared index buffer: concatenate per-object index ranges
    idx_parts = []
    offset = 0
    primitives = []
    for fi, (name, vc, tc, mat_name) in enumerate(_OBJECTS):
        ic = tc * 3
        ids = rng.integers(offset, offset + vc, size=ic, dtype=np.uint32)
        idx_parts.append(ids)
        primitives.append(bmc.PrimitiveEntry(
            name=name,
            start_index=offset,
            index_count=ic,
            material_id=0,
            flexmesh_index=fi,  # each object is its own flexmesh
        ))
        offset += vc

    index_data = np.concatenate(idx_parts)

    # Shared UVs
    uv_data = rng.random((total_verts, 2), dtype=np.float32)

    flags = bmc.FLAG_HAS_UVS if _HAS_UVS else 0
    static_size = bmc.compute_static_size(
        total_idx, total_verts, flags, primitives,
        [bmc.MaterialEntry(name="default")]
    )
    frame_size = bmc.compute_frame_size(total_verts, has_transform=False)

    header = bmc.BmcHeader(
        version=1,
        vertex_count=total_verts,
        index_count=total_idx,
        primitive_count=len(primitives),
        material_count=1,
        flags=flags,
        frame_size=frame_size,
        static_size=static_size,
    )

    capture = bmc.BmcCapture(
        header=header,
        index_data=index_data,
        uv_data=uv_data,
        primitives=primitives,
        materials=[bmc.MaterialEntry(name="default")],
    )

    bmc.write_bmc(path, capture)

    # Append frames: timestamp + positions
    with open(path, "ab") as f:
        for fi in range(_FRAMES):
            f.write(struct.pack("<d", float(fi) / 60.0))
            pos = rng.random((total_verts, 3), dtype=np.float32)
            f.write(pos.tobytes())

    return path


def test_build_from_capture_roundtrip(tmp_path):
    bmc_path = _write_bmc(str(tmp_path / "capture.bmc"))
    out = tmp_path / "capture.bvc"

    manifest = CacheBuilder(tmp_path, out).build_from_capture(bmc_path)

    expected_names = [o[0] for o in _OBJECTS]
    assert manifest.stable_objects == expected_names
    assert manifest.dynamic_objects == []

    with BmcReader(bmc_path) as bmc_reader, CacheReader(out) as bvc_reader:
        assert bvc_reader.frame_count == _FRAMES
        assert bvc_reader.object_names() == expected_names

        for name, vc, tc, _mat in _OBJECTS:
            # Indices match
            obj_info = bmc_reader.object_info(name)
            expected_idx = bmc_reader.base_indices(name)
            got_idx = bvc_reader.base_indices(name)
            np.testing.assert_array_equal(got_idx, expected_idx)

            # UVs match
            expected_uv = bmc_reader.base_uvs(name)
            got_uv = bvc_reader.base_uvs(name)
            np.testing.assert_allclose(got_uv, expected_uv, atol=1e-6)

            # Positions match for every frame (pool → Blender Y↔Z swap applied by builder)
            for f in range(_FRAMES):
                raw = bmc_reader.frame_positions(f, name)
                expected_pos = np.empty_like(raw)
                expected_pos[:, 0] = raw[:, 2]
                expected_pos[:, 1] = raw[:, 0]
                expected_pos[:, 2] = raw[:, 1]
                got_pos = bvc_reader.frame_positions(name, f)
                np.testing.assert_allclose(got_pos, expected_pos, atol=1e-4)


def test_frame_count_matches(tmp_path):
    bmc_path = _write_bmc(str(tmp_path / "capture.bmc"))
    out = tmp_path / "capture.bvc"
    CacheBuilder(tmp_path, out).build_from_capture(bmc_path)

    with BmcReader(bmc_path) as r:
        assert r.frame_count() == _FRAMES
