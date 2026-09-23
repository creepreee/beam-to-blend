from __future__ import annotations

"""Stick objects to the proxy mesh via Blender-native vertex parenting.

A child parented to 3 proxy vertices (``parent_type = "VERTEX_3"``) follows
the triangle's **centroid** for position AND the triangle's own orientation
for rotation — Blender's ``ob_parvert3`` builds the parent matrix from
``tri_to_quat(v1, v2, v3)`` + ``(v1+v2+v3)/3`` (object.cc).  That is exactly
"extract the rotations from the mesh" with zero per-frame Python: vertex
parenting reads the proxy's **evaluated** mesh, so it tracks the frame
handler's per-frame vertex writes during playback and the MESH_CACHE modifier
during a fluid bake, and the rigid motion comes from the proxy's own parent
Empty.

Stick time
----------
For each selected object we find the proxy triangle nearest to its origin,
then set up the parenting with a keep-transform so the object does not move:

1.  Park the object at its current world matrix ``W`` (parent = None,
    parentinv = I, matrix_local = W).
2.  Set parent = proxy, parent_type = "VERTEX_3", parent_vertices = [i,j,k].
    Now Blender evaluates ``world = parentmat @ I @ W``, so
    ``parentmat = eval_world @ W^-1`` — read back from the evaluated object.
    This uses Blender's *own* ``tri_to_quat``, so no formula replication and
    it automatically uses the evaluated proxy positions (MESH_CACHE .mdd if
    the fluid effector is prepared).
3.  Set ``parentinv = parentmat^-1 @ W`` and ``matrix_local = I``.  The object
    stays exactly at ``W`` and afterwards follows the triangle.

Fallback: if the object is not in the evaluated depsgraph (e.g. an orphan
not linked to any collection — can't normally happen for viewport-selected
objects), we replicate ``tri_to_quat_ex`` (verified against Blender 4.5.9's
evaluation) using the **evaluated** proxy vertex positions.
"""

import math

try:  # pragma: no cover - only inside Blender
    import bpy
    from mathutils import Matrix, Quaternion, Vector
except ImportError:  # pragma: no cover
    bpy = None
    Matrix = Quaternion = Vector = None

from .proxy_mesh import _PROXY_NAME

#: Custom property stamped on each stuck object (list of the 3 proxy vertex
#: indices it is parented to).  Also marks it as stuck for the unstick op.
_STICK_PROP = "_beamng_stuck_to_proxy"


def _tri_to_quat(v1, v2, v3):
    """Replicate Blender's ``tri_to_quat`` (math_rotation_c.cc) for 3 points.

    Returns a mathutils.Quaternion.  Only used by the fallback path.
    """
    # normal_tri_v3: n1 = v1-v2, n2 = v2-v3, n = n1 x n2 (normalised).
    n1 = (v1[0] - v2[0], v1[1] - v2[1], v1[2] - v2[2])
    n2 = (v2[0] - v3[0], v2[1] - v3[1], v2[2] - v3[2])
    n = (n1[1] * n2[2] - n1[2] * n2[1],
         n1[2] * n2[0] - n1[0] * n2[2],
         n1[0] * n2[1] - n1[1] * n2[0])
    ln = math.sqrt(sum(x * x for x in n))
    if ln < 1e-30:
        return Quaternion((1.0, 0.0, 0.0, 0.0))
    no = (n[0] / ln, n[1] / ln, n[2] / ln)
    return _tri_to_quat_ex(v1, v2, no)


def _tri_to_quat_ex(v1, v2, no):
    """Replicate ``tri_to_quat_ex`` with ``no_orig`` given."""
    vec = list(no)
    axis = (vec[1], -vec[0], 0.0)
    ln = math.sqrt(axis[0] * axis[0] + axis[1] * axis[1] + axis[2] * axis[2])
    if ln == 0.0:
        axis = (1.0, 0.0, 0.0)
    else:
        axis = (axis[0] / ln, axis[1] / ln, axis[2] / ln)

    angle = -0.5 * math.acos(max(-1.0, min(1.0, vec[2])))
    co, si = math.cos(angle), math.sin(angle)
    q1 = Quaternion((co, axis[0] * si, axis[1] * si, 0.0))

    imat = q1.to_matrix().inverted()
    edge = Vector((v2[0] - v1[0], v2[1] - v1[1], v2[2] - v1[2]))
    edge_rot = imat @ edge
    edge_rot.z = 0.0
    el = edge_rot.length
    edge_rot = edge_rot / el if el > 1e-30 else Vector((1.0, 0.0, 0.0))

    angle = 0.5 * math.atan2(edge_rot.y, edge_rot.x)
    co, si = math.cos(angle), math.sin(angle)
    q2 = Quaternion((co, 0.0, 0.0, si))
    return q1 @ q2  # mul_qt_qtqt(q1, q2): q2 applied first


def _proxy_triangles(proxy):
    """Return the proxy's triangles at the current frame.

    Returns ``(verts, faces)`` where *verts* is a list of ``(x, y, z)`` tuples
    in proxy-LOCAL space and *faces* is a list of ``(i0, i1, i2)`` index
    tuples.  Both are ``None`` when the proxy has no usable geometry.
    """
    me = proxy.data
    if me is None or not me.vertices or not me.polygons:
        return None, None
    verts = [tuple(v.co[:3]) for v in me.vertices]
    faces = []
    for p in me.polygons:
        v = p.vertices
        if len(v) >= 3:
            faces.append((int(v[0]), int(v[1]), int(v[2])))
    if not faces:
        return None, None
    return verts, faces


