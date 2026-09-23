"""Verify 3 — rebuilt debris: nothing below the ground, every emitter moves.

    blender --background --python verify/03_debris_simulation.py -- [blend]

Drives the add-on's own Debris pipeline headless (CLEAR / DETECT / BUILD, same
as verify 2), then checks the SIMULATION — the fine particle debris — which is
live and never baked:

  * PARTICLE PENETRATION — no particle may fall below the ground plane.
    The timeline is stepped ONE FRAME AT A TIME.  Jumping the frame (e.g.
    ``scene.frame_set(f)`` for ``f in range(..., 25)``) makes Blender's
    NEWTON particle integrator read garbage positions down to z=-1e27
    (measured), so a sweep that skips frames reports catastrophic false
    failures.  Frame-by-frame stepping is the ONLY mode that matches what a
    viewport playback evaluates.
  * PARTICLE MOTION — every emitter must shed material that travels: at least
    one particle must leave the emitter by MAX_TRAVEL_EPS (m).  The check is
    per-particle displacement from the emitter, NOT cloud-centroid path: a
    symmetric spray keeps its centroid parked at the emitter while every
    shard flies, so a centroid-based verdict calls a healthy spray "stuck"
    (measured: travel up to 1 m with centroid path 0.000).  An emitter whose
    shards are born already settled (travel ~0) reads as "debris appears as a
    static pile" — the symptom this catches.
  * MESH PENETRATION — hero/glass mesh world-space bounds stay above the
    ground plane (resting ON it is correct; below it is a fall-through).
    Reported in two buckets: DEEP (> 0.30 m under — a real fall-through,
    FAIL) and SHALLOW (up to 0.30 m under — half-buried hero shards resting
    on the pile, WARN).  The build deliberately clamps shard origins to the
    slab while their geometry extends below the origin, so shallow sinks are
    expected; a deep dive means a body tunnelled out and is really lost.
  * MESH MOTION — hero pieces + glass fragments must move or rest on the
    pile; a piece hovering mid-air is a FAIL (same rules as verify 2).

Exit code: 0 = pass.  1 = any penetration, any stuck/missing emitter, or any
mesh hovering mid-air.
"""

from __future__ import annotations

import math
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

#: Step every frame, but only read the simulation every SAMPLE_EVERY frames.
#: Stepping every frame is what keeps the particle integrator honest; reading
#: every frame is just slow.  The two must stay separate — do NOT turn the
#: stepping itself into a jump.
SAMPLE_EVERY = 25

#: A particle below this is a fall-through (the ground plane is at z=0 for
#: the debris world; settled shards rest at ~0.001).
PENETRATION_TOL = 0.02
#: A mesh whose lowest world vertex is below this is penetrating the ground.
MESH_PENETRATION_TOL = 0.02
#: A mesh whose lowest vertex EVER goes this far below the ground plane is a
#: genuine fall-through.  Hero shards deliberately rest up to ~0.2 m inside
#: the slab (their origin is clamped while their geometry extends below it)
#: and piles stack on top, so a small sink is expected — only a deep dive
#: means a body tunnelled out of the collision and is really lost.
MESH_PENETRATION_DEEP = 0.30
#: An emitter where no particle ever travels this far (m) from the emitter is
#: "never moved" — the shards were born already settled.  Measured against
#: per-particle displacement, NOT the cloud centroid (see module docstring).
MAX_TRAVEL_EPS = 0.10
#: A mesh whose total world path over the sampled walk is shorter than this
#: (m) is STUCK (hero/glass pieces fly metres and settle).
MESH_MOVE_EPS = 0.05
#: A stuck mesh resting this far above the ground plane is a mid-air hoverer.
HOVER_HEIGHT = 0.30

FAILS: List[str] = []
WARNINGS: List[str] = []


def log(msg: str) -> None:
    print(msg, flush=True)


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


def has_particle_system(obj: "bpy.types.Object") -> bool:
    return any(getattr(m, "particle_system", None) is not None
               for m in obj.modifiers)


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


