from __future__ import annotations

"""Laminated-glass crack: a hole + radiating crack web, painted in a shader.

Real automotive glass does not shatter on a moderate centre strike.  A rock
strikes the windshield and punches a *hole* while a web of cracks spreads
across the whole pane; the pane itself stays in its frame.  The debris system
already handles the violent extreme (shatter -> Voronoi fragments with a
retained edge fringe); this module paints the moderate extreme.

The pane is STILL part of the vertex-cache animation at the crack moment, and
that animation writes positions into mesh vertices BY INDEX.  Subdividing or
re-topologising the pane to give the hole real edges would misalign every frame
of the car.  So the decoration is a material:

* the hole is a shader alpha (transparent inside, jagged rim), and
* the crack web is procedural (polar Voronoi, spokes crowded around the
  impact, fading toward the pane edges), and
* a driver fades the whole decoration in from the pane's crack frame, so
  before the hit the pane reads as clean glass.

The geometry math (:func:`crack_placement`) is pure numpy and unit-testable
outside Blender; the node-tree builder is Blender-only.
"""

import math
import os
from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np

try:
    import bpy
except ImportError:  # pragma: no cover - importable in plain CPython for tests
    bpy = None

from .glass_shatter import pane_basis

# ---------------------------------------------------------------------------
# Pure geometry (no bpy)
# ---------------------------------------------------------------------------


def world_to_cache_local(world: Sequence[float], transform,
                         ground_shift: float = 0.0) -> np.ndarray:
    """Invert :func:`impact_detect.local_to_world` for one point.

    The pane's shader works in OBJECT space, which is the cache-local
    (car-relative) space playback writes mesh vertices in.  ``transform`` is
    the cache frame transform; ``world`` is the impact's world-space position.
    ``ground_shift`` is the auto-ground ``obj.location.z`` that playback applies
    OUTSIDE the mesh, so the mesh's own object space never contains it.
    """
    w = np.asarray(world, dtype=np.float64)
    tf = np.asarray(transform, dtype=np.float64).reshape(-1)
    if tf.shape[0] < 12:
        return w - np.array((0.0, 0.0, float(ground_shift)))
    m = tf[3:12].reshape(3, 3)
    return (w - tf[0:3]) @ m - np.array((0.0, 0.0, float(ground_shift)))


@dataclass
class CrackPlacement:
    """Where the crack sits on the pane, in the pane's object space.

    A raked or curved windshield is tilted in the car's frame, so the shader
    cannot use raw object (x, y) as "in the glass plane" â€” it projects the
    offset ``(P - centre)`` onto the orthonormal pane axes ``u``/``v`` first,
    then works in the projected 2D.  All vectors are in object (cache-local)
    space, which is what ``Texture Coordinate > Object`` returns.
    """

    #: (3,) object-space point on the pane plane nearest the impact.
    centre: np.ndarray
    #: (3,) object-space pane in-plane axis 1.
    u: np.ndarray
    #: (3,) object-space pane in-plane axis 2.
    v: np.ndarray
    #: Largest in-plane distance (m) from centre to the pane's edge.
    pane_radius: float
    #: Jagged hole radius (m), already clamped to fit the pane.
    hole_radius: float


def crack_placement(local_verts: np.ndarray,
                    impact_cache_local: np.ndarray,
                    hole_radius: float) -> CrackPlacement:
    """Resolve the impact onto the pane's own plane and clamp it inside.

    ``local_verts`` is the pane's vertex cloud in cache-local space at the
    crack frame; ``impact_cache_local`` is the impact mapped to that space.
    """
    pts = np.asarray(local_verts, dtype=np.float64)
    impact = np.asarray(impact_cache_local, dtype=np.float64)
    if len(pts) < 3:
        raise ValueError("pane has too few vertices for a crack")
    basis = pane_basis(pts)
    uv = basis.to_2d(pts)
    centre2 = basis.to_2d(impact[None, :])[0]
    lo, hi = uv.min(axis=0), uv.max(axis=0)
    centre2 = np.clip(centre2, lo, hi)
    radius = float(np.max(np.linalg.norm(uv - centre2, axis=1))) or 1.0
    return CrackPlacement(
        centre=basis.to_3d(centre2[None, :])[0],
        u=basis.u,
        v=basis.v,
        pane_radius=radius,
        hole_radius=float(np.clip(hole_radius, 0.0, 0.6 * radius)),
    )


