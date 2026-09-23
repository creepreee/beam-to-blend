"""Verify 2 — clean rebuild of impact debris + WORLD-space motion check.

    blender --background --python verify/02_rebuild_and_motion.py -- [blend]

This drives the add-on's own Debris pipeline headless and then checks that
EVERYTHING the debris module created actually MOVES in world space:

  1. CLEAR   — remove every object / emitter the debris module created
               (runtime.debris_spawn.clear_debris).
  2. DETECT  — CacheReader + runtime.impact_detect.detect_impacts on the
               imported cache -> crash/impact events.  The earliest impact
               frame IS the first crash point; the events also give every
               crash frame (the car hits the ground more than once).
  3. BUILD   — runtime.debris_spawn.build_debris (hero rigid bodies + fine
               particle emitters + shattered glass panes) + bake_debris
               (rigid bodies frozen into F-curves).  Deterministic seed.
  4. COUNT   — Y = every object in the debris module's OWN collections
               ("BeamNG Debris" + "BeamNG Debris Glass").
  5. MOTION  — starting from the first crash point, walk forward through the
               timeline; for every mesh sample matrix_world.translation, for
               every emitter sample its particle cloud (depsgraph) + object.
               Verdict per object: MOVING / STUCK.
  6. REPORT  — how many emitters, particle systems, hero meshes and glass
               fragments the module created and how many are moving; what and
               how many are NOT moving; X/Y summary.

Exit code: 0 = everything the debris module created is moving, 1 = stuck.
"""

from __future__ import annotations

import os
import sys
from typing import Dict, List, Optional, Sequence, Tuple

# The add-on's runtime package lives in the project root; make it importable
# even when this script is run without the add-on enabled in preferences.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import bpy
from mathutils import Vector

#: Default blend (PERMANENT RULE — no other blend files allowed for testing).
# Point at a .blend that already contains an imported BeamNG cache
# (pass one explicitly: blender --background --python verify/01_... -- /path/to/scene.blend)
DEFAULT_BLEND = ""

#: A mesh whose total world path over the sampled walk is shorter than this (m)
#: is STUCK.  Debris pieces fly metres and settle; a live shard always beats it.
MESH_MOVE_EPS = 0.05
#: A stuck mesh resting this far above the ground plane is a mid-air hoverer
#: (the "debris hanging in the air" symptom), not a piece resting on the pile.
HOVER_HEIGHT = 0.30
#: An emitter whose particle-cloud centroid travels less than this (m) over its
#: emission window is STUCK.
PARTICLE_MOVE_EPS = 0.10
#: Step the emitter walk frame-by-frame (mandatory — see analyse_emitter) but
#: only read the particle cloud every N frames.
PARTICLE_SAMPLE_EVERY = 25
#: How many sample points the playback walk uses across the crash..end range.
SAMPLES = 24

LOG: List[str] = []


def log(msg: str) -> None:
    LOG.append(msg)
    print(msg, flush=True)


def has_particle_system(obj: "bpy.types.Object") -> bool:
    return any(getattr(m, "particle_system", None) is not None
               for m in obj.modifiers)


def blender_frame_for(cache_frame: int, frame_start: int,
                      playback_fps: float, output_fps: float) -> int:
    """Mirror of debris_spawn._blender_frame_for (cache frame -> Blender frame)."""
    if playback_fps <= 0:
        return int(frame_start + cache_frame)
    return int(round(frame_start + cache_frame * (output_fps / playback_fps)))


def debris_settings(scene) -> "object":
    """Replicate addon/operators._debris_settings from the scene UI props."""
    from runtime.debris_spawn import DebrisSettings
    props = scene.beamng_debris
    return DebrisSettings(
        density=float(getattr(props, "debris_density", 1.0)),
        scale=float(getattr(props, "debris_scale", 1.0)),
        hero_count=int(getattr(props, "debris_hero_count", 14)),
        fine_count=int(getattr(props, "debris_fine_count", 90)),
        max_hero_total=int(getattr(props, "debris_max_hero", 240)),
        speed=float(getattr(props, "debris_speed", 0.0)),
        spread=float(getattr(props, "debris_spread", 55.0)),
        bounciness=float(getattr(props, "debris_bounciness", 0.25)),
        scatter=float(getattr(props, "debris_scatter", 0.45)),
        min_severity=float(getattr(props, "debris_min_severity", 0.12)),
        min_blast_severity=float(getattr(props, "debris_min_blast_severity", 0.35)),
        variants=int(getattr(props, "debris_variants", 8)),
        settle_frames=int(getattr(props, "debris_settle_frames", 260)),
        seed=int(getattr(props, "debris_seed", 12345)),
        shatter_glass=bool(getattr(props, "debris_shatter_glass", True)),
    )


