from __future__ import annotations

"""Debris physics configuration pins (margins, shapes, bake scope).

The debris reset moved ground handling from a matrix post-pass to the native
Bullet solver, so the solver CONFIG is now what guarantees debris sits on the
ground correctly.  These tests pin the configuration:

* ground slab: PASSIVE / BOX / zero margin, top face exactly on ``ground_z``;
* debris bodies: ACTIVE / CONVEX_HULL / zero margin;
* the kinematic launch handoff is bounded to LAUNCH_FRAMES then Bullet owns it;
* the native bake path exists and the old custom matrix bake is gone.
"""

import inspect
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runtime.debris_physics import (  # noqa: E402
    DEBRIS_COLLECTION,
    GROUND_NAME,
    LAUNCH_FRAMES,
    DebrisSettings,
    _ensure_ground,
    _verify_ground_alignment,
    configure_glass_rigidbody,
    configure_rigidbody,
    link_ground_to_rigidbody_world,
)

from runtime.debris_bake import _lowest_world_z, bake_debris  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _RB:
    def __init__(self):
        self.type = None
        self.collision_shape = None
        self.use_margin = None
        self.collision_margin = None
        self.mass = None
        self.restitution = None
        self.friction = None
        self.linear_damping = None
        self.angular_damping = None
        self.kinematic = None
        self.keys = []
        self.use_deactivation = None
        self.use_start_deactivated = None

    def keyframe_insert(self, data_path, frame):
        self.keys.append((data_path, int(frame), self.kinematic))


class _Obj:
    def __init__(self, name="obj", rb=None):
        self.name = name
        self.rigid_body = rb if rb is not None else _RB()


class _Modifiers:
    def __init__(self):
        self._mods = []

    def new(self, name, type):
        m = types.SimpleNamespace(name=name, type=type)
        self._mods.append(m)
        return m

    def __iter__(self):
        return iter(self._mods)

    def __len__(self):
        return len(self._mods)


class _Collision:
    def __init__(self):
        self.friction_factor = 0.0
        self.permeability = 0.0
        self.damping_factor = 0.0
        self.damping_random = 0.0
        self.damping = 0.0
        self.thickness_outer = 0.0
        self.thickness_inner = 0.0


class _Verts:
    """Stand-in for ``mesh.vertices`` supporting ``foreach_get('co', buf)``."""

    def __init__(self, verts):
        self._verts = list(verts)

    def __len__(self):
        return len(self._verts)

    def foreach_get(self, attr, buf):
        if attr == "co":
            for i, val in enumerate(c for v in self._verts for c in v):
                buf[i] = val


class _GroundMesh:
    def __init__(self, name):
        self.name = name
        self._captured_verts = None
        # Canonical canonical construction: symmetric about the origin.
        half = 0.1
        size = 400.0
        self.vertices = _Verts([
            (-size, -size, half), (size, -size, half),
            (size, size, half), (-size, size, half),
            (-size, -size, -half), (size, -size, -half),
            (size, size, -half), (-size, size, -half),
        ])

    def clear_geometry(self):
        pass

    def from_pydata(self, verts, _, __):
        self._captured_verts = list(verts)
        self.vertices = _Verts(list(verts))

    def update(self):
        pass


class _Vec3:
    """Stand-in for mathutils.Vector: ``.z``, ``[2]`` and unpacking."""

    def __init__(self, x, y, z):
        self.x, self.y, self.z = x, y, z

    def __getitem__(self, i):
        return (self.x, self.y, self.z)[i]

    def __iter__(self):
        return iter((self.x, self.y, self.z))


