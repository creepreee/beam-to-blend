"""Headless Blender check for the LIVE "Smooth Car Stop" retune.

Run with:
    blender --background --python tests/blender_smooth_stop.py -- <cache.bvc>

Verifies against a REAL cache, in real Blender, that the smooth-stop settle
(the car easing to a full rest past the last captured frame) actually works:

  * the timeline is extended by exactly the requested frames, and the scene
    stores the value for undo/reload recovery;
  * the tail SWINGS: it continues the damped oscillation the car was still
    rocking through when the capture ended (fitted from the recent frames —
    period and amplitude must match the data), both the vertex deformation and
    the rigid root transform move, the root CROSSES its rest centre and comes
    back (motion reverses, it is not a rigid glide to a stop), and the
    per-frame velocity DECAYS toward rest instead of snapping;
  * the local deformation relaxes back toward the last captured frame and the
    whole pose holds still once past the tail end;
  * THE BUG CLASS: a parked playhead updates without scrubbing.  Retuning the
    tail length while parked mid-tail re-points the same frame at a different
    glide pose, and toggling the feature OFF collapses the geometry back to the
    exact final captured pose — both require an explicit `_refresh_current_frame`
    because the playhead does not move;
  * toggling OFF clamps the playhead back inside the shortened range;
  * undo/reload recovery restores the LIVE tail, not the imported one, and the
    glided geometry matches what it was before the wipe.

Exits non-zero on any failure.
"""

import math
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

import math

import numpy as np

from runtime.cache_reader import CacheReader
from runtime.mesh_update import CachePlayback
from runtime import frame_handler


def _argv_after_ddash():
    return sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []


_failures = []


def check(cond, msg):
    if cond:
        print(f"[SMOOTH][ok]   {msg}")
    else:
        print(f"[SMOOTH][FAIL] {msg}")
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


def delta(a, b):
    """Max single-vertex displacement across every object (metres)."""
    return max(float(np.abs(a[n] - b[n]).max()) for n in a)


def root_location(playback):
    return np.array(playback._transform_empty.location, dtype=np.float64).copy()


def goto(scene, frame):
    """Move the playhead so the real frame_change_pre handler fires."""
    scene.frame_set(frame)


