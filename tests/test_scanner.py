from __future__ import annotations

import numpy as np

from importer.scanner import SequenceScanner
from tests.glb_fixtures import build_glb, simple_quad


def _make_sequence(tmp_path, n_frames: int, frame_builder):
    """Write ``n_frames`` GLB files; ``frame_builder(i)`` returns the object list."""
    seq = tmp_path / "seq"
    seq.mkdir()
    for i in range(n_frames):
        # zero-padded names so lexical sort == frame order
        (seq / f"frame_{i:04d}.glb").write_bytes(build_glb(frame_builder(i)))
    return seq


def test_all_stable_when_only_positions_move(tmp_path):
    # Two objects, topology fixed, vertices move each frame (z = frame index).
    def builder(i):
        return [simple_quad("body", z=float(i)), simple_quad("hood", z=float(-i))]

    seq = _make_sequence(tmp_path, 5, builder)
    manifest = SequenceScanner(seq).scan()

    assert manifest.frame_count == 5
    assert manifest.stable_objects == ["body", "hood"]
    assert manifest.dynamic_objects == []
    assert manifest.objects["body"].cacheable is True
    assert manifest.objects["body"].vertex_count == 4
    assert manifest.objects["body"].face_count == 2


def test_topology_change_marks_object_dynamic(tmp_path):
    # "tierod" gains vertices at frame 3; "body" stays stable. Mirrors the
    # real BeamNG finding: 1 object changes topology, the rest are stable.
    def builder(i):
        body = simple_quad("body", z=float(i))
        if i < 3:
            tierod = simple_quad("tierod", z=float(i))
        else:
            positions = np.random.rand(9, 3).astype(np.float32)  # 9 verts now
            indices = np.array([[0, 1, 2], [3, 4, 5], [6, 7, 8]], dtype=np.uint32)
            tierod = ("tierod", positions, indices)
        return [body, tierod]

    seq = _make_sequence(tmp_path, 6, builder)
    manifest = SequenceScanner(seq).scan()

    assert manifest.stable_objects == ["body"]
    assert manifest.dynamic_objects == ["tierod"]
    tierod = manifest.objects["tierod"]
    assert tierod.cacheable is False
    assert "topology changed" in tierod.fallback_reason


def test_disappearing_object_is_dynamic(tmp_path):
    def builder(i):
        objs = [simple_quad("body", z=float(i))]
        if i == 0:
            objs.append(simple_quad("debris"))  # only in frame 0
        return objs

    seq = _make_sequence(tmp_path, 3, builder)
    manifest = SequenceScanner(seq).scan()

    assert "body" in manifest.stable_objects
    assert manifest.objects["debris"].cacheable is False
    assert "missing" in manifest.objects["debris"].fallback_reason


def test_manifest_json_roundtrip(tmp_path):
    seq = _make_sequence(tmp_path, 2, lambda i: [simple_quad("body", z=float(i))])
    manifest = SequenceScanner(seq).scan()

    import json

    data = json.loads(manifest.to_json())
    assert data["frame_count"] == 2
    assert data["stable_objects"] == ["body"]
    assert data["objects"]["body"]["topology_hash"]
