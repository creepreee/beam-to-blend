from __future__ import annotations

"""Live retuning of the two fps knobs (no re-import).

The bug this pins: ``update_fps`` recomputed the timeline but never re-ran the
frame the playhead sits on.  Both fps values feed :func:`_cache_frame_for`, so
changing either re-points a *parked* playhead at a different cache frame — yet
the playhead does not move, so no frame-change handler fires and the viewport
kept showing the old geometry.  The field looked dead and the only way to see a
new speed was to re-import the cache (and re-link all the materials).

Same contract as ``update_start_frame`` / ``update_tyre``: mutate state,
re-derive the timeline, then :func:`_refresh_current_frame`.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runtime import frame_handler as fh


class _Scene(dict):
    """Enough of a bpy Scene: attributes for the timeline, dict for custom props."""

    def __init__(self):
        super().__init__()
        self.frame_start = 0
        self.frame_end = 0
        self.frame_current = 0
        self.sync_mode = "NONE"
        self.render = type("R", (), {"fps": 24, "fps_base": 1.0,
                                     "use_frame_drop": False})()


class _Reader:
    def __init__(self, frame_count: int):
        self.frame_count = frame_count
        self.path = "fake.bvc"


class _Playback:
    """Records every set_frame() so we can assert the live refresh happened."""

    def __init__(self, frame_count: int = 601):
        self.reader = _Reader(frame_count)
        self.calls: list[int] = []

    def set_frame(self, frame: int) -> None:
        self.calls.append(frame)


@pytest.fixture
def env(monkeypatch):
    """Install a stub bpy + active playback, and restore module state after."""
    scene = _Scene()
    bpy_stub = type("Bpy", (), {})()
    bpy_stub.context = type("Ctx", (), {"scene": scene, "screen": None})()
    monkeypatch.setattr(fh, "bpy", bpy_stub)

    playback = _Playback()
    monkeypatch.setattr(fh, "_active", playback)
    monkeypatch.setattr(fh, "_playback_fps", 24.0)
    monkeypatch.setattr(fh, "_output_fps", 60.0)
    monkeypatch.setattr(fh, "_start_frame", 0)
    monkeypatch.setattr(fh, "_frame_start", 0)
    return scene, playback


# --- the actual bug -------------------------------------------------------

def test_playback_fps_pushes_the_new_frame_to_the_mesh(env):
    """THE regression: a parked playhead must be re-run with the new mapping."""
    scene, playback = env
    scene.frame_current = 300
    playback.calls.clear()

    fh.update_fps(playback_fps=15)

    assert playback.calls, (
        "expected the live handler to re-run the current frame — without this "
        "the new speed is invisible until the playhead moves"
    )
    # 300 * 15/60 = 75, where the old 24 fps mapping gave 120.
    assert playback.calls[-1] == 75


def test_output_fps_pushes_the_new_frame_to_the_mesh(env):
    """The other half of the same knob: output_fps also remaps the playhead."""
    scene, playback = env
    scene.frame_current = 300
    playback.calls.clear()

    fh.update_fps(output_fps=30)

    assert playback.calls
    # 300 * 24/30 = 240 (was 300 * 24/60 = 120).
    assert playback.calls[-1] == 240


def test_refresh_uses_the_new_fps_not_the_old(env):
    """The refresh must happen AFTER the state mutation, not before."""
    scene, playback = env
    scene.frame_current = 240
    stale = fh._cache_frame_for(240)      # under playback_fps=24

    playback.calls.clear()
    fh.update_fps(playback_fps=12)

    assert playback.calls[-1] == fh._cache_frame_for(240)
    assert playback.calls[-1] != stale


# --- surrounding contract that must not regress ---------------------------

def test_playback_fps_changes_duration(env):
    """Speed governs duration: half the source fps = twice as long."""
    scene, _ = env
    fh.update_fps(playback_fps=24)
    span_24 = scene.frame_end - scene.frame_start
    fh.update_fps(playback_fps=12)
    assert scene.frame_end - scene.frame_start == span_24 * 2


def test_output_fps_does_not_change_wall_clock_duration(env):
    """Smoothness, not speed: more output frames covering the same seconds."""
    scene, _ = env
    fh.update_fps(playback_fps=24, output_fps=60)
    seconds_60 = (scene.frame_end - scene.frame_start) / 60.0
    fh.update_fps(output_fps=30)
    seconds_30 = (scene.frame_end - scene.frame_start) / 30.0
    assert seconds_60 == pytest.approx(seconds_30, abs=1e-9)


def test_output_fps_drives_scene_render_fps(env):
    scene, _ = env
    fh.update_fps(output_fps=48)
    assert scene.render.fps == 48
    assert scene.render.fps_base == 1.0


def test_playback_fps_does_not_touch_render_fps(env):
    """Speed is baked into the timeline length, never into render.fps."""
    scene, _ = env
    fh.update_fps(output_fps=60)
    fh.update_fps(playback_fps=15)
    assert scene.render.fps == 60


def test_start_offset_is_preserved(env):
    scene, _ = env
    fh.update_start_frame(150)
    fh.update_fps(playback_fps=15)
    assert scene.frame_start == 150
    assert fh._start_frame == 150


def test_playhead_pulled_inside_a_shortened_range(env):
    """Speeding up shortens the timeline; a playhead past the end would map
    beyond the cache and freeze on the last frame."""
    scene, _ = env
    fh.update_fps(playback_fps=12)       # long timeline
    scene.frame_current = scene.frame_end
    fh.update_fps(playback_fps=48)       # 4x shorter
    assert scene.frame_current == scene.frame_end


def test_state_persisted_for_undo_recovery(env):
    scene, _ = env
    fh.update_fps(playback_fps=15, output_fps=48)
    assert scene["_beamng_playback_fps"] == 15.0
    assert scene["_beamng_output_fps"] == 48.0


def test_realtime_sync_reasserted(env):
    """FRAME_DROP is what makes the viewport speed match the render speed."""
    scene, _ = env
    scene.sync_mode = "NONE"
    fh.update_fps(playback_fps=15)
    assert scene.sync_mode == "FRAME_DROP"


def test_no_active_playback_is_a_noop_not_a_crash(env, monkeypatch):
    scene, _ = env
    monkeypatch.setattr(fh, "_active", None)
    fh.update_fps(playback_fps=15, output_fps=30)
    # State still lands, so a later attach()/recover picks it up.
    assert fh._playback_fps == 15.0
    assert fh._output_fps == 30.0
    assert scene.render.fps == 30


def test_zero_and_negative_fps_clamped(env):
    """A zero would divide by zero in _cache_frame_for."""
    _, _ = env
    fh.update_fps(playback_fps=0, output_fps=-5)
    assert fh._playback_fps > 0
    assert fh._output_fps > 0
    fh._cache_frame_for(100)   # must not raise


def test_partial_update_leaves_the_other_alone(env):
    _, _ = env
    fh.update_fps(playback_fps=15, output_fps=60)
    fh.update_fps(playback_fps=30)
    assert fh._output_fps == 60.0
    fh.update_fps(output_fps=24)
    assert fh._playback_fps == 30.0
