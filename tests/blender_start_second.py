"""Headless Blender check for LIVE "Start at Frame" retuning.

Run with:
    blender --background --python tests/blender_start_second.py -- <cache.bvc>

Verifies against a REAL cache, in real Blender, that changing the start offset
AFTER import (the UI slider path, no re-import) actually works:

  * the timeline range shifts by exactly the offset, duration unchanged;
  * the geometry at (frame_start + k) is BIT-IDENTICAL to the geometry that was
    at (old_frame_start + k) before the shift — i.e. the animation slid in time
    rather than being resampled or reset;
  * the viewport is refreshed without scrubbing (the playhead may not move);
  * the frame-change handler agrees with the new mapping;
  * the offset is held in FRAMES, so an output-fps change keeps the frame
    number (typing 500 means frame 500, not 500 *seconds* = frame 30000);
  * undo/reload recovery restores the live offset, not the imported one.

Exits non-zero on any failure.
"""

import os
import sys

import bpy

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Put the repo FIRST and evict any already-imported copies: an installed
# beamng_cache_importer add-on bundles its own `runtime`/`importer` packages and
# imports them at Blender startup, which would otherwise shadow the working tree
# and test the deployed build instead of the code under test.
sys.path.insert(0, _REPO)
for _mod in [m for m in sys.modules
             if m == "runtime" or m.startswith("runtime.")
             or m == "importer" or m.startswith("importer.")]:
    del sys.modules[_mod]

import numpy as np

from runtime.cache_reader import CacheReader
from runtime.mesh_update import CachePlayback
from runtime import frame_handler


def _argv_after_ddash():
    return sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []


_failures = []


def check(cond, msg):
    if cond:
        print(f"[START][ok]   {msg}")
    else:
        print(f"[START][FAIL] {msg}")
        _failures.append(msg)


def read_verts(obj):
    n = len(obj.data.vertices)
    buf = np.empty(n * 3, dtype=np.float32)
    obj.data.vertices.foreach_get("co", buf)
    return buf.reshape(n, 3)


def snapshot(objs):
    """All playback geometry at the current frame, keyed by object name."""
    return {o.name: read_verts(o) for o in objs}


def goto(scene, frame):
    """Move the playhead so the real frame_change_pre handler fires."""
    scene.frame_set(frame)