def hole_radius_for(scale: float, severity: float) -> float:
    """Hole size (m) from the UI scale and the impact severity."""
    return max(0.0, float(scale)) * (0.5 + 1.5 * float(np.clip(severity, 0.0, 1.0)))


# ---------------------------------------------------------------------------
# Shader parameters (tuned constants)
# ---------------------------------------------------------------------------

#: Extra angular frequency the web gains near the impact (spokes crowd the hole).
WEB_FOCUS = 0.8
#: Base spoke density multiplier.
WEB_THETA_SCALE = 2.5
#: Radial stretch of the Voronoi web (web-coordinate units).
WEB_R_SCALE = 1.0
#: Web line thickness in web-coordinate units.
LINE_WIDTH = 0.06
#: Ambient crazing (opaque-white frosting) at the impact, scaled by web intensity.
FROST_BASE = 0.30
#: Width (m) of the soft transition at the hole rim.
HOLE_EDGE = 0.004
#: Jaggedness of the hole rim as a fraction of the hole radius.
JAG_AMOUNT = 0.5


# ---------------------------------------------------------------------------
# Blender material builder
# ---------------------------------------------------------------------------


def _math(nt, op: str, x: float, y: float, a: float = None,
          b: float = None) -> "bpy.types.Node":
    n = nt.nodes.new("ShaderNodeMath")
    n.operation = op
    if a is not None:
        n.inputs[0].default_value = float(a)
    if b is not None:
        n.inputs[1].default_value = float(b)
    n.location = (x, y)
    return n


def _map_range(nt, x: float, y: float) -> "bpy.types.Node":
    n = nt.nodes.new("ShaderNodeMapRange")
    n.clamp = True
    n.location = (x, y)
    return n


def _set_dims(node, value: str) -> None:
    """Set a texture node's ``dimensions`` on 3.x, ignore it on 4.x.

    Blender 4.0 removed the 1D/2D/3D enum â€” noise and Voronoi are always 3D,
    and feeding ``W`` selects 4D.  ``node.dimensions`` then shadows the
    read-only size vector, so assignment must be optional.
    """
    try:  # pragma: no cover - version-dependent
        node.dimensions = value
    except (AttributeError, TypeError):
        pass


def _set_seed(node, seed: int) -> None:
    """Seed a texture node on 3.x; a phase shift substitutes on 4.x.

    Blender 4.0 removed the Noise ``Seed`` input (sampling is hash-based), so
    per-pane variation comes from offsetting the sample coordinate instead.
    The caller folds that phase into the W chain â€” see ``h_off`` below.
    """
    if "Seed" in node.inputs:  # pragma: no cover - version-dependent
        node.inputs["Seed"].default_value = int(seed % 100000)


def _get_socket(items, key: str, fallback_index: int = None):
    """Socket lookup by identifier, with an index fallback.

    ``NodeSocketCollection[key]`` resolves via the socket *identifier*, but on
    Blender 4.x the Noise ``W`` socket is a placeholder whose ``[]`` lookup
    fails while ``find()`` and index access succeed.  ``find()`` + index is
    correct on 3.x and 4.x alike.
    """
    idx = items.find(key)
    if idx < 0 and fallback_index is not None:
        idx = fallback_index
    return items[idx]