def main():
    args = _argv_after_ddash()
    if not args:
        print("[SMOOTH][FAIL] usage: -- <cache.bvc>")
        return 1
    cache_path = args[0]
    if not os.path.exists(cache_path):
        print(f"[SMOOTH][FAIL] cache not found: {cache_path}")
        return 1

    PLAYBACK_FPS = 24
    OUTPUT_FPS = 60
    TAIL = 30                      # timeline frames of settle
    RATIO = OUTPUT_FPS / PLAYBACK_FPS   # blender frames per cache frame (2.5)

    reader = CacheReader(cache_path)
    playback = CachePlayback(reader)
    playback.build_scene()
    frame_handler.attach(playback, playback_fps=PLAYBACK_FPS,
                         output_fps=OUTPUT_FPS, frame_start=0,
                         smooth_stop_frames=0)

    scene = bpy.context.scene
    objs = [o for o in bpy.data.collections["BeamNG Cache"].objects
            if o.type == "MESH"]
    n_src = reader.frame_count
    last = n_src - 1
    print(f"[SMOOTH] cache={os.path.basename(cache_path)} "
          f"frames={n_src} objects={len(objs)}")

    def rel_for(cache):
        return int(round(cache * RATIO))

    base_end = scene.frame_end            # imported with tail=0
    final_rel = base_end - 1              # -> cache 1199 (last captured)
    mid_rel = base_end + TAIL // 2        # mid-tail, u ~= 0.5
    late_rel = base_end + TAIL - 1        # near rest, u ~= 0.97
    near_late_rel = late_rel - 1
    print(f"[SMOOTH] base_end={base_end} final_rel={final_rel} "
          f"mid_rel={mid_rel} late_rel={late_rel}")

    # Sanity: the cache must animate, else "pose changed" proves nothing.
    goto(scene, scene.frame_start)
    first = snapshot(objs)
    goto(scene, final_rel)
    final_pose = snapshot(objs)
    check(delta(first, final_pose) > 1e-4,
          f"cache animates (max delta {delta(first, final_pose):.4f} m)")

    # --- A. timeline extension + persistence ----------------------------
    frame_handler.update_smooth_stop(frames=TAIL)
    check(scene.frame_end == base_end + TAIL,
          f"timeline extended by {TAIL} frames "
          f"({base_end} -> {scene.frame_end})")
    check(int(scene["_beamng_smooth_stop_frames"]) == TAIL,
          "tail length persisted to the scene for recovery")
    check(abs(playback._smooth_stop_tail - TAIL / RATIO) < 1e-6,
          f"playback got the tail in CACHE units "
          f"({playback._smooth_stop_tail:.1f} = {TAIL} / {RATIO:.1f})")

    # --- B. the tail SWINGS (continues the captured oscillation) and decays --
    fit = playback._tail_fit
    check(fit is not None,
          "tail fitted the car's residual oscillation from recent frames")
    if fit is not None:
        check(fit["amplitude"] > 1e-4,
              f"fitted a real oscillation (amplitude "
              f"{fit['amplitude']*1000:.2f} mm)")
        check(abs(2 * math.pi / fit["omega"] - 10.0) < 1.0,
              f"fitted period ~10 cache frames "
              f"(got {2 * math.pi / fit['omega']:.1f})")

    goto(scene, mid_rel)
    mid_pose = snapshot(objs)
    goto(scene, late_rel)
    late_pose = snapshot(objs)
    check(delta(final_pose, mid_pose) > 5e-5,
          f"mid-tail deformation swung off the last captured frame "
          f"({delta(final_pose, mid_pose)*1000:.3f} mm)")
    check(delta(final_pose, late_pose) < delta(final_pose, mid_pose),
          f"local deformation relaxes back toward the last frame "
          f"({delta(final_pose, late_pose)*1000:.3f} mm < "
          f"{delta(final_pose, mid_pose)*1000:.3f} mm)")
    goto(scene, mid_rel - 1)
    mid_prev_pose = snapshot(objs)
    mid_vel = delta(mid_pose, mid_prev_pose)
    goto(scene, near_late_rel)
    near_late_pose = snapshot(objs)
    late_vel = delta(late_pose, near_late_pose)
    check(late_vel < 0.5 * mid_vel,
          f"deformation velocity DECAYS (per-frame move "
          f"{mid_vel*1e6:.2f} um mid-tail -> {late_vel*1e6:.2f} um near rest)")
    # At/after the tail end the ease clamps to u=1: the pose stops moving.
    goto(scene, base_end + TAIL)
    rest_pose = snapshot(objs)
    goto(scene, base_end + TAIL + 4)
    rest_pose2 = snapshot(objs)
    check(same(rest_pose, rest_pose2),
          "pose converges to a full rest after the tail ends")

    # --- C. the rigid root transform SWINGS through rest, then settles ------
    if playback._transform_empty is not None and fit is not None:
        cx = fit["pos"][0][0]
        goto(scene, final_rel)
        root_start = root_location(playback)
        init_dev = root_start[0] - cx
        check(abs(init_dev) > 1e-4,
              f"car is mid-swing at capture end "
              f"(x off the rest centre by {abs(init_dev)*1000:.2f} mm)")

        # Sample the whole tail: a rigid glide would drift one way; the swing
        # crosses its rest centre and comes back — the motion REVERSES.
        crossed = False
        for s in range(0, int(TAIL / RATIO) + 1):
            goto(scene, base_end + int(round(s * RATIO)))
            if (root_location(playback)[0] - cx) * init_dev < 0:
                crossed = True
        check(crossed,
              "root SWINGS through its rest centre and back "
              "(motion reverses — not a rigid glide to a stop)")

        goto(scene, base_end + TAIL)
        root_end = root_location(playback)
        check(abs(root_end[0] - cx) < 1e-3,
              f"root settles ON the oscillation centre "
              f"({abs(root_end[0] - cx)*1000:.3f} mm off)")

        goto(scene, mid_rel)
        root_mid = root_location(playback)
        goto(scene, mid_rel - 1)
        root_mid_prev = root_location(playback)
        root_mid_vel = float(np.abs(root_mid - root_mid_prev).max())
        goto(scene, late_rel)
        root_late = root_location(playback)
        goto(scene, near_late_rel)
        root_near_late = root_location(playback)
        root_late_vel = float(np.abs(root_late - root_near_late).max())
        check(root_late_vel < 0.5 * root_mid_vel,
              f"root velocity DECAYS "
              f"({root_mid_vel*1e6:.3f} um/fr mid-tail -> "
              f"{root_late_vel*1e6:.3f} um/fr near rest)")
    else:
        print("[SMOOTH][skip] no rigid transform / fit — root swing untested")

    # --- D. THE BUG: retune while parked in the tail --------------------
    # The playhead does not move, so without an explicit refresh the viewport
    # keeps the OLD glide pose and the slider looks dead.
    goto(scene, mid_rel)
    parked_before = snapshot(objs)
    frame_handler.update_smooth_stop(frames=TAIL * 2)
    check(scene.frame_current == mid_rel,
          f"playhead stayed parked at {mid_rel} (got {scene.frame_current})")
    parked_after = snapshot(objs)
    check(not same(parked_after, parked_before),
          "LIVE: retuning the tail length refreshed the parked playhead's "
          "glide pose without scrubbing (the bug — stale before the fix)")

    # --- E. toggling OFF returns the exact final pose + clamps playhead --
    goto(scene, mid_rel)
    frame_handler.update_smooth_stop(enabled=False)
    check(scene.frame_current == base_end,
          f"disabling clamped the playhead back inside the range "
          f"(got {scene.frame_current}, expected {base_end})")
    check(same(snapshot(objs), final_pose),
          "disabling returned the mesh to the exact final captured pose "
          "(would stay glided without the refresh)")
    check(scene.frame_end == base_end,
          f"timeline shrunk back to the captured range "
          f"(frame_end {scene.frame_end})")

    # --- F. undo/reload recovery keeps the LIVE tail --------------------
    frame_handler.update_smooth_stop(frames=TAIL)
    goto(scene, mid_rel)
    live_mid = snapshot(objs)

    frame_handler._active = None          # simulate undo wiping module state
    frame_handler._smooth_stop_frames = 0
    recovered = frame_handler._try_recover(scene)
    check(recovered, "recovery rebuilt the playback from scene props")
    check(frame_handler._smooth_stop_frames == TAIL,
          "recovery restored the LIVE tail, not the imported one "
          f"(got {frame_handler._smooth_stop_frames})")
    goto(scene, mid_rel)
    check(same(snapshot(objs), live_mid),
          "recovered playback reproduces the glided pose bit-identically")

    print(f"[SMOOTH] {len(_failures)} failure(s)")
    if _failures:
        for f in _failures:
            print(f"  - {f}")
        return 1
    print("[SMOOTH][PASS] Smooth Car Stop extends, glides, converges and recovers")
    return 0


if __name__ == "__main__":
    sys.exit(main())
