from __future__ import annotations

"""Render-path wiring for the cache playback (unit level, no Blender).

THE BUG THIS PINS: Blender never fires ``frame_change_*`` handlers during a
render, so a playback driven only from ``_on_frame_change`` froze on the
viewport's last pose — scrubbing looked perfect while Ctrl+F12 / Viewport
Render Animation output a statue.  ``_on_render_pre`` now applies the mapped
cache frame once per rendered frame, and ``attach`` / ``detach_handler`` /
``_ensure_handler_registered`` manage it alongside the frame-change handler.

Contract tested here with a stub bpy:

* ``_on_render_pre`` maps the playhead (including ``frame_current_float``
  subframes) through the SAME ``_cache_frame_for`` arithmetic as scrubbing;
* the every-Nth-frame experiment knob (``_skip_n``) must NOT decimate the
  render path — a render is always exact;
* ``_ensure_handler_registered`` registers both handlers exactly once;
* ``detach_handler`` removes both;
* ``_on_render_pre`` recovers after simulated undo (``_active = None``) the
  same way the frame-change path does.
"""

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runtime import frame_handler as fh


class _Handlers:
    """Stand-in for bpy.app.handlers: named lists with append/remove."""

    def __init__(self):
        self.frame_change_pre = []
        self.frame_change_post = []
        self.render_pre = []
        self.render_post = []


class _Scene:
    def __init__(self, frame: float = 0.0):
        self.frame_current = int(frame)
        self.frame_current_float = float(frame)

    def get(self, key, default=None):
        """Custom-prop access used by _try_recover; stub has none stored."""
        return default


@pytest.fixture
def env(monkeypatch):
    """Stub bpy + active playback recording set_frame calls."""
    scene = _Scene()
    handlers = _Handlers()
    bpy_stub = type("Bpy", (), {})()
    bpy_stub.app = type("App", (), {"handlers": handlers})()
    bpy_stub.context = type("Ctx", (), {"scene": scene})()

    class _Reader:
        frame_count = 601
        path = "fake.bvc"

    class _Playback:
        def __init__(self):
            self.reader = _Reader()
            self.calls = []

        def set_frame(self, frame):
            self.calls.append(frame)

    playback = _Playback()
    monkeypatch.setattr(fh, "bpy", bpy_stub)
    monkeypatch.setattr(fh, "_active", playback)
    monkeypatch.setattr(fh, "_playback_fps", 24.0)
    monkeypatch.setattr(fh, "_output_fps", 60.0)
    monkeypatch.setattr(fh, "_start_frame", 0)
    monkeypatch.setattr(fh, "_frame_start", 0)
    return handlers, scene, playback


def test_render_pre_applies_the_mapped_cache_frame(env):
    handlers, scene, playback = env
    scene.frame_current = 300
    scene.frame_current_float = 300.0
    playback.calls.clear()

    fh._on_render_pre(scene, None)

    # Same mapping the viewport uses: 300 * 24/60 = 120.
    assert playback.calls == [120.0]


def test_render_pre_uses_subframes(env):
    handlers, scene, playback = env
    scene.frame_current = 301          # int part of a motion-blur subframe
    scene.frame_current_float = 300.5
    playback.calls.clear()

    fh._on_render_pre(scene, None)

    # 300.5 * 24/60 = 120.2 — the fractional playhead reaches the cache.
    assert playback.calls == [pytest.approx(120.2)]


def test_render_path_is_never_decimated_by_skip_n(env, monkeypatch):
    """skip_n is a viewport experiment; a render must stay exact."""
    handlers, scene, playback = env
    scene.frame_current = 240
    scene.frame_current_float = 240.0
    fh.set_skip_n(10)                  # would skip 9 of 10 viewport updates
    try:
        playback.calls.clear()
        fh._on_render_pre(scene, None)
        assert playback.calls == [96.0]  # 240 * 24/60
    finally:
        fh.set_skip_n(0)


def test_both_handlers_registered_once_and_detached_together(env):
    handlers, scene, playback = env
    fh._ensure_handler_registered()
    fh._ensure_handler_registered()    # idempotent

    assert [getattr(h, "__name__", "") for h in handlers.frame_change_pre] \
        == ["_on_frame_change"]
    assert [getattr(h, "__name__", "") for h in handlers.render_pre] \
        == ["_on_render_pre"]

    fh.detach_handler()
    assert handlers.frame_change_pre == []
    assert handlers.render_pre == []


def test_render_pre_recovers_after_undo(env, monkeypatch):
    """A background render of a freshly opened .blend must move."""
    handlers, scene, playback = env
    scene.frame_current = 120
    scene.frame_current_float = 120.0

    monkeypatch.setattr(fh, "_active", None)   # simulated undo wipe
    # No stored scene props -> recovery returns False, handler must no-op.
    assert fh._on_render_pre(scene, None) is None


def test_render_pre_applies_from_job_threads(env, monkeypatch):
    """THE regression: GUI renders run handlers on the WM JOB thread.

    Measured in real Blender: an animation render from the UI fires
    render-pre and frame-change on a background thread.  The render path
    must apply updates there or every GUI-rendered frame shows the
    viewport's last pose (the frozen-car bug).
    """
    handlers, scene, playback = env
    scene.frame_current = 240
    scene.frame_current_float = 240.0
    playback.calls.clear()

    real_main = threading.main_thread
    real_current = threading.current_thread
    monkeypatch.setattr(threading, "current_thread",
                        lambda: type("T", (), {"name": "render-job"})())
    monkeypatch.setattr(threading, "main_thread", lambda: type("T", (), {})())
    try:
        fh._on_render_pre(scene, None)
    finally:
        monkeypatch.setattr(threading, "current_thread", real_current)
        monkeypatch.setattr(threading, "main_thread", real_main)

    assert playback.calls == [96.0]       # 240 * 24/60 — applied anyway


def test_frame_change_still_refuses_non_main_threads(env, monkeypatch):
    """The Mantaflow-bake crash guard stays on the frame-change path."""
    handlers, scene, playback = env
    scene.frame_current = 120
    scene.frame_current_float = 120.0
    playback.calls.clear()

    real_main = threading.main_thread
    real_current = threading.current_thread
    monkeypatch.setattr(threading, "current_thread",
                        lambda: type("T", (), {"name": "bake-thread"})())
    monkeypatch.setattr(threading, "main_thread", lambda: type("T", (), {})())
    try:
        fh._on_frame_change(scene)
    finally:
        monkeypatch.setattr(threading, "current_thread", real_current)
        monkeypatch.setattr(threading, "main_thread", real_main)

    assert playback.calls == []