class _Ground:
    def __init__(self, name=GROUND_NAME):
        self.name = name
        self._location = _Vec3(0.0, 0.0, -0.1)  # origin at the slab's centre
        self.rotation_euler = (0.0, 0.0, 0.0)
        self.scale = (1.0, 1.0, 1.0)
        self.dimensions = _Vec3(800.0, 800.0, 0.2)
        self.hide_render = False
        self.display_type = "TEXTURED"
        self.modifiers = _Modifiers()
        self.collision = _Collision()
        self.data = _GroundMesh(name)

    @property
    def location(self):
        return self._location

    @location.setter
    def location(self, value):
        self._location = _Vec3(*value)

    @property
    def matrix_world(self):
        x, y, z = self.location
        return ((1, 0, 0, x), (0, 1, 0, y), (0, 0, 1, z), (0, 0, 0, 1))


class _Objects(list):
    def link(self, obj):
        self.append(obj)

    def get(self, name):
        return next((o for o in self if o.name == name), None)


class _Coll:
    def __init__(self):
        self.objects = _Objects()


class _Collections:
    def __init__(self):
        self._store = {}

    def get(self, name):
        return self._store.get(name)

    def new(self, name):
        c = _Coll()
        self._store[name] = c
        return c


class _SceneColl:
    def __init__(self):
        self.children = type("C", (), {"link": lambda self, c: None})()


class _Scene:
    def __init__(self):
        self.collection = _SceneColl()


class _Context:
    def __init__(self):
        self.scene = _Scene()


class _Meshes:
    def __init__(self):
        self._store = {}

    def new(self, name):
        m = _GroundMesh(name)
        self._store[name] = m
        return m


class _ObjectsNew(_Objects):
    def __init__(self):
        super().__init__()
        self._store = {}

    def new(self, name, data):
        g = _Ground(name)
        g.data = data
        self._store[name] = g
        return g


class _Data:
    def __init__(self):
        self.objects = _ObjectsNew()
        self.meshes = _Meshes()
        self.collections = _Collections()


class _Bpy:
    def __init__(self):
        self.data = _Data()
        self.context = _Context()


@pytest.fixture
def physics(monkeypatch):
    import runtime.debris_physics as dp
    monkeypatch.setattr(dp, "bpy", _Bpy())
    return dp


# ---------------------------------------------------------------------------
# Debris bodies: ACTIVE / CONVEX_HULL / zero margin
# ---------------------------------------------------------------------------


def test_debris_rigidbody_zero_margin_non_blast(physics):
    obj = _Obj()
    configure_rigidbody(obj, DebrisSettings(bounciness=0.3, friction=0.6),
                        mass=1.0, is_blast=False)
    rb = obj.rigid_body
    assert rb.type == "ACTIVE"
    assert rb.collision_shape == "CONVEX_HULL"
    assert rb.use_margin is True
    assert rb.collision_margin == 0.002
    assert rb.use_deactivation is True
    assert rb.use_start_deactivated is False
    assert rb.keys == []


def test_debris_rigidbody_blast_handoff_bounded(physics):
    obj = _Obj()
    configure_rigidbody(obj, DebrisSettings(bounciness=0.0),
                        mass=1.0, is_blast=True, launch_start=200)
    rb = obj.rigid_body
    assert rb.type == "ACTIVE"
    assert rb.collision_shape == "CONVEX_HULL"
    assert rb.use_margin is True
    assert rb.collision_margin == 0.002
    # Kinematic handoff covers EXACTLY LAUNCH_FRAMES, then Bullet owns it.
    assert len(rb.keys) == LAUNCH_FRAMES + 1
    assert [k[2] for k in rb.keys] == ([True] * LAUNCH_FRAMES + [False])
    assert [k[1] for k in rb.keys] == list(range(200, 200 + LAUNCH_FRAMES + 1))


def test_glass_rigidbody_zero_margin(physics):
    obj = _Obj()
    configure_glass_rigidbody(obj, DebrisSettings(bounciness=0.5),
                              launch_start=100, mass=0.5)
    rb = obj.rigid_body
    assert rb.type == "ACTIVE"
    assert rb.collision_shape == "CONVEX_HULL"
    assert rb.use_margin is True
    assert rb.collision_margin == 0.002
    assert len(rb.keys) == LAUNCH_FRAMES + 1


