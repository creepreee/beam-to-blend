from __future__ import annotations

import numpy as np

from importer.cache_builder import CacheBuilder
from importer.scanner import SequenceScanner
from runtime.cache_reader import CacheReader
from tests.glb_fixtures import build_glb, simple_quad

# BVC now stores Blender Z-up positions (glTF→Blender conversion at write time).
# Apply the same conversion to expected positions for test assertions.
_gltf_to_blender = CacheBuilder._gltf_to_blender


def _make_sequence(tmp_path, frame_builder, n_frames):
    seq = tmp_path / "seq"
    seq.mkdir()
    for i in range(n_frames):
        (seq / f"frame_{i:04d}.glb").write_bytes(build_glb(frame_builder(i)))
    return seq


def _simple_dynamic(name: str, frame: int):
    """Quad on even frames, triangle on odd — changes topology to test fallback."""
    if frame % 2 == 0:
        positions = np.array(
            [[0, 0, float(frame)], [1, 0, float(frame)],
             [1, 1, float(frame)], [0, 1, float(frame)]],
            dtype=np.float32,
        )
        indices = np.array([[0, 1, 2], [0, 2, 3]], dtype=np.uint32)
    else:
        positions = np.array(
            [[0, 0, float(frame)], [1, 0, float(frame)], [0.5, 1, float(frame)]],
            dtype=np.float32,
        )
        indices = np.array([[0, 1, 2]], dtype=np.uint32)
    return name, positions, indices


def test_cache_roundtrip_positions_exact(tmp_path):
    def builder(i):
        return [simple_quad("body", z=float(i)), simple_quad("hood", z=float(-i * 2))]

    n = 4
    seq = _make_sequence(tmp_path, builder, n)
    out = tmp_path / "cache.bvc"

    manifest = CacheBuilder(seq, out).build()
    assert manifest.stable_objects == ["body", "hood"]

    with CacheReader(out) as reader:
        assert reader.frame_count == n
        assert set(reader.object_names()) == {"body", "hood"}

        np.testing.assert_array_equal(
            reader.base_indices("body"),
            np.array([[0, 1, 2], [0, 2, 3]], dtype=np.int32),
        )

        for i in range(n):
            _, body_pos, _ = simple_quad("body", z=float(i))
            _, hood_pos, _ = simple_quad("hood", z=float(-i * 2))
            np.testing.assert_array_equal(
                reader.frame_positions("body", i), _gltf_to_blender(body_pos))
            np.testing.assert_array_equal(
                reader.frame_positions("hood", i), _gltf_to_blender(hood_pos))


def test_cache_reader_rejects_out_of_range_frame(tmp_path):
    seq = _make_sequence(tmp_path, lambda i: [simple_quad("body", z=float(i))], 2)
    out = tmp_path / "cache.bvc"
    CacheBuilder(seq, out).build()

    import pytest

    with CacheReader(out) as reader:
        with pytest.raises(IndexError):
            reader.frame_positions("body", 5)


def test_cache_roundtrip_dynamic_object(tmp_path):
    """A topology-changing object is classified dynamic and stored in the
    dynamic section; the reader returns per-frame correct geometry."""
    def builder(i):
        return [simple_quad("stable_body", z=float(i)), _simple_dynamic("tierod", i)]

    n = 4
    seq = _make_sequence(tmp_path, builder, n)
    out = tmp_path / "cache.bvc"

    manifest = CacheBuilder(seq, out).build()
    assert "stable_body" in manifest.stable_objects
    assert "tierod" in manifest.dynamic_objects

    with CacheReader(out) as reader:
        assert reader.frame_count == n
        assert set(reader.object_names()) == {"stable_body", "tierod"}
        assert reader.dynamic_objects()[0].name == "tierod"

        for i in range(n):
            _, pos, _ = simple_quad("stable_body", z=float(i))
            np.testing.assert_array_equal(
                reader.frame_positions("stable_body", i), _gltf_to_blender(pos))

        for i in range(n):
            _, exp_pos, exp_idx = _simple_dynamic("tierod", i)
            got_pos, got_idx = reader.frame_dynamic_geometry("tierod", i)
            np.testing.assert_array_equal(got_pos, _gltf_to_blender(exp_pos))
            np.testing.assert_array_equal(got_idx, np.asarray(exp_idx, dtype=np.int32))


def test_cache_dynamic_object_appears_disappears(tmp_path):
    """Dynamic object that is absent in some frames."""
    def builder(i):
        objs = [simple_quad("stay", z=float(i))]
        # Only present on frames 1 and 3
        if i in (1, 3):
            objs.append(_simple_dynamic("comet", i))
        return objs

    n = 4
    seq = _make_sequence(tmp_path, builder, n)
    out = tmp_path / "cache.bvc"

    manifest = CacheBuilder(seq, out).build()
    assert "comet" in manifest.dynamic_objects

    with CacheReader(out) as reader:
        # Frame 0 — object absent.
        pos, idx = reader.frame_dynamic_geometry("comet", 0)
        assert len(pos) == 0
        assert len(idx) == 0

        # Frame 1 — object present.
        pos, idx = reader.frame_dynamic_geometry("comet", 1)
        assert len(pos) == 3
        assert len(idx) == 1

        # Frame 2 — absent again.
        pos, idx = reader.frame_dynamic_geometry("comet", 2)
        assert len(pos) == 0
        assert len(idx) == 0

        # Frame 3 — present again.
        pos, idx = reader.frame_dynamic_geometry("comet", 3)
        assert len(pos) == 3
        assert len(idx) == 1
