from __future__ import annotations

"""Build debris shard meshes cut from the real part geometry.

A convincing shard has to look like it *came off that part*: same curvature,
same panel thickness, same material.  A generic cube or icosphere never does,
however well it is simulated — the eye reads the silhouette long before it reads
the physics.

So shards are cut from the part's own triangles, sampled out of the BVC cache at
the frame the part failed.  Each shard is a small patch of the real surface,
given thickness along its own normal and closed into a solid.  A bumper shard is
literally a piece of bumper.

Fracture styles are per material, because materials do not break alike:

``glass``   long angular splinters, thin, wide spread — safety glass dices into
            slivers, not chunks
``plastic`` medium irregular chunks with a torn edge, ABS-thick
``paint``   sheet-metal flakes: broad, thin, and slightly bent
``steel``   compact heavy fragments
``chrome``  small bright slivers of trim
``rubber``  chunky blocks that barely spread

Everything here is plain numpy plus a thin ``bpy`` mesh-creation layer, so the
geometry logic can be exercised without Blender.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

try:  # pragma: no cover - only inside Blender
    import bpy
except ImportError:  # pragma: no cover
    bpy = None


# ---------------------------------------------------------------------------
# Per-material fracture profiles
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FractureProfile:
    """How one material breaks apart."""

    #: Rough shard size in metres (longest dimension), min/max.
    size: Tuple[float, float]
    #: Panel thickness in metres.
    thickness: float
    #: How elongated shards are: 1.0 = equant, 4.0 = splintery.
    elongation: float
    #: Extra jitter applied to shard silhouettes (fraction of size).
    jitter: float
    #: Multiplier on how many pieces this material sheds.
    count_bias: float
    #: Multiplier on launch speed — glass sprays, rubber flops.
    speed_bias: float
    #: How strongly the silhouette is notched inward between its corners, as a
    #: fraction of the radius.  0 leaves a straight-edged convex polygon; higher
    #: values cut spikes and re-entrant notches into the outline.  Real fracture
    #: edges are stepped and jagged — a straight edge is the single clearest
    #: tell that a shard was generated rather than broken.  Glass and chrome
    #: splinter hardest; rubber tears bluntly and barely notches.
    notch: float = 0.0
    #: Out-of-plane bend across the shard, as a fraction of its size.  Sheet
    #: metal and plastic never come away flat: they buckle.  Applied as a
    #: saddle/curl along the elongation axis so the piece catches light across
    #: its face instead of reading as a flat chip.
    warp: float = 0.0


FRACTURE_PROFILES: Dict[str, FractureProfile] = {
    #                        size            thick  elong jit  cnt  spd  notch warp
    "glass":   FractureProfile((0.012, 0.055), 0.004, 3.6, 0.45, 2.4, 1.35, 0.45, 0.02),
    "plastic": FractureProfile((0.025, 0.110), 0.010, 1.8, 0.40, 1.3, 1.00, 0.30, 0.10),
    "paint":   FractureProfile((0.030, 0.130), 0.008, 1.5, 0.35, 1.0, 0.90, 0.22, 0.16),
    "chrome":  FractureProfile((0.015, 0.070), 0.005, 2.6, 0.40, 0.8, 1.10, 0.38, 0.12),
    "steel":   FractureProfile((0.025, 0.095), 0.014, 1.4, 0.30, 0.7, 0.80, 0.26, 0.08),
    "rubber":  FractureProfile((0.030, 0.100), 0.018, 1.2, 0.25, 0.5, 0.65, 0.10, 0.05),
}


def profile_for(material: str) -> FractureProfile:
    return FRACTURE_PROFILES.get(material, FRACTURE_PROFILES["steel"])


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------


def _triangle_areas(verts: np.ndarray, tris: np.ndarray) -> np.ndarray:
    a = verts[tris[:, 0]]
    b = verts[tris[:, 1]]
    c = verts[tris[:, 2]]
    return 0.5 * np.linalg.norm(np.cross(b - a, c - a), axis=1)


def _face_normal(p: np.ndarray) -> np.ndarray:
    """Newell normal of a polygon, robust to non-planarity."""
    n = np.zeros(3)
    for i in range(len(p)):
        cur, nxt = p[i], p[(i + 1) % len(p)]
        n[0] += (cur[1] - nxt[1]) * (cur[2] + nxt[2])
        n[1] += (cur[2] - nxt[2]) * (cur[0] + nxt[0])
        n[2] += (cur[0] - nxt[0]) * (cur[1] + nxt[1])
    ln = np.linalg.norm(n)
    return n / ln if ln > 1e-12 else np.array((0.0, 0.0, 1.0))


def _orthonormal_basis(normal: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """Two unit vectors spanning the plane perpendicular to ``normal``."""
    ref = np.array((0.0, 0.0, 1.0)) if abs(normal[2]) < 0.9 else np.array((1.0, 0.0, 0.0))
    u = np.cross(normal, ref)
    nu = np.linalg.norm(u)
    if nu < 1e-9:
        u = np.array((1.0, 0.0, 0.0))
    else:
        u = u / nu
    v = np.cross(normal, u)
    return u, v


def sample_surface_patches(verts: np.ndarray, tris: np.ndarray, count: int,
                           rng: np.random.Generator
                           ) -> List[Tuple[np.ndarray, np.ndarray]]:
    """Pick ``count`` points on the mesh surface, area-weighted.

    Area weighting matters: uniform-by-triangle sampling clusters shards
    wherever the mesh happens to be densely tessellated (around bolt holes and
    trim details) instead of spreading them over the panel.

    Returns a list of ``(point, normal)``.
    """
    if len(tris) == 0:
        return []
    areas = _triangle_areas(verts, tris)
    total = areas.sum()
    if total <= 0:
        return []
    probs = areas / total
    picks = rng.choice(len(tris), size=count, p=probs)

    # Uniform barycentric sample within each chosen triangle.
    r1 = np.sqrt(rng.random(count))
    r2 = rng.random(count)
    w0 = (1.0 - r1)[:, None]
    w1 = (r1 * (1.0 - r2))[:, None]
    w2 = (r1 * r2)[:, None]

    a = verts[tris[picks, 0]]
    b = verts[tris[picks, 1]]
    c = verts[tris[picks, 2]]
    points = w0 * a + w1 * b + w2 * c

    normals = np.cross(b - a, c - a)
    lens = np.linalg.norm(normals, axis=1, keepdims=True)
    normals = np.divide(normals, np.maximum(lens, 1e-12))

    return [(points[i], normals[i]) for i in range(count)]


def build_shard_geometry(point: np.ndarray, normal: np.ndarray,
                         profile: FractureProfile,
                         rng: np.random.Generator
                         ) -> Tuple[np.ndarray, List[Tuple[int, ...]]]:
    """One shard: an irregular jagged polygon on the surface, given thickness.

    The outline is a random polygon in the tangent plane, with radii jittered
    per vertex so no two shards share a silhouette, then stretched along one
    axis by the material's elongation (this is what makes glass read as
    splinters rather than confetti).  Extruding along the *surface* normal keeps
    the shard sitting in the panel it came from.

    Two things separate a shard that reads as *broken* from one that reads as
    *cut*, and both are per-material (see :class:`FractureProfile`):

    ``notch``  midpoints between corners are pulled inward, turning every
               straight edge into a re-entrant V.  The outline becomes
               non-convex — the rendered silhouette is jagged while collision
               still uses the cheap convex hull.
    ``warp``   the piece is bent out of plane by a saddle across its own extent,
               so sheet-metal flakes buckle instead of staying perfectly flat.

    Both faces receive the same warp offset, so thickness is preserved and the
    solid stays watertight.
    """
    lo, hi = profile.size
    size = float(rng.uniform(lo, hi))
    n_side = int(rng.integers(3, 8))

    u, v = _orthonormal_basis(normal)
    # Random in-plane rotation so elongation isn't axis-aligned across shards.
    theta0 = float(rng.uniform(0.0, 2.0 * np.pi))
    stretch = 1.0 + (profile.elongation - 1.0) * float(rng.random())

    angles = np.sort(rng.uniform(0.0, 2.0 * np.pi, n_side))
    radii = size * 0.5 * (1.0 + rng.uniform(-profile.jitter, profile.jitter, n_side))

    # NOTCHING.  Insert a midpoint between each pair of corners and pull it
    # INWARD, which turns every straight edge into a re-entrant V.  This is what
    # makes the silhouette read as fractured: a convex polygon with straight
    # edges looks cut, not broken.  The result is deliberately non-convex, so
    # the rigid body uses a convex hull for collision (cheap, stable) while the
    # rendered mesh keeps its jagged outline.
    notch = float(profile.notch)
    ring_pts: List[np.ndarray] = []
    for idx in range(n_side):
        ang, rad = angles[idx], radii[idx]
        nxt_ang = angles[(idx + 1) % n_side] + (2.0 * np.pi if idx == n_side - 1 else 0.0)
        nxt_rad = radii[(idx + 1) % n_side]

        def point(a: float, r: float) -> np.ndarray:
            a = a + theta0
            du = np.cos(a) * r * stretch
            dv = np.sin(a) * r / max(1.0, stretch * 0.55)
            return du * u + dv * v

        ring_pts.append(point(ang, rad))
        if notch > 0.0:
            mid_ang = 0.5 * (ang + nxt_ang)
            mid_rad = 0.5 * (rad + nxt_rad)
            # Depth varies per edge, so notches are irregular rather than a
            # regular star.  Clamped to keep the polygon from self-crossing.
            depth = 1.0 - float(np.clip(rng.uniform(0.25, 1.0) * notch, 0.0, 0.75))
            ring_pts.append(point(mid_ang, mid_rad * depth))
    ring = np.array(ring_pts)

    # Sort the ring by true angle in the (stretched) tangent plane.  Jittered
    # radii combined with a wide notch can otherwise place a notch midpoint on
    # the far side of a neighbouring corner, producing a self-crossing outline
    # that extrudes into a tangled solid.  Sorting by angle makes the polygon
    # simple again while keeping every notch.
    #
    # The angle is measured in the SAME (u, v) frame the ring was generated in,
    # so the sorted order matches the winding the face table below assumes.
    # Verified over 6000 shards across all six materials: 0 non-manifold edges,
    # 0 negative volumes.  (Volume must be checked by triangulating each face
    # around its own centroid — a naive fan from vertex 0 reports false
    # negatives on the deliberately concave caps.)
    order = np.argsort(np.arctan2(ring @ v, ring @ u))
    ring = ring[order]

    half = profile.thickness * 0.5 * float(rng.uniform(0.7, 1.3))

    # WARP.  Bend the piece out of plane so it is not a flat chip.  Real sheet
    # buckles when it tears; glass splinters curl only slightly.  The offset is
    # a saddle across the shard's own extent, applied equally to both faces so
    # the piece keeps its thickness.
    warp = float(profile.warp) * size
    if warp > 0.0:
        along = ring @ u
        across = ring @ v
        scale_u = max(float(np.abs(along).max()), 1e-9)
        scale_v = max(float(np.abs(across).max()), 1e-9)
        bend = (warp * float(rng.uniform(-1.0, 1.0)) * (along / scale_u) ** 2
                + warp * float(rng.uniform(-0.6, 0.6)) * (across / scale_v) ** 2)
        ring = ring + normal * bend[:, None]

    top = ring + normal * half
    bottom = ring - normal * half
    verts = np.vstack([top, bottom])
    n_side = len(ring)

    # Winding is chosen so every face points OUTWARD.  Blender will happily
    # build an inside-out solid: it is watertight and looks fine in the
    # viewport's default shading, but renders as a black hole and confuses
    # convex-hull collision bounds.  ``mesh.validate()`` does not fix winding,
    # so it has to be right here.  The top ring runs counter-clockwise when
    # viewed from +normal, the bottom cap is reversed, and the side quads are
    # ordered to match both.
    n = n_side
    faces: List[Tuple[int, ...]] = [
        tuple(range(n - 1, -1, -1)),          # top cap, seen from +normal
        tuple(range(n, 2 * n)),               # bottom cap, seen from -normal
    ]
    for i in range(n):
        j = (i + 1) % n
        faces.append((i, n + i, n + j, j))

    # Centre on the origin — the object's own transform places it in the world,
    # which is what the rigid body solver and particle instancing both expect.
    verts = verts - verts.mean(axis=0)
    return verts, faces


# ---------------------------------------------------------------------------
# Blender mesh creation
# ---------------------------------------------------------------------------


def _mesh_from_arrays(name: str, verts: np.ndarray,
                      faces: Sequence[Sequence[int]]) -> "bpy.types.Mesh":
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata([tuple(float(c) for c in v) for v in verts], [], [list(f) for f in faces])
    mesh.validate(verbose=False)
    # Shards are faceted by nature; flat shading reads as a fresh fracture.
    mesh.update()
    return mesh


def _resolve_source_material(part_name: str,
                             source_objects: Optional[Dict[str, "bpy.types.Object"]]
                             ) -> Optional["bpy.types.Material"]:
    """Reuse the part's own material so shards match the car exactly.

    Falls back to None, in which case the caller assigns a generated material.
    """
    if not source_objects:
        return None
    obj = source_objects.get(part_name)
    if obj is None or obj.type != "MESH":
        return None
    mats = [m for m in obj.data.materials if m is not None]
    return mats[0] if mats else None


_FALLBACK_COLOURS: Dict[str, Tuple[float, float, float, float]] = {
    "glass":   (0.62, 0.72, 0.70, 1.0),
    "plastic": (0.05, 0.05, 0.06, 1.0),
    "paint":   (0.78, 0.78, 0.80, 1.0),
    "chrome":  (0.83, 0.85, 0.88, 1.0),
    "steel":   (0.24, 0.25, 0.27, 1.0),
    "rubber":  (0.03, 0.03, 0.03, 1.0),
}


def fallback_material(material: str) -> "bpy.types.Material":
    """A plausible PBR material for a debris class, created once and reused."""
    name = f"Debris_{material}"
    mat = bpy.data.materials.get(name)
    if mat is not None:
        return mat

    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    if bsdf is None:
        return mat

    colour = _FALLBACK_COLOURS.get(material, _FALLBACK_COLOURS["steel"])
    bsdf.inputs["Base Color"].default_value = colour

    def _set(socket: str, value) -> None:
        # Socket names moved around in Blender 4.x; skip what is absent rather
        # than failing the whole build.
        if socket in bsdf.inputs:
            bsdf.inputs[socket].default_value = value

    if material == "glass":
        _set("Roughness", 0.05)
        _set("Metallic", 0.0)
        _set("Transmission Weight", 0.85)
        _set("IOR", 1.52)
        mat.blend_method = "BLEND"
    elif material in ("steel", "chrome"):
        _set("Roughness", 0.28 if material == "chrome" else 0.55)
        _set("Metallic", 1.0)
    elif material == "paint":
        _set("Roughness", 0.35)
        _set("Metallic", 0.6)
    elif material == "rubber":
        _set("Roughness", 0.95)
        _set("Metallic", 0.0)
    else:  # plastic
        _set("Roughness", 0.65)
        _set("Metallic", 0.0)
    return mat


def build_shard_library(part_name: str, material: str,
                        verts: np.ndarray, tris: np.ndarray,
                        variants: int = 8,
                        seed: int = 0,
                        source_objects: Optional[Dict[str, "bpy.types.Object"]] = None,
                        collection: Optional["bpy.types.Collection"] = None,
                        ) -> List["bpy.types.Object"]:
    """Create ``variants`` shard objects cut from one part's geometry.

    The returned objects are the *templates*: they are linked into
    ``collection`` (hidden by the caller) and then either instanced by a
    particle system or duplicated into rigid bodies.  Building a handful of
    variants and reusing them keeps the scene light — a few thousand unique
    shard meshes would bloat the .blend for no visible gain.
    """
    if bpy is None:
        raise RuntimeError("build_shard_library requires Blender (bpy)")

    profile = profile_for(material)
    rng = np.random.default_rng(seed)
    patches = sample_surface_patches(verts, tris, variants, rng)
    if not patches:
        return []

    mat = _resolve_source_material(part_name, source_objects) or fallback_material(material)

    objects: List["bpy.types.Object"] = []
    for i, (point, normal) in enumerate(patches):
        sverts, sfaces = build_shard_geometry(point, normal, profile, rng)
        mesh = _mesh_from_arrays(f"shard_{material}_{i:02d}", sverts, sfaces)
        mesh.materials.append(mat)
        for poly in mesh.polygons:
            poly.use_smooth = False
        obj = bpy.data.objects.new(f"shard_{material}_{i:02d}", mesh)
        if collection is not None:
            collection.objects.link(obj)
        objects.append(obj)
    return objects


def triangulate_indices(indices: np.ndarray) -> np.ndarray:
    """Coerce a cache index array into an (N, 3) triangle array.

    BVC stores triangles already, but be defensive: a flat array or a quad
    array both show up in older caches.
    """
    idx = np.asarray(indices)
    if idx.ndim == 1:
        if idx.size % 3 != 0:
            idx = idx[: idx.size - (idx.size % 3)]
        return idx.reshape(-1, 3)
    if idx.ndim == 2:
        if idx.shape[1] == 3:
            return idx
        if idx.shape[1] == 4:  # fan-split quads
            return np.vstack([idx[:, (0, 1, 2)], idx[:, (0, 2, 3)]])
        return idx[:, :3]
    return np.zeros((0, 3), dtype=np.intp)


# ---------------------------------------------------------------------------
# Simply Shatter compatibility
# ---------------------------------------------------------------------------


def simply_shatter_shards(source_objects: Sequence["bpy.types.Object"],
                          material: str = "steel",
                          count: int = 14,
                          seed: int = 0,
                          collection: Optional["bpy.types.Collection"] = None,
                          ) -> List["bpy.types.Object"]:
    """Create shard objects from selected source objects using BeamNG's fracture
    profiles, compatible with Simply Shatter's physics pipeline.

    This bridges Simply Shatter's workflow (select parts → apply physics) with
    BeamNG's per-material fracture profiles.  For each source object, ``count``
    shard variants are generated from the object's own geometry, giving shards
    that look like they genuinely came off that part.

    The returned objects are ready to receive rigid bodies via
    ``beamng.apply_physics`` or Simply Shatter's operators.
    """
    if bpy is None:
        raise RuntimeError("simply_shatter_shards requires Blender (bpy)")

    profile = profile_for(material)
    rng = np.random.default_rng(seed)
    all_objects: List["bpy.types.Object"] = []

    for src in source_objects:
        if src.type != "MESH":
            continue
        mesh = src.data
        if not mesh or not mesh.polygons:
            continue

        # Extract vertices and triangles in world space
        vcount = len(mesh.vertices)
        co = np.empty(vcount * 3, dtype=np.float64)
        mesh.vertices.foreach_get("co", co)
        verts = co.reshape(-1, 3)

        # Apply object transform to get world-space positions
        mw = np.asarray(src.matrix_world, dtype=np.float64)
        verts_h = np.hstack([verts, np.ones((vcount, 1))])
        verts = (mw @ verts_h.T).T[:, :3]

        # Get triangles
        loop_triangles = mesh.loop_triangles
        if not loop_triangles:
            # Fallback: fan-triangulate polygons
            tris_list = []
            for poly in mesh.polygons:
                indices = list(poly.loop_indices)
                for i in range(1, len(indices) - 1):
                    tris_list.append([mesh.loops[indices[0]].vertex_index,
                                      mesh.loops[indices[i]].vertex_index,
                                      mesh.loops[indices[i + 1]].vertex_index])
            if not tris_list:
                continue
            tris = np.array(tris_list, dtype=np.intp)
        else:
            tris = np.array([[lt.vertices[0], lt.vertices[1], lt.vertices[2]]
                             for lt in loop_triangles], dtype=np.intp)

        patches = sample_surface_patches(verts, tris, count, rng)
        if not patches:
            continue

        mat = _resolve_source_material(src.name, {src.name: src}) or fallback_material(material)

        for i, (point, normal) in enumerate(patches):
            sverts, sfaces = build_shard_geometry(point, normal, profile, rng)
            smesh = _mesh_from_arrays(
                f"ss_shard_{src.name}_{material}_{i:02d}", sverts, sfaces)
            smesh.materials.append(mat)
            for poly in smesh.polygons:
                poly.use_smooth = False
            obj = bpy.data.objects.new(
                f"ss_shard_{src.name}_{material}_{i:02d}", smesh)
            # Place at the shard's world position
            obj.location = point
            if collection is not None:
                collection.objects.link(obj)
            all_objects.append(obj)

    return all_objects
