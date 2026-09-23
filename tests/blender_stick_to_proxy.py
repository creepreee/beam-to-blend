"""Headless Blender check for sticking objects to the proxy (vertex parenting).

Run with:
    blender --background --python tests/blender_stick_to_proxy.py

Builds a synthetic proxy mesh (2 triangles) parented to a rigid Empty with
baked keyframes, then verifies in real Blender that:
  * stick_objects_to_proxy() vertex-parents the object (VERTEX_3) to the
    triangle nearest its origin,
  * the world transform (position AND rotation) is preserved bit-exactly at
    stick time,
  * as frames advance the object FOLLOWS the proxy triangle through BOTH the
    rigid Empty keyframes AND direct per-frame proxy mesh deformation
    (matched exactly against the evaluated triangle matrix),
  * the motion is real — the object's world at the last frame differs from the
    first,
  * unstick_objects() restores parenting, keeps the world transform, and
    removes the stuck marker.

Exits non-zero on any failure.
"""

import os
import sys

import bpy

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Evict any add-on-bundled runtime so we test THIS tree (see blender_tyre_contact.py).
sys.path.insert(0, _REPO)
for _mod in [m for m in sys.modules
             if m == "runtime" or m.startswith("runtime.")
             or m == "importer" or m.startswith("importer.")]:
    del sys.modules[_mod]

from mathutils import Matrix, Quaternion, Vector

from runtime import proxy_mesh
from runtime.stick_to_proxy import (
    stick_objects_to_proxy,
    unstick_objects,
    _replicated_parent_matrix,
)

_FAILURES = []


def check(cond, msg):
    if cond:
        print(f"[STICK][ok]   {msg}")
    else:
        print(f"[STICK][FAIL] {msg}")
        _FAILURES.append(msg)


def vdiff(a, b):
    return [round(float(x - y), 6) for x, y in zip(a, b)]


def build_scene():
    scene = bpy.context.scene
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o, do_unlink=True)
    for m in list(bpy.data.meshes):
        bpy.data.meshes.remove(m)
    scene.frame_start = 1
    scene.frame_end = 20
    scene.frame_set(1)

    # Rigid Empty with baked motion keyframes.
    rigid = bpy.data.objects.new("BeamNG_Proxy__Rigid", None)
    scene.collection.objects.link(rigid)
    rigid.location = (0.0, 0.0, 0.0)
    rigid.keyframe_insert("location", frame=1)
    rigid.rotation_euler = (0.0, 0.0, 0.0)
    rigid.keyframe_insert("rotation_euler", frame=1)
    rigid.location = (5.0, 2.0, 1.0)
    rigid.rotation_euler = (0.4, 0.2, 0.7)
    rigid.keyframe_insert("location", frame=10)
    rigid.keyframe_insert("rotation_euler", frame=10)
    rigid.location = (-3.0, 4.0, 2.0)
    rigid.rotation_euler = (-0.3, 0.5, 1.2)
    rigid.keyframe_insert("location", frame=20)
    rigid.keyframe_insert("rotation_euler", frame=20)

    # Proxy mesh: two triangles over a shared diagonal of a quad.
    me = bpy.data.meshes.new("proxy_mesh")
    me.from_pydata(
        [(-1.0, -1.0, 0.0), (1.0, -1.0, 0.0), (1.0, 1.0, 0.0), (-1.0, 1.0, 0.0)],
        [],
        [(0, 1, 2), (0, 2, 3)],
    )
    proxy = bpy.data.objects.new(proxy_mesh._PROXY_NAME, me)
    scene.collection.objects.link(proxy)
    proxy.parent = rigid
    proxy.parent_type = "OBJECT"
    proxy.matrix_parent_inverse = Matrix.Identity(4)
    proxy.matrix_local = Matrix.Identity(4)
    bpy.context.view_layer.update()

    proxy_mesh._proxy_object = proxy

    # Test object to stick — an offset origin away from the surface so the
    # fixed offset baked into parentinv is measurable.
    tme = bpy.data.meshes.new("stick_test_mesh")
    tme.from_pydata(
        [(-0.5, -0.5, 0.0), (0.5, -0.5, 0.0), (0.5, 0.5, 0.0), (-0.5, 0.5, 0.0)],
        [(0, 1), (1, 2), (2, 3), (3, 0)],
        [(0, 1, 2, 3)],
    )
    obj = bpy.data.objects.new("stick_test", tme)
    scene.collection.objects.link(obj)
    obj.matrix_world = Matrix.Translation(Vector((0.5, 0.5, 2.0)))
    bpy.context.view_layer.update()
    return scene, proxy, rigid, obj