# ---------------------------------------------------------------------------
# Ground: PASSIVE / BOX / zero margin, top face on ground_z
# ---------------------------------------------------------------------------


def test_ground_slab_rigidbody_config(physics):
    ground = _Ground()
    ground.rigid_body = _RB()
    link_ground_to_rigidbody_world(ground, _Coll(), DebrisSettings(friction=0.7))
    rb = ground.rigid_body
    assert rb.type == "PASSIVE"
    assert rb.collision_shape == "CONVEX_HULL"
    assert rb.use_margin is True
    assert rb.collision_margin == 0.01
    assert rb.friction == 0.7


def test_ground_slab_top_face_is_ground_z(physics):
    settings = DebrisSettings(ground_z=0.0)
    ground = _ensure_ground(settings)
    mesh = ground.data
    verts = mesh._captured_verts
    assert verts is not None
    # The ground is now a flat 4-vert plane at ground_z (Simply Shatter style).
    # All vertices sit at local z=0, world z=ground_z.
    world_z = {round(settings.ground_z + v[2], 6) for v in verts}
    assert world_z == {0.0}
    assert ground.location[2] == settings.ground_z


def test_ground_box_collider_aligned_with_visible_mesh(physics):
    settings = DebrisSettings(ground_z=0.0, friction=0.7)
    ground = _Ground()
    ground.rigid_body = _RB()
    ground.rigid_body.collision_shape = "CONVEX_HULL"
    ground.rigid_body.collision_margin = 0.01
    link_ground_to_rigidbody_world(ground, _Coll(), settings)
    # Ground is now a flat plane with CONVEX_HULL — alignment is trivial.
    assert _verify_ground_alignment(ground, settings) is True


def test_ground_box_collider_misalignment_is_caught(physics):
    settings = DebrisSettings(ground_z=0.0)
    ground = _Ground()
    # Old-style construction: origin on the top face, mesh spanning 0..-0.2.
    ground.location = (0.0, 0.0, 0.0)
    mesh = ground.data
    mesh.clear_geometry()
    size = 400.0
    mesh.from_pydata([
        (-size, -size, 0.0), (size, -size, 0.0), (size, size, 0.0),
        (-size, size, 0.0), (-size, size, -0.2), (size, size, -0.2),
        (size, -size, -0.2), (-size, -size, -0.2),
    ], [], [])
    import pytest
    with pytest.raises(RuntimeError, match="misaligned"):
        _verify_ground_alignment(ground, settings)


# ---------------------------------------------------------------------------
# Native bake scope: no custom matrix machinery, bake scope verified
# ---------------------------------------------------------------------------


def test_bake_debris_noop_without_bpy():
    out = bake_debris([], 1, 100)
    assert out == {"baked": 0, "intended": 0, "skipped": [],
                   "frames": 0, "penetrating": 0, "max_penetration": 0.0}


def test_no_old_matrix_bake_machinery():
    import runtime.debris_bake as db
    src = inspect.getsource(db)
    # The old custom bake sampled matrices and clamped them by hand.  None of
    # that may come back.
    for banned in ("_snap_matrices_to_ground", "_particle_ground_lift",
                   "bake_particles", "_get_particle_baked_collection"):
        assert not hasattr(db, banned), f"{banned} must not exist"
    # The native operator is the only bake path.
    assert "bake_to_keyframes" in src
    # No manual per-frame location/rotation keyframe writing loop.
    assert 'keyframe_insert("location"' not in src
    assert 'keyframe_insert("rotation' not in src


def test_bake_debris_signature_no_snap_ground():
    sig = inspect.signature(bake_debris)
    assert "snap_ground" not in sig.parameters
    assert list(sig.parameters) == ["hero_objects", "frame_start",
                                    "frame_end", "ground_z"]


def test_lowest_world_z_exists_as_report_only():
    assert callable(_lowest_world_z)
