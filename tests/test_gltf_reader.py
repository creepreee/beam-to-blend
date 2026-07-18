from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from importer.gltf_reader import GLBReadError, read_glb
from tests.glb_fixtures import build_glb, build_shared_pool_glb, simple_quad


def _write(tmp_path: Path, data: bytes, name: str = "frame.glb") -> Path:
    p = tmp_path / name
    p.write_bytes(data)
    return p


def test_reads_single_object(tmp_path):
    glb = build_glb([simple_quad("body")])
    doc = read_glb(_write(tmp_path, glb))

    assert len(doc.objects) == 1
    obj = doc.objects[0]
    assert obj.name == "body"
    assert obj.vertex_count == 4
    assert obj.face_count == 2
    assert obj.positions.shape == (4, 3)
    assert obj.positions.dtype == np.float32
    assert obj.indices.shape == (2, 3)


def test_reads_multiple_objects_keyed_by_name(tmp_path):
    glb = build_glb([simple_quad("hood"), simple_quad("door")])
    doc = read_glb(_write(tmp_path, glb))

    by_name = doc.by_name()
    assert set(by_name) == {"hood", "door"}
    assert by_name["hood"].vertex_count == 4


def test_positions_roundtrip_exactly(tmp_path):
    name, positions, indices = simple_quad("part", z=2.5)
    glb = build_glb([(name, positions, indices)])
    doc = read_glb(_write(tmp_path, glb))

    np.testing.assert_array_equal(doc.objects[0].positions, positions)
    np.testing.assert_array_equal(doc.objects[0].indices.reshape(-1), indices.reshape(-1))


def test_non_indexed_geometry(tmp_path):
    positions = np.random.rand(9, 3).astype(np.float32)  # 3 implicit triangles
    glb = build_glb([("tri_soup", positions, None)])
    doc = read_glb(_write(tmp_path, glb))

    obj = doc.objects[0]
    # Non-indexed geometry is given explicit sequential indices for a uniform
    # downstream contract (every object has an index array).
    assert obj.vertex_count == 9
    assert obj.face_count == 3
    np.testing.assert_array_equal(
        obj.indices, np.arange(9, dtype=np.int32).reshape(3, 3)
    )


def test_rejects_non_glb(tmp_path):
    with pytest.raises(GLBReadError):
        read_glb(_write(tmp_path, b"not a glb file at all"))


def test_rejects_bad_version(tmp_path):
    glb = bytearray(build_glb([simple_quad()]))
    glb[4:8] = (1).to_bytes(4, "little")  # version -> 1
    with pytest.raises(GLBReadError):
        read_glb(_write(tmp_path, bytes(glb)))


def test_shared_vertex_pool_extracts_per_object(tmp_path):
    # Regression for the real BeamNG layout: many objects index disjoint slices
    # of ONE shared POSITION accessor. Each object must own only its vertices,
    # with indices remapped local — not the whole pool duplicated per object.
    pool = np.array(
        [[i, 0, 0] for i in range(6)], dtype=np.float32
    )  # 6 shared vertices
    # objA uses verts 0,1,2 ; objB uses verts 3,4,5 (disjoint partition)
    objs = [
        ("objA", np.array([0, 1, 2], dtype=np.uint32)),
        ("objB", np.array([3, 4, 5], dtype=np.uint32)),
    ]
    glb = build_shared_pool_glb(pool, objs)
    doc = read_glb(_write(tmp_path, glb))

    by = doc.by_name()
    assert by["objA"].vertex_count == 3  # NOT 6 (the whole pool)
    assert by["objB"].vertex_count == 3
    # indices are remapped into the object-local range
    assert int(by["objA"].indices.max()) < 3
    assert int(by["objB"].indices.max()) < 3
    # objA's local vertices must be the pool rows 0,1,2
    np.testing.assert_array_equal(by["objA"].positions, pool[[0, 1, 2]])
    np.testing.assert_array_equal(by["objB"].positions, pool[[3, 4, 5]])


# --- Optional: exercise a real sample if present on this machine ---------

_REAL_SAMPLE = Path(
    r"C:\Users\ubaid_i2c\Downloads\toyota-corolla-2020\source\MDL13625_reversed.glb"
)


@pytest.mark.skipif(not _REAL_SAMPLE.exists(), reason="real GLB sample not available")
def test_reads_real_sample():
    doc = read_glb(_REAL_SAMPLE)
    assert len(doc.objects) >= 1
    for obj in doc.objects:
        assert obj.vertex_count > 0
        assert obj.positions.shape[1] == 3
        if obj.indices is not None:
            assert obj.indices.shape[1] == 3
            # indices must reference valid vertices
            assert int(obj.indices.max()) < obj.vertex_count