def glass_settings(scene) -> "object":
    """Replicate addon/operators._glass_settings from the scene UI props."""
    from runtime.impact_detect import GlassSettings
    props = scene.beamng_debris
    return GlassSettings(
        crack_deform=float(getattr(props, "glass_crack_deform", 0.006)),
        shatter_deform=float(getattr(props, "glass_shatter_deform", 0.022)),
        shatter_ground_depth=float(getattr(props, "glass_shatter_ground_depth", 0.03)),
        edge_retain=float(getattr(props, "glass_edge_retain", 0.05)),
    )


def collection_objects(coll_names: Sequence[str]) -> List["bpy.types.Object"]:
    objs: List["bpy.types.Object"] = []
    seen = set()
    for name in coll_names:
        coll = bpy.data.collections.get(name)
        if coll is None:
            continue
        for o in coll.objects:
            if o.name not in seen:
                seen.add(o.name)
                objs.append(o)
    return objs


def analyse_mesh(obj: "bpy.types.Object", world: Dict[int, Vector]) -> dict:
    """World-space displacement of a baked debris mesh over the walk.

    Also reports the final resting height: a STUCK piece that ends well above
    the ground plane is a mid-air hoverer (the "debris hanging in the air"
    symptom), not a shard that fell and is resting on the pile.
    """
    pts = [world[f].get(obj.name) for f in sorted(world)]
    pts = [p for p in pts if p is not None]
    if len(pts) < 2:
        return {"verdict": "UNSAMPLED", "path": 0.0}
    path = sum((pts[i + 1] - pts[i]).length for i in range(len(pts) - 1))
    rest_z = round(float(pts[-1].z), 3)
    result = {"name": obj.name,
              "verdict": "MOVING" if path >= MESH_MOVE_EPS else "STUCK",
              "path": round(float(path), 4),
              "rest_z": rest_z}
    if result["verdict"] == "STUCK":
        result["hover"] = rest_z > HOVER_HEIGHT
    return result


def analyse_emitter(obj: "bpy.types.Object",
                    scene) -> dict:
    """Emitter check: object world path + particle-cloud centroid path."""
    result = {"name": obj.name, "obj_path": 0.0, "particles": 0,
              "p_centroid": 0.0, "verdict": "NO_PARTICLES"}
    psys = next((getattr(m, "particle_system", None) for m in obj.modifiers),
                None)
    if psys is None:
        return result

    fs = int(psys.settings.frame_start)
    if fs < scene.frame_start or fs > scene.frame_end:
        return {**result, "verdict": "OFF_RANGE"}

    # Walk the emission window ONE FRAME AT A TIME, reading the cloud every
    # PARTICLE_SAMPLE_EVERY frames.  Jumping the frame directly (frame_set in
    # 25-frame strides) makes Blender's live NEWTON particle integrator read
    # garbage positions (measured: z down to -1e27), so a stride-sampled sweep
    # reports every emitter as wildly MOVING.  Frame-by-frame stepping is the
    # only read that matches what a viewport playback evaluates.  The step
    # back to fs-1 first proves a backward jump does not poison the walk.
    dg = bpy.context.evaluated_depsgraph_get()
    scene.frame_set(fs - 1)
    end = min(scene.frame_end, fs + 160)
    centroids: List[Tuple[float, float, float]] = []
    n_max = 0
    try:
        for f in range(fs, end + 1):
            scene.frame_set(f)
            dg.update()
            if f % PARTICLE_SAMPLE_EVERY != 0:
                continue
            ev = dg.objects.get(obj.name, None)
            if ev is None or not ev.particle_systems:
                continue
            locs = [p.location for p in ev.particle_systems[0].particles]
            if not locs:
                continue
            n_max = max(n_max, len(locs))
            c = (sum(float(p.x) for p in locs) / len(locs),
                 sum(float(p.y) for p in locs) / len(locs),
                 sum(float(p.z) for p in locs) / len(locs))
            centroids.append(c)
    except Exception as exc:
        result["note"] = f"{type(exc).__name__}: {exc}"
        result["verdict"] = "EVAL_ERROR"
        return result

    result["particles"] = n_max
    if len(centroids) < 2:
        return result
    path = sum((Vector(centroids[i + 1]) - Vector(centroids[i])).length
               for i in range(len(centroids) - 1))
    result["p_centroid"] = round(float(path), 4)
    result["verdict"] = ("MOVING" if path >= PARTICLE_MOVE_EPS else "STUCK")
    return result


