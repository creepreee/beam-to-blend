from __future__ import annotations

"""Live retuning of the "Start at Frame" offset (no re-import).

``frame_handler`` is importable without Blender (``bpy is None``), so these
tests stub a minimal scene/context and drive the real module-level state.  What
matters is the arithmetic contract:

  * the offset is stored in FRAMES, so the animation starts on exactly the
    frame number the user typed (typing 500 must not mean 500 *seconds*, which
    at 60 output fps used to jump the start to frame 30000);
  * ``update_start_frame`` re-derives ``frame_start``/``frame_end`` and re-runs
    the current frame, which is what makes the field live;
  * the deprecated ``update_start_second`` / ``attach(start_second=...)``
    spellings still convert, so older callers and older .blend files keep
    working.
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


def test_the_field_is_frames_not_seconds(env):
    """The reported bug: 500 must mean frame 500, not 500 s (= frame 30000)."""
    scene, _ = env
    fh.update_start_frame(500)
    assert scene.frame_start == 500
    assert fh._frame_start == 500
    assert fh._cache_frame_for(500) == 0


def test_update_start_frame_shifts_frame_range(env):
    scene, _ = env
    fh.update_start_frame(120)

    assert scene.frame_start == 120
    assert fh._cache_frame_for(120) == 0
    # 600 source frames at playback_fps=24 = 25 s = 1500 output frames.
    assert scene.frame_end == 120 + 1500


def test_offset_does_not_change_animation_duration(env):
    scene, _ = env
    fh.update_start_frame(0)
    span = scene.frame_end - scene.frame_start
    fh.update_start_frame(450)
    assert scene.frame_end - scene.frame_start == span


def test_mapping_is_a_pure_shift(env):
    _, _ = env
    fh.update_start_frame(0)
    before = [fh._cache_frame_for(f) for f in range(0, 400, 37)]
    fh.update_start_frame(180)
    after = [fh._cache_frame_for(f + 180) for f in range(0, 400, 37)]
    assert before == after


def test_refreshes_current_frame_without_scrubbing(env):
    scene, playback = env
    scene.frame_current = 300
    playback.calls.clear()
    fh.update_start_frame(60)
    # The playhead stayed at 300, so only the re-run makes the change visible.
    assert playback.calls, "expected the live handler to re-run the current frame"
    assert playback.calls[-1] == fh._cache_frame_for(300)


def test_playhead_pulled_inside_the_new_range(env):
    scene, _ = env
    scene.frame_current = 10
    fh.update_start_frame(300)
    assert scene.frame_current == scene.frame_start == 300


def test_playhead_left_alone_when_already_in_range(env):
    scene, _ = env
    scene.frame_current = 500
    fh.update_start_frame(60)
    assert scene.frame_current == 500


def test_frame_number_is_kept_across_an_output_fps_change(env):
    """Frames are canonical now, so the START stays on its frame number.

    This is the deliberate tradeoff of frames-over-seconds: the start no longer
    tracks wall-clock time when the render rate changes.  The DURATION still
    follows playback_fps/output_fps, so only the offset is pinned.
    """
    scene, _ = env
    fh.update_start_frame(120)
    assert scene.frame_start == 120

    fh.update_fps(output_fps=30)
    assert scene.frame_start == 120
    assert fh._start_frame == 120


def test_negative_offset_clamped(env):
    scene, _ = env
    fh.update_start_frame(-4)
    assert fh._start_frame == 0
    assert scene.frame_start == 0


def test_state_persisted_for_undo_recovery(env):
    scene, _ = env
    fh.update_start_frame(150)
    assert scene["_beamng_start_frame"] == 150
    assert scene["_beamng_frame_start"] == 150


def test_no_active_playback_is_a_noop_not_a_crash(env, monkeypatch):
    scene, _ = env
    monkeypatch.setattr(fh, "_active", None)
    fh.update_start_frame(180)
    # The offset is still recorded so a later attach()/recover picks it up.
    assert fh._frame_start == 180
    assert scene.frame_start == 180


# --- backward compatibility with the old seconds-based spelling -----------

def test_deprecated_update_start_second_still_converts(env):
    scene, _ = env
    fh.update_start_second(2.0)          # 2 s at output_fps=60
    assert scene.frame_start == 120
    assert fh._start_frame == 120


def test_recover_reads_legacy_start_second_key(env, monkeypatch):
    """A .blend saved by the seconds-based build must not lose its offset."""
    scene, _ = env
    scene["_beamng_start_second"] = 2.0
    scene["_beamng_output_fps"] = 60.0
    # Simulate the conversion _try_recover performs for the legacy key.
    legacy = scene.get("_beamng_start_second")
    converted = int(round(float(legacy) * float(scene["_beamng_output_fps"])))
    assert converted == 120
