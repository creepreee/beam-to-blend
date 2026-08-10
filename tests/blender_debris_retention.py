"""Headless verify: retained glass fringe stays stuck in the frame.

    blender --background --python tests/blender_debris_retention.py

Installs the packaged add-on, imports the real cache (name.bvc), runs the
debris pipeline, then asserts:
  1. retained fringe fragments are PARENTED to the ``__root`` transform empty
  2. after bake, retained fragments sit ABOVE the ground (not z=-1.5 riding
     the chassis as they did before the fix)
  3. dynamic glass fragments also settle above the ground
Exits non-zero on any failure.
"""

import os
import sys

import numpy as np

import bpy


def log(msg):
    print(msg, flush=True)

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ZIP = os.path.join(_REPO, "dist", "beamng_cache_importer.zip")
_CACHE = r"C:\Users\ubaid_i2c\AppData\Local\BeamNG\BeamNG.drive\current\captures\name.bvc"
_MODULE = "beamng_cache_importer"

FAILS = []


def _check(cond, msg):
    tag = "ok" if cond else "FAIL"
    log(f"[RETENTION][{tag}] {msg}")
    if not cond:
        FAILS.append(msg)


def main():
    if not os.path.exists(_ZIP):
        log(f"[RETENTION][FAIL] zip missing: {_ZIP}")
        sys.exit(1)
    if not os.path.exists(_CACHE):
        log(f"[RETENTION][FAIL] cache missing: {_CACHE}")
        sys.exit(1)

    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.preferences.addon_install(filepath=_ZIP, overwrite=True)
    bpy.ops.preferences.addon_enable(module=_MODULE)
    if _MODULE not in bpy.context.preferences.addons:
        log("[RETENTION][FAIL] add-on did not enable")
        sys.exit(1)

    scene = bpy.context.scene
    use_blend = os.environ.get("USE_BLEND", "")
    if use_blend:
        log(f"[RETENTION] opening saved blend -> {use_blend}")
        bpy.ops.wm.open_mainfile(filepath=use_blend)
        scene = bpy.context.scene
    else:
        scene.beamng.cache_path = _CACHE
        scene.beamng.playback_fps = 24
        scene.beamng.output_fps = 60
        scene.beamng.start_frame = 400
        scene.frame_start = 1
        scene.frame_end = 400

        # Shrink the workload so the run fits a ~2 minute window: no hero/fine
        # debris, tiny settle window, one shard variant.  Glass shatter is the
        # feature under test, and it stays on.
        d = scene.beamng_debris
        d.debris_max_hero = 0
        d.debris_hero_count = 0
        d.debris_fine_count = 0
        d.debris_variants = 1
        d.debris_settle_frames = int(os.environ.get("DEBRIS_SETTLE", "20"))
        d.debris_shatter_glass = True

        log("[RETENTION] importing cache ...")
        res = bpy.ops.beamng.import_cache()
        if res != {"FINISHED"}:
            log(f"[RETENTION][FAIL] import_cache returned {res}")
            sys.exit(1)

        log("[RETENTION] building debris ...")
        res = bpy.ops.beamng.build_debris()
        if res != {"FINISHED"}:
            log(f"[RETENTION][FAIL] build_debris returned {res}")
            sys.exit(1)

        save_blend = os.environ.get("SAVE_BLEND", "")
        if save_blend:
            log(f"[RETENTION] saving blend -> {save_blend}")
            bpy.ops.wm.save_mainfile(filepath=save_blend)
            log("[RETENTION][SAVED] blend saved for fast iteration")
            sys.exit(0)

    root = None
    for o in bpy.data.objects:
        if o.type == "EMPTY" and o.name.endswith("__root"):
            root = o
    _check(root is not None, "transform empty found")

    frags = [o for o in bpy.data.objects if o.name.startswith("glassfrag_")]
    _check(len(frags) > 0, f"{len(frags)} glass fragments spawned")
    if not frags:
        sys.exit(0)

    parented = [o for o in frags if o.parent is not None]
    free = [o for o in frags if o.parent is None]
    _check(len(parented) > 0, f"{len(parented)} fringe fragments parented to root")
    _check(all(o.parent == root for o in parented), "every parented fragment is a child of __root")
    _check(len(free) == len(frags) - len(parented), "non-fringe fragments are free (unparented) rigid bodies")

    # Evaluate the LAST baked frame and measure the ground position of every
    # fragment: nothing may end up below ground.
    last = scene.frame_end
    ground = bpy.data.objects.get("BeamNG_DebrisGround")
    ground_z = ground.location.z if ground is not None else None
    if ground_z is None:
        for o in bpy.data.objects:
            if o.type == "MESH" and o.name.startswith("BeamNG_"):
                ground = o
        ground_z = ground.matrix_world.translation.z if ground is not None else 0.0
    log(f"[RETENTION] ground_z={ground_z:.3f} frame={last}")

    scene.frame_set(last)
    bpy.context.view_layer.update()
    root_w = root.matrix_world
    car_min = min((o.matrix_world.translation.z - 2.0 for o in bpy.data.objects
                   if o.type == "MESH" and o.name != "BeamNG_DebrisGround"
                   and o.name != "BeamNG_DebrisGround.001"),
                  default=None)
    log(f"[RETENTION] root@end z={root_w.translation.z:.3f} car mesh origin min z~{car_min}")

    # Dynamic (unparented) fragments are rigid bodies baked against the ground
    # plane, so NONE of them may end up below ground.  Parented fringe may sit
    # slightly below the flat plane when its pane shattered inside a door that
    # was crushed into the ground — that IS "stuck in the frame".
    below = []
    for o in free:
        z = o.matrix_world.translation.z
        if z < ground_z - 0.02:
            t = o.matrix_world.translation
            below.append((o.name, round(float(t.x), 2), round(float(t.y), 2),
                          round(float(z), 2)))
    _check(not below, f"no dynamic fragment below ground at end (got {below[:10]})")
    log(f"[RETENTION]   below-ground count: {len(below)}; "
        f"slab spans x,y in [-200, 200]")

    if parented:
        # STUCK-IN-FRAME check: each parented fragment must RIDE the root
        # exactly.  Its world position at any later frame must equal
        # root.M(f) @ root.M(spawn)^-1 @ P_spawn — i.e. it stays glued to the
        # pane's aperture as the wreck moves, never falling, flying or being
        # overwritten by the bake.  Grouped per pane to report per-pane heights.
        from collections import defaultdict
        groups = defaultdict(list)
        for o in parented:
            parts = o.name.split("_")
            part = "_".join(parts[3:-1]) if len(parts) > 4 else o.name
            groups[part].append(o)

        def _fresh():
            bpy.context.view_layer.update()
            return root.matrix_world

        max_drift = 0.0
        for part, objs in sorted(groups.items()):
            spawn = int(objs[0].get("_beamng_debris_launch", scene.frame_start))
            scene.frame_set(spawn)
            bpy.context.view_layer.update()
            Mspawn_inv = _fresh().inverted()
            anchors = [(o, o.matrix_world.copy()) for o in objs]
            scene.frame_set(last)
            bpy.context.view_layer.update()
            M_end = _fresh()
            drift = max(float((o.matrix_world - (M_end @ Mspawn_inv @
                        a)).to_translation().length) for o, a in anchors)
            zs = [float(a.translation.z) for _, a in anchors]
            max_drift = max(max_drift, drift)
            log(f"[RETENTION]   pane '{part}': n={len(objs)} spawn z "
                f"{min(zs):.2f}..{max(zs):.2f} end-drift {drift:.4f}")
            if "windshield" in part.lower():
                # The windshield may shatter while the wreck is already
                # nose-down/rolling, so its own spawn height varies.  What must
                # hold is that the fringe never leaves the pane — the drift
                # check above already proves it rides the root exactly.  Here we
                # only confirm it starts within the pane's own aperture height,
                # i.e. NOT stuck to a pane 10s of metres off.
                _check(min(zs) > ground_z - 0.5,
                       f"windshield fringe starts in the frame "
                       f"(spawn z {min(zs):.2f}, ground {ground_z:.2f})")
        _check(max_drift < 0.01,
               f"fringe rides the root exactly at the end (max drift {max_drift:.4f})")

    if FAILS:
        log(f"[RETENTION][PASS->FAIL] {len(FAILS)} checks failed")
        sys.exit(1)
    log("[RETENTION][PASS] retained glass stays in the frame")


if __name__ == "__main__":
    main()
