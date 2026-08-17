"""Diagnostic — do 200 production shards stay above the ground plane?

    blender --background --python tests/diag_debris_ground_200.py

Self-contained (no .blend needed).  Builds the REAL production debris physics:
the solid PASSIVE/BOX ground (top face exactly at Z=0, zero collision margin),
shard templates cut with the production generator
(:func:`debris_shards.build_shard_library`) from a synthetic panel, 200 hero
shards as ACTIVE/CONVEX_HULL rigid bodies (zero margin) thrown with AGGRESSIVE
velocities through the production kinematic launch handoff
(:func:`debris_physics.configure_rigidbody`), then simulates the solver frame by
frame.

MEASUREMENT ONLY — nothing is corrected.  For every frame the lowest world
vertex of every VISIBLE shard is compared to Z=0.  Two metrics are separated:
the RESTING minimum (the settled tail of the simulation — the meaningful
"does the debris stay off the ground" number) and the overall minimum, which
additionally catches the single-frame overshoot of a high-speed impact (a shard
falling at ~21 m/s tunnels ~30 mm past the surface in one solver frame before
the impulse resolves — transient, not resting).  Verify with your own eyes in
the viewport; this script exists to put numbers on it.
"""

from __future__ import annotations

import os
import sys
from typing import Dict, List

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

for mod in [m for m in list(sys.modules)
            if m.split(".")[0] in ("runtime", "importer", "addon")]:
    del sys.modules[mod]

import bpy
import numpy as np

from runtime.debris_physics import (
    DEBRIS_COLLECTION,
    GROUND_NAME,
    LAUNCH_FRAMES,
    SHARD_COLLECTION,
    DebrisSettings,
    _ensure_ground,
    _ensure_rigidbody_world,
    _frozen_handlers,
    _get_collection,
    _linearise,
    _local_verts,
    configure_rigidbody,
    link_ground_to_rigidbody_world,
)
from runtime.debris_shards import build_shard_library

N_SHARDS = 200
N_FRAMES = 240
SPAWN_Z = 12.0
SPEED = 14.0  # aggressive: faster than any real shed
SEED = 20260817
REST_FROM = N_FRAMES - 40  # settled tail used for the resting metric


def log(msg: str) -> None:
    print(msg, flush=True)


def _synthetic_panel() -> tuple:
    """A flat 2 m x 1 m panel mesh the shard generator can cut from."""
    verts = np.array([
        (-1.0, -0.5, 0.0), (1.0, -0.5, 0.0), (1.0, 0.5, 0.0), (-1.0, 0.5, 0.0),
    ], dtype=np.float64)
    tris = np.array([[0, 1, 2], [0, 2, 3]], dtype=np.int64)
    return verts, tris


