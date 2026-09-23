"""Verify 1 — keyframe counts and genuine WORLD-space motion per debris element.

    blender --background --python verify/01_keyframes_and_motion.py -- [blend]

For every debris object (names containing "debris"/"glassfrag"/"frag"/"shard")
and every particle emitter in the scene, this script checks:

  * how many location keyframes each object carries, and
  * whether its WORLD position GENUINELY changes over the animation range.

The motion check samples ``matrix_world.translation`` at ~12 frames across the
timeline (a single global frame pass, so parents/root-empties are honoured).
This is the "debris stuck in the air with 2 keyframes" check: an object whose
location keys sit on two adjacent frames (e.g. 2457 and 2458) has no motion and
hangs at its spawn point.  Parented fringe is correctly NOT flagged, because it
genuinely rides the wreck in world space.

Particle emitters are checked separately: particle count, point-cache frame
range, and whether sampled particles move between two distant frames.

Exit code is non-zero if any debris element is stuck in world space.
"""

from __future__ import annotations

import os
import sys
from typing import List, Optional, Sequence, Tuple

import bpy
from mathutils import Vector

#: Default blend (PERMANENT RULE — no other blend files allowed for testing).
# Point at a .blend that already contains an imported BeamNG cache
# (pass one explicitly: blender --background --python verify/01_... -- /path/to/scene.blend)
DEFAULT_BLEND = ""

#: An object whose sampled world path is shorter than this (m) is "stuck".
MOTION_EPS = 0.01

#: Objects whose location keys all sit within this many frames of each other are
#: the reported "2 keyframes -> no animation" symptom.
MIN_KEY_SPAN = 3

#: How many sample points to evaluate across the timeline.
SAMPLES = 12

FAILS: List[str] = []


def log(msg: str) -> None:
    print(msg, flush=True)


def is_debris_name(name: str) -> bool:
    n = name.lower()
    return any(k in n for k in ("debris", "glassfrag", "_frag", "shard"))


def has_particle_system(obj: "bpy.types.Object") -> bool:
    return any(getattr(m, "particle_system", None) is not None
               for m in obj.modifiers)


def location_key_stats(obj: "bpy.types.Object") -> Tuple[int, Optional[Tuple[int, int]]]:
    """Return (key count, (min_frame, max_frame)) for the location channel."""
    data = obj.animation_data
    if data is None or data.action is None:
        return 0, None
    frames = [int(round(kp.co[0]))
              for fc in data.action.fcurves
              if fc.data_path == "location"
              for kp in fc.keyframe_points]
    if not frames:
        return 0, None
    return len(frames), (min(frames), max(frames))


def analyse_object(obj: "bpy.types.Object", world_path: float) -> dict:
    keys, spread = location_key_stats(obj)
    if world_path < MOTION_EPS:
        if keys > 0 and spread is not None and \
                (spread[1] - spread[0]) <= MIN_KEY_SPAN:
            verdict = "STUCK_2KEYS"
        elif keys == 0:
            verdict = "NO_MOTION_NO_KEYS"
        else:
            verdict = "STUCK"
    else:
        verdict = "MOVING"
    return {
        "name": obj.name,
        "keys": keys,
        "path": round(float(world_path), 4),
        "verdict": verdict,
        "spread": spread,
    }


def analyse_particles(obj: "bpy.types.Object", scene) -> dict:
    """Particle-emitter check: count + actual motion between two frames."""
    result = {
        "name": obj.name,
        "type": "emitter",
        "particles": 0,
        "cache_frames": None,
        "move_avg": None,
        "verdict": "NO_PARTICLES",
    }
    psys = next((getattr(m, "particle_system", None) for m in obj.modifiers),
                None)
    if psys is None:
        return result

    pc = psys.point_cache
    cache_frames = None
    try:
        if pc is not None and pc.is_baked:
            br = pc.baked_frame_range()
            if br:
                cache_frames = (int(br[0]), int(br[1]))
    except Exception:
        cache_frames = None

    frame_a = int(psys.settings.frame_start)
    dg = bpy.context.evaluated_depsgraph_get()
    locs = {}
    try:
        fa = max(scene.frame_start, frame_a + 30)
        fb = min(scene.frame_end, frame_a + 110)
        for f in (fa, fb):
            scene.frame_set(f)
            dg.update()
            ev = dg.objects.get(obj.name, None)
            if ev is None or not ev.particle_systems:
                locs[f] = {}
                continue
            m = {}
            for p in ev.particle_systems[0].particles:
                if getattr(p, "alive_state", 0) != 0:
                    continue
                m[p.id] = (float(p.location.x), float(p.location.y),
                           float(p.location.z))
            locs[f] = m
    except Exception as exc:
        result["verdict"] = "EVAL_ERROR"
        result["note"] = f"{type(exc).__name__}: {exc}"
        return result

    result["particles"] = max(len(v) for v in locs.values())
    result["cache_frames"] = cache_frames
    map_a, map_b = locs.get(fa, {}), locs.get(fb, {})
    if not map_a or not map_b:
        result["verdict"] = "NO_PARTICLES"
        return result

    common = set(map_a) & set(map_b)
    total = sum((Vector(map_a[i]) - Vector(map_b[i])).length for i in common)
    avg = total / max(1, len(common))
    result["matched"] = len(common)
    result["move_avg"] = round(float(avg), 4)
    result["verdict"] = "MOVING" if avg >= MOTION_EPS else "STUCK"
    return result


