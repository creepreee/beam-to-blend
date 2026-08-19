from __future__ import annotations

"""Move the Mantaflow fluid effector onto the low-poly proxy mesh.

Why this exists
---------------
Mantaflow bakes its domain on a **background thread**.  During the bake it
steps through frames, firing ``frame_change_pre``.  Our cache playback
drives the car meshes from that handler via ``foreach_set`` on the mesh
position attribute — and that call posts a WM notifier, which is not
thread-safe from a job thread.  The result is an
``EXCEPTION_ACCESS_VIOLATION`` in ``note_cmp_for_queue_fn`` the moment the
bake's first frame-change handler runs (see the user's crash log).

The heavy fix (drive the full-resolution chunked meshes natively) is
expensive.  But the *fluid effector* doesn't need 550K verts — it needs a
collision surface that Mantaflow can read on its own thread.  This module
gives it exactly that:

1.  The proxy mesh (already built for viewport purposes, <= 1000 verts) is
    the effector surface.
2.  Its per-frame vertex positions are baked to a ``.mdd`` point-cache file
    and attached as a ``MESH_CACHE`` modifier.  A ``MESH_CACHE`` modifier is
    evaluated **natively by the depsgraph** — no Python handler involved —
    so Mantaflow reads the proxy's full deformation on the bake thread with
    zero crash risk.
3.  The rigid transform is baked into keyframes on a **separate** Empty
    (``BeamNG_Proxy__Rigid``) that parents ONLY the proxy — never the real
    transform Empty.  Keyframing the real Empty would give it F-curves, and
    the depsgraph would then evaluate it every frame (smooth interpolation),
    overriding the frame handler's cache-frame-rate ``matrix_basis`` writes
    and desyncing the car's motion from its vertices.  The separate empty is
    parented to nothing, so its keyframes are pure world transforms and the
    real chunked meshes are untouched.
4.  The FLUID modifier is removed from the heavy chunked meshes (body /
    fenders_bumpers / trunk) and added to the proxy instead — one lightweight
    collision surface, no per-frame Python mesh writes during the bake.

The viewport preview is unaffected: normal playback still drives everything
via ``frame_change_pre``.  This module only *prepares* for a fluid bake; it
does not run during one.
"""

import os
import sys
import tempfile
from typing import List, Optional

import numpy as np

try:  # pragma: no cover - only inside Blender
    import bpy
    import mathutils
except ImportError:  # pragma: no cover
    bpy = None
    mathutils = None

from .baker import MddWriter
from .proxy_mesh import _PROXY_NAME, create_proxy, gather_proxy_positions, has_proxy

#: Objects whose FLUID modifiers we take over.  Anything else with a FLUID
#: modifier is left alone.
_EFFECTOR_SOURCE_NAMES = ("body", "fenders_bumpers", "trunk")

#: Separate world-space Empty that carries the baked rigid-transform keyframes
#: for the proxy ONLY.  Keyframing the *real* transform Empty would give it
#: F-curves, and Blender's depsgraph would then evaluate that Empty every frame
#: (smooth interpolation) and override the frame handler's cache-frame-rate
#: matrix_basis writes — desyncing the car's motion from its vertices.  This
#: empty is parented to nothing, so its keyframes are pure world transforms and
#: it can never affect the real chunked meshes.
_RIGID_NAME = "BeamNG_Proxy__Rigid"


def _active_playback():
    """Return the module-level active CachePlayback (or None)."""
    from . import frame_handler
    return frame_handler._active


