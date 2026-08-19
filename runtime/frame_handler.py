from __future__ import annotations

"""Wires a CachePlayback to Blender's timeline via a frame-change handler.

On each frame change the handler maps ``scene.frame_current`` to a cache frame
and asks the playback to update vertex positions. Only one active playback is
tracked at a time (the common case: one imported sequence).

**Undo/reload survival:** the handler stores playback metadata (BVC path,
frame offset, chunked flag) as custom properties on the scene.  If ``_active``
is lost (Blender undo wipes module-level state), the handler auto-recreates
the CacheReader + CachePlayback from the stored metadata on the next frame
change — so the animation keeps playing after Ctrl+Z or file reload.
"""

from typing import Dict, Optional

from .mesh_update import CachePlayback
from .tyre_deform import TyreSettings

try:  # pragma: no cover - only inside Blender
    import bpy
except ImportError:  # pragma: no cover
    bpy = None


_ACTIVE_COLLECTION = "BeamNG Cache"

_active: Optional[CachePlayback] = None
_frame_start: int = 0
# The "Start at Frame" knob, in Blender FRAMES — this is the canonical storage
# and _frame_start is just a copy of it.
#
# It used to be held in SECONDS (_frame_start = start_second * output_fps) so
# the offset kept cache frame 0 at the same *time* across an output_fps change.
# That is defensible, but it surprised the user: typing 500 meant 500 SECONDS,
# which at 60 output fps put frame 0 at frame 30000.  Frames are what the
# timeline actually shows, so frames is what the field now means.
#
# The tradeoff this re-introduces on purpose: changing Output FPS now keeps the
# same frame NUMBER, so the start moves in time (frame 500 is 8.3 s at 60 fps
# but 20.8 s at 24 fps).  Set the start after settling on Output FPS.
_start_frame: int = 0
# Time-based mapping: one *source* (captured) frame plays every
# (output_fps / playback_fps) Blender frames.  This decouples the animation's
# SPEED (playback_fps — how many captured frames advance per real second) from
# the scene's OUTPUT frame rate (scene.render.fps — render smoothness).  When
# they were the same knob, rendering at 60 fps replayed a 15-fps-tuned sequence
# 4x too fast.  Mirrors the glTF exporter, which is time-based, not frame-based.
_playback_fps: float = 24.0   # captured/source frames per second (speed)
_output_fps: float = 24.0     # scene.render.fps (render smoothness)
_force_depsgraph: bool = False
_skip_n: int = 0       # 0 = disabled; > 0 = update only every Nth frame
_frame_counter: int = 0
# Smooth-stop tail, in TIMELINE frames: 0 = disabled.  When enabled the
# timeline is extended and the car glides to a full rest along its residual
# motion (damped-sine continuation) instead of freezing instantly at the final
# captured pose.
_smooth_stop_frames: int = 0
# Smooth-stop onset frame, in TIMELINE frames: 0 = unset (the tail starts at
# the end of the captured sequence, the original behaviour).  When > 0 the
# smooth stop begins at this frame instead — the car plays normally up to it
# and the damped settle takes over from there, cutting any remaining captured
# frames in favour of the continuation.  Clamped to the capture end on attach
# so a value past the capture behaves like the default.
_smooth_stop_start_frame: int = 0


def _cache_frame_for(blender_frame: int) -> float:
    """Map a Blender timeline frame to a source (cache) frame by TIME.

    cache_frame = (blender_frame - frame_start) * playback_fps / output_fps

    With playback_fps=15, output_fps=60 this advances the cache 15 frames per
    real second regardless of how many Blender frames render in that second, so
    the viewport preview and the final render play at identical speed.

    Returns a FLOAT.  Values at or below the last captured frame are rounded to
    integer cache frames by the playback; values PAST it (the smooth-stop tail
    added to ``frame_end``) drive the eased settle to rest.
    """
    if _output_fps <= 0:
        return float(blender_frame - _frame_start)
    rel = blender_frame - _frame_start
    return rel * (_playback_fps / _output_fps)