def main(argv: Sequence[str]) -> int:
    use_saved = "--use-saved" in sys.argv
    argv = [a for a in argv if a.lower().endswith(".blend")]
    blend = argv[0] if argv else DEFAULT_BLEND
    save_to = os.path.join(os.environ.get("TEMP", "."),
                           "verify2_rebuilt.blend")
    if use_saved:
        if not os.path.exists(save_to):
            log(f"[VERIFY2][FAIL] --use-saved given but no rebuilt scene at "
                f"{save_to} (run without the flag first)")
            return 1
        blend = save_to
    if not os.path.exists(blend):
        log(f"[VERIFY2][FAIL] blend not found: {blend}")
        return 1

    from runtime.cache_reader import CacheReader
    from runtime.impact_detect import detect_impacts, summarise
    from runtime.debris_spawn import (
        DEBRIS_COLLECTION,
        GLASS_COLLECTION,
        GROUND_NAME,
        clear_debris,
        build_debris,
    )
    from runtime.debris_bake import bake_debris
    from runtime.debris_physics import _frozen_handlers

    bpy.ops.wm.open_mainfile(filepath=blend)
    log(f"[VERIFY2] opening {blend}")
    scene = bpy.context.scene
    frame_start = int(scene.frame_start)
    frame_end = int(scene.frame_end)
    log(f"[VERIFY2] timeline {frame_start}..{frame_end}")

    cache_path = scene.get("_beamng_cache_path") or \
        getattr(scene.beamng, "cache_path", "")
    if not cache_path or not os.path.exists(cache_path):
        log(f"[VERIFY2][FAIL] cache not found: {cache_path}")
        return 1
    start_frame = int(getattr(scene.beamng, "start_frame", 0))
    playback_fps = float(getattr(scene.beamng, "playback_fps", 24))
    output_fps = float(getattr(scene.beamng, "output_fps", 60))
    ground_shift = float(scene.get("_beamng_ground_shift", 0.0))
    log(f"[VERIFY2] cache {os.path.basename(cache_path)} "
        f"(start={start_frame} play={playback_fps:.0f} out={output_fps:.0f} "
        f"ground_shift={ground_shift:.4f})")

    # --- 1-3. CLEAR / DETECT / BUILD (or load the persisted rebuild) ----------
    if not use_saved:
        with _frozen_handlers():
            removed = clear_debris()
            log(f"[VERIFY2] 1. CLEAR — removed {removed} debris objects "
                f"(collections present now: {[c.name for c in bpy.data.collections if 'Debris' in c.name] or 'none'})")

            # --- 2. DETECT crash/impact frames ---------------------------------
            reader = CacheReader(cache_path)
            events = []
            try:
                events = detect_impacts(
                    reader,
                    ground_shift=ground_shift,
                    playback_fps=float(playback_fps),
                )
            finally:
                reader.close()

            if not events:
                log("[VERIFY2][FAIL] no impacts detected — nothing to rebuild")
                return 1

            crash_blend = sorted(blender_frame_for(
                e.cache_frame, start_frame, playback_fps, output_fps)
                for e in events)
            crash_blend = [f for f in crash_blend if f >= scene.frame_start]
            first_crash = crash_blend[0]
            log(f"[VERIFY2] 2. DETECT — {len(events)} impact events on "
                f"{len({e.part for e in events})} parts; {summarise(events)}")
            log(f"[VERIFY2]    crash frame(s) (Blender): first={first_crash} "
                f"count={len(crash_blend)} range={crash_blend[0]}..{crash_blend[-1]}")

            # --- 3. BUILD --------------------------------------------------------
            source_objects = {o.name: o for o in bpy.data.objects
                              if o.type == "MESH"}
            settings = debris_settings(scene)
            gsettings = glass_settings(scene)
            log(f"[VERIFY2] 3. BUILD — spawning (hero budget "
                f"{settings.max_hero_total}, fine {settings.fine_count}/impact, "
                f"shatter_glass={settings.shatter_glass}) ...")
            summary = build_debris(
                CacheReader(cache_path), events, settings,
                glass_settings=gsettings,
                frame_start=int(start_frame),
                playback_fps=float(playback_fps),
                output_fps=float(output_fps),
                ground_shift=ground_shift,
                source_objects=source_objects,
            )
            bake = bake_debris(
                summary.get("hero_objects", []),
                summary.get("bake_start", scene.frame_start),
                summary.get("bake_end", scene.frame_end),
            )
            log(f"[VERIFY2]    built: {summary.get('events')} events -> "
                f"{summary.get('hero', 0)} hero, {summary.get('emitters', 0)} "
                f"emitters, {summary.get('shards', 0)} shards, "
                f"{summary.get('glass', 0)} glass "
                f"({summary.get('retained', 0)} edge retained); "
                f"{bake.get('baked', 0)} bodies baked "
                f"({summary.get('bake_start')}..{summary.get('bake_end')})")
            scene["_verify2_first_crash"] = first_crash
            scene["_verify2_build_log"] = \
                f"{summary.get('events')} events -> {summary.get('hero', 0)} " \
                f"hero, {summary.get('emitters', 0)} emitters, " \
                f"{summary.get('shards', 0)} shards, {summary.get('glass', 0)} " \
                f"glass ({summary.get('retained', 0)} edge retained); " \
                f"{bake.get('baked', 0)} bodies baked"
    else:
        first_crash = int(scene.get("_verify2_first_crash", scene.frame_start))
        log(f"[VERIFY2] --use-saved: loaded rebuilt scene; "
            f"first crash = {first_crash}; build = "
            f"{scene.get('_verify2_build_log', 'unknown')}")

    # --- 4. COUNT (Y) --------------------------------------------------------
    frame_end = int(scene.frame_end)
    targets = collection_objects([DEBRIS_COLLECTION, GLASS_COLLECTION])
    emitters = [o for o in targets if has_particle_system(o)]
    meshes = [o for o in targets if not has_particle_system(o)
              and o.name != GROUND_NAME]
    ground = next((o for o in targets if o.name == GROUND_NAME), None)
    y = len(targets)
    log(f"[VERIFY2] 4. COUNT — debris module collections contain {y} objects "
        f"= {len(emitters)} emitters + {len(meshes)} meshes"
        + (f" + 1 ground collider ({GROUND_NAME})" if ground else ""))

    # --- 5. MOTION (from the first crash point forward) ----------------------
    with _frozen_handlers():
        # Clean slate before any measurement: reset the timeline to the start
        # so both fresh-build and loaded sessions evaluate identically
        # (particle/rigid-body state left over from the bake would otherwise
        # skew the walk).
        scene.frame_set(scene.frame_start)
        bpy.context.view_layer.update()
        first = max(scene.frame_start, first_crash)
        span = max(1, frame_end - first)
        frames = [first + (span * i) // (SAMPLES - 1) for i in range(SAMPLES)]
        world: Dict[int, Dict[str, Vector]] = {}
        for f in frames:
            scene.frame_set(f)
            bpy.context.view_layer.update()
            world[f] = {o.name: o.matrix_world.translation.copy() for o in meshes}

        rows: List[dict] = []
        stuck_mesh: List[str] = []
        stuck_emitter: List[str] = []
        for obj in sorted(meshes, key=lambda o: o.name):
            r = analyse_mesh(obj, world)
            rows.append(r)
            tag = ("hover" if r.get("hover") else "ground"
                   if r["verdict"] == "STUCK" else "")
            log(f"[VERIFY2] mesh    {r['verdict']:10s} path={r['path']:8.4f} "
                f"rest_z={r.get('rest_z', 0.0):7.3f} {tag:6s} {obj.name}")
            if r["verdict"] == "STUCK":
                stuck_mesh.append(
                    f"{obj.name} (rest_z={r.get('rest_z', 0.0)})")

        for obj in sorted(emitters, key=lambda o: o.name):
            r = analyse_emitter(obj, scene)
            rows.append(r)
            log(f"[VERIFY2] emitter {r['verdict']:10s} "
                f"obj_path={r['obj_path']:8.4f} "
                f"particles={r.get('particles')} "
                f"p_centroid={r.get('p_centroid', 0.0):9.4f} {obj.name}")
            if r["verdict"] == "STUCK":
                stuck_emitter.append(f"{obj.name} (particles)")

    # --- 5b. PERSIST the rebuilt scene for later probing --------------------
    if not use_saved:
        try:
            bpy.ops.wm.save_as_mainfile(filepath=save_to)
            log(f"[VERIFY2] rebuilt scene saved: {save_to}")
        except Exception as exc:
            log(f"[VERIFY2] could not save rebuilt scene: {exc}")

    # --- 6. REPORT -----------------------------------------------------------
    moving = [r for r in rows if r["verdict"] == "MOVING"]
    mesh_moving = sum(1 for o in meshes
                      if any(r.get("name") == o.name and r["verdict"] == "MOVING"
                             for r in rows))
    em_moving = sum(1 for o in emitters
                    if any(r.get("name") == o.name and r["verdict"] == "MOVING"
                           for r in rows))

    log(f"[VERIFY2] 6. REPORT")
    log(f"[VERIFY2]    emitters : {em_moving}/{len(emitters)} moving")
    log(f"[VERIFY2]    meshes   : {mesh_moving}/{len(meshes)} moving "
        f"(hero debris + glass fragments)")
    log(f"[VERIFY2]    ground   : static collider by design"
        + (f" ({ground.name})" if ground else ""))
    hover = [s for s in stuck_mesh if "rest_z=" in s
             and float(s.split("rest_z=")[1].rstrip(")")) > HOVER_HEIGHT]
    rest = [s for s in stuck_mesh if s not in hover]
    log(f"[VERIFY2]    NOT moving ({len(stuck_mesh) + len(stuck_emitter)}): "
        f"{len(hover)} hovering mid-air (rest_z > {HOVER_HEIGHT}m) + "
        f"{len(rest)} resting-on-ground + {len(stuck_emitter)} emitters with "
        f"barely-moving particles")
    if hover:
        log(f"[VERIFY2]      -- mid-air hoverers --")
        for s in hover[:25]:
            log(f"[VERIFY2]      {s}")
        if len(hover) > 25:
            log(f"[VERIFY2]      ... and {len(hover) - 25} more")
    if rest:
        log(f"[VERIFY2]      -- resting-on-ground --")
        for s in rest[:15]:
            log(f"[VERIFY2]      {s}")
        if len(rest) > 15:
            log(f"[VERIFY2]      ... and {len(rest) - 15} more")
    if stuck_emitter:
        log(f"[VERIFY2]      -- emitters with barely-moving particles "
            f"({len(stuck_emitter)}) --")
        for s in stuck_emitter[:15]:
            log(f"[VERIFY2]      {s}")
        if len(stuck_emitter) > 15:
            log(f"[VERIFY2]      ... and {len(stuck_emitter) - 15} more")

    x = len(moving)
    log(f"[VERIFY2] SUMMARY: {x}/{y} debris-module objects are moving in "
        f"world space from the first crash point (frame {first_crash})")
    if hover:
        log(f"[VERIFY2][FAIL] {len(hover)} element(s) are hovering "
            f"mid-air (rest_z > {HOVER_HEIGHT}m) and never move")
        return 1
    log(f"[VERIFY2][PASS] no debris is hanging in the air "
        f"({len(hover)} mid-air hoverers)")
    if len(rest) or stuck_emitter:
        log(f"[VERIFY2][WARN] {len(rest)} mesh piece(s) rest on the ground "
            f"(below-blast fragments settling at the impact point) and "
            f"{len(stuck_emitter)} emitter(s) have barely-moving particle "
            f"clouds ({len(stuck_emitter)}/{len(emitters)})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