def in_scene(obj: "bpy.types.Object", scene) -> bool:
    """True if the object is reachable from the scene's collection hierarchy."""
    name = obj.name
    seen = set()

    def walk(coll, name):
        if any(o.name == name for o in coll.objects):
            return True
        for child in coll.children:
            if child.name in seen:
                continue
            seen.add(child.name)
            if walk(child, name):
                return True
        return False

    return walk(scene.collection, name)


def main(argv: Sequence[str]) -> int:
    argv = [a for a in argv if a.lower().endswith(".blend")]
    blend = argv[0] if argv else DEFAULT_BLEND
    if not os.path.exists(blend):
        log(f"[VERIFY1][FAIL] blend not found: {blend}")
        return 1

    bpy.ops.wm.read_factory_settings(use_empty=True)
    log(f"[VERIFY1] opening {blend}")
    bpy.ops.wm.open_mainfile(filepath=blend)
    scene = bpy.context.scene
    frame_start = int(scene.frame_start)
    frame_end = int(scene.frame_end)
    log(f"[VERIFY1] range {frame_start}..{frame_end}")

    debris: List["bpy.types.Object"] = []
    emitters: List["bpy.types.Object"] = []
    for obj in bpy.data.objects:
        if not in_scene(obj, scene):
            continue
        if has_particle_system(obj):
            emitters.append(obj)
        elif is_debris_name(obj.name):
            debris.append(obj)

    if not debris and not emitters:
        log("[VERIFY1][WARN] no debris objects or emitters found in the scene")
        return 2

    # --- one global frame pass: record world positions of every target ----
    targets = debris + emitters
    span = max(1, frame_end - frame_start)
    sample_frames = [frame_start + (span * i) // (SAMPLES - 1)
                     for i in range(SAMPLES)]
    world: dict = {o.name: [] for o in targets}
    for f in sample_frames:
        scene.frame_set(f)
        bpy.context.view_layer.update()
        for o in targets:
            world[o.name].append(o.matrix_world.translation.copy())

    def path_of(name: str) -> float:
        pts = world[name]
        return sum((pts[i + 1] - pts[i]).length for i in range(len(pts) - 1))

    # --- debris / fragment objects ---------------------------------------
    rows = []
    for obj in sorted(debris, key=lambda o: o.name):
        r = analyse_object(obj, path_of(obj.name))
        rows.append(r)
        log(f"[VERIFY1] debris   {r['verdict']:14s} keys={r['keys']:4d} "
            f"path={r['path']:8.4f} frames={r['spread']} {r['name']}")

    # --- particle emitters ------------------------------------------------
    for obj in sorted(emitters, key=lambda o: o.name):
        r = analyse_object(obj, path_of(obj.name))
        pr = analyse_particles(obj, scene)
        log(f"[VERIFY1] emitter  {pr['verdict']:14s} "
            f"obj_path={r['path']:8.4f} obj_keys={r['keys']:3d} "
            f"particles={pr.get('particles')} matched={pr.get('matched', '-')} "
            f"cache={pr.get('cache_frames')} "
            f"move_avg={pr.get('move_avg', 'n/a')} "
            f"{pr['name']}")
        rows.append(r)
        if pr["verdict"] == "STUCK":
            FAILS.append(f"{pr['name']} (particles stuck)")

    # --- summary ----------------------------------------------------------
    moved_n = sum(1 for r in rows if r["verdict"] == "MOVING")
    stuck_n = sum(1 for r in rows if r["verdict"] != "MOVING")
    for r in rows:
        if r["verdict"] != "MOVING":
            FAILS.append(r["name"])
    log(f"[VERIFY1] summary: {moved_n} MOVING, {stuck_n} NOT MOVING "
        f"(of {len(rows)} debris)")

    if FAILS:
        log(f"[VERIFY1][FAIL] stuck elements ({len(FAILS)}): "
            + ", ".join(FAILS[:25]))
        return 1
    log("[VERIFY1][PASS] every debris element genuinely moves in world space")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