def _smooth_stop_tail_cache() -> float:
    """The smooth-stop tail converted to CACHE-frame units for the playback."""
    if _output_fps <= 0:
        return float(_smooth_stop_frames)
    return _smooth_stop_frames * (_playback_fps / _output_fps)


def _smooth_stop_start_cache() -> float:
    """The smooth-stop onset frame converted to CACHE-frame units.

    Returns 0 when the start frame is unset (<= ``_frame_start``), which
    :meth:`CachePlayback.set_smooth_stop` interprets as "start at the end of
    the capture" — the default behaviour.
    """
    if _smooth_stop_start_frame <= _frame_start or _output_fps <= 0:
        return 0.0
    return (_smooth_stop_start_frame - _frame_start) * (_playback_fps / _output_fps)


def _set_realtime_sync() -> None:
    """Force the viewport to play at real wall-clock speed (frame dropping).

    THE "RENDER TOO FAST" FIX (2026-07-24)
    --------------------------------------
    Blender's default playback sync is ``'NONE'`` ("Play Every Frame"), which
    is COMPUTE-BOUND: it shows every frame no matter how long the frame handler
    takes.  With ~550K verts/frame our handler can't hit ``output_fps``, so the
    viewport crawls in *unintended* slow-motion.  The render, by contrast, emits
    every frame at exactly ``scene.render.fps`` — true speed.  So a speed tuned
    against the laggy viewport renders far too fast.

    ``'FRAME_DROP'`` makes the viewport target real wall-clock time and DROP
    frames it can't compute, so the viewport plays at the identical speed as the
    render.  Now what you tune in the viewport is exactly what renders.
    """
    if bpy is None:
        return
    scene = bpy.context.scene
    if scene is None:
        return
    # Modern API (Blender 2.8+): Scene.sync_mode enum.
    try:
        scene.sync_mode = "FRAME_DROP"
    except (AttributeError, TypeError):
        pass
    # Legacy boolean, harmless if absent.
    try:
        scene.render.use_frame_drop = True
    except (AttributeError, TypeError):
        pass


def _apply_timeline(scene, keep_playhead: bool = True) -> None:
    """Re-derive ``scene.frame_start``/``frame_end`` from the live settings.

    ``_frame_start`` is simply ``_start_frame`` — the offset is stored in
    Blender frames, so the animation starts on the frame number the user typed.
    The end is ``frame_start + duration_s * output_fps`` with
    ``duration_s = (n_src - 1) / playback_fps``, so the DURATION still tracks
    playback speed even though the START is a fixed frame.

    When the smooth-stop onset is set to a frame within the captured range, the
    timeline is cut short at that frame and instead extended by the stop-frames
    tail from it — the car settles from the chosen frame, not from the absolute
    end.  A start frame at or past the capture end falls back to the default.

    With ``keep_playhead`` the playhead is nudged back inside the new range when
    the offset pushed it outside; otherwise every frame before ``frame_start``
    clamps to cache frame 0 and the car looks frozen/broken.
    """
    global _frame_start
    _frame_start = int(_start_frame)
    scene.frame_start = _frame_start
    scene["_beamng_frame_start"] = _frame_start
    scene["_beamng_start_frame"] = _frame_start

    if _active is None:
        return
    n_src = _active.reader.frame_count
    duration_s = (n_src - 1) / _playback_fps if n_src > 1 else 0.0
    capture_end = _frame_start + int(round(duration_s * _output_fps))
    if (_smooth_stop_frames > 0
            and _frame_start < _smooth_stop_start_frame < capture_end):
        # Smooth-stop onset is inside the capture: cut the timeline at it and
        # extend by the tail.
        scene.frame_end = _smooth_stop_start_frame + _smooth_stop_frames
    else:
        scene.frame_end = capture_end + _smooth_stop_frames

    if keep_playhead:
        clamped = min(max(scene.frame_current, scene.frame_start), scene.frame_end)
        if clamped != scene.frame_current:
            scene.frame_current = clamped