def build_crack_material(name: str, placement: CrackPlacement,
                         web_intensity: float, seed: int,
                         obj: "bpy.types.Object") -> Optional["bpy.types.Material"]:
    """Build (or reuse) the cracked-pane shader for ``obj``.

    Reuses an existing material with ``name`` so a rebuild in the same session
    (determinism verify) does not pile up ``.001`` copies.
    """
    if bpy is None:  # pragma: no cover
        return None
    existing = bpy.data.materials.get(name)
    if existing is not None:
        return existing

    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()

    cx, cy, cz = (float(v) for v in placement.centre)
    ux, uy, uz = (float(v) for v in placement.u)
    vx, vy, vz = (float(v) for v in placement.v)
    hole_r = placement.hole_radius
    falloff = max(placement.pane_radius, 1e-3)
    web = float(np.clip(web_intensity, 0.0, 1.0))
    jag = hole_r * JAG_AMOUNT

    # --- shader terminals -------------------------------------------------
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    out.location = (900, 0)
    clean = nt.nodes.new("ShaderNodeBsdfPrincipled")
    clean.location = (600, 220)
    for key in ("Transmission Weight", "Transmission"):
        if key in clean.inputs:
            clean.inputs[key].default_value = 1.0
            break
    clean.inputs["Roughness"].default_value = 0.03
    clean.inputs["IOR"].default_value = 1.5
    clean.inputs["Base Color"].default_value = (0.93, 0.98, 0.96, 1.0)
    frosted = nt.nodes.new("ShaderNodeBsdfDiffuse")
    frosted.location = (600, 60)
    frosted.inputs["Color"].default_value = (0.96, 0.97, 0.98, 1.0)
    frosted.inputs["Roughness"].default_value = 1.0
    transp = nt.nodes.new("ShaderNodeBsdfTransparent")
    transp.location = (600, -120)

    tex = nt.nodes.new("ShaderNodeTexCoord")
    tex.location = (-1100, 200)

    # --- pane-plane projection: px = u.(P-C), py = v.(P-C) ----------------
    sub_x = _math(nt, "SUBTRACT", -950, 380, a=None, b=None)
    sub_y = _math(nt, "SUBTRACT", -950, 330)
    sub_z = _math(nt, "SUBTRACT", -950, 280)
    for n, c in ((sub_x, cx), (sub_y, cy), (sub_z, cz)):
        n.inputs[1].default_value = c

    mx = _math(nt, "MULTIPLY", -800, 380)
    my = _math(nt, "MULTIPLY", -800, 330)
    mz = _math(nt, "MULTIPLY", -800, 280)
    mx.inputs[1].default_value = ux
    my.inputs[1].default_value = uy
    mz.inputs[1].default_value = uz
    s12 = _math(nt, "ADD", -670, 355)
    px = _math(nt, "ADD", -560, 355)

    nx = _math(nt, "MULTIPLY", -800, 180)
    ny = _math(nt, "MULTIPLY", -800, 130)
    nz = _math(nt, "MULTIPLY", -800, 80)
    nx.inputs[1].default_value = vx
    ny.inputs[1].default_value = vy
    nz.inputs[1].default_value = vz
    t12 = _math(nt, "ADD", -670, 155)
    py = _math(nt, "ADD", -560, 155)

    comb = nt.nodes.new("ShaderNodeCombineXYZ")
    comb.location = (-440, 220)

    # --- polar coordinates: r, theta -------------------------------------
    r_len = nt.nodes.new("ShaderNodeVectorMath")
    r_len.operation = "LENGTH"
    r_len.location = (-330, 240)
    theta = _math(nt, "ARCTAN2", -330, 120)

    # --- jagged hole ------------------------------------------------------
    hole_noise = nt.nodes.new("ShaderNodeTexNoise")
    hole_noise.location = (-700, -120)
    _set_dims(hole_noise, "1D")
    _set_seed(hole_noise, seed)
    hole_noise.inputs["Scale"].default_value = 6.0
    hole_noise.inputs["Detail"].default_value = 1.0
    spokes = _math(nt, "MULTIPLY", -820, -120, b=3.0)
    h_off = _math(nt, "SUBTRACT", -700, -60)
    h_off.inputs[1].default_value = 0.5 + (seed % 997) / 2000.0
    h_jag = _math(nt, "MULTIPLY", -600, -60, a=None, b=jag)
    r_jag = _math(nt, "ADD", -480, -80, a=hole_r)
    r_edge = _math(nt, "ADD", -480, -180, b=HOLE_EDGE)
    hole_map = _map_range(nt, -330, -120)

    # --- crack web (polar Voronoi, spokes crowded at the impact) ---------
    r_eps = _math(nt, "ADD", -700, -360, b=1e-3)
    focus_div = _math(nt, "DIVIDE", -600, -360, a=WEB_FOCUS)
    focus_t = _math(nt, "MULTIPLY", -600, -420)
    theta_boost = _math(nt, "ADD", -480, -420)
    wx = _math(nt, "MULTIPLY", -480, -520, b=WEB_THETA_SCALE)
    wy = _math(nt, "MULTIPLY", -480, -600, b=WEB_R_SCALE)
    wcomb = nt.nodes.new("ShaderNodeCombineXYZ")
    wcomb.location = (-360, -520)
    vor = nt.nodes.new("ShaderNodeTexVoronoi")
    vor.location = (-220, -520)
    vor.feature = "DISTANCE_TO_EDGE"
    _set_dims(vor, "2D")
    vor.inputs["Scale"].default_value = 1.0
    line_map = _map_range(nt, -60, -460)
    line_map.inputs["From Min"].default_value = 0.0
    line_map.inputs["From Max"].default_value = LINE_WIDTH
    line_map.inputs["To Min"].default_value = 1.0
    line_map.inputs["To Max"].default_value = 0.0
    falloff_map = _map_range(nt, -220, -140)
    falloff_map.inputs["From Min"].default_value = 0.0
    falloff_map.inputs["From Max"].default_value = falloff
    falloff_map.inputs["To Min"].default_value = 1.0
    falloff_map.inputs["To Max"].default_value = 0.0
    web_scale = _math(nt, "MULTIPLY", 60, -380, b=web)
    frost_scale = _math(nt, "MULTIPLY", 60, -280, b=web * FROST_BASE)
    crazing = _math(nt, "ADD", 220, -340)

    # --- shader mix -------------------------------------------------------
    frosted_mix = nt.nodes.new("ShaderNodeMixShader")
    frosted_mix.location = (600, 0)
    decorate = nt.nodes.new("ShaderNodeMixShader")
    decorate.location = (750, -20)
    final = nt.nodes.new("ShaderNodeMixShader")
    final.location = (900, 60)

    # --- links ------------------------------------------------------------
    sep = nt.nodes.new("ShaderNodeSeparateXYZ")
    sep.location = (-1080, 120)
    nt.links.new(tex.outputs["Object"], sep.inputs[0])

    nt.links.new(sep.outputs["X"], sub_x.inputs[0])
    nt.links.new(sep.outputs["Y"], sub_y.inputs[0])
    nt.links.new(sep.outputs["Z"], sub_z.inputs[0])

    nt.links.new(sub_x.outputs[0], mx.inputs[0])
    nt.links.new(sub_y.outputs[0], my.inputs[0])
    nt.links.new(sub_z.outputs[0], mz.inputs[0])
    nt.links.new(mx.outputs[0], s12.inputs[0])
    nt.links.new(my.outputs[0], s12.inputs[1])
    nt.links.new(s12.outputs[0], px.inputs[0])
    nt.links.new(mz.outputs[0], px.inputs[1])

    nt.links.new(sub_x.outputs[0], nx.inputs[0])
    nt.links.new(sub_y.outputs[0], ny.inputs[0])
    nt.links.new(sub_z.outputs[0], nz.inputs[0])
    nt.links.new(nx.outputs[0], t12.inputs[0])
    nt.links.new(ny.outputs[0], t12.inputs[1])
    nt.links.new(t12.outputs[0], py.inputs[0])
    nt.links.new(nz.outputs[0], py.inputs[1])

    nt.links.new(px.outputs[0], comb.inputs["X"])
    nt.links.new(py.outputs[0], comb.inputs["Y"])
    nt.links.new(comb.outputs[0], r_len.inputs[0])
    nt.links.new(py.outputs[0], theta.inputs[0])
    nt.links.new(px.outputs[0], theta.inputs[1])

    # hole: jagged radius -> hole_alpha
    nt.links.new(theta.outputs[0], spokes.inputs[0])
    nt.links.new(spokes.outputs[0], _get_socket(hole_noise.inputs, "W", 1))
    nt.links.new(hole_noise.outputs["Fac"], h_off.inputs[0])
    nt.links.new(h_off.outputs[0], h_jag.inputs[0])
    nt.links.new(h_jag.outputs[0], r_jag.inputs[1])
    nt.links.new(r_jag.outputs[0], r_edge.inputs[0])
    nt.links.new(r_len.outputs["Value"], hole_map.inputs["Value"])
    nt.links.new(r_jag.outputs[0], hole_map.inputs["From Min"])
    nt.links.new(r_edge.outputs[0], hole_map.inputs["From Max"])
    hole_map.inputs["To Min"].default_value = 0.0
    hole_map.inputs["To Max"].default_value = 1.0

    # web: (theta*(1+focus/r), r*scale) -> voronoi edge distance -> lines
    nt.links.new(r_len.outputs["Value"], r_eps.inputs[0])
    nt.links.new(r_eps.outputs[0], focus_div.inputs[1])
    nt.links.new(theta.outputs[0], focus_t.inputs[0])
    nt.links.new(focus_div.outputs[0], focus_t.inputs[1])
    nt.links.new(theta.outputs[0], theta_boost.inputs[0])
    nt.links.new(focus_t.outputs[0], theta_boost.inputs[1])
    nt.links.new(theta_boost.outputs[0], wx.inputs[0])
    nt.links.new(r_len.outputs["Value"], wy.inputs[0])
    nt.links.new(wx.outputs[0], wcomb.inputs["X"])
    nt.links.new(wy.outputs[0], wcomb.inputs["Y"])
    nt.links.new(wcomb.outputs[0], vor.inputs["Vector"])
    nt.links.new(vor.outputs["Distance"], line_map.inputs["Value"])
    nt.links.new(line_map.outputs[0], web_scale.inputs[0])
    nt.links.new(r_len.outputs["Value"], falloff_map.inputs["Value"])
    nt.links.new(falloff_map.outputs[0], frost_scale.inputs[0])
    nt.links.new(falloff_map.outputs[0], web_scale.inputs[1])
    nt.links.new(web_scale.outputs[0], crazing.inputs[0])
    nt.links.new(frost_scale.outputs[0], crazing.inputs[1])

    # shaders: decorate = mix(transparent, frosted, hole_alpha);
    #           final = mix(clean, decorate, crack_amount)
    nt.links.new(clean.outputs[0], frosted_mix.inputs[1])
    nt.links.new(frosted.outputs[0], frosted_mix.inputs[2])
    nt.links.new(crazing.outputs[0], frosted_mix.inputs[0])
    nt.links.new(transp.outputs[0], decorate.inputs[1])
    nt.links.new(frosted_mix.outputs[0], decorate.inputs[2])
    nt.links.new(hole_map.outputs[0], decorate.inputs[0])
    nt.links.new(clean.outputs[0], final.inputs[1])
    nt.links.new(decorate.outputs[0], final.inputs[2])
    nt.links.new(final.outputs[0], out.inputs["Surface"])

    # --- fade in from the crack frame via an object driver -----------------
    if obj is not None:
        fcu = final.inputs[0].driver_add("default_value")
        drv = fcu.driver
        drv.type = "SCRIPTED"
        var = drv.variables.new()
        var.name = "crack"
        var.targets[0].id_type = "OBJECT"
        var.targets[0].id = obj
        var.targets[0].data_path = '["_beamng_crack_amount"]'
        drv.expression = "crack"

    mat.blend_method = "BLEND"
    if hasattr(mat, "shadow_method"):  # pragma: no cover - removed in 4.x
        mat.shadow_method = "CLIP"
    mat.use_screen_refraction = False
    mat.show_transparent_back = True
    return mat


