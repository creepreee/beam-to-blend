from __future__ import annotations
"""Unit tests for impact detection and the velocity-units contract.

The velocity contract is the one that burned us: ``debris_spawn`` launched
debris ~2x too fast because ``ImpactEvent.velocity`` was claimed to be metres
per second while detection emitted raw per-sample deltas (a stride-2 scan spans
2 cache frames) and the spawner multiplied by 24.0 on top.

These tests pin the single source of truth:
``sample_delta_to_ms`` converts a per-sample delta to scene m/s as
``delta * playback_fps / stride``, and ``detect_impacts`` emits exactly that.

Pure numpy + a fake reader — no Blender needed.
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
import pytest

from runtime.impact_detect import (
    DetectSettings,
    GlassSettings,
    classify_glass_damage,
    classify_material,
    detect_impacts,
    is_glass,
    local_to_world,
    resolve_glass_damage,
    sample_delta_to_ms,
)
from runtime.impact_detect import (
    GLASS_INTACT,
    GLASS_CRACKED,
    GLASS_SHATTERED,
)


class _QuadReader:
    """A fake CacheReader: a 4-vertex quad part that glides +0.5 m/frame in X
    while one corner also pushes +0.05 m/frame in Z (real deformation)."""

    def __init__(self, n_frames: int = 6):
        self.n_frames = n_frames

    @property
    def frame_count(self) -> int:
        return self.n_frames

    def object_names(self):
        return ["testbumper"]

    def get_object(self, name):
        assert name == "testbumper"
        return SimpleNamespace(vertex_count=4)

    def frame_transform(self, _f):
        return None

    def frame_positions(self, _name, f: int):
        base = np.array([[0.0, 0.0, 0.0],
                         [1.0, 0.0, 0.0],
                         [1.0, 1.0, 0.0],
                         [0.0, 1.0, 0.0]], dtype=np.float64)
        out = base.copy()
        out[:, 0] += 0.5 * f          # rigid glide, +0.5 m per cache frame
        out[0, 2] += 0.05 * f         # shape change, survives Kabsch
        return out


def test_sample_delta_to_ms_contract():
    """A sample delta spanning ``stride`` cache frames -> m/s via fps/stride."""
    delta = np.array([1.0, 0.0, 0.0])
    # stride=1: one sample = one cache frame = 1/24 s.
    assert sample_delta_to_ms(delta, 24.0, 1)[0] == pytest.approx(24.0)
    # stride=2: one sample = two cache frames, so the same delta is faster.
    assert sample_delta_to_ms(delta, 24.0, 2)[0] == pytest.approx(12.0)
    # fps scaling: faster playback maps the same cached motion to more m/s.
    assert sample_delta_to_ms(delta, 60.0, 2)[0] == pytest.approx(30.0)
    # degenerate inputs never divide by zero / blow up.
    out = sample_delta_to_ms(delta, 0.0, 0)
    assert np.isfinite(out).all() and abs(out[0]) < 1e-5


def test_detect_impacts_emits_velocity_in_ms():
    """The glide of 0.5 m per cache frame must come out as 0.5 * fps / stride."""
    for stride, fps in ((1, 24.0), (2, 24.0), (1, 60.0), (2, 60.0)):
        events = detect_impacts(
            _QuadReader(n_frames=8),
            settings=DetectSettings(stride=stride),
            playback_fps=fps,
        )
        assert events, f"no events for stride={stride} fps={fps}"
        vel = np.array(events[0].velocity)
        # A sample delta spans `stride` cache frames (0.5 * stride m) and is
        # scaled by fps/stride, so the stride cancels: the true motion is
        # 0.5 m per cache frame * fps cache-frames/s = 0.5 * fps m/s.
        expected = 0.5 * fps
        assert vel[0] == pytest.approx(expected, abs=1e-6), (
            f"stride={stride} fps={fps}: velocity {vel} != {expected} m/s "
            f"(the per-cache-frame delta must be scaled by fps/stride)")
        # The z drift from the deforming corner is small; X dominates.
        assert abs(vel[2]) < expected * 0.1


def test_detection_actually_fires_on_shape_change():
    """Deformation is the only trigger — a pure glide produces no event."""
    glide = detect_impacts(_QuadReader(), playback_fps=24.0)
    assert glide

    class _RigidGlide(_QuadReader):
        def frame_positions(self, _name, f):
            base = np.array([[0.0, 0.0, 0.0],
                             [1.0, 0.0, 0.0],
                             [1.0, 1.0, 0.0],
                             [0.0, 1.0, 0.0]], dtype=np.float64)
            base[:, 0] += 0.5 * f   # rigid motion only — no shape change
            return base

    assert detect_impacts(_RigidGlide(), playback_fps=24.0) == []


def test_material_classification():
    assert classify_material("windshield") == "glass"
    assert classify_material("backlight") == "glass"
    assert classify_material("front_bumper") == "plastic"
    assert classify_material("body") == "paint"
    assert classify_material("exhaust") == "chrome"
    assert classify_material("tire_frontleft") == "rubber"
    assert classify_material("subframe") == "steel"
    assert is_glass("doorglass")
    assert not is_glass("body")


def _glass_event(part, deform, ground_depth, cache_frame):
    return SimpleNamespace(
        part=part, material="glass", severity=0.9,
        deform=deform, ground_depth=ground_depth,
        cache_frame=cache_frame, position=(1.0, 2.0, 3.0),
        velocity=(0.0, 0.0, -1.0), direction=(0.0, 0.0, -1.0))


def test_classify_glass_damage_tiers():
    assert classify_glass_damage(
        _glass_event("windshield", 0.001, 0.0, 0)) == GLASS_INTACT
    assert classify_glass_damage(
        _glass_event("windshield", 0.010, 0.0, 0)) == GLASS_CRACKED
    # Deformation above the shatter threshold leaves the car.
    assert classify_glass_damage(
        _glass_event("windshield", 0.025, 0.0, 0)) == GLASS_SHATTERED
    # Face-on ground strike shatters regardless of modest deformation.
    assert classify_glass_damage(
        _glass_event("windshield", 0.001, 0.05, 0)) == GLASS_SHATTERED
    # Custom thresholds override the calibrated defaults.
    custom = GlassSettings(crack_deform=0.02, shatter_deform=0.05)
    assert classify_glass_damage(
        _glass_event("windshield", 0.03, 0.0, 0), custom) == GLASS_CRACKED


def test_resolve_glass_damage_keeps_worst_tier_first_frame():
    """Repeated crush peaks on one pane collapse to ONE break at its first,
    worst frame — never three sets of shards from an already-empty frame."""
    events = [
        _glass_event("windshield", 0.010, 0.0, 100),   # cracks first
        _glass_event("windshield", 0.030, 0.0, 200),   # then shatters
        _glass_event("windshield", 0.028, 0.0, 300),   # wreck scrapes again
        _glass_event("backlight", 0.002, 0.0, 150),    # intact -> dropped
    ]
    out = resolve_glass_damage(events)
    assert set(out) == {"windshield"}
    tier, frame, _event = out["windshield"]
    assert tier == GLASS_SHATTERED
    assert frame == 200  # the FIRST frame to reach shattered, not the last peak


def test_local_to_world_applies_rotation_and_shift():
    """Map cache-local pane verts into world space at the shatter frame."""
    identity = np.array([0.0, 0.0, 0.0,
                         1.0, 0.0, 0.0,
                         0.0, 1.0, 0.0,
                         0.0, 0.0, 1.0])
    local = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])
    world = local_to_world(local, identity, ground_shift=0.5)
    # ground_shift lifts Z before the transform, so Z becomes +0.5.
    assert world[:, 2] == pytest.approx(0.5)
    # A 90-degree rotation about Z with a translation moves X onto Y etc.
    rot = np.array([1.0, 2.0, 3.0,
                    0.0, -1.0, 0.0,
                    1.0, 0.0, 0.0,
                    0.0, 0.0, 1.0])
    world = local_to_world(local, rot)
    # local (0,0,0) -> position only.
    assert world[0] == pytest.approx([1.0, 2.0, 3.0])
    # local +X (1,0,0) rotated by the matrix's first column (0,1,0) -> (1, 3, 3).
    assert world[1] == pytest.approx([1.0, 3.0, 3.0])
    # No transform block -> local is returned untouched.
    assert np.allclose(local_to_world(local, None, ground_shift=0.5),
                       local + np.array([0.0, 0.0, 0.5]))