def _refresh_current_frame(scene) -> None:
    """Re-run the frame the playhead sits on and redraw the 3D views.

    Used by the live-retune entry points: the playhead may not have moved, so no
    frame-change handler fires on its own and the viewport would keep showing
    the geometry from the previous settings.
    """
    if _active is None:
        return
    _active.set_frame(_cache_frame_for(scene.frame_current))
    for area in getattr(getattr(bpy.context, "screen", None), "areas", ()) or ():
        if area.type == "VIEW_3D":
            area.tag_redraw()


def _retime_debris(scene) -> None:
    """Rescale baked debris/particle timing to the CURRENT live mapping.

    The debris build bakes every keyframe to an absolute timeline frame at its
    build-time ``(frame_start, playback_fps, output_fps)``.  The car re-times
    procedurally, so after :func:`update_fps` / :func:`update_start_frame` the
    two would live on different clocks — the shards fire before/after the panel
    they came off.  This hands the current live values to
    :func:`runtime.debris_retime.retime_debris`, which affinely rescales the
    baked keys so each one keeps the cache frame (the moment in the crash) it
    was baked for.

    Imported lazily so a missing/wrong debris module can never take down the
    fps sliders; a failure is logged and ignored.
    """
    if bpy is None or scene is None:
        return
    try:
        from .debris_retime import retime_debris
        retime_debris(scene, _frame_start, _playback_fps, _output_fps)
    except Exception as exc:  # pragma: no cover - Blender-only path
        print(f"[BeamNG] debris retime skipped: {exc}")


def update_start_frame(start_frame: int) -> None:
    """Move cache frame 0 to Blender frame ``start_frame`` on the LIVE timeline.

    Called from the add-on's "Start at Frame" property callback so the offset is
    retunable without re-importing the cache: the mapping in
    :func:`_cache_frame_for` is pure arithmetic on ``_frame_start``, so shifting
    the whole animation is just a matter of re-deriving the frame range and
    re-running the current frame.
    """
    global _start_frame
    _start_frame = max(0, int(round(float(start_frame))))

    if bpy is None:
        return
    scene = bpy.context.scene
    if scene is None:
        return
    _apply_timeline(scene)
    _retime_debris(scene)
    _refresh_current_frame(scene)


def update_start_second(start_second: float) -> None:
    """Deprecated seconds-based alias for :func:`update_start_frame`.

    The panel field is frames now.  This is kept so an older saved .blend, or
    any caller that still thinks in seconds, converts instead of breaking.
    """
    fps = _output_fps if _output_fps > 0 else 1.0
    update_start_frame(int(round(max(0.0, float(start_second)) * fps)))


def update_fps(playback_fps: Optional[float] = None,
               output_fps: Optional[float] = None) -> None:
    """Update the playback/output frame rates on the LIVE handler.

    Called from the add-on's fps property callbacks so the two fps fields take
    effect immediately (no re-import needed).  Recomputes the timeline end so
    the sequence duration tracks ``playback_fps``, keeps ``scene.render.fps`` in
    sync with ``output_fps``, and re-asserts realtime viewport sync.

    Like the other live-retune entry points this MUST end in
    :func:`_refresh_current_frame`.  Both fps values are inputs to
    :func:`_cache_frame_for`, so changing either one re-points the *parked*
    playhead at a different cache frame (at frame 300, playback_fps 24 -> 15
    moves cache frame 120 -> 75).  The playhead itself usually does not move, so
    no frame-change handler fires and the viewport would keep displaying the old
    cache frame — which is exactly what made this field look dead and forced a
    re-import.  Refreshing in place (rather than sliding the playhead to hold
    the current cache frame) also gives the slider visible feedback while
    dragging.
    """
    global _playback_fps, _output_fps
    if playback_fps is not None:
        _playback_fps = max(0.001, float(playback_fps))
    if output_fps is not None:
        _output_fps = max(0.001, float(output_fps))

    if bpy is None:
        return
    scene = bpy.context.scene
    if scene is None:
        return

    scene.render.fps = max(1, int(round(_output_fps)))
    scene.render.fps_base = 1.0
    _set_realtime_sync()

    # Persist for undo/reload recovery.
    scene["_beamng_playback_fps"] = _playback_fps
    scene["_beamng_output_fps"] = _output_fps

    # Recompute the timeline: the start offset is held in FRAMES, so a new
    # output_fps keeps frame_start fixed and only re-derives frame_end (the
    # duration still tracks the fps ratio).
    _apply_timeline(scene)

    # Rescale the baked debris/particle keys to the new mapping so they keep
    # hitting their cache frames (see _retime_debris).
    _retime_debris(scene)

    # Re-run the frame the playhead sits on — see the docstring: the new fps
    # remaps the parked playhead to a different cache frame, and nothing else
    # would push that to the mesh.
    _refresh_current_frame(scene)