def prepare_fluid_effector(playback=None,
                           mdd_dir: Optional[str] = None,
                           object_names: Optional[List[str]] = None) -> bool:
    """Prepare the proxy mesh as the Mantaflow fluid effector.

    Returns True on success.  *playback* defaults to the module-level active
    playback.  *mdd_dir* is where the proxy .mdd is written; it defaults to a
    temp dir.  *object_names* selects which FLUID modifiers are moved from the
    heavy meshes onto the proxy (default: the standard body/fenders/trunk).
    """
    if bpy is None:
        return False
    if playback is None:
        playback = _active_playback()
    if playback is None:
        sys.stderr.write("[fluid_effector] no active playback\n")
        sys.stderr.flush()
        return False
    if not has_proxy():
        proxy = create_proxy(playback)
        if proxy is None:
            sys.stderr.write("[fluid_effector] proxy creation failed\n")
            sys.stderr.flush()
            return False
    else:
        proxy = bpy.data.objects.get(_PROXY_NAME)
    if proxy is None:
        return False

    reader = playback.reader
    if mdd_dir is None:
        mdd_dir = tempfile.mkdtemp(prefix="beamng_fluid_eff_")
    os.makedirs(mdd_dir, exist_ok=True)

    # ------------------------------------------------------------------
    # Timing: honour the live playback_fps / output_fps mapping.
    #
    # The frame handler maps timeline frames to cache frames by TIME:
    #     cache_frame = (blender_frame - _frame_start) * (playback_fps / output_fps)
    #
    # The .mdd file frame f *is* cache frame f.  A MESH_CACHE modifier maps
    # scene frames to file frames as (Blender 4.5 MOD_meshcache.cc):
    #     file_frame = frame_scale * scene_frame - frame_start
    #
    # We need file_frame == cache_frame for every scene frame, i.e.
    #     frame_scale * scene_frame - frame_start
    #         == (scene_frame - _frame_start) * (playback_fps / output_fps)
    # Matching coefficients:
    #     frame_scale = playback_fps / output_fps
    #     frame_start = _frame_start * (playback_fps / output_fps)
    #
    # (playback 24 / output 60  ->  scale 0.4: 60 output frames cover 24
    # cache frames, i.e. 1 real second.  scale 1.0 (equal fps) is the plain
    # 1:1 case the old code hard-coded — correct only when the fps match.)
    # ------------------------------------------------------------------
    from . import frame_handler
    playback_fps = float(getattr(frame_handler, "_playback_fps", 24.0))
    output_fps = float(getattr(frame_handler, "_output_fps", 24.0))
    if output_fps <= 0.0:
        output_fps = playback_fps
    scale = playback_fps / output_fps
    frame_start = float(getattr(frame_handler, "_frame_start", 0.0))

    sys.stderr.write(
        "[fluid_effector] timing: playback_fps={0}, output_fps={1}, "
        "frame_scale={2}, frame_start={3}, effective_start={4}\n".format(
            playback_fps, output_fps, scale, frame_start, scale * frame_start))
    sys.stderr.flush()

    # ------------------------------------------------------------------
    # 1. Bake the proxy's per-frame positions to a .mdd point cache.
    # ------------------------------------------------------------------
    mdd_path = os.path.join(mdd_dir, _PROXY_NAME + ".mdd")
    writer = MddWriter(mdd_path)
    for f in range(reader.frame_count):
        pos = gather_proxy_positions(f)
        if pos is None:
            sys.stderr.write(
                "[fluid_effector] gather failed at cache frame {0}\n".format(f))
            sys.stderr.flush()
            return False
        writer.add_frame(pos)
    writer.write()

    # ------------------------------------------------------------------
    # 2. Attach a MESH_CACHE modifier so the depsgraph evaluates the
    #    deformation natively (no Python handler on the bake thread).
    # ------------------------------------------------------------------
    for mod in list(proxy.modifiers):
        if mod.type == "MESH_CACHE":
            proxy.modifiers.remove(mod)
    mod = proxy.modifiers.new(name="BeamNG_MDD", type="MESH_CACHE")
    mod.cache_format = "MDD"
    mod.filepath = mdd_path
    mod.time_mode = "FRAME"
    mod.play_mode = "SCENE"
    mod.frame_start = scale * frame_start
    mod.frame_scale = scale
    mod.interpolation = "LINEAR"
    mod.deform_mode = "OVERWRITE"
    # Map file coords (X=right, Y=forward, Z=up) to Blender coords.
    mod.forward_axis = "POS_Y"
    mod.up_axis = "POS_Z"
    sys.stderr.write(
        "[fluid_effector] MESH_CACHE on proxy: file={0}, frame_start={1}, "
        "frame_scale={2}\n".format(mdd_path, mod.frame_start, mod.frame_scale))
    sys.stderr.flush()

    # ------------------------------------------------------------------
    # 3. Bake the rigid transform onto a SEPARATE Empty that parents ONLY the
    #    proxy, so the real transform Empty never gains F-curves.
    #
    #    Why separate: the frame handler drives the real Empty's matrix_basis
    #    at cache-frame resolution every frame change.  If that Empty had
    #    keyframes (as the first version did), Blender's depsgraph evaluated
    #    the F-curves at EVERY scene frame with smooth interpolation and
    #    overrode the handler's writes — the car's transform ran at high fps
    #    while its vertices still updated at cache-frame rate, desyncing the
    #    two.  This empty is parented to nothing, so its baked keyframes are
    #    pure world transforms and it can never affect the real meshes.
    # ------------------------------------------------------------------
    collection = bpy.data.collections.get(playback.collection_name)
    rigid = bpy.data.objects.get(_RIGID_NAME)
    if rigid is None:
        rigid = bpy.data.objects.new(_RIGID_NAME, None)
        if collection is not None:
            collection.objects.link(rigid)
    rigid.empty_display_type = "ARROWS"
    rigid.hide_render = True
    rigid.rotation_mode = "QUATERNION"
    if rigid.animation_data is not None:
        rigid.animation_data_clear()

    rigid.rotation_mode = "QUATERNION"
    n_keys = 0
    for f in range(reader.frame_count):
        tf = reader.frame_transform(f)
        if tf is None:
            continue
        rigid.matrix_basis = playback._matrix_from_transform(tf)
        # Same time mapping as the MESH_CACHE modifier: cache frame f
        # sits at timeline frame `frame_start + f / scale`.
        frame = frame_start + f / scale
        rigid.keyframe_insert(data_path="location", frame=frame)
        rigid.keyframe_insert(data_path="rotation_quaternion", frame=frame)
        n_keys += 1
    sys.stderr.write(
        "[fluid_effector] rigid Empty baked: {0} keyframes on {1}\n".format(
            n_keys, rigid.name))
    sys.stderr.flush()

    # Reparent the proxy from the real transform Empty onto the rigid Empty.
    # Set an identity parent-inverse so proxy world = rigid.world @ local,
    # exactly as the real meshes relate to their own transform Empty.
    if proxy.parent is not rigid:
        proxy.parent = rigid
    if mathutils is not None:
        proxy.matrix_parent_inverse = mathutils.Matrix.Identity(4)
        proxy.matrix_local = mathutils.Matrix.Identity(4)

    # ------------------------------------------------------------------
    # 4. Move the FLUID effector from the heavy meshes onto the proxy.
    # ------------------------------------------------------------------
    names = object_names if object_names is not None else list(_EFFECTOR_SOURCE_NAMES)
    for name in names:
        obj = bpy.data.objects.get(name)
        if obj is None:
            continue
        for m in list(obj.modifiers):
            if m.type == "FLUID":
                obj.modifiers.remove(m)
    # Ensure the proxy has exactly one EFFECTOR modifier.
    for m in list(proxy.modifiers):
        if m.type == "FLUID":
            proxy.modifiers.remove(m)
    eff = proxy.modifiers.new(name="FluidEffector", type="FLUID")
    eff.fluid_type = "EFFECTOR"
    eff.effector_settings.effector_type = "COLLISION"
    eff.effector_settings.surface_distance = 1.0
    eff.effector_settings.subframes = 2
    eff.effector_settings.use_effector = True

    # The proxy must be visible in the view layer for the bake's depsgraph
    # to evaluate it.  It is render-hidden (viewport helper), but the fluid
    # bake reads the depsgraph object, not a render — hide_render is fine,
    # hide_viewport must be off.
    proxy.hide_viewport = False
    proxy.hide_render = True

    sys.stderr.write(
        "[fluid_effector] FLUID EFFECTOR on {0}; removed from {1}\n".format(
            _PROXY_NAME, names))
    sys.stderr.flush()
    return True