def main() -> int:
    scene = bpy.context.scene
    scene.frame_start = 1
    scene.frame_end = N_FRAMES
    scene.frame_current = 1

    # Fresh debris collections.
    for coll in (DEBRIS_COLLECTION, SHARD_COLLECTION):
        existing = bpy.data.collections.get(coll)
        if existing is not None:
            for obj in list(existing.objects):
                bpy.data.objects.remove(obj, do_unlink=True)
            bpy.data.collections.remove(existing)

    settings = DebrisSettings(ground_z=0.0, bounciness=0.25, friction=0.72)

    # 1. Rigid body world + solid ground (top face exactly at Z=0).
    _ensure_rigidbody_world(scene, 1, N_FRAMES)
    ground = _ensure_ground(settings)
    rb_coll = bpy.data.collections.get("RigidBodyWorld")
    link_ground_to_rigidbody_world(ground, rb_coll, settings)
    top = ground.location[2] + ground.dimensions.z / 2.0
    log(f"[DIAG] ground {GROUND_NAME}: PASSIVE/BOX margin=0.0 "
        f"top_face_z={top:.4f}")

    # 2. Shard templates from the PRODUCTION generator.
    shard_coll = _get_collection(SHARD_COLLECTION)
    verts, tris = _synthetic_panel()
    templates = build_shard_library(
        part_name="diag_panel", material="paint",
        verts=verts, tris=tris, variants=8, seed=SEED,
        collection=shard_coll)
    log(f"[DIAG] {len(templates)} shard templates built via build_shard_library")
    if not templates:
        log("[DIAG] FAIL: no shard templates produced")
        return 1

    # 3. Spawn N_SHARDS hero rigid bodies with aggressive launch velocities,
    #    using the production launch handoff (kinematic keys, then release).
    debris_coll = _get_collection(DEBRIS_COLLECTION)
    rng = np.random.default_rng(SEED)
    dt = 1.0 / 60.0
    placed: List[tuple] = []
    for i in range(N_SHARDS):
        tpl = templates[int(rng.integers(0, len(templates)))]
        obj = tpl.copy()
        obj.data = tpl.data
        obj.name = f"diag_debris_{i:03d}"
        jitter = rng.normal(0.0, 1.2, 3)
        spawn_at = np.array([jitter[0], jitter[1], SPAWN_Z + jitter[2]])
        obj.location = tuple(spawn_at)
        obj.rotation_euler = tuple(rng.uniform(0.0, 2.0 * np.pi, 3))
        s = float(rng.uniform(0.7, 1.4))
        obj.scale = (s, s, s)
        debris_coll.objects.link(obj)

        # Aggressive random direction, mostly upward so the fall is long.
        theta = rng.uniform(0.0, 2.0 * np.pi)
        phi = rng.uniform(0.25, 1.15)  # 15-66 deg above horizontal
        vel = SPEED * np.array([
            np.cos(phi) * np.cos(theta),
            np.cos(phi) * np.sin(theta),
            np.sin(phi),
        ])
        launch_start = 4  # all shards launch together
        for k in range(LAUNCH_FRAMES + 1):
            f = launch_start + k
            loc = spawn_at + vel * dt * k
            if loc[2] < settings.ground_z + 0.004:
                loc[2] = settings.ground_z + 0.004
            obj.location = tuple(loc)
            obj.keyframe_insert("location", frame=f)
        _linearise(obj, "location")
        obj.hide_viewport = True
        obj.hide_render = True
        obj.keyframe_insert("hide_viewport", frame=launch_start - 1)
        obj.keyframe_insert("hide_render", frame=launch_start - 1)
        obj.hide_viewport = False
        obj.hide_render = False
        obj.keyframe_insert("hide_viewport", frame=launch_start)
        obj.keyframe_insert("hide_render", frame=launch_start)
        placed.append((obj, launch_start))

    # Phase 2: evaluate, then register the rigid bodies.
    bpy.context.view_layer.update()
    for obj, launch_start in placed:
        rb_coll.objects.link(obj)
        configure_rigidbody(
            obj, settings, mass=0.05, is_blast=True, launch_start=launch_start)

    # 4. Simulate frame by frame, measuring lowest visible vertex per shard.
    local = {o.name: _local_verts(o) for o, _ in placed}
    min_z: Dict[str, float] = {}        # every frame (incl. impact transients)
    rest_min: Dict[str, float] = {}     # the settled tail only
    lowest_overall = float("inf")
    frames_measured = 0

    with _frozen_handlers():
        for f in range(1, N_FRAMES + 1):
            scene.frame_set(f)
            bpy.context.view_layer.update()
            any_visible = False
            frame_lowest = float("inf")
            for obj, launch_start in placed:
                if obj.hide_viewport:
                    continue
                any_visible = True
                co = local.get(obj.name)
                if co is None:
                    continue
                mw = np.asarray(obj.matrix_world, dtype=np.float64)
                zrow = (mw[2][0], mw[2][1], mw[2][2], mw[2][3])
                low = float((co @ zrow[:3] + zrow[3]).min())
                min_z[obj.name] = min(min_z.get(obj.name, low), low)
                if f >= REST_FROM:
                    rest_min[obj.name] = min(rest_min.get(obj.name, low), low)
                frame_lowest = min(frame_lowest, low)
            if any_visible:
                frames_measured += 1
                lowest_overall = min(lowest_overall, frame_lowest)

    # 5. Report.  The resting metric is the meaningful one: debris should come
    #    to rest ON the visible top face (Z=0).  The overall metric additionally
    #    shows the single-frame overshoot of high-speed impacts.
    log(f"[DIAG] simulated {N_FRAMES} frames over {N_SHARDS} shards "
        f"({frames_measured} frames with a visible shard)")
    rest_below = sum(1 for z in rest_min.values()
                     if z < settings.ground_z - 1e-3)
    log(f"[DIAG] REST (frames {REST_FROM}-{N_FRAMES}): shards resting with a "
        f"vertex >1mm below Z=0: {rest_below}/{N_SHARDS}; "
        f"lowest resting vertex: {min(rest_min.values(), default=float('nan')):.6f}")
    for i in range(N_SHARDS):
        z = rest_min.get(f"diag_debris_{i:03d}", None)
        if z is None:
            continue
        log(f"Shard {i:03d}: RESTING minimum Z = {z:.6f}")
    log(f"[DIAG] overall lowest visible vertex anywhere: {lowest_overall:.6f} "
        f"(includes single-frame impact overshoot)")
    log("[DIAG] done (measurement only; nothing corrected)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
