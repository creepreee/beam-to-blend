from __future__ import annotations

"""Wires a CachePlayback to Blender's timeline via a frame-change handler.

On each frame change the handler maps ``scene.frame_current`` to a cache frame
and asks the playback to update vertex positions. Only one active playback is
tracked at a time (the common case: one imported sequence).
"""

from typing import Optional

from .mesh_update import CachePlayback

try:  # pragma: no cover - only inside Blender
    import bpy
except ImportError:  # pragma: no cover
    bpy = None


_active: Optional[CachePlayback] = None
_frame_start: int = 0
_force_depsgraph: bool = False
_skip_n: int = 0       # 0 = disabled; > 0 = update only every Nth frame
_frame_counter: int = 0


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


def _on_frame_change(scene, _depsgraph=None) -> None:  # pragma: no cover - Blender cb
    if _active is None:
        return
    cache_frame = scene.frame_current - _frame_start

    # Every-Nth-frame experiment — skip actual update on most frames
    global _frame_counter
    if _skip_n > 0:
        _frame_counter += 1
        if _frame_counter % _skip_n != 0:
            return

    _active.set_frame(cache_frame)
    if _force_depsgraph:
        bpy.context.view_layer.update()


def attach(playback: CachePlayback, frame_start: int = 0) -> None:
    """Register ``playback`` as the active sequence and hook the timeline.

    ``frame_start`` is the Blender frame that maps to cache frame 0.
    """
    if bpy is None:
        raise RuntimeError("frame handler requires Blender (bpy)")
    global _active, _frame_start
    _active = playback
    _frame_start = frame_start

    detach_handler()  # avoid duplicate registrations
    bpy.app.handlers.frame_change_pre.append(_on_frame_change)

    scene = bpy.context.scene
    scene.frame_start = frame_start
    scene.frame_end = frame_start + playback.reader.frame_count - 1


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
