from __future__ import annotations

"""Retiming baked debris when the live frame mapping changes.

The bug this pins: ``build_debris`` bakes every debris keyframe, particle window
and launch prop to an absolute TIMELINE frame at build-time fps.  The car
re-times procedurally (``frame_handler._cache_frame_for``), so changing
Playback Speed / Output FPS / Start at Frame desyncs the debris from the crash
— the shards fire at the wrong moment.  ``retime_debris`` must rescale every
baked key about the start frame so each key keeps the CACHE frame it was baked
for::

    f_new = start_new + (f_old - start_old) * scale
    scale  = (playback_old / output_old) / (playback_new / output_new)

and successive calls must COMPOSE (each call re-records the mapping), not apply
the scale twice.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class _Kp:
    def __init__(self, x: float):
        self.co = type("C", (), {"x": float(x)})()
        self.handle_left = type("H", (), {"x": float(x)})()
        self.handle_right = type("H", (), {"x": float(x)})()


class _FCurve:
    def __init__(self, xs):
        self.keyframe_points = [_Kp(x) for x in xs]
        self.updated = 0

    def update(self):
        self.updated += 1


class _Action:
    def __init__(self, name, keys):
        self.name = name
        self.fcurves = [_FCurve(keys)]


class _ParticleSettings:
    def __init__(self, start, end, lifetime):
        self.frame_start = start
        self.frame_end = end
        self.lifetime = lifetime


class _ParticleMod:
    def __init__(self, settings):
        self.particle_system = type("PS", (), {"settings": settings})()


class _Obj:
    def __init__(self, name, action=None, parts=None, launch=None):
        self.name = name
        self.modifiers = parts or []
        self.animation_data = (
            type("AD", (), {"action": action})() if action is not None else None)
        self._props = {}
        if launch is not None:
            self._props["_beamng_debris_launch"] = launch
        self._changed = []

    def get(self, key, default=None):
        return self._props.get(key, default)

    def __setitem__(self, key, value):
        self._changed.append((key, value))
        self._props[key] = value


class _Coll:
    def __init__(self, name, objects):
        self.name = name
        self.all_objects = objects


class _RigidWorld:
    def __init__(self, start, end):
        self.point_cache = type("PC", (), {
            "frame_start": start, "frame_end": end})()


def _make_bpy(objs):
    bpy = type("Bpy", (), {"data": type("D", (), {})()})()

    class _Colls:
        def __init__(self, items):
            self._items = list(items)

        def get(self, name):
            return next((c for c in self._items if c.name == name), None)

    colls = _Colls([
        _Coll("BeamNG Debris", [objs[0]]),
        _Coll("BeamNG Debris Shards", [objs[1]]),
        _Coll("BeamNG Debris Glass", [objs[2]]),
    ])
    bpy.data.collections = colls
    return bpy


def _scene():
    scene = type("S", (dict,), {})()
    scene.rigidbody_world = None
    return scene


@pytest.fixture
def debris_env(monkeypatch):
    """Recorded mapping: start=100, playback=24, output=60.
    Keys at frames 1500/1600, particle window 1500..1700 life 100,
    launch prop 1500.  Under this mapping cache frame 560 = (1500-100)*24/60.
    """
    objs = [
        _Obj("hero", action=_Action("act1", [1500.0, 1600.0]),
             parts=[_ParticleMod(_ParticleSettings(1500, 1700, 100))],
             launch=1500),
        _Obj("shard_template"),
        _Obj("glass"),
    ]
    bpy = _make_bpy(objs)

    import runtime.debris_retime as dr
    monkeypatch.setattr(dr, "bpy", bpy)
    scene = _scene()
    scene.rigidbody_world = _RigidWorld(1480, 1800)
    dr.record_build_timing(scene, 100, 24.0, 60.0)
    return dr, scene, objs


def test_affine_remap_keeps_cache_frame(debris_env):
    """Playback 24->12 at output 60: scale = (24/60)/(12/60) = 2.0.
    A key at timeline 1500 was cache 560; the new timeline frame for the same
    cache frame is 100 + 560*60/12 = 2900.
    """
    dr, scene, objs = debris_env
    out = dr.retime_debris(scene, 100, 12.0, 60.0)
    assert out["objects"] == 1 and out["keys"] == 2 and out["emitters"] == 1
    kp = objs[0].animation_data.action.fcurves[0].keyframe_points[0]
    assert kp.co.x == pytest.approx(2900.0)
    assert objs[0].animation_data.action.fcurves[0].keyframe_points[1].co.x == \
        pytest.approx(3100.0)
    assert kp.handle_left.x == pytest.approx(2900.0)
    assert kp.handle_right.x == pytest.approx(2900.0)
    assert objs[0].animation_data.action.fcurves[0].updated == 1
    ps = objs[0].modifiers[0].particle_system.settings
    assert ps.frame_start == pytest.approx(2900.0)
    assert ps.frame_end == pytest.approx(3300.0)
    assert ps.lifetime == 200
    assert objs[0].get("_beamng_debris_launch") == 2900


def test_composition_single_scale(debris_env):
    """Calling twice with the same target must not scale twice."""
    dr, scene, objs = debris_env
    dr.retime_debris(scene, 100, 12.0, 60.0)
    dr.retime_debris(scene, 100, 12.0, 60.0)
    kp = objs[0].animation_data.action.fcurves[0].keyframe_points[0]
    assert kp.co.x == pytest.approx(2900.0)


def test_noop_when_mapping_unchanged(debris_env):
    """An unchanged mapping moves nothing and re-records nothing."""
    dr, scene, objs = debris_env
    out = dr.retime_debris(scene, 100, 24.0, 60.0)
    assert out == {"objects": 0, "keys": 0, "emitters": 0}
    kp = objs[0].animation_data.action.fcurves[0].keyframe_points[0]
    assert kp.co.x == pytest.approx(1500.0)


def test_start_shift_moves_keys_by_offset(debris_env):
    """Start 100->200 with identical fps: keys shift +100, scale 1.0."""
    dr, scene, objs = debris_env
    dr.retime_debris(scene, 200, 24.0, 60.0)
    kp = objs[0].animation_data.action.fcurves[0].keyframe_points[0]
    assert kp.co.x == pytest.approx(1600.0)


def test_no_record_no_retime(debris_env):
    """A scene without the recorded mapping is skipped, not guessed at."""
    dr, _, objs = debris_env
    scene = _scene()
    out = dr.retime_debris(scene, 100, 12.0, 60.0)
    assert out == {"objects": 0, "keys": 0, "emitters": 0}


def test_launch_prop_and_rigid_cache_follow(debris_env):
    """Output 60->30 at playback 24: scale = (24/60)/(24/30) = 0.5.
    Launch 1500 -> 800; the rigid world cache window scales about the start.
    """
    dr, scene, objs = debris_env
    dr.retime_debris(scene, 100, 24.0, 30.0)
    assert objs[0].get("_beamng_debris_launch") == 800
    pc = scene.rigidbody_world.point_cache
    assert pc.frame_start == pytest.approx(790.0)
    assert pc.frame_end == pytest.approx(950.0)