_TYRE_KEYS = ("amount", "extra", "bulge", "release", "ground_z", "names")


def update_smooth_stop(enabled: Optional[bool] = None,
                       frames: Optional[int] = None,
                       start_frame: Optional[int] = None) -> None:
    """Retune the smooth-stop settle tail on the LIVE handler.

    Called from the add-on's "Smooth Stop" checkbox/frames/start-frame callbacks so
    the car's rest motion is tunable without re-importing.  ``enabled=False`` (or a
    zero ``frames``) removes the tail — the timeline shrinks back to the last
    captured frame and the car freezes there.  ``start_frame`` moves the onset of
    the settle to an earlier timeline frame, cutting the remaining captured motion
    in favour of the damped continuation.  0 (or <= ``frame_start``) restores the
    default onset at the end of the capture.  All values are persisted as scene
    props so undo/reload recovery restores them.

    Like every other live-retune entry point this MUST end in
    :func:`_refresh_current_frame` — the playhead usually does not move, so
    without it the viewport keeps showing the previous length/settle.
    """
    global _smooth_stop_frames, _smooth_stop_start_frame
    if frames is not None:
        _smooth_stop_frames = max(0, int(round(float(frames))))
    if start_frame is not None:
        _smooth_stop_start_frame = max(0, int(round(float(start_frame))))
    if enabled is not None and not enabled:
        _smooth_stop_frames = 0

    if _active is not None:
        _active.set_smooth_stop(_smooth_stop_tail_cache(),
                                _smooth_stop_start_cache())

    if bpy is None:
        return
    scene = bpy.context.scene
    if scene is None:
        return
    scene["_beamng_smooth_stop_frames"] = _smooth_stop_frames
    scene["_beamng_smooth_stop_start"] = _smooth_stop_start_frame
    _apply_timeline(scene)
    _refresh_current_frame(scene)


def update_tyre(**kwargs) -> None:
    """Push tyre ground-contact settings to the LIVE playback and redraw.

    Called from the add-on's tyre property callbacks so the sliders update the
    viewport immediately (no re-import).  Accepts any subset of
    :class:`TyreSettings` fields; unknown/None values are ignored.  The values
    are also stored on the scene so undo/reload recovery restores them.
    """
    if _active is None:
        if bpy is not None and bpy.context.scene is not None:
            _store_tyre(bpy.context.scene, TyreSettings().update(**kwargs))
        return

    tyre = TyreSettings.from_dict(_active.tyre.to_dict()).update(**kwargs)
    _active.set_tyre_settings(tyre)

    if bpy is None:
        return
    scene = bpy.context.scene
    if scene is None:
        return
    _store_tyre(scene, tyre)
    # Re-run the current frame so the change is visible without scrubbing.
    _refresh_current_frame(scene)


def _store_tyre(scene, tyre: TyreSettings) -> None:
    """Persist tyre settings as scene custom props (survives undo/reload)."""
    data = tyre.to_dict()
    for key in _TYRE_KEYS:
        scene[f"_beamng_tyre_{key}"] = data[key]


def _load_tyre(scene) -> TyreSettings:
    """Restore tyre settings from scene custom props (undo/reload recovery)."""
    stored = {}
    for key in _TYRE_KEYS:
        val = scene.get(f"_beamng_tyre_{key}")
        if val is not None:
            stored[key] = val
    return TyreSettings.from_dict(stored)


_SHATTER_KEY = "_beamng_shattered_panes"
_SHATTER_RETAIN_KEY = "_beamng_shatter_edge_retain"