def clear_fluid_effector() -> bool:
    """Remove the MESH_CACHE modifier + FLUID effector from the proxy.

    Also removes the separate rigid Empty (and its baked keyframes) and
    reparents the proxy back onto the real transform Empty so viewport
    playback is exactly as before the effector was prepared.
    """
    if bpy is None:
        return False
    proxy = bpy.data.objects.get(_PROXY_NAME)
    if proxy is None:
        return False
    for m in list(proxy.modifiers):
        if m.type in ("MESH_CACHE", "FLUID"):
            proxy.modifiers.remove(m)

    # Restore the proxy's original parenting (real transform Empty) with the
    # identity parent-inverse it had from creation.
    from . import frame_handler
    real = getattr(getattr(frame_handler, "_active", None),
                   "_transform_empty", None)
    if real is not None and proxy.parent is not real:
        proxy.parent = real
        if mathutils is not None:
            proxy.matrix_parent_inverse = mathutils.Matrix.Identity(4)
            proxy.matrix_local = mathutils.Matrix.Identity(4)

    rigid = bpy.data.objects.get(_RIGID_NAME)
    if rigid is not None:
        if rigid.animation_data is not None:
            rigid.animation_data_clear()
        bpy.data.objects.remove(rigid, do_unlink=True)
    return True