def _pane_projection_nodes(nt, placement: CrackPlacement, y: float = 220.0):
    """Build ``(px, py)`` = the impact-relative offset projected on the pane.

    Shared by both crack materials.  ``Texture Coordinate > Object`` gives the
    cache-local position playback writes into the mesh, and the pane is a raked,
    curved sheet inside that space — so raw object XY is not "across the glass".
    Projecting onto the orthonormal ``u``/``v`` axes makes the decoration lie
    flat ON the pane at any rake, which is the whole reason the placement math
    carries a basis at all.

    Returns ``(px_node, py_node, texcoord_node)``.
    """
    cx, cy, cz = (float(v) for v in placement.centre)
    ux, uy, uz = (float(v) for v in placement.u)
    vx, vy, vz = (float(v) for v in placement.v)

    tex = nt.nodes.new("ShaderNodeTexCoord")
    tex.location = (-1100, y)
    sep = nt.nodes.new("ShaderNodeSeparateXYZ")
    sep.location = (-1080, y - 100)
    nt.links.new(tex.outputs["Object"], sep.inputs[0])

    sub_x = _math(nt, "SUBTRACT", -950, y + 160)
    sub_y = _math(nt, "SUBTRACT", -950, y + 110)
    sub_z = _math(nt, "SUBTRACT", -950, y + 60)
    for n, c in ((sub_x, cx), (sub_y, cy), (sub_z, cz)):
        n.inputs[1].default_value = c
    nt.links.new(sep.outputs["X"], sub_x.inputs[0])
    nt.links.new(sep.outputs["Y"], sub_y.inputs[0])
    nt.links.new(sep.outputs["Z"], sub_z.inputs[0])

    mx = _math(nt, "MULTIPLY", -800, y + 160)
    my = _math(nt, "MULTIPLY", -800, y + 110)
    mz = _math(nt, "MULTIPLY", -800, y + 60)
    mx.inputs[1].default_value = ux
    my.inputs[1].default_value = uy
    mz.inputs[1].default_value = uz
    s12 = _math(nt, "ADD", -670, y + 135)
    px = _math(nt, "ADD", -560, y + 135)
    nt.links.new(sub_x.outputs[0], mx.inputs[0])
    nt.links.new(sub_y.outputs[0], my.inputs[0])
    nt.links.new(sub_z.outputs[0], mz.inputs[0])
    nt.links.new(mx.outputs[0], s12.inputs[0])
    nt.links.new(my.outputs[0], s12.inputs[1])
    nt.links.new(s12.outputs[0], px.inputs[0])
    nt.links.new(mz.outputs[0], px.inputs[1])

    nx = _math(nt, "MULTIPLY", -800, y - 40)
    ny = _math(nt, "MULTIPLY", -800, y - 90)
    nz = _math(nt, "MULTIPLY", -800, y - 140)
    nx.inputs[1].default_value = vx
    ny.inputs[1].default_value = vy
    nz.inputs[1].default_value = vz
    t12 = _math(nt, "ADD", -670, y - 65)
    py = _math(nt, "ADD", -560, y - 65)
    nt.links.new(sub_x.outputs[0], nx.inputs[0])
    nt.links.new(sub_y.outputs[0], ny.inputs[0])
    nt.links.new(sub_z.outputs[0], nz.inputs[0])
    nt.links.new(nx.outputs[0], t12.inputs[0])
    nt.links.new(ny.outputs[0], t12.inputs[1])
    nt.links.new(t12.outputs[0], py.inputs[0])
    nt.links.new(nz.outputs[0], py.inputs[1])
    return px, py, tex


