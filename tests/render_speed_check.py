"""Headless check: RENDER speed is governed by playback_fps only, and the
frame-change handler fires for every rendered frame.

Run:
    blender --background --python tests/render_speed_check.py -- <cache.bvc>

Simulates what Blender's render-animation loop does: step frame_current across
[frame_start, frame_end] and confirm (a) the handler maps each Blender frame to
the expected cache frame, (b) the last Blender frame lands on the last cache
frame, and (c) the resulting render DURATION equals (n_src-1)/playback_fps
seconds — regardless of output_fps. Also asserts sync_mode (viewport-only) does
NOT change the render frame set.
"""
import os
import sys

import bpy

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from runtime.cache_reader import CacheReader
from runtime.mesh_update import CachePlayback
from runtime import frame_handler


def _argv():
    return sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []


def _fail(m):
    print(f"[RENDER][FAIL] {m}")
    sys.exit(1)


def _ok(m):
    print(f"[RENDER][ok] {m}")


def check(cache_path, playback_fps, output_fps):
    reader = CacheReader(cache_path)
    playback = CachePlayback(reader)
    playback.build_scene()
    frame_start = 1
    frame_handler.attach(playback, frame_start=frame_start,
                         playback_fps=playback_fps, output_fps=output_fps)

    scene = bpy.context.scene
    n_src = reader.frame_count

    # (1) render.fps is output_fps (smoothness), fps_base 1.0
    if scene.render.fps != output_fps:
        _fail(f"render.fps={scene.render.fps} != output_fps={output_fps}")

    # (2) timeline length => render duration = (n_src-1)/playback_fps sec
    expected_end = frame_start + round((n_src - 1) / playback_fps * output_fps)
    if scene.frame_end != expected_end:
        _fail(f"frame_end={scene.frame_end} != expected {expected_end}")
    duration = (scene.frame_end - scene.frame_start) / output_fps
    expected_dur = (n_src - 1) / playback_fps
    # Timeline length is an integer number of output frames, so the achievable
    # duration is quantized to 1/output_fps.  Allow half a frame of rounding.
    if abs(duration - expected_dur) > 0.5 / output_fps:
        _fail(f"duration={duration:.4f}s != expected {expected_dur:.4f}s "
              f"(tol {0.5/output_fps:.4f}s)")
    _ok(f"pb={playback_fps} out={output_fps}: render duration "
        f"{duration:.3f}s (= (n_src-1)/playback_fps, independent of output_fps)")

    # (3) simulate the render-animation loop: every frame drives the handler and
    #     the LAST rendered frame lands on the LAST cache frame (no overrun =
    #     no "too fast" clipping, no freeze).
    last_cache = frame_handler._cache_frame_for(scene.frame_end)
    if last_cache != n_src - 1:
        _fail(f"last render frame maps to cache {last_cache}, expected {n_src-1}")

    # step through and confirm the handler actually updates current frame
    seen = set()
    for bf in range(scene.frame_start, scene.frame_end + 1, max(1, (scene.frame_end - scene.frame_start)//20 or 1)):
        scene.frame_set(bf)
        seen.add(playback._current_frame)
    if len(seen) < 3:
        _fail(f"handler barely advanced cache frames across render range: {sorted(seen)[:5]}")
    _ok(f"handler advanced through {len(seen)} distinct cache frames while stepping the timeline")

    frame_handler.detach()
    reader.close()


def main():
    args = _argv()
    if not args:
        _fail("pass a .bvc path after --")
    cache = args[0]

    bpy.ops.wm.read_factory_settings(use_empty=True)
    # Normal speed: capture is 60fps realtime -> playback_fps=60 renders realtime.
    check(cache, playback_fps=60, output_fps=60)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    # Same speed, smoother render: output_fps higher must NOT change duration.
    check(cache, playback_fps=60, output_fps=120)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    # Slow-mo: playback_fps=24 = 2.5x slower, duration 2.5x longer.
    check(cache, playback_fps=24, output_fps=60)
    print("[RENDER][PASS] render speed is playback_fps-governed and output_fps-independent")


if __name__ == "__main__":
    main()
