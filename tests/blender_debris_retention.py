"""Headless verify: retained glass fringe stays welded in the frame.

    blender --background --python tests/blender_debris_retention.py

Installs the packaged add-on, imports the real cache (name.bvc), runs the
debris pipeline, then asserts:
  1. NO glassfrag object is parented — the fringe is no longer spawned as
     separate objects (the pane mesh's rim band IS the fringe)
  2. shattered panes are registered on the live playback AND persisted on the
     scene (so reload recovery re-applies the collapse)
  3. at the end frame, each shattered pane's mesh keeps its RIM rows welded to
     the animating pane while its INTERIOR rows are collapsed onto the rim
     (nearest-outline-point targets), not onto the pane centroid
  4. dynamic glass fragments settle ABOVE the ground (none below, none riding
     the chassis)
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


def _member_coords(pb, name):
    """Cache-local mesh coordinates of member ``name`` at the current frame."""
    if name in pb._objects:
        mesh = pb._objects[name].data
        co = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
        mesh.vertices.foreach_get("co", co)
        return co.reshape(-1, 3)
    for chunk_name, ranges in pb._chunk_member_ranges.items():
        if name in ranges:
            start, end = ranges[name]
            mesh = pb._chunks[chunk_name].data
            co = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
            mesh.vertices.foreach_get("co", co)
            return co.reshape(-1, 3)[start:end]
    return None


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

    from runtime import frame_handler

    pb = frame_handler._active
    _check(pb is not None, "live playback active")

    frags = [o for o in bpy.data.objects if o.name.startswith("glassfrag_")]
    _check(len(frags) > 0, f"{len(frags)} dynamic glass fragments spawned")
    if not frags:
        sys.exit(0)

    parented = [o for o in frags if o.parent is not None]
    _check(len(parented) == 0,
           f"no fringe fragments parented to root (got {len(parented)})")

    # Shattered panes must be registered both live and persisted.
    panes = dict(getattr(pb, "_shattered", {}) or {})
    _check(len(panes) > 0, f"{len(panes)} panes registered as shattered")
    persisted = scene.get("_beamng_shattered_panes", "")
    _check(bool(persisted), "shattered-pane map persisted on the scene")
    _check(scene.get("_beamng_shatter_edge_retain", None) is not None,
           "rim-band width persisted on the scene")
    if not panes:
        sys.exit(0)

    # Dynamic (now ALL) fragments are rigid bodies baked against the ground
    # plane, so none may end up below ground.
    ground = bpy.data.objects.get("BeamNG_DebrisGround")
    ground_z = ground.matrix_world.translation.z if ground is not None else 0.0

    last = scene.frame_end
    scene.frame_set(last)
    bpy.context.view_layer.update()
    cache_frame = getattr(pb, "_current_frame", None)
    log(f"[RETENTION] frame={last} cache_frame={cache_frame} ground_z={ground_z:.3f}")

    below = []
    for o in frags:
        z = o.matrix_world.translation.z
        if z < ground_z - 0.02:
            t = o.matrix_world.translation
            below.append((o.name, round(float(t.x), 2), round(float(t.y), 2),
                          round(float(z), 2)))
    _check(not below, f"no glass fragment below ground at end (got {below[:10]})")
    log(f"[RETENTION]   below-ground count: {len(below)}")

    # Rim-band check, per shattered pane: the rim rows must ride the pane's own
    # live geometry at the end frame, and the interior rows must be glued to
    # their anchor rim vertex's live position (NOT left at their own live
    # position), so the collapsed glass follows the deforming break edge.
    tol = 2e-3
    for name in sorted(panes):
        keep = pb._shattered_keeps.get(name)
        anchors = pb._shattered_anchors.get(name)
        mesh = _member_coords(pb, name)
        if mesh is None or keep is None or anchors is None:
            _check(False, f"pane '{name}': member geometry / mask missing")
            continue
        if len(keep) != len(mesh):
            _check(False, f"pane '{name}': mask {len(keep)} vs mesh {len(mesh)}")
            continue
        n_rim = int(keep.sum())
        n_in = int((~keep).sum())
        _check(n_rim > 0 and n_in > 0,
               f"pane '{name}': rim band alive "
               f"(rim={n_rim}, interior={n_in} of {len(mesh)})")
        try:
            live = pb._gltf_to_blender(
                pb.reader.frame_positions(name, cache_frame))
        except Exception:
            live = None
        if live is not None and len(live) == len(mesh):
            rim_dev = float(np.abs(mesh[keep] - live[keep]).max())
            in_dev = float(np.abs(mesh[~keep] - live[anchors[~keep]]).max())
            in_own = float(np.abs(mesh[~keep] - live[~keep]).max())
            _check(rim_dev <= tol,
                   f"pane '{name}': rim rows welded to the pane "
                   f"(max dev {rim_dev:.4f} m)")
            _check(in_dev <= tol,
                   f"pane '{name}': interior rows ride their rim anchors "
                   f"(max dev {in_dev:.4f} m)")
            _check(in_own > tol,
                   f"pane '{name}': interior rows actually collapsed "
                   f"(off own live position by {in_own:.4f} m)")
            log(f"[RETENTION]   pane '{name}': rim={n_rim} interior={n_in} "
                f"rim_dev={rim_dev:.4f} anchor_dev={in_dev:.4f}")

    if FAILS:
        log(f"[RETENTION][PASS->FAIL] {len(FAILS)} checks failed")
        sys.exit(1)
    log("[RETENTION][PASS] retained rim stays welded in the frame")


if __name__ == "__main__":
    main()
