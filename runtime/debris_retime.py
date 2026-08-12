from __future__ import annotations

"""Retime already-built debris when the live frame mapping changes.

WHY THIS EXISTS
---------------
The car is animated *procedurally*: ``frame_handler._cache_frame_for`` maps the
Blender frame to a cache frame every time the playhead moves, so changing
"Playback Speed" / "Output FPS" / "Start at Frame" re-times the car for free.

The debris is the opposite — it is **baked**.  ``build_debris`` places each
impact at a TIMELINE frame through ``_blender_frame_for`` (the inverse mapping,
evaluated once at build time) and ``bake_debris`` freezes the rigid-body
simulation into F-curves.  Particle emitters likewise get absolute
``frame_start`` / ``frame_end`` / ``lifetime`` in timeline frames.

So after a build, the two live on different clocks.  Change Playback Speed and
the car re-times while every debris keyframe stays exactly where it was: the
shards fire at the wrong moment, no longer tracking the panel they came off.
That is the "debris does not update its animation when I change the frame rate"
bug this module fixes.

WHAT IT DOES
------------
The build's frame mapping is recorded on the scene (:func:`record_build_timing`).
When the mapping changes, :func:`retime_debris` rescales every debris keyframe
about the start frame so each key keeps the CACHE frame — and therefore the
moment in the crash — it was baked for::

    cache = (f_old - start_old) * playback_old / output_old
    f_new = start_new + cache * output_new / playback_new

which is an affine map ``f_new = start_new + (f_old - start_old) * scale`` with
``scale = (playback_old / output_old) * (output_new / playback_new)``.

Retiming is applied INCREMENTALLY and the stored timing is updated after each
call, so repeated slider drags compose instead of fighting each other.

KNOWN LIMITATION (particles)
----------------------------
Rigid-body debris is baked, so rescaling its F-curves rescales the motion itself
— slow the car down and the shards correctly fly in slow motion.  Particle
systems are NOT baked: only their emission timing lives in frames, while the
solver integrates at ``scene.render.fps``.  Emission stays in sync with the
impact (which is the visible bug), but the particles' own fall speed does not
slow down with the car.  Baking particles would be needed for that; see
``bake_debris`` for the rigid-body precedent.
"""

from typing import Dict, List, Optional, Tuple

try:
    import bpy
except ImportError:  # pragma: no cover - allows import outside Blender
    bpy = None

from .debris_spawn import (
    DEBRIS_COLLECTION,
    GLASS_COLLECTION,
    LAUNCH_PROP,
    SHARD_COLLECTION,
)

#: Scene keys holding the frame mapping the debris was BUILT with.
_KEY_START = "_beamng_debris_build_start"
_KEY_PLAYBACK = "_beamng_debris_build_playback_fps"
_KEY_OUTPUT = "_beamng_debris_build_output_fps"

#: Below this the rescale is a no-op — avoids rewriting thousands of keyframes
#: (and nudging them by float noise) on every mouse-move of a slider drag.
_EPS = 1e-9


def record_build_timing(scene, frame_start: int, playback_fps: float,
                        output_fps: float) -> None:
    """Remember the frame mapping a debris build baked itself against.

    Stored as scene custom properties so it survives save/reload and undo, like
    the rest of the ``_beamng_*`` live state.
    """
    if scene is None:
        return
    scene[_KEY_START] = int(frame_start)
    scene[_KEY_PLAYBACK] = float(playback_fps)
    scene[_KEY_OUTPUT] = float(output_fps)


def build_timing(scene) -> Optional[Tuple[int, float, float]]:
    """The recorded build mapping, or ``None`` when no debris has been built.

    ``None`` also covers debris built by a version older than this module —
    there is nothing to retime *from*, so callers must skip rather than guess.
    """
    if scene is None:
        return None
    if _KEY_PLAYBACK not in scene or _KEY_OUTPUT not in scene:
        return None
    try:
        playback = float(scene[_KEY_PLAYBACK])
        output = float(scene[_KEY_OUTPUT])
        start = int(scene.get(_KEY_START, 0))
    except (TypeError, ValueError):
        return None
    if playback <= 0.0 or output <= 0.0:
        return None
    return start, playback, output


def clear_build_timing(scene) -> None:
    """Forget the recorded mapping (used when debris is cleared)."""
    if scene is None:
        return
    for key in (_KEY_START, _KEY_PLAYBACK, _KEY_OUTPUT):
        if key in scene:
            del scene[key]


def debris_objects() -> List["bpy.types.Object"]:
    """Every object belonging to a debris collection.

    Covers hero shards, glass fragments and the particle emitters.  Shard
    *templates* live in ``SHARD_COLLECTION``: they carry no animation of their
    own (they are instanced by the particle systems), but they are included so a
    template that ever gains keys is not silently left behind.
    """
    if bpy is None:
        return []
    seen = {}
    for name in (DEBRIS_COLLECTION, SHARD_COLLECTION, GLASS_COLLECTION):
        coll = bpy.data.collections.get(name)
        if coll is None:
            continue
        for obj in coll.all_objects:
            seen[obj.name] = obj
    return list(seen.values())


