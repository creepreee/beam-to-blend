from __future__ import annotations

"""Freeze the finished debris simulation into permanent animation data.

Owns the conversion of a finished rigid-body simulation into native F-curve
keyframes — and ONLY that.  The simulation itself is owned by the Bullet
solver (set up in :mod:`debris_physics`), and the objects it bakes are the
ones spawned in :mod:`debris_spawn`.  This module has no spawning logic and no
physics configuration.

BAKE MECHANISM.  The conversion samples matrices BY HAND: the solver is
stepped frame-by-frame with ``scene.frame_set`` (the depsgraph evaluates the
rigid-body world for each frame), each body's world matrix is captured,
committed as native ``location`` + ``rotation_euler`` F-curves, and the next
frame steps on.  The rigid body world is removed afterwards, leaving the
debris as inert animation data.  No operator, no selection, no viewport
context, and no keying-set machinery is involved.

WHY NOT THE OPERATOR.  ``rigidbody.bake_to_keyframes`` crashed Blender
mid-bake on the full multi-thousand-frame range, and it additionally requires
*selected* ACTIVE rigid bodies — a selection made while a body is hidden (or
one that re-hides when the bake steps to a frame before its launch frame)
makes the operator silently bake nothing or fail its keying-set lookup.  The
manual loop is behaviourally identical (same per-frame solver evaluation, same
per-frame world-matrix capture) with none of that machinery.

VISIBILITY.  Debris is invisible before its launch frame (the spawner keys
``hide_viewport``/``hide_render`` so each shard pops in at its impact).  Those
curves are MUTED for the bake — a hidden ACTIVE object breaks keying — and the
base visibility is cleared, so the stepping loop can see every body.  Unmuting
afterwards restores the hide-until-launch behaviour.  The bake only writes
``location``/``rotation_euler``; the visibility curves are preserved.

PARTICLES.  The NEWTON particle emitters stay LIVE and are deliberately NOT
baked (the retime module rescales their emission windows with the playback
sliders).  They are hidden for the duration of the bake so their particle
systems do not simulate on every stepped frame, and re-shown afterwards.
"""

from typing import List, Optional, Sequence

try:  # pragma: no cover - only inside Blender
    import bpy
except ImportError:  # pragma: no cover
    bpy = None

import numpy as np
import sys

from .debris_physics import (
    LAUNCH_FRAMES,
    _frozen_handlers,
    _local_verts,
    _viewport_context,
)


def _hide_curves(obj: "bpy.types.Object") -> list:
    """The ``hide_viewport``/``hide_render`` F-curves of ``obj``, if any."""
    ad = obj.animation_data
    if ad is None or ad.action is None:
        return []
    return [fcu for fcu in ad.action.fcurves
            if fcu.data_path in ("hide_viewport", "hide_render")]


def _lowest_world_z(obj: "bpy.types.Object",
                    frame: int) -> Optional[float]:
    """Lowest world-space vertex of ``obj`` at ``frame`` (float64).

    Returns ``None`` when the object has no mesh data.  Used ONLY for the
    post-bake ground-penetration REPORT — never to correct anything.
    """
    if bpy is None or obj.data is None:
        return None
    co = _local_verts(obj)
    if co is None:
        return None
    mw = np.asarray(obj.matrix_world, dtype=np.float64)
    zrow = (mw[2][0], mw[2][1], mw[2][2], mw[2][3])
    return float((co @ np.asarray(zrow[:3], dtype=np.float64) + zrow[3]).min())