def set_shattered_panes(panes: Dict[str, int],
                        edge_retain: Optional[float] = None) -> None:
    """Register shattered glass panes on the LIVE playback (and persist them).

    ``panes`` maps a pane member name to the cache frame it broke.  From that
    frame on the intact glass collapses so the spawned fragments take over —
    the pane's RIM band stays welded to the aperture and acts as the fringe.
    Called by the debris builder; also stored on the scene so undo /reload
    recovery re-applies the shatter (the .blend keeps the fragments, so the
    panes must keep collapsing to match).  ``edge_retain`` is the rim-band
    width the build used; it is persisted alongside the map so a recovered
    playback can rebuild the same rim.
    """
    if _active is not None:
        _active.set_shattered_panes(panes, edge_retain=edge_retain)
    if bpy is not None and bpy.context.scene is not None:
        if panes:
            bpy.context.scene[_SHATTER_KEY] = ",".join(
                f"{k}:{int(v)}" for k, v in sorted(panes.items()))
            if edge_retain is not None:
                bpy.context.scene[_SHATTER_RETAIN_KEY] = float(edge_retain)
        else:
            for key in (_SHATTER_KEY, _SHATTER_RETAIN_KEY):
                if key in bpy.context.scene:
                    del bpy.context.scene[key]


def _load_shattered_panes(scene) -> Dict[str, int]:
    """Restore the shattered-pane map from scene custom props."""
    raw = scene.get(_SHATTER_KEY)
    if not raw:
        return {}
    out = {}
    for tok in str(raw).split(","):
        if ":" not in tok:
            continue
        name, frame = tok.rsplit(":", 1)
        try:
            out[name] = int(frame)
        except ValueError:
            continue
    return out


def _load_shatter_edge_retain(scene) -> float:
    """Restore the rim-band width the debris build used, for recovery."""
    try:
        return float(scene.get(_SHATTER_RETAIN_KEY, 0.05))
    except (TypeError, ValueError):
        return 0.05


def set_force_depsgraph(enabled: bool) -> None:
    """When True, call view_layer.update() after every set_frame().

    Enable this before Alembic/USD export so exporters see updated geometry.
    Disable it for normal playback to avoid unnecessary depsgraph overhead.
    """
    global _force_depsgraph
    _force_depsgraph = enabled


def set_skip_n(n: int) -> None:
    """Every-Nth-frame experiment: skip mesh updates on most frames.

    0 = update every frame (normal).
    1 = update every frame (same).
    2 = update every other frame.
    10 = update only every 10th frame.

    If FPS jumps dramatically (e.g. 9 → 50) when skip_n=10, the
    bottleneck IS the per-frame foreach_set / mesh update.
    If FPS stays the same (~9), the overhead is just from having
    the handler + meshes registered in the depsgraph.
    """
    global _skip_n, _frame_counter
    _skip_n = n
    _frame_counter = 0