def _nearest_triangle(local_pt, verts, faces):
    """Index of the triangle closest to *local_pt* (proxy-local space).

    Uses ``mathutils.geometry.closest_point_on_tri`` (exact closest point on a
    triangle; ``distance_point_to_tri`` is not exposed in all Blender builds).
    Degenerate triangles yield zero-area results and are skipped by the ``<``
    comparison.
    """
    from mathutils.geometry import closest_point_on_tri

    best_d2 = float("inf")
    best = None
    for i, (i0, i1, i2) in enumerate(faces):
        try:
            cp = closest_point_on_tri(local_pt, verts[i0], verts[i1], verts[i2])
            d2 = (cp - local_pt).length_squared
        except Exception:
            continue
        if d2 < best_d2:
            best_d2 = d2
            best = i
    return best


def _evaluated_parent_matrix(child, world):
    """Best-effort parentmat via the depsgraph; None if child isn't in it.

    Returns ``(parentmat, ok)``.  When *ok* is False the child is not in the
    evaluated depsgraph and the caller must fall back to direct replication.

    *world* is the child's world matrix captured BEFORE parenting; it must be
    passed in because Blender rewrites ``matrix_local`` when ``parent_type`` is
    set to VERTEX_3, so ``child.matrix_local`` is no longer trustworthy here.
    """
    bpy.context.view_layer.update()
    deps = bpy.context.evaluated_depsgraph_get()
    ev = deps.objects.get(child.name)
    if ev is None:
        return None, False
    parentmat = ev.matrix_world @ world.inverted()
    return parentmat, True


def _replicated_parent_matrix(proxy, indices):
    """Compute parentmat = proxy.world @ [tri_to_quat | centroid] directly.

    Uses the **evaluated** proxy vertex positions (depsgraph) so it matches
    what Blender evaluates even with a MESH_CACHE modifier active.
    """
    deps = bpy.context.evaluated_depsgraph_get()
    ev_proxy = deps.objects.get(proxy.name)
    ev_mesh = ev_proxy.data if ev_proxy is not None else proxy.data
    pos = [tuple(v.co[:3]) for v in ev_mesh.vertices]
    v = [Vector(pos[i]) for i in indices]
    q = _tri_to_quat(v[0], v[1], v[2])
    m = q.to_matrix().to_4x4()
    m.translation = (v[0] + v[1] + v[2]) / 3.0
    return proxy.matrix_world @ m


def _apply_vertex_parent(proxy, child, indices) -> bool:
    """Vertex-parent *child* to 3 proxy vertices, keeping its world transform."""
    W = child.matrix_world.copy()

    # Park at W with identity inverse so the eval gives parentmat directly.
    child.parent = None
    child.parent_type = "OBJECT"
    child.matrix_parent_inverse = Matrix.Identity(4)
    child.matrix_local = W

    child.parent = proxy
    child.parent_type = "VERTEX_3"
    child.parent_vertices = indices

    parentmat, ok = _evaluated_parent_matrix(child, W)
    if not ok:
        # Not in the depsgraph (orphan / excluded collection) — replicate.
        try:
            parentmat = _replicated_parent_matrix(proxy, indices)
        except Exception:
            return False

    # Order matters: assigning matrix_local first lets Blender recompute the
    # inverse, then the explicit parentinv wins and the object stays at W.
    child.matrix_local = Matrix.Identity(4)
    child.matrix_parent_inverse = parentmat.inverted() @ W
    bpy.context.view_layer.update()

    child[_STICK_PROP] = list(indices)
    return True


def stick_objects_to_proxy(objects) -> int:
    """Stick each object in *objects* to the nearest proxy triangle.

    Returns how many objects were (already) stuck.  Requires the proxy object
    (resolved by name — no reliance on proxy_mesh module state, so this keeps
    working after an addon reload or file reopen).
    """
    if bpy is None:
        return 0
    proxy = bpy.data.objects.get(_PROXY_NAME)
    if proxy is None or proxy.type != "MESH":
        return 0

    verts, faces = _proxy_triangles(proxy)
    if verts is None or faces is None:
        return 0

    world_inv = proxy.matrix_world.inverted()
    stuck = 0
    for obj in list(objects):
        if obj is proxy:
            continue
        if obj.parent is proxy and obj.parent_type in ("VERTEX", "VERTEX_3"):
            stuck += 1
            continue
        local_pt = world_inv @ obj.matrix_world.translation
        fi = _nearest_triangle(local_pt, verts, faces)
        if fi is None:
            continue
        if _apply_vertex_parent(proxy, obj, tuple(faces[fi])):
            stuck += 1
    return stuck


def unstick_objects(objects) -> int:
    """Clear vertex parenting to the proxy for each object in *objects*.

    Returns how many objects were unstuck.
    """
    if bpy is None:
        return 0
    n = 0
    for obj in list(objects):
        if (obj.parent is not None
                and obj.parent.name == _PROXY_NAME
                and obj.parent_type in ("VERTEX", "VERTEX_3")):
            W = obj.matrix_world.copy()
            obj.parent = None
            obj.parent_type = "OBJECT"
            obj.matrix_parent_inverse = Matrix.Identity(4)
            obj.matrix_local = W
            if _STICK_PROP in obj:
                del obj[_STICK_PROP]
            n += 1
    return n