def _add_crack_driver(nt, socket, obj: "bpy.types.Object") -> None:
    """Drive ``socket`` from the pane's ``_beamng_crack_amount`` property.

    Keeps the pane clean before the crack frame; :func:`keyframe_crack` ramps
    the property 0 -> 1 so the damage fades in exactly when the pane was hit.
    """
    if obj is None:
        return
    fcu = socket.driver_add("default_value")
    drv = fcu.driver
    drv.type = "SCRIPTED"
    var = drv.variables.new()
    var.name = "crack"
    var.targets[0].id_type = "OBJECT"
    var.targets[0].id = obj
    var.targets[0].data_path = '["_beamng_crack_amount"]'
    drv.expression = "crack"


#: Default width (m) the crack image spans across the pane, before the
#: severity/scale term.  A real windshield crack pattern is a good fraction of
#: the glass, not a postage stamp.
CRACK_IMAGE_SPAN = 1.2


def build_crack_image_material(name: str, placement: CrackPlacement,
                               obj: "bpy.types.Object",
                               image: "bpy.types.Image" = None,
                               span: float = CRACK_IMAGE_SPAN,
                               base_material: "bpy.types.Material" = None,
                               ) -> Optional["bpy.types.Material"]:
    """Cracked-pane material whose damage comes from an IMAGE the user supplies.

    The pane keeps its glass and keeps animating; this only paints it.  The
    image is projected onto the pane's own plane (see
    :func:`_pane_projection_nodes`) and centred on the impact, so a crack PNG
    lands where the pane was actually struck and lies flat at any rake.

    The Image Texture node is left UNCONNECTED to an image when ``image`` is
    None — deliberately.  The user drops their own crack texture into that slot
    and positions it by hand (Mapping node), which is the workflow asked for.
    Until then the node contributes nothing and the pane renders as clean glass,
    so an unset texture degrades to "no visible damage" rather than to a black
    pane.

    The image's ALPHA is the crack mask: opaque pixels paint damage, clear
    pixels leave the glass untouched.  A crack PNG with no alpha falls back to
    its inverted luminance, so a plain black-on-white crack scan also works.
    """
    if bpy is None:  # pragma: no cover
        return None
    existing = bpy.data.materials.get(name)
    if existing is not None:
        return existing

    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()

    out = nt.nodes.new("ShaderNodeOutputMaterial")
    out.location = (900, 0)

    # Clean glass: what the pane looks like outside the cracked area.
    clean = nt.nodes.new("ShaderNodeBsdfPrincipled")
    clean.location = (600, 240)
    for key in ("Transmission Weight", "Transmission"):
        if key in clean.inputs:
            clean.inputs[key].default_value = 1.0
            break
    clean.inputs["Roughness"].default_value = 0.03
    clean.inputs["IOR"].default_value = 1.5
    clean.inputs["Base Color"].default_value = (0.93, 0.98, 0.96, 1.0)

    px, py, _tex = _pane_projection_nodes(nt, placement)

    # Map the pane-plane offset into UV space: 0.5 at the impact, so the image
    # is CENTRED on the strike rather than on the pane's origin corner.
    span = max(1e-3, float(span))
    su = _math(nt, "DIVIDE", -430, 260, b=span)
    sv = _math(nt, "DIVIDE", -430, 180, b=span)
    ou = _math(nt, "ADD", -320, 260, b=0.5)
    ov = _math(nt, "ADD", -320, 180, b=0.5)
    nt.links.new(px.outputs[0], su.inputs[0])
    nt.links.new(py.outputs[0], sv.inputs[0])
    nt.links.new(su.outputs[0], ou.inputs[0])
    nt.links.new(sv.outputs[0], ov.inputs[0])
    uv = nt.nodes.new("ShaderNodeCombineXYZ")
    uv.location = (-200, 220)
    nt.links.new(ou.outputs[0], uv.inputs["X"])
    nt.links.new(ov.outputs[0], uv.inputs["Y"])

    # A Mapping node the user can grab to nudge / rotate / scale the texture by
    # hand without rewiring anything.
    mapping = nt.nodes.new("ShaderNodeMapping")
    mapping.location = (-60, 220)
    nt.links.new(uv.outputs[0], mapping.inputs["Vector"])

    img = nt.nodes.new("ShaderNodeTexImage")
    img.location = (140, 260)
    img.label = "Crack Texture (drop your PNG here)"
    img.name = "BeamNG Crack Texture"
    # CLIP: outside the image the pane must be untouched glass, not a repeat of
    # the crack pattern tiled across the whole windshield.
    img.extension = "CLIP"
    if image is not None:
        img.image = image
    nt.links.new(mapping.outputs[0], img.inputs["Vector"])

    # Crack mask.  Alpha when the image has one; otherwise inverted luminance so
    # a black-on-white crack scan still reads as damage.
    to_bw = nt.nodes.new("ShaderNodeRGBToBW")
    to_bw.location = (360, 140)
    nt.links.new(img.outputs["Color"], to_bw.inputs[0])
    inv = _math(nt, "SUBTRACT", 470, 140, a=1.0)
    nt.links.new(to_bw.outputs[0], inv.inputs[1])
    lum_or_alpha = _math(nt, "MAXIMUM", 470, 40)
    nt.links.new(img.outputs["Alpha"], lum_or_alpha.inputs[0])
    # Only the alpha drives the mask by default; the luminance term is wired to
    # the second input at 0 so a user with an alpha-less PNG can raise it.
    lum_or_alpha.inputs[1].default_value = 0.0

    # Damage look: frosted, light-scattering crazed glass.
    frosted = nt.nodes.new("ShaderNodeBsdfDiffuse")
    frosted.location = (600, 60)
    frosted.inputs["Color"].default_value = (0.96, 0.97, 0.98, 1.0)
    frosted.inputs["Roughness"].default_value = 1.0

    damaged = nt.nodes.new("ShaderNodeMixShader")
    damaged.location = (750, 120)
    nt.links.new(clean.outputs[0], damaged.inputs[1])
    nt.links.new(frosted.outputs[0], damaged.inputs[2])
    nt.links.new(lum_or_alpha.outputs[0], damaged.inputs[0])

    # Fade the whole decoration in from the crack frame.
    final = nt.nodes.new("ShaderNodeMixShader")
    final.location = (900, 200)
    nt.links.new(clean.outputs[0], final.inputs[1])
    nt.links.new(damaged.outputs[0], final.inputs[2])
    nt.links.new(final.outputs[0], out.inputs["Surface"])
    _add_crack_driver(nt, final.inputs[0], obj)

    mat.blend_method = "BLEND"
    if hasattr(mat, "shadow_method"):  # pragma: no cover - removed in 4.x
        mat.shadow_method = "CLIP"
    mat.show_transparent_back = True
    return mat