def _try_recover(scene) -> bool:
    """Attempt to recreate _active from scene custom properties after undo.

    Returns True if recovery succeeded (or wasn't needed).
    """
    global _active, _frame_start, _start_frame, _playback_fps, _output_fps
    global _smooth_stop_frames, _smooth_stop_start_frame

    # Already active — nothing to do
    if _active is not None:
        return True

    # No stored state — not a recoverable situation
    cache_path = scene.get("_beamng_cache_path")
    if not cache_path:
        return False

    import os
    from pathlib import Path
    from .cache_reader import CacheReader

    if not cache_path or not os.path.exists(cache_path):
        return False

    try:
        frame_start = int(scene.get("_beamng_frame_start", 0))
        use_chunked = bool(scene.get("_beamng_use_chunked", False))
        _playback_fps = float(scene.get("_beamng_playback_fps", _playback_fps))
        _output_fps = float(scene.get("_beamng_output_fps", _output_fps))
        _smooth_stop_frames = max(
            0, int(round(float(scene.get("_beamng_smooth_stop_frames", 0)))))
        _smooth_stop_start_frame = max(
            0, int(round(float(scene.get("_beamng_smooth_stop_start", 0)))))
        # Frames are canonical.  A .blend saved by the older seconds-based build
        # only has "_beamng_start_second", so convert it with the recovered
        # output_fps — otherwise reopening such a file would reset the offset.
        stored_frame = scene.get("_beamng_start_frame")
        if stored_frame is None:
            legacy_second = scene.get("_beamng_start_second")
            stored_frame = (float(legacy_second) * _output_fps
                            if legacy_second is not None else frame_start)
        _start_frame = max(0, int(round(float(stored_frame))))

        reader = CacheReader(cache_path)
        chunk_map = None
        if use_chunked:
            from .mesh_update import CHUNK_MAP_E180
            # Deep-copy so we don't mutate the module-level constant
            chunk_map = {k: list(v) for k, v in CHUNK_MAP_E180.items()}

        # Mesh objects already exist after undo — just reconnect the reader
        playback = CachePlayback(reader, chunk_map=chunk_map,
                                tyre=_load_tyre(scene))
        # Rebuild internal name→object lookup from existing scene objects
        collection = bpy.data.collections.get(_ACTIVE_COLLECTION)
        if collection is None:
            return False

        # Source objects (chunked mode keeps individual meshes in source coll)
        source_coll = bpy.data.collections.get(f"{_ACTIVE_COLLECTION} (Source)")
        playback_coll = collection  # visible playback collection

        # --- reconnect the rigid-transform parent empty ---------------------
        # build_scene() creates a "<collection>__root" Empty and drives its
        # matrix_basis per frame from the BVC transform block; every mesh is
        # parented to it.  The Empty and the parenting are saved in the .blend,
        # but _transform_empty is module state, so after a reload it is None and
        # _apply_transform() returns early — the car then deforms correctly but
        # sits at the origin instead of following the captured motion.
        # Re-bind it by name (it is only present when the cache HAS transform
        # data, matching _create_transform_empty's own guard).
        if reader.header.get("transform_data_offset", 0):
            root_name = f"{_ACTIVE_COLLECTION}__root"
            root = playback_coll.objects.get(root_name)
            if root is None:
                root = bpy.data.objects.get(root_name)
            if root is None:
                # Parenting survives even if the Empty was moved out of the
                # collection, so fall back to the meshes' shared parent.
                for obj in playback_coll.objects:
                    if obj.type == "MESH" and obj.parent is not None \
                            and obj.parent.type == "EMPTY":
                        root = obj.parent
                        break
            playback._transform_empty = root

        # --- rebuild _objects (source / individual meshes) ---
        for obj in source_coll.objects if source_coll else collection.objects:
            if obj.type == "MESH":
                playback._objects[obj.name] = obj

        # --- rebuild _chunks + _chunk_member_ranges ---
        if chunk_map:
            dynamic_names = {c.name for c in reader.dynamic_objects()}
            from .mesh_update import validate_chunk_map
            all_names = list(playback._objects.keys()) + list(dynamic_names)
            validate_chunk_map(chunk_map, all_names, dynamic_names=dynamic_names)

            for chunk_name, member_names in chunk_map.items():
                chunk_obj = playback_coll.objects.get(chunk_name)
                if chunk_obj is None:
                    continue
                playback._chunks[chunk_name] = chunk_obj
                # Rebuild vertex AND face offset ranges for this chunk.  The
                # offsets must be accumulated over exactly the members
                # _build_chunk merged — it skips dynamic members, so walking the
                # raw chunk_map here would shift every subsequent member's slice
                # and address the wrong geometry.
                ranges = {}
                face_ranges = {}
                vert_offset = 0
                face_offset = 0
                for mname in member_names:
                    if mname in dynamic_names:
                        continue
                    co = reader.get_object(mname)
                    vc = co.vertex_count
                    fc = int(reader.base_indices(mname).shape[0])
                    ranges[mname] = (vert_offset, vert_offset + vc)
                    face_ranges[mname] = (face_offset, face_offset + fc)
                    vert_offset += vc
                    face_offset += fc
                playback._chunk_member_ranges[chunk_name] = ranges
                playback._chunk_member_faces[chunk_name] = face_ranges
        else:
            # Non-chunked: all collection mesh objects are individual targets
            for obj in playback_coll.objects:
                if obj.type == "MESH" and obj.name not in playback._objects:
                    playback._objects[obj.name] = obj

        # --- rebuild _dynamic_objects ---
        dynamic_names = {c.name for c in reader.dynamic_objects()}
        for obj in playback_coll.objects:
            if obj.type == "MESH" and obj.name in dynamic_names:
                playback._dynamic_objects[obj.name] = obj

        _active = playback
        _frame_start = frame_start

        # Restore the smooth-stop tail (and onset frame) so the settled rest
        # keeps playing with the right seam.
        playback.set_smooth_stop(_smooth_stop_tail_cache(),
                                 _smooth_stop_start_cache())

        # Re-apply shattered panes so recovered playback collapses them too.
        panes = _load_shattered_panes(scene)
        if panes:
            playback.set_shattered_panes(
                panes, edge_retain=_load_shatter_edge_retain(scene))

        # Ensure Mantaflow fluid effector fix survives undo/reload
        playback._ensure_fluid_animation()

        # Re-register handler if missing
        _ensure_handler_registered()

        return True
    except Exception:
        import traceback
        traceback.print_exc()
        return False