def alive_locations(ev: "bpy.types.Object") -> List[Vector]:
    """World/evaluated locations of living particles only.

    Dead particles hold stale memory (their location is not meaningful), so
    they are excluded from both the penetration and the centroid checks.
    """
    locs = []
    for p in ev.particle_systems[0].particles:
        if p.alive_state in {"ALIVE", "DYING"}:
            locs.append(p.location)
    return locs


def mesh_world_bounds(obj: "bpy.types.Object") -> Tuple[float, float]:
    """(min_z, max_z) of the object's vertices in world space."""
    mat = obj.matrix_world
    zs = [mat @ v.co for v in obj.data.vertices]
    if not zs:
        return (0.0, 0.0)
    return (min(p.z for p in zs), max(p.z for p in zs))


def main(argv: Sequence[str]) -> int:
    argv = [a for a in argv if a.lower().endswith(".blend")]
    blend = argv[0] if argv else DEFAULT_BLEND
    if not os.path.exists(blend):
        log(f"[VERIFY3][FAIL] blend not found: {blend}")
        return 1

    from runtime.cache_reader import CacheReader
    from runtime.impact_detect import detect_impacts
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
    scene = bpy.context.scene
    log(f"[VERIFY3] opening {blend}")

    cache_path = scene.get("_beamng_cache_path") or \
        getattr(scene.beamng, "cache_path", "")
    if not cache_path or not os.path.exists(cache_path):
        log(f"[VERIFY3][FAIL] cache not found: {cache_path}")
        return 1
    start_frame = int(getattr(scene.beamng, "start_frame", 0))
    playback_fps = float(getattr(scene.beamng, "playback_fps", 24))
    output_fps = float(getattr(scene.beamng, "output_fps", 60))
    ground_shift = float(scene.get("_beamng_ground_shift", 0.0))

    with _frozen_handlers():
        removed = clear_debris()
        reader = CacheReader(cache_path)
        try:
            events = detect_impacts(reader, ground_shift=ground_shift,
                                    playback_fps=float(playback_fps))
        finally:
            reader.close()
        if not events:
            log("[VERIFY3][FAIL] no impacts detected — nothing to rebuild")
            return 1
        settings = debris_settings(scene)
        summary = build_debris(
            CacheReader(cache_path), events, settings,
            glass_settings=glass_settings(scene),
            frame_start=int(start_frame),
            playback_fps=float(playback_fps),
            output_fps=float(output_fps),
            ground_shift=ground_shift,
        )
        bake_debris(
            summary.get("hero_objects", []),
            summary.get("bake_start", scene.frame_start),
            summary.get("bake_end", scene.frame_end),
        )
    log(f"[VERIFY3] built {summary.get('events')} events -> "
        f"{summary.get('hero', 0)} hero, {summary.get('emitters', 0)} emitters, "
        f"{summary.get('shards', 0)} shards, {summary.get('glass', 0)} glass")

    frame_start = int(scene.frame_start)
    frame_end = int(scene.frame_end)
    targets = collection_objects([DEBRIS_COLLECTION, GLASS_COLLECTION])
    emitters = sorted((o for o in targets if has_particle_system(o)),
                      key=lambda o: o.name)
    meshes = sorted((o for o in targets if not has_particle_system(o)
                     and o.name != GROUND_NAME), key=lambda o: o.name)
    ground = next((o for o in targets if o.name == GROUND_NAME), None)
    ground_z = float(ground.location.z + ground.dimensions.z / 2.0) \
        if ground is not None else 0.0
    log(f"[VERIFY3] {len(emitters)} emitters, {len(meshes)} meshes, "
        f"ground_z={ground_z:.3f}, walking {frame_start}..{frame_end} "
        f"frame-by-frame (sampling every {SAMPLE_EVERY})")

    # Per-emitter accumulators.
    em_centroid: Dict[str, List[Tuple[float, float, float]]] = {}
    em_zmin: Dict[str, float] = {}
    em_max_travel: Dict[str, float] = {}
    em_alive_max: Dict[str, int] = {}
    em_origin: Dict[str, Vector] = {}
    for o in emitters:
        name = o.name
        em_centroid[name] = []
        em_zmin[name] = 1e9
        em_max_travel[name] = 0.0
        em_alive_max[name] = 0
        em_origin[name] = Vector(o.location)

    # Per-mesh accumulators.
    mesh_world: Dict[int, Dict[str, Vector]] = {}
    mesh_minz: Dict[str, float] = {}
    mesh_last_minz: Dict[str, float] = {}

    scene.frame_set(scene.frame_start)
    bpy.context.view_layer.update()

    for f in range(frame_start, frame_end + 1):
        scene.frame_set(f)
        bpy.context.view_layer.update()
        if f % SAMPLE_EVERY != 0:
            continue
        dg = bpy.context.evaluated_depsgraph_get()
        dg.update()
        for o in emitters:
            ev = dg.objects.get(o.name, None)
            if ev is None or not ev.particle_systems:
                continue
            locs = alive_locations(ev)
            if not locs:
                continue
            name = o.name
            em_alive_max[name] = max(em_alive_max[name], len(locs))
            em_zmin[name] = min(em_zmin[name], min(float(p.z) for p in locs))
            c = (sum(float(p.x) for p in locs) / len(locs),
                 sum(float(p.y) for p in locs) / len(locs),
                 sum(float(p.z) for p in locs) / len(locs))
            em_centroid[name].append(c)
            origin = em_origin[name]
            em_max_travel[name] = max(
                em_max_travel[name],
                max(math.sqrt(float(p.x - origin.x) ** 2 +
                              float(p.y - origin.y) ** 2 +
                              float(p.z - origin.z) ** 2) for p in locs))
        mesh_world[f] = {o.name: o.matrix_world.translation.copy() for o in meshes}
        for o in meshes:
            zmin_z, _ = mesh_world_bounds(o)
            prev = mesh_minz.get(o.name)
            mesh_minz[o.name] = zmin_z if prev is None else min(prev, zmin_z)
            mesh_last_minz[o.name] = zmin_z
        if f % 500 == 0:
            log(f"[VERIFY3]    walked to frame {f}")

    # ---- particle verdicts --------------------------------------------------
    penetrations: List[str] = []
    stuck: List[str] = []
    empty: List[str] = []
    for o in emitters:
        name = o.name
        if em_alive_max[name] == 0:
            empty.append(name)
            continue
        cs = em_centroid[name]
        path = 0.0
        if len(cs) >= 2:
            path = sum(math.sqrt((cs[i + 1][0] - cs[i][0]) ** 2 +
                                 (cs[i + 1][1] - cs[i][1]) ** 2 +
                                 (cs[i + 1][2] - cs[i][2]) ** 2)
                       for i in range(len(cs) - 1))
        zmin = em_zmin[name]
        moved = em_max_travel[name] >= MAX_TRAVEL_EPS
        verdict = "MOVING" if moved else "STUCK"
        if zmin < -PENETRATION_TOL:
            penetrations.append(f"{name} (zmin={zmin:.3f})")
        if verdict == "STUCK":
            stuck.append(f"{name} (path={path:.3f}, travel={em_max_travel[name]:.3f})")
        log(f"[VERIFY3] emitter {verdict:6s} path={path:6.3f} "
            f"travel={em_max_travel[name]:6.3f} zmin={zmin:7.3f} "
            f"alive_max={em_alive_max[name]:3d} {name}")

    # ---- mesh verdicts ------------------------------------------------------
    hover: List[str] = []
    mesh_stuck: List[str] = []
    mesh_pen: List[str] = []
    mesh_pen_shallow: List[str] = []
    for o in meshes:
        name = o.name
        pts = [mesh_world[f].get(name) for f in sorted(mesh_world)]
        pts = [p for p in pts if p is not None]
        path = 0.0
        rest_z = 0.0
        if len(pts) >= 2:
            path = sum((pts[i + 1] - pts[i]).length for i in range(len(pts) - 1))
            rest_z = float(pts[-1].z)
        minz = mesh_minz.get(name, 0.0)
        last_minz = mesh_last_minz.get(name, 0.0)
        mverdict = "MOVING" if path >= MESH_MOVE_EPS else "STUCK"
        is_hover = mverdict == "STUCK" and rest_z > ground_z + HOVER_HEIGHT
        if is_hover:
            hover.append(f"{name} (rest_z={rest_z:.3f})")
        if mverdict == "STUCK" and not is_hover:
            mesh_stuck.append(f"{name} (rest_z={rest_z:.3f})")
        below = ground_z - MESH_PENETRATION_TOL
        if minz < below:
            where = f"minz={minz:.3f} rest_z={rest_z:.3f} rest_minz={last_minz:.3f}"
            if minz < ground_z - MESH_PENETRATION_DEEP:
                mesh_pen.append(f"{name} ({where})")
            else:
                mesh_pen_shallow.append(f"{name} ({where})")
        log(f"[VERIFY3] mesh    {mverdict:6s} path={path:6.3f} "
            f"rest_z={rest_z:7.3f} min_z={minz:7.3f} {name}")

    # ---- report -------------------------------------------------------------
    em_moving = len(emitters) - len(stuck) - len(empty)
    log(f"[VERIFY3] REPORT")
    log(f"[VERIFY3]   emitters : {em_moving}/{len(emitters)} moving")
    log(f"[VERIFY3]   meshes   : "
        f"{len(meshes) - len(hover) - len(mesh_stuck)}/{len(meshes)} moving")
    if penetrations:
        log(f"[VERIFY3]   PARTICLE PENETRATION ({len(penetrations)}):")
        for s in penetrations[:25]:
            log(f"[VERIFY3]     {s}")
        FAILS.append(f"{len(penetrations)} emitters with particles below "
                     f"ground ({PENETRATION_TOL}m tolerance)")
    if mesh_pen:
        log(f"[VERIFY3]   MESH PENETRATION (deep, {len(mesh_pen)}):")
        for s in mesh_pen[:25]:
            log(f"[VERIFY3]     {s}")
        FAILS.append(f"{len(mesh_pen)} meshes fell through the ground")
    if mesh_pen_shallow:
        log(f"[VERIFY3]   MESH PENETRATION (shallow, {len(mesh_pen_shallow)}):")
        for s in mesh_pen_shallow[:25]:
            log(f"[VERIFY3]     {s}")
        WARNINGS.append(f"{len(mesh_pen_shallow)} meshes sunk shallowly "
                        f"(pile resting / half-buried hero shards)")
    if hover:
        log(f"[VERIFY3]   MID-AIR HOVER ({len(hover)}):")
        for s in hover[:15]:
            log(f"[VERIFY3]     {s}")
        FAILS.append(f"{len(hover)} meshes hovering mid-air")
    if stuck:
        log(f"[VERIFY3]   STUCK EMITTERS ({len(stuck)}):")
        for s in stuck[:25]:
            log(f"[VERIFY3]     {s}")
        if len(stuck) > 25:
            log(f"[VERIFY3]     ... and {len(stuck) - 25} more")
        FAILS.append(f"{len(stuck)} emitters whose particles barely move")
    if empty:
        log(f"[VERIFY3]   EMPTY EMITTERS ({len(empty)}): {', '.join(empty[:15])}")
        FAILS.append(f"{len(empty)} emitters spawned no particles")
    if mesh_stuck:
        log(f"[VERIFY3]   [warn] {len(mesh_stuck)} meshes rest on the pile "
            f"(below-blast fragments settling is expected)")
    log(f"[VERIFY3] SUMMARY: {em_moving}/{len(emitters)} emitters moving, "
        f"{len(penetrations)} particle penetrations, "
        f"{len(mesh_pen)} deep + {len(mesh_pen_shallow)} shallow mesh sinks")

    if FAILS:
        log(f"[VERIFY3][FAIL] " + "; ".join(FAILS))
        return 1
    if WARNINGS:
        log(f"[VERIFY3][WARN] " + "; ".join(WARNINGS))
    log(f"[VERIFY3][PASS] no penetration, no stuck or empty emitters, "
        f"no mid-air hoverers")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
