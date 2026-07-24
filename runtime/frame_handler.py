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

from typing import Optional

from .mesh_update import CachePlayback

try:  # pragma: no cover - only inside Blender
    import bpy
except ImportError:  # pragma: no cover
    bpy = None


_ACTIVE_COLLECTION = "BeamNG Cache"

_active: Optional[CachePlayback] = None
_frame_start: int = 0
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


def _cache_frame_for(blender_frame: int) -> int:
    """Map a Blender timeline frame to a source (cache) frame by TIME.

    cache_frame = (blender_frame - frame_start) * playback_fps / output_fps

    With playback_fps=15, output_fps=60 this advances the cache 15 frames per
    real second regardless of how many Blender frames render in that second, so
    the viewport preview and the final render play at identical speed.
    """
    if _output_fps <= 0:
        return blender_frame - _frame_start
    rel = blender_frame - _frame_start
    return int(round(rel * (_playback_fps / _output_fps)))


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


def update_fps(playback_fps: Optional[float] = None,
               output_fps: Optional[float] = None) -> None:
    """Update the playback/output frame rates on the LIVE handler.

    Called from the add-on's fps property callbacks so the two fps fields take
    effect immediately (no re-import needed).  Recomputes the timeline end so
    the sequence duration tracks ``playback_fps``, keeps ``scene.render.fps`` in
    sync with ``output_fps``, and re-asserts realtime viewport sync.
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

    # Recompute timeline length so the whole cache still fits and the duration
    # reflects the new speed (duration_s = (n_src - 1) / playback_fps).
    if _active is not None:
        n_src = _active.reader.frame_count
        duration_s = (n_src - 1) / _playback_fps if n_src > 1 else 0.0
        scene.frame_end = _frame_start + int(round(duration_s * _output_fps))


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
    global _active, _frame_start, _playback_fps, _output_fps

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

        reader = CacheReader(cache_path)
        chunk_map = None
        if use_chunked:
            from .mesh_update import CHUNK_MAP_E180
            # Deep-copy so we don't mutate the module-level constant
            chunk_map = {k: list(v) for k, v in CHUNK_MAP_E180.items()}

        # Mesh objects already exist after undo — just reconnect the reader
        playback = CachePlayback(reader, chunk_map=chunk_map)
        # Rebuild internal name→object lookup from existing scene objects
        collection = bpy.data.collections.get(_ACTIVE_COLLECTION)
        if collection is None:
            return False

        # Source objects (chunked mode keeps individual meshes in source coll)
        source_coll = bpy.data.collections.get(f"{_ACTIVE_COLLECTION} (Source)")
        playback_coll = collection  # visible playback collection

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
                # Rebuild vertex offset ranges for this chunk
                ranges = {}
                vert_offset = 0
                for mname in member_names:
                    co = reader.get_object(mname)
                    vc = co.vertex_count
                    ranges[mname] = (vert_offset, vert_offset + vc)
                    vert_offset += vc
                playback._chunk_member_ranges[chunk_name] = ranges
        else:
            # Non-chunked: all collection mesh objects are individual targets
            for obj in playback_coll.objects:
                if obj.type == "MESH" and obj.name not in playback._objects:
                    playback._objects[obj.name] = obj

        # --- rebuild _dynamic_objects ---
        for obj in playback_coll.objects:
            if obj.type == "MESH" and obj.name not in playback._objects and obj.name not in playback._chunks:
                playback._dynamic_objects[obj.name] = obj

        _active = playback
        _frame_start = frame_start

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
    if _force_depsgraph:
        bpy.context.view_layer.update()


def attach(playback: CachePlayback, frame_start: int = 0,
           playback_fps: float = 24.0, output_fps: float = 24.0) -> None:
    """Register ``playback`` as the active sequence and hook the timeline.

    ``frame_start`` is the Blender frame that maps to cache frame 0.
    ``playback_fps`` is the capture/source rate (animation SPEED — how many
    captured frames advance per real second).  ``output_fps`` is the scene's
    render frame rate (smoothness).  The two are independent: the timeline is
    stretched so the whole sequence lasts ``frame_count / playback_fps``
    seconds at ``output_fps``, and the viewport preview matches the render.

    Stores playback metadata on the scene so the handler can recover
    after Blender undo or file reload.
    """
    if bpy is None:
        raise RuntimeError("frame handler requires Blender (bpy)")
    global _active, _frame_start, _playback_fps, _output_fps
    _active = playback
    _frame_start = frame_start
    _playback_fps = max(0.001, float(playback_fps))
    _output_fps = max(0.001, float(output_fps))

    detach_handler()  # avoid duplicate registrations
    bpy.app.handlers.frame_change_pre.append(_on_frame_change)

    scene = bpy.context.scene
    scene.frame_start = frame_start

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

    # Timeline length in OUTPUT frames = duration_seconds * output_fps, where
    # duration_seconds = (frame_count - 1) / playback_fps.
    n_src = playback.reader.frame_count
    duration_s = (n_src - 1) / _playback_fps if n_src > 1 else 0.0
    scene.frame_end = frame_start + int(round(duration_s * _output_fps))

    # Viewport-only nicety: play at real wall-clock speed (drop frames instead
    # of crawling) so the PREVIEW matches the render.  Has NO effect on the
    # rendered output — render speed is set by render.fps + frame_end above.
    _set_realtime_sync()

    # Store state for undo/reload recovery
    scene["_beamng_cache_path"] = str(playback.reader.path)
    scene["_beamng_frame_start"] = frame_start
    scene["_beamng_use_chunked"] = playback._chunk_map is not None
    scene["_beamng_playback_fps"] = _playback_fps
    scene["_beamng_output_fps"] = _output_fps


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
    global _active
    detach_handler()
    _active = None
    # Clear stored state so _try_recover doesn't fire stale data
    if bpy is not None and bpy.context.scene is not None:
        for key in ("_beamng_cache_path", "_beamng_frame_start", "_beamng_use_chunked",
                    "_beamng_playback_fps", "_beamng_output_fps"):
            if key in bpy.context.scene:
                del bpy.context.scene[key]
