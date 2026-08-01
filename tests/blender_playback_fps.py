"""Headless Blender check for LIVE "Playback Speed" / "Output FPS" retuning.

Run with:
    blender --background --python tests/blender_playback_fps.py -- <cache.bvc>

Verifies against a REAL cache, in real Blender, that changing either fps field
AFTER import (the UI slider path, no re-import) actually works:

  * THE BUG: the parked playhead's geometry updates without scrubbing.  Both fps
    values feed _cache_frame_for, so a new speed re-points a stationary playhead
    at a different cache frame — but the playhead does not move, so no
    frame-change handler fires.  Before the fix the viewport kept showing the old
    cache frame and the field looked dead;
  * the mesh shows the frame the NEW mapping asks for, bit-identically to
    scrubbing there by hand (retimed, not resampled);
  * Playback Speed changes the DURATION (half the source fps = twice as long);
  * Output FPS changes render.fps and smoothness but NOT wall-clock duration;
  * the start offset survives an fps change;
  * round-tripping back to the original fps restores the original range/geometry;
  * undo/reload recovery restores the LIVE fps, not the imported one.

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
        print(f"[FPS][ok]   {msg}")
    else:
        print(f"[FPS][FAIL] {msg}")
        _failures.append(msg)


def read_verts(obj):
    n = len(obj.data.vertices)
    buf = np.empty(n * 3, dtype=np.float32)
    obj.data.vertices.foreach_get("co", buf)
    return buf.reshape(n, 3)


def snapshot(objs):
    """All playback geometry at the current frame, keyed by object name."""
    return {o.name: read_verts(o) for o in objs}


def same(a, b):
    return all(np.array_equal(a[n], b[n]) for n in a)


def goto(scene, frame):
    """Move the playhead so the real frame_change_pre handler fires."""
    scene.frame_set(frame)


def main():
    args = _argv_after_ddash()
    if not args:
        print("[FPS][FAIL] usage: -- <cache.bvc>")
        return 1
    cache_path = args[0]
    if not os.path.exists(cache_path):
        print(f"[FPS][FAIL] cache not found: {cache_path}")
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
    n_src = reader.frame_count
    print(f"[FPS] cache={os.path.basename(cache_path)} "
          f"frames={n_src} objects={len(objs)}")

    start0, end0 = scene.frame_start, scene.frame_end
    dur0 = end0 - start0
    check(scene.render.fps == OUTPUT_FPS,
          f"import -> render.fps={OUTPUT_FPS} (got {scene.render.fps})")

    # Sanity: the cache must actually animate, else "geometry changed" below
    # would prove nothing.
    goto(scene, start0)
    first = snapshot(objs)
    goto(scene, min(end0, start0 + max(1, dur0 // 2)))
    mid = snapshot(objs)
    moved = max(float(np.abs(mid[n] - first[n]).max()) for n in first)
    check(moved > 0.0, f"cache animates (max delta {moved:.6f} m)")

    # --- THE BUG: live speed change on a PARKED playhead ------------------
    # Park somewhere in the middle so the new (shorter/longer) range still
    # contains it and _apply_timeline's clamp leaves it alone — then ONLY the
    # explicit refresh can update the mesh.
    PARK = start0 + max(1, dur0 // 4)
    goto(scene, PARK)
    parked_before = snapshot(objs)
    cache_before = frame_handler._cache_frame_for(PARK)

    NEW_PLAYBACK = 12          # half speed -> different cache frame at PARK
    frame_handler.update_fps(playback_fps=NEW_PLAYBACK)

    check(scene.frame_current == PARK,
          f"playhead stayed parked at {PARK} (got {scene.frame_current})")
    cache_after = frame_handler._cache_frame_for(PARK)
    check(cache_after != cache_before,
          f"new speed remaps the parked playhead "
          f"(cache {cache_before} -> {cache_after})")

    parked_after = snapshot(objs)
    check(not same(parked_after, parked_before),
          "LIVE: parked playhead's mesh updated without scrubbing "
          "(this is the bug — was stale before the fix)")

    # The mesh must show what the NEW mapping asks for.  Compare against
    # scrubbing to a frame that maps to the same cache frame under the new fps.
    ratio = OUTPUT_FPS / NEW_PLAYBACK          # blender frames per source frame
    probe = start0 + int(round(cache_after * ratio))
    goto(scene, probe)
    scrubbed = snapshot(objs)
    check(same(parked_after, scrubbed),
          f"refreshed geometry == geometry scrubbed to cache frame "
          f"{cache_after} (retimed, not resampled)")

    # --- duration follows Playback Speed --------------------------------
    dur_12 = scene.frame_end - scene.frame_start
    check(abs(dur_12 - dur0 * 2) <= 1,
          f"half speed doubles duration ({dur0} -> {dur_12} frames)")
    check(scene.render.fps == OUTPUT_FPS,
          f"Playback Speed left render.fps at {OUTPUT_FPS} "
          f"(got {scene.render.fps})")

    # --- Output FPS: smoothness, not speed ------------------------------
    seconds_before = (scene.frame_end - scene.frame_start) / float(OUTPUT_FPS)
    goto(scene, scene.frame_start + max(1, (scene.frame_end - scene.frame_start) // 3))
    out_parked_before = snapshot(objs)
    out_cache_before = frame_handler._cache_frame_for(scene.frame_current)

    NEW_OUTPUT = 30
    frame_handler.update_fps(output_fps=NEW_OUTPUT)

    check(scene.render.fps == NEW_OUTPUT,
          f"Output FPS -> render.fps={NEW_OUTPUT} (got {scene.render.fps})")
    seconds_after = (scene.frame_end - scene.frame_start) / float(NEW_OUTPUT)
    check(abs(seconds_before - seconds_after) < 0.05,
          f"Output FPS kept wall-clock duration "
          f"({seconds_before:.3f}s -> {seconds_after:.3f}s)")

    # Output FPS also remaps a parked playhead, so it needs the same refresh.
    if frame_handler._cache_frame_for(scene.frame_current) != out_cache_before:
        check(not same(snapshot(objs), out_parked_before),
              "LIVE: Output FPS change also refreshed the parked mesh")
    else:
        print("[FPS][skip] Output FPS did not remap this playhead")

    # --- the start offset survives an fps change ------------------------
    frame_handler.update_fps(playback_fps=PLAYBACK_FPS, output_fps=OUTPUT_FPS)
    OFFSET_F = 180 if 180 <= end0 else max(1, dur0 // 2)
    frame_handler.update_start_frame(OFFSET_F)
    frame_handler.update_fps(playback_fps=15)
    check(scene.frame_start == OFFSET_F,
          f"start offset {OFFSET_F} survived a speed change "
          f"(got {scene.frame_start})")
    check(frame_handler._cache_frame_for(OFFSET_F) == 0,
          "new speed still maps frame_start to cache frame 0")

    # --- round trip restores the original range AND geometry -------------
    frame_handler.update_start_frame(0)
    frame_handler.update_fps(playback_fps=PLAYBACK_FPS, output_fps=OUTPUT_FPS)
    check((scene.frame_start, scene.frame_end) == (start0, end0),
          f"round trip restores range ({start0}, {end0}) -> "
          f"({scene.frame_start}, {scene.frame_end})")
    goto(scene, start0)
    check(same(snapshot(objs), first),
          "round trip restores frame_start geometry bit-identically")

    # --- undo/reload recovery keeps the LIVE fps ------------------------
    frame_handler.update_fps(playback_fps=15, output_fps=48)
    check(float(scene["_beamng_playback_fps"]) == 15.0,
          "live playback_fps persisted to the scene for recovery")
    check(float(scene["_beamng_output_fps"]) == 48.0,
          "live output_fps persisted to the scene for recovery")

    frame_handler._active = None          # simulate undo wiping module state
    frame_handler._playback_fps = 999.0
    frame_handler._output_fps = 999.0
    recovered = frame_handler._try_recover(scene)
    check(recovered, "recovery rebuilt the playback from scene props")
    check(frame_handler._playback_fps == 15.0
          and frame_handler._output_fps == 48.0,
          f"recovery restored the LIVE fps, not the imported ones "
          f"(got {frame_handler._playback_fps}/{frame_handler._output_fps})")

    print(f"[FPS] {len(_failures)} failure(s)")
    if _failures:
        for f in _failures:
            print(f"  - {f}")
        return 1
    print("[FPS][PASS] Playback Speed and Output FPS retune the live cache")
    return 0


if __name__ == "__main__":
    sys.exit(main())