def _retime_action(action, start_old: float, start_new: float,
                   scale: float) -> int:
    """Affinely retime every keyframe of *action*.  Returns keys moved.

    Bezier handles are moved with their key, otherwise a rescale shears the
    interpolation (handles keep their old x while the key moves, so the curve
    develops kinks and can even run backwards between keys).
    """
    moved = 0

    def remap(x: float) -> float:
        return start_new + (x - start_old) * scale

    for fcurve in action.fcurves:
        for kp in fcurve.keyframe_points:
            kp.co.x = remap(kp.co.x)
            kp.handle_left.x = remap(kp.handle_left.x)
            kp.handle_right.x = remap(kp.handle_right.x)
            moved += 1
        fcurve.update()
    return moved


def _retime_particles(obj, start_old: float, start_new: float,
                      scale: float) -> int:
    """Retime the emission window and lifetime of *obj*'s particle systems.

    ``frame_start`` / ``frame_end`` are absolute timeline frames, so they map
    like keyframes.  ``lifetime`` is a DURATION, so it only scales.
    """
    count = 0
    for mod in getattr(obj, "modifiers", ()) or ():
        psys = getattr(mod, "particle_system", None)
        if psys is None:
            continue
        st = psys.settings
        st.frame_start = start_new + (float(st.frame_start) - start_old) * scale
        st.frame_end = start_new + (float(st.frame_end) - start_old) * scale
        # Keep at least a frame of life; a lifetime of 0 deletes every particle
        # on the frame it is born, which reads as "the spray never appeared".
        st.lifetime = max(1, int(round(float(st.lifetime) * scale)))
        count += 1
    return count


def retime_debris(scene, frame_start: int, playback_fps: float,
                  output_fps: float) -> Dict[str, int]:
    """Retime built debris from the recorded mapping to the one given.

    Safe to call on every live-retune: it no-ops when no debris has been built,
    when the recorded mapping is missing, or when the mapping is unchanged.
    Updates the recorded mapping afterwards so successive calls compose.

    Returns a summary dict (``{"objects", "keys", "emitters"}``); all zeros means
    nothing needed doing.
    """
    none = {"objects": 0, "keys": 0, "emitters": 0}
    if bpy is None or scene is None:
        return none
    recorded = build_timing(scene)
    if recorded is None:
        return none
    start_old, playback_old, output_old = recorded

    playback_new = float(playback_fps)
    output_new = float(output_fps)
    start_new = int(frame_start)
    if playback_new <= 0.0 or output_new <= 0.0:
        return none

    # Cache frames per timeline frame, before and after.  scale > 1 stretches
    # the debris out (the car got slower), scale < 1 compresses it.
    rate_old = playback_old / output_old
    rate_new = playback_new / output_new
    scale = rate_old / rate_new

    if abs(scale - 1.0) < _EPS and start_old == start_new:
        return none

    objs = debris_objects()
    if not objs:
        # No debris in the scene (cleared, or never built) — just re-sync the
        # record so a later build/retime is not measured from a stale mapping.
        record_build_timing(scene, start_new, playback_new, output_new)
        return none

    keys = 0
    emitters = 0
    touched = 0
    seen_actions = set()
    for obj in objs:
        did = False
        adt = getattr(obj, "animation_data", None)
        action = getattr(adt, "action", None) if adt is not None else None
        if action is not None and action.name not in seen_actions:
            # Two objects can share an action; retiming it twice would apply the
            # scale squared.
            seen_actions.add(action.name)
            keys += _retime_action(action, start_old, start_new, scale)
            did = True
        n = _retime_particles(obj, start_old, start_new, scale)
        if n:
            emitters += n
            did = True

        # The frame a hero piece becomes visible is re-derived from this after a
        # bake clears animation data, so it has to travel with the keys.
        launch = obj.get(LAUNCH_PROP)
        if launch is not None:
            try:
                obj[LAUNCH_PROP] = int(round(
                    start_new + (float(launch) - start_old) * scale))
                did = True
            except (TypeError, ValueError):
                pass
        if did:
            touched += 1

    # The rigid body world is normally removed by ``bake_debris``, but keep its
    # cache range consistent when a build left it in place (bake skipped).
    rbw = getattr(scene, "rigidbody_world", None)
    if rbw is not None and getattr(rbw, "point_cache", None) is not None:
        pc = rbw.point_cache
        try:
            pc.frame_start = int(round(
                start_new + (float(pc.frame_start) - start_old) * scale))
            pc.frame_end = int(round(
                start_new + (float(pc.frame_end) - start_old) * scale))
        except (TypeError, ValueError, AttributeError):
            pass

    record_build_timing(scene, start_new, playback_new, output_new)
    return {"objects": touched, "keys": keys, "emitters": emitters}