def main():
    args = _argv_after_ddash()
    if not args:
        print("[START][FAIL] usage: -- <cache.bvc>")
        return 1
    cache_path = args[0]
    if not os.path.exists(cache_path):
        print(f"[START][FAIL] cache not found: {cache_path}")
        return 1

    PLAYBACK_FPS = 24
    OUTPUT_FPS = 60

    reader = CacheReader(cache_path)
    playback = CachePlayback(reader)
    playback.build_scene()
    frame_handler.attach(playback, playback_fps=PLAYBACK_FPS,
                         output_fps=OUTPUT_FPS, frame_start=0)

    scene = bpy.context.scene
    objs = [o for o in bpy.data.collections["BeamNG Cache"].objects
            if o.type == "MESH"]
    print(f"[START] cache={os.path.basename(cache_path)} "
          f"frames={reader.frame_count} objects={len(objs)}")

    start0, end0 = scene.frame_start, scene.frame_end
    check(start0 == 0, f"import at 0 s -> frame_start=0 (got {start0})")

    # --- capture reference geometry at a few offsets into the animation ---
    # Probes are derived from the actual timeline length so this works on a
    # 5-frame smoke cache as well as a 2000-frame capture.
    DURATION = end0 - start0
    PROBES = sorted({0, 1, DURATION // 2, DURATION})
    ref = {}
    for k in PROBES:
        goto(scene, start0 + k)
        ref[k] = snapshot(objs)
    # Sanity: the cache must actually be animating, else "identical" proves
    # nothing below.
    moved = max(float(np.abs(ref[k][n] - ref[0][n]).max())
                for k in PROBES[1:] for n in ref[0])
    check(moved > 0.0, f"cache animates across probe frames (max delta {moved:.6f})")

    # --- LIVE shift: the UI slider path, no re-import -------------------
    # Frames are the unit of the field now: 180 FRAMES, not 3 seconds.
    OFFSET_F = 180
    expect_start = OFFSET_F
    # Park the playhead inside BOTH the old and the new range, so the clamp in
    # _apply_timeline leaves it alone and only the explicit refresh can update
    # the mesh.  On a short cache the ranges may not overlap at all — then fall
    # back to a smaller offset that guarantees they do.
    if expect_start > end0:
        OFFSET_F = max(1, DURATION // 2)
        expect_start = OFFSET_F
    PARK = max(expect_start, min(end0, expect_start + DURATION // 2))
    goto(scene, PARK)
    parked_before = snapshot(objs)

    frame_handler.update_start_frame(OFFSET_F)
    check(scene.frame_current == PARK,
          f"playhead stayed at {PARK} (got {scene.frame_current})")
    check(scene.frame_start == expect_start,
          f"live shift -> frame_start={expect_start} (got {scene.frame_start})")
    check(scene.frame_end - scene.frame_start == end0 - start0,
          f"duration unchanged ({end0 - start0} frames, "
          f"got {scene.frame_end - scene.frame_start})")

    # The refresh must have reached the mesh WITHOUT scrubbing: the playhead did
    # not move, but it now maps to an earlier cache frame, so the geometry on
    # screen has to have changed to match the new mapping.
    live = snapshot(objs)
    changed = any(not np.array_equal(live[n], parked_before[n])
                  for n in parked_before)
    check(changed, "live refresh reached the mesh without scrubbing the timeline")

    # --- the real invariant: same k -> same geometry, before and after -----
    for k in PROBES:
        goto(scene, scene.frame_start + k)
        now = snapshot(objs)
        same = all(np.array_equal(now[n], ref[k][n]) for n in ref[k])
        check(same, f"geometry at frame_start+{k} bit-identical after live shift")

    # --- handler mapping agrees with the new offset ---------------------
    check(frame_handler._cache_frame_for(expect_start) == 0,
          "handler maps the new frame_start to cache frame 0")
    check(frame_handler._cache_frame_for(expect_start - 1) < 0
          or frame_handler._cache_frame_for(expect_start - 1) == 0,
          "frames before the start do not map past cache frame 0")

    # --- shifting back returns to the original range -------------------
    frame_handler.update_start_frame(0)
    check((scene.frame_start, scene.frame_end) == (start0, end0),
          f"shift back to frame 0 restores ({start0}, {end0}) "
          f"(got ({scene.frame_start}, {scene.frame_end}))")
    k = PROBES[-1]
    goto(scene, start0 + k)
    back = snapshot(objs)
    check(all(np.array_equal(back[n], ref[k][n]) for n in ref[k]),
          "geometry restored bit-exactly after shifting back")

    # --- offset is held in FRAMES across an output-fps change ----------
    # Frames are canonical: the start keeps its frame NUMBER, not its time.
    frame_handler.update_start_frame(120)
    frame_handler.update_fps(output_fps=30)
    check(scene.frame_start == 120,
          f"frame 120 survives output_fps 60->30 as frame 120 (got {scene.frame_start})")
    check(scene.render.fps == 30, f"render.fps followed (got {scene.render.fps})")
    frame_handler.update_fps(output_fps=OUTPUT_FPS)
    check(scene.frame_start == 120,
          f"frame 120 back at 60 fps is still frame 120 (got {scene.frame_start})")

    # --- undo/reload recovery keeps the LIVE offset --------------------
    frame_handler.update_start_frame(240)
    live_start = scene.frame_start
    frame_handler._active = None          # simulate undo wiping module state
    goto(scene, live_start + 10)          # handler must self-recover
    check(frame_handler._active is not None, "handler recovered after undo")
    check(scene.frame_start == live_start,
          f"recovery kept the live offset (frame_start={live_start}, "
          f"got {scene.frame_start})")
    check(frame_handler._start_frame == 240,
          f"recovery restored frame 240 (got {frame_handler._start_frame})")

    print()
    if _failures:
        print(f"[START] {len(_failures)} FAILURE(S):")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print("[START] ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    rc = main()
    sys.stdout.flush()
    sys.exit(rc)