def bake_debris(hero_objects: Sequence["bpy.types.Object"],
                frame_start: int, frame_end: int,
                ground_z: float = 0.0) -> dict:
    """Bake the finished Bullet simulation into native F-curves.

    Runs with all frame handlers detached (see :func:`_frozen_handlers`).  The
    solver is stepped frame-by-frame with ``scene.frame_set`` and every body's
    world matrix is written out by hand as native ``location``/
    ``rotation_euler`` F-curves; the rigid body world is then removed — the
    debris afterwards is inert animation data the playback handler cannot
    disturb.

    BAKE SCOPE.  Every intended body in ``hero_objects`` is keyed every frame,
    and afterwards every intended body is verified to have received
    ``location`` + ``rotation_euler`` curves.  The result dict reports the
    intended vs. actually-baked counts and any skipped names.

    GROUND REPORT.  The bake never corrects anything.  It only MEASURES: at the
    final baked frame every body's lowest world vertex is compared to
    ``ground_z`` and any penetration is reported to stderr (per body, with the
    depth) and counted in the result dict.  If a visible vertex sits below the
    ground this is surfaced, never hidden.

    Returns a summary dict for the operator report:
    ``{"baked", "intended", "skipped", "frames", "penetrating",
    "max_penetration"}``.
    """
    if bpy is None or not hero_objects:
        return {"baked": 0, "intended": 0, "skipped": [],
                "frames": 0, "penetrating": 0, "max_penetration": 0.0}

    scene = bpy.context.scene
    alive = [o for o in hero_objects
             if o.name in bpy.data.objects
             and getattr(o, "type", "") == "MESH"
             and o.rigid_body is not None]
    if not alive:
        return {"baked": 0, "intended": 0, "skipped": [],
                "frames": 0, "penetrating": 0, "max_penetration": 0.0}

    frame_start = int(frame_start)
    frame_end = int(frame_end)
    # Simulate from a few frames before the earliest launch so the solver owns
    # the velocity transfer (same anchor the old stepping used); the operator
    # writes keys over its whole stepping range.
    sim_start = int(max(scene.frame_start, frame_start - LAUNCH_FRAMES - 2))
    frame_orig = scene.frame_current

    with _frozen_handlers():
        if scene.rigidbody_world is not None:
            scene.rigidbody_world.point_cache.frame_start = sim_start
            scene.rigidbody_world.point_cache.frame_end = frame_end

        # Euler rotation mode: the operator bakes with the object's current
        # rotation mode, so forcing an Euler mode (XYZ, not the invalid
        # "EULER" string) guarantees rotation_euler curves for every body
        # regardless of how the spawner set it up.
        for obj in alive:
            obj.rotation_mode = "XYZ"

        # --- suspend launch visibility + emitters for the bake ---------------
        # Debris is invisible before its launch frame: the spawner keys
        # ``hide_viewport``/``hide_render`` so each shard pops in at its
        # impact.  That visibility must be suspended while the solver steps,
        # or every body re-hides the moment the playhead moves to a frame
        # before its launch.  So the base visibility is cleared and the
        # visibility F-curves are MUTED for the duration of the bake; the bake
        # only writes location / rotation_euler curves, so unmuting afterwards
        # restores the hide-until-launch behaviour for playback.
        #
        # The fine-particle emitters are the opposite: they stay LIVE (never
        # baked), but every stepped frame makes the depsgraph simulate their
        # NEWTON particle systems too — hundreds of extra solver evaluations
        # per frame that only slow the bake and add crash surface.  They are
        # hidden for the bake (base True + muted curves so the keys that would
        # re-show them at their spawn frames cannot fire) and restored after.
        muted: list = []
        for obj in alive:
            obj.hide_viewport = False
            obj.hide_render = False
            for fcu in _hide_curves(obj):
                if not fcu.mute:
                    fcu.mute = True
                    muted.append(fcu)
        for obj in bpy.data.objects:
            if obj.name.startswith("debris_emit_"):
                obj.hide_viewport = True
                for fcu in _hide_curves(obj):
                    if not fcu.mute:
                        fcu.mute = True
                        muted.append(fcu)
        bpy.context.view_layer.update()

        # --- phase 1: step the solver and capture every body per frame --------
        # ``scene.frame_set`` makes the depsgraph evaluate the rigid-body world
        # for that frame; each body's world matrix is captured (kinematic
        # bodies hold their spawner-keyed launch pose, ACTIVE bodies hold the
        # solver pose).  Only matrices are recorded here — writing keys per
        # frame is O(keys²) per curve and was the wall-clock cost that made the
        # old operator bake time out.  The F-curves are written in one bulk
        # pass afterwards (phase 2).
        #
        # ``rigidbody.bake_to_keyframes`` crashed Blender on the full
        # multi-thousand-frame range, and it also drags in operator/context
        # machinery (selection, hidden objects, keying sets) that fails without
        # a valid 3D-viewport context.  This manual capture needs none of it.
        n_frames = frame_end - sim_start + 1
        bakes: dict = {obj: np.empty((n_frames, 6), dtype=np.float64)
                       for obj in alive}
        try:
            for i, f in enumerate(range(sim_start, frame_end + 1)):
                scene.frame_set(f)
                for obj in alive:
                    m = obj.matrix_world
                    loc = m.to_translation()
                    rot = m.to_euler("XYZ")
                    row = bakes[obj][i]
                    row[0], row[1], row[2] = loc.x, loc.y, loc.z
                    row[3], row[4], row[5] = rot.x, rot.y, rot.z
                if i % 200 == 0 or i == n_frames - 1:
                    sys.stderr.write(
                        f"[BeamNG] debris sim {i + 1}/{n_frames} "
                        f"(frame {f})\n")
                    sys.stderr.flush()
                    bpy.context.view_layer.update()
        finally:
            for fcu in muted:
                fcu.mute = False

        # --- phase 2: write the F-curves in one bulk pass ---------------------
        # Replace the spawner's location/rotation keys with the captured range
        # as LINEAR keyframes (the honest interpolation between recorded
        # samples; the old operator produced the same).  One fcurve per
        # (path, index), filled from the numpy arrays in a single sweep.
        for obj in alive:
            data = bakes[obj]
            ad = obj.animation_data
            if ad is None:
                ad = obj.animation_data_create()
            if ad.action is None:
                ad.action = bpy.data.actions.new(obj.name + "Action")
            action = ad.action
            for path, ncomp in (("location", 3), ("rotation_euler", 3)):
                for idx in range(ncomp):
                    fcu = action.fcurves.find(path, index=idx)
                    if fcu is None:
                        fcu = action.fcurves.new(path, index=idx)
                    else:
                        fcu.keyframe_points.clear()
                    fcu.keyframe_points.add(n_frames)
                    pts = fcu.keyframe_points
                    for i in range(n_frames):
                        kp = pts[i]
                        kp.co = (float(sim_start + i), float(data[i, idx]))
                        kp.interpolation = "LINEAR"
                    fcu.keyframe_points.update()

        # Remove the baked bodies from the simulation and drop the world + the
        # ground's rigid body (the bake operator used to do this itself).
        rbw = scene.rigidbody_world
        if rbw is not None:
            with _viewport_context():
                bpy.ops.object.select_all(action="DESELECT")
                for obj in alive:
                    obj.hide_viewport = False
                    obj.select_set(True)
                if alive:
                    bpy.context.view_layer.objects.active = alive[0]
                try:
                    bpy.ops.rigidbody.objects_remove()
                except Exception:
                    pass
            with _viewport_context():
                try:
                    bpy.ops.rigidbody.world_remove()
                except Exception:
                    scene.rigidbody_world = None

        # --- verify bake scope ----------------------------------------------
        # Every intended body must carry native location + rotation_euler
        # F-curves.  Anything missing is reported, not silently accepted.
        baked: List[str] = []
        skipped: List[str] = []
        for obj in alive:
            paths = set()
            ad = obj.animation_data
            if ad is not None and ad.action is not None:
                paths = {fc.data_path for fc in ad.action.fcurves}
            if {"location", "rotation_euler"} <= paths:
                baked.append(obj.name)
            else:
                skipped.append(obj.name)

        # --- ground penetration REPORT (no correction) ----------------------
        scene.frame_set(frame_end)
        bpy.context.view_layer.update()
        penetrating: List[tuple] = []
        for obj in alive:
            low = _lowest_world_z(obj, frame_end)
            if low is not None and low < float(ground_z):
                penetrating.append((obj.name, low))
        penetrating.sort(key=lambda t: t[1])
        for name, z in penetrating:
            sys.stderr.write(
                f"[BeamNG] DEBRIS GROUND PENETRATION: {name} lowest vertex "
                f"z={z:.6f} (below ground_z={float(ground_z)})\n")
            sys.stderr.flush()
        max_pen = max((-z for _, z in penetrating), default=0.0)

        scene.frame_set(frame_orig)

    return {
        "baked": len(baked),
        "intended": len(alive),
        "skipped": skipped,
        "frames": frame_end - sim_start + 1,
        "penetrating": len(penetrating),
        "max_penetration": max_pen,
    }