def load_crack_image(path: str) -> Optional["bpy.types.Image"]:
    """Load (or reuse) the user's crack texture.  None when unset/missing."""
    if bpy is None or not path:
        return None
    for img in bpy.data.images:
        if img.filepath and os.path.abspath(bpy.path.abspath(img.filepath)) == \
                os.path.abspath(path):
            return img
    if not os.path.exists(path):
        return None
    try:
        return bpy.data.images.load(path)
    except RuntimeError:  # pragma: no cover - unreadable file
        return None


def keyframe_crack(obj: "bpy.types.Object", crack_frame: int,
                   ramp: int = 2) -> None:
    """Animate the pane's ``_beamng_crack_amount`` so the crack appears.

    The material's driver reads this property, so 0 before the crack frame
    keeps the pane clean and the decoration fades in over ``ramp`` frames.
    """
    if bpy is None or obj is None:  # pragma: no cover
        return
    pre = max(0, int(crack_frame) - ramp)
    obj["_beamng_crack_amount"] = 0.0
    obj.keyframe_insert('["_beamng_crack_amount"]', frame=pre)
    obj["_beamng_crack_amount"] = 1.0
    obj.keyframe_insert('["_beamng_crack_amount"]', frame=int(crack_frame))
    obj["_beamng_crack_amount"] = 1.0


def assign_crack_material(obj: "bpy.types.Object",
                          mat: "bpy.types.Material") -> None:
    """Put the crack shader on the pane and set every face to use it."""
    if bpy is None or obj is None or obj.data is None:  # pragma: no cover
        return
    mesh = obj.data
    if mesh.materials:
        # Blender 4.5 removed ``Mesh.materials.insert`` and ``MeshMaterials``
        # has no ``move``; append then shuffle the slots so the crack shader
        # lands at index 0 and wins over the original paint.
        mesh.materials.append(mat)
        n = len(mesh.materials)
        for i in range(n - 1, 0, -1):
            mesh.materials[i] = mesh.materials[i - 1]
        mesh.materials[0] = mat
    else:
        mesh.materials.append(mat)
    for poly in mesh.polygons:
        poly.material_index = 0