def main():
    scene, proxy, rigid, obj = build_scene()

    W0 = obj.matrix_world.copy()
    n = stick_objects_to_proxy([obj])
    check(n == 1, "stick_objects_to_proxy stuck 1 object")
    check(obj.parent is proxy, "object parented to proxy")
    check(obj.parent_type == "VERTEX_3", f"VERTEX_3 parenting (got {obj.parent_type})")
    check(len(obj.parent_vertices) == 3, "3 parent vertices")

    check(vdiff(obj.matrix_world.translation, W0.translation) == [0.0, 0.0, 0.0],
          f"world translation preserved at stick time (got {vdiff(obj.matrix_world.translation, W0.translation)})")
    check(vdiff(obj.matrix_world.to_euler(), W0.to_euler()) == [0.0, 0.0, 0.0],
          f"world rotation preserved at stick time (got {vdiff(obj.matrix_world.to_euler(), W0.to_euler())})")

    indices = tuple(obj.parent_vertices)
    f_stick = scene.frame_current
    pm_stick = _replicated_parent_matrix(proxy, indices)

    first_world = obj.matrix_world.copy()
    moved = False
    for f in (2, 5, 10, 15, 20):
        # Deform the proxy mesh directly from frame 10 on (vertex animation)
        # to prove the followed triangle is the EVALUATED mesh.
        if f >= 10:
            for vi in indices:
                proxy.data.vertices[vi].co[0] += 0.05 * (f - 10)
                proxy.data.vertices[vi].co[1] += 0.03 * (f - 10)
                proxy.data.vertices[vi].co[2] += 0.02 * (f - 10)
            proxy.data.update()
            proxy.data.update_tag()
            proxy.update_tag()

        scene.frame_set(f)
        bpy.context.view_layer.update()

        pm = _replicated_parent_matrix(proxy, indices)
        expected = pm @ pm_stick.inverted() @ W0
        w = obj.matrix_world
        if not moved and vdiff(w.translation, first_world.translation) != [0.0, 0.0, 0.0]:
            moved = True
        check(vdiff(w.translation, expected.translation) == [0.0, 0.0, 0.0],
              f"frame {f}: follows position (delta {vdiff(w.translation, expected.translation)})")
        check(vdiff(w.to_euler(), expected.to_euler()) == [0.0, 0.0, 0.0],
              f"frame {f}: follows rotation (delta {vdiff(w.to_euler(), expected.to_euler())})")

    check(moved, "object actually moves with the proxy (rigid + mesh deformation)")

    # Unstick: parenting cleared, world preserved, marker removed.
    W_last = obj.matrix_world.copy()
    un = unstick_objects([obj])
    check(un == 1, "unstick_objects unstuck 1 object")
    check(obj.parent is None, "parent cleared after unstick")
    check(vdiff(obj.matrix_world.translation, W_last.translation) == [0.0, 0.0, 0.0],
          f"world preserved after unstick (delta {vdiff(obj.matrix_world.translation, W_last.translation)})")
    check("_beamng_stuck_to_proxy" not in obj, "stuck marker removed")

    print()
    if _FAILURES:
        print(f"[STICK] {len(_FAILURES)} failure(s)")
        for m in _FAILURES:
            print("  -", m)
        sys.exit(1)
    print("[STICK] all checks passed")


if __name__ == "__main__":
    main()