def _ensure_handler_registered() -> None:
    """Register the frame-change handler if not already present."""
    if bpy is None:
        return
    handlers = bpy.app.handlers.frame_change_pre
    already = any(
        getattr(h, "__name__", "") == "_on_frame_change"
        for h in handlers
    )
    if not already:
        handlers.append(_on_frame_change)


def _on_frame_change(scene, _depsgraph=None) -> None:  # pragma: no cover - Blender cb
    # Auto-recover after undo (wipes module-level _active)
    if _active is None:
        if not _try_recover(scene):
            return

    cache_frame = _cache_frame_for(scene.frame_current)

    # Every-Nth-frame experiment — skip actual update on most frames
    global _frame_counter
    if _skip_n > 0:
        _frame_counter += 1
        if _frame_counter % _skip_n != 0:
            return

    _active.set_frame(cache_frame)

    # Update the proxy mesh if one exists.
    try:
        from .proxy_mesh import update_proxy_from_frame
        update_proxy_from_frame(cache_frame)
    except Exception:
        pass

    if _force_depsgraph:
        bpy.context.view_layer.update()


def attach(playback: CachePlayback, frame_start: int = 0,
           playback_fps: float = 24.0, output_fps: float = 24.0,
           start_second: Optional[float] = None,
           smooth_stop_frames: int = 0,
           smooth_stop_start_frame: int = 0) -> None:
    """Register ``playback`` as the active sequence and hook the timeline.

    ``frame_start`` is the Blender frame that maps to cache frame 0 — the
    canonical form (see :data:`_start_frame`), and what
    :func:`update_start_frame` retunes live.  ``start_second`` is the deprecated
    seconds spelling of the same thing; when given it is converted with
    ``output_fps`` and takes precedence, so old callers keep working.
    ``playback_fps`` is the capture/source rate (animation SPEED — how many
    captured frames advance per real second).  ``output_fps`` is the scene's
    render frame rate (smoothness).  The two are independent: the timeline is
    stretched so the whole sequence lasts ``frame_count / playback_fps``
    seconds at ``output_fps``, and the viewport preview matches the render.

    ``smooth_stop_frames`` extends the timeline past the last captured frame and
    eases the car to rest along its residual motion.  ``smooth_stop_start_frame``
    moves that onset to an earlier timeline frame (0 = unset, start at the end of
    capture).

    Stores playback metadata on the scene so the handler can recover
    after Blender undo or file reload.
    """
    if bpy is None:
        raise RuntimeError("frame handler requires Blender (bpy)")
    global _active, _frame_start, _start_frame, _playback_fps, _output_fps
    global _smooth_stop_frames, _smooth_stop_start_frame
    _active = playback
    _playback_fps = max(0.001, float(playback_fps))
    _output_fps = max(0.001, float(output_fps))
    _smooth_stop_frames = max(0, int(round(float(smooth_stop_frames))))
    _smooth_stop_start_frame = max(0, int(round(float(smooth_stop_start_frame))))
    # Frames are canonical; the deprecated seconds spelling converts into them.
    _start_frame = (int(round(max(0.0, float(start_second)) * _output_fps))
                    if start_second is not None
                    else max(0, int(round(float(frame_start)))))
    _frame_start = _start_frame

    detach_handler()  # avoid duplicate registrations
    bpy.app.handlers.frame_change_pre.append(_on_frame_change)

    scene = bpy.context.scene
    scene.frame_start = _frame_start

    # THE RENDER-SPEED FIX (the part that actually governs the RENDER, not the
    # viewport): the rendered clip plays Blender frames [frame_start, frame_end]
    # at scene.render.fps and each maps to a cache frame by TIME, so the clip
    # lasts exactly (n_src-1)/playback_fps SECONDS — independent of output_fps
    # and of viewport speed.  render.fps MUST be output_fps for that duration to
    # hold, so attach() sets it here (previously only the import operator did,
    # leaving undo-recovery / any other attach() caller rendering at the wrong
    # rate).  playback_fps=60 renders at true realtime (capture is 60fps);
    # lower playback_fps = proportionally slower render.  This is what fixes
    # "the crash renders 2-3x too fast".
    scene.render.fps = max(1, int(round(_output_fps)))
    scene.render.fps_base = 1.0

    # Timeline length.  When the smooth-stop onset is inside the capture, the
    # timeline is cut at it and extended by the tail; otherwise it plays all
    # captured frames plus the tail.
    n_src = playback.reader.frame_count
    duration_s = (n_src - 1) / _playback_fps if n_src > 1 else 0.0
    capture_end = _frame_start + int(round(duration_s * _output_fps))
    if (_smooth_stop_frames > 0
            and _frame_start < _smooth_stop_start_frame < capture_end):
        scene.frame_end = _smooth_stop_start_frame + _smooth_stop_frames
    else:
        scene.frame_end = capture_end + _smooth_stop_frames
    playback.set_smooth_stop(_smooth_stop_tail_cache(),
                             _smooth_stop_start_cache())

    # Viewport-only nicety: play at real wall-clock speed (drop frames instead
    # of crawling) so the PREVIEW matches the render.  Has NO effect on the
    # rendered output — render speed is set by render.fps + frame_end above.
    _set_realtime_sync()

    # Store state for undo/reload recovery
    scene["_beamng_cache_path"] = str(playback.reader.path)
    scene["_beamng_frame_start"] = _frame_start
    scene["_beamng_start_frame"] = _frame_start
    scene["_beamng_use_chunked"] = playback._chunk_map is not None
    scene["_beamng_playback_fps"] = _playback_fps
    scene["_beamng_output_fps"] = _output_fps
    scene["_beamng_smooth_stop_frames"] = _smooth_stop_frames
    scene["_beamng_smooth_stop_start"] = _smooth_stop_start_frame
    _store_tyre(scene, playback.tyre)


def detach_handler() -> None:
    """Remove our frame-change handler if present (leaves objects intact)."""
    if bpy is None:
        return
    handlers = bpy.app.handlers.frame_change_pre
    for h in list(handlers):
        if getattr(h, "__name__", "") == "_on_frame_change":
            handlers.remove(h)


def detach() -> None:
    """Fully detach: remove handler and clear the active playback."""
    global _active, _smooth_stop_frames, _smooth_stop_start_frame
    detach_handler()
    _active = None
    _smooth_stop_frames = 0
    _smooth_stop_start_frame = 0
    # Clear stored state so _try_recover doesn't fire stale data
    if bpy is not None and bpy.context.scene is not None:
        keys = ["_beamng_cache_path", "_beamng_frame_start", "_beamng_start_frame",
                "_beamng_start_second",  # legacy key from the seconds-based build
                "_beamng_use_chunked", "_beamng_playback_fps", "_beamng_output_fps",
                "_beamng_smooth_stop_frames", "_beamng_smooth_stop_start",
                _SHATTER_KEY, _SHATTER_RETAIN_KEY]
        keys += [f"_beamng_tyre_{k}" for k in _TYRE_KEYS]
        for key in keys:
            if key in bpy.context.scene:
                del bpy.context.scene[key]
