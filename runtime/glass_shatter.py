from __future__ import annotations

"""Shatter a glass pane into fragments cut from the real pane geometry.

Automotive glass does not break like the other materials, so it does not go
through :mod:`debris_shards` (which punches small patches out of a big panel and
throws them).  A windshield is a *whole object* that fails all at once, and the
failure mode is characteristic enough that faking it with generic chips reads
wrong immediately:

* **Laminated glass** (windshield) is two sheets bonded to a PVB interlayer.  It
  crazes into a dense web of tiny fragments that stay stuck to the interlayer —
  the pane goes opaque-white but keeps its shape.  It only empties out of the
  frame when something punches through it or it grinds along the road.
* **Tempered glass** (side and rear) dices into thousands of blunt cubes and
  leaves the frame all at once.

Both share the detail that sells the shot: **a fringe of glass stays welded into
the rubber seal**.  A car with a perfectly empty, perfectly clean window
aperture looks like the glass was deleted, because that is exactly what it is.
:func:`shatter_pane` keeps a border ring of fragments pinned in place.

Fragmentation is a **2D Voronoi tessellation in the pane's own plane**, not a
scatter of loose polygons:

* Voronoi cells TILE.  Every point of the pane belongs to exactly one fragment,
  so the pieces fit together like the real thing and the pane reads as *broken*
  rather than *replaced by some triangles*.  Independently sampled polygons
  overlap and leave gaps, which is what makes naive shatter look like confetti.
* The cells are clipped to the pane's real outline, so the fragment set has the
  windshield's actual silhouette, curvature and rake.
* Seed density is driven by impact intensity, and seeds are biased toward the
  impact point, so the strike zone is pulverised while the far corners come away
  in larger plates — which is how real glass breaks.

Pure numpy: no ``bpy`` in the geometry path, so it is unit-testable outside
Blender (the Blender layer lives in :mod:`debris_spawn`).
"""

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

import numpy as np


# ---------------------------------------------------------------------------
# Pane frame
# ---------------------------------------------------------------------------


@dataclass
class PaneBasis:
    """The pane's own 2D coordinate frame.

    A windshield is a raked, curved quad in 3D.  Fracturing it in world XY would
    stretch the cells with the rake angle; fracturing it in its own plane keeps
    the fragments square-on to the glass.
    """

    origin: np.ndarray      # centroid, world space
    u: np.ndarray           # in-plane axis 1 (longest extent)
    v: np.ndarray           # in-plane axis 2
    normal: np.ndarray      # pane normal

    def to_2d(self, points: np.ndarray) -> np.ndarray:
        d = np.asarray(points, dtype=np.float64) - self.origin
        return np.column_stack((d @ self.u, d @ self.v))

    def to_3d(self, uv: np.ndarray, offset: float = 0.0) -> np.ndarray:
        uv = np.asarray(uv, dtype=np.float64)
        return (self.origin
                + uv[:, 0:1] * self.u
                + uv[:, 1:2] * self.v
                + offset * self.normal)


def pane_basis(verts: np.ndarray) -> PaneBasis:
    """Fit a plane to the pane and build an in-plane basis via PCA.

    The smallest principal component of a sheet is its normal; the largest two
    span it.  This is robust to the pane being curved (a windshield is), because
    PCA fits the dominant plane rather than trusting any single triangle.
    """
    pts = np.asarray(verts, dtype=np.float64)
    origin = pts.mean(axis=0)
    centred = pts - origin
    # SVD of the covariance: columns of vt are principal axes, largest first.
    _u, _s, vt = np.linalg.svd(centred.T @ centred)
    u, v, normal = vt[0], vt[1], vt[2]
    return PaneBasis(origin=origin,
                     u=u / np.linalg.norm(u),
                     v=v / np.linalg.norm(v),
                     normal=normal / np.linalg.norm(normal))


# ---------------------------------------------------------------------------
# Voronoi fragmentation
# ---------------------------------------------------------------------------


def _convex_hull_2d(points: np.ndarray) -> np.ndarray:
    """Monotone-chain convex hull.  Returns hull points in CCW order.

    Used as the pane outline to clip cells against.  A convex hull is a good
    approximation of a window aperture (they are all convex-ish) and avoids a
    concave-clipping dependency.
    """
    pts = np.unique(np.asarray(points, dtype=np.float64), axis=0)
    if len(pts) < 3:
        return pts
    order = np.lexsort((pts[:, 1], pts[:, 0]))
    pts = pts[order]

    def half(seq):
        out: List[np.ndarray] = []
        for p in seq:
            while len(out) >= 2:
                a, b = out[-2], out[-1]
                if np.cross(b - a, p - a) <= 0:
                    out.pop()
                else:
                    break
            out.append(p)
        return out

    lower = half(pts)
    upper = half(pts[::-1])
    return np.array(lower[:-1] + upper[:-1])


def _clip_polygon(poly: np.ndarray, normal: np.ndarray, offset: float) -> np.ndarray:
    """Sutherland-Hodgman clip of ``poly`` against the half-plane n.x <= offset."""
    if len(poly) == 0:
        return poly
    out: List[np.ndarray] = []
    n = len(poly)
    for i in range(n):
        cur = poly[i]
        nxt = poly[(i + 1) % n]
        dc = float(normal @ cur) - offset
        dn = float(normal @ nxt) - offset
        if dc <= 0:
            out.append(cur)
        # Crossing the boundary: add the intersection point.
        if (dc < 0 < dn) or (dn < 0 < dc):
            t = dc / (dc - dn)
            out.append(cur + t * (nxt - cur))
    return np.array(out) if out else np.zeros((0, 2))


def voronoi_cells(seeds: np.ndarray, outline: np.ndarray) -> List[np.ndarray]:
    """Clip ``outline`` to each seed's Voronoi cell.

    Implemented as repeated half-plane clipping — for each pair (i, j) the cell
    of ``i`` lies on ``i``'s side of the perpendicular bisector.  O(n^2) in the
    seed count, which is fine at the few-hundred scale a pane needs and avoids
    depending on ``scipy.spatial`` (not available inside Blender).

    Returns one polygon (possibly empty) per seed, tiling the outline exactly.
    """
    seeds = np.asarray(seeds, dtype=np.float64)
    cells: List[np.ndarray] = []
    for i, s in enumerate(seeds):
        poly = outline
        for j, t in enumerate(seeds):
            if i == j:
                continue
            d = t - s
            length = np.linalg.norm(d)
            if length < 1e-12:
                continue
            # Perpendicular bisector of s and t, keeping s's side.
            poly = _clip_polygon(poly, d / length, float((d / length) @ ((s + t) * 0.5)))
            if len(poly) == 0:
                break
        cells.append(poly)
    return cells


def _seed_points(outline: np.ndarray, count: int, impact_uv: Optional[np.ndarray],
                 focus: float, rng: np.random.Generator) -> np.ndarray:
    """Seed positions, biased toward the impact point.

    Real glass is pulverised where it was struck and comes away in larger plates
    further out.  Uniform seeding gives a uniform fragment size, which reads as
    a regular pattern — the one thing broken glass never looks like.
    """
    lo = outline.min(axis=0)
    hi = outline.max(axis=0)
    pts: List[np.ndarray] = []
    guard = 0
    while len(pts) < count and guard < count * 60:
        guard += 1
        p = rng.uniform(lo, hi)
        if not _point_in_polygon(p, outline):
            continue
        if impact_uv is not None and focus > 0.0:
            # Rejection-sample toward the impact: probability falls off with
            # distance, so density is highest at the strike.
            span = float(np.linalg.norm(hi - lo)) or 1.0
            d = float(np.linalg.norm(p - impact_uv)) / span
            if rng.random() > np.exp(-focus * d):
                continue
        pts.append(p)
    if len(pts) < 3:  # pragma: no cover - degenerate pane
        return np.array([outline.mean(axis=0)] + list(outline[:3]))
    return np.array(pts)


def _point_in_polygon(p: np.ndarray, poly: np.ndarray) -> bool:
    """Winding test for a convex CCW polygon."""
    n = len(poly)
    for i in range(n):
        a, b = poly[i], poly[(i + 1) % n]
        if np.cross(b - a, p - a) < 0:
            return False
    return True


def _dist_to_outline(p: np.ndarray, outline: np.ndarray) -> float:
    """Shortest distance from a point to the boundary of the outline polygon."""
    best = float("inf")
    n = len(outline)
    for i in range(n):
        a, b = outline[i], outline[(i + 1) % n]
        ab = b - a
        t = float(np.clip(((p - a) @ ab) / max(ab @ ab, 1e-12), 0.0, 1.0))
        best = min(best, float(np.linalg.norm(p - (a + t * ab))))
    return best


# ---------------------------------------------------------------------------
# Shatter
# ---------------------------------------------------------------------------


#: Hard ceiling on how much of a pane may be classified as retained fringe.
#: The band test alone is not enough of a guarantee: on a coarse tessellation a
#: single cell can be wider than the band itself, and if the band logic ever
#: matches most of them the user sees the pane's whole rim stay behind and only
#: its middle break out — the "only the central vertices shatter" symptom.  With
#: this cap the worst case is a thin rim, never a pane with a hole punched in it.
MAX_RETAINED_FRACTION = 0.35


@dataclass
class GlassFragment:
    """One piece of a shattered pane."""

    #: (N, 3) world-space vertices of the solid fragment.
    verts: np.ndarray
    #: Faces indexing into :attr:`verts`.
    faces: List[Tuple[int, ...]]
    #: Centroid, world space — where the object origin goes.
    centre: np.ndarray
    #: True when this fragment stays welded into the window frame.
    retained: bool
    #: Distance from the impact point, normalised to the pane's size.
    impact_distance: float


def shatter_pane(verts: np.ndarray,
                 impact_point: Optional[np.ndarray] = None,
                 fragments: int = 90,
                 thickness: float = 0.005,
                 edge_retain: float = 0.05,
                 focus: float = 2.2,
                 seed: int = 0) -> List[GlassFragment]:
    """Break a pane into tiling fragments cut from its real geometry.

    ``verts`` is the pane's vertex cloud (cache-local or world — the result is
    returned in the same space).  ``impact_point`` biases fragment density and
    must be in that same space.  ``edge_retain`` is the fraction of the pane's
    half-extent, measured inward from the outline, within which a fragment is
    marked :attr:`GlassFragment.retained` and left stuck in the frame.

    Retained is measured as the CELL CENTROID'S distance to the outline, not the
    farthest-vertex reach from the pane centre.  The old reach test measured
    against the pane's diagonal, so on a wide windshield it kept the corner and
    end cells but silently dropped the top and bottom edge strips — the user
    then saw the pane's long edges shed all their glass while the corners
    travelled with the car, reading as "the whole windshield came off".  A
    centroid-to-outline test keeps a uniform thin ring around the FULL
    perimeter; everything inside the ring falls.

    Returns one :class:`GlassFragment` per Voronoi cell.
    """
    pts = np.asarray(verts, dtype=np.float64)
    if len(pts) < 4:
        return []

    basis = pane_basis(pts)
    uv = basis.to_2d(pts)
    outline = _convex_hull_2d(uv)
    if len(outline) < 3:
        return []

    rng = np.random.default_rng(seed)
    impact_uv = None
    if impact_point is not None:
        impact_uv = basis.to_2d(np.asarray(impact_point, dtype=np.float64)[None, :])[0]
        # Guard against a space mismatch (e.g. a ground-shift disagreement
        # between the impact events and the pane verts) putting the impact
        # wildly outside the pane.  Rejection-sampling toward a point that far
        # out rejects every candidate seed and the tessellation collapses to a
        # handful of cells (measured: 4), all of which then read as "retained"
        # and follow the car.  Fall back to uniform seeding in that case.
        if not _point_in_polygon(impact_uv, outline):
            impact_uv = None

    seeds = _seed_points(outline, max(4, int(fragments)), impact_uv, focus, rng)
    cells = voronoi_cells(seeds, outline)

    centre_uv = outline.mean(axis=0)
    # Half-extent used to decide what counts as "near the frame edge".
    extent = float(np.abs(outline - centre_uv).max()) or 1.0
    span = float(np.linalg.norm(outline.max(axis=0) - outline.min(axis=0))) or 1.0
    half = float(thickness) * 0.5

    band = float(edge_retain) * extent
    out: List[GlassFragment] = []
    depths: List[float] = []   # how far each fragment reaches in from the outline
    for cell in cells:
        if len(cell) < 3:
            continue
        area = _polygon_area(cell)
        if area <= 1e-9:
            continue

        cell_centre = cell.mean(axis=0)
        # Fringe test: the cell's CENTROID sits inside the band around the frame.
        # Using the centroid (rather than the cell's deepest vertex) is
        # deliberate — a Voronoi cell on a 59-vertex windshield is often wider
        # than the band itself, so requiring the whole cell inside it retains
        # nothing at all and the aperture comes away completely empty.
        #
        # The centroid test alone is too generous though, and that is what
        # produced "only the central vertices of the windshield shatter": a cell
        # spanning from the rim into the middle of the pane still centroids
        # inside the band, so the rim held a thick collar and only the innermost
        # cells flew out.  `depth` is recorded so the caller can trim the fringe
        # back to a thin ring — see MAX_RETAINED_FRACTION below.
        depth = _dist_to_outline(cell_centre, outline)
        retained = depth <= band

        top = basis.to_3d(cell, half)
        bottom = basis.to_3d(cell, -half)
        fverts = np.vstack([top, bottom])

        n = len(cell)
        # Outward winding, same convention as debris_shards.build_shard_geometry.
        faces: List[Tuple[int, ...]] = [
            tuple(range(n - 1, -1, -1)),
            tuple(range(n, 2 * n)),
        ]
        for i in range(n):
            j = (i + 1) % n
            faces.append((i, n + i, n + j, j))

        centre3 = fverts.mean(axis=0)
        dist = (float(np.linalg.norm(cell_centre - impact_uv)) / span
                if impact_uv is not None else 0.0)
        out.append(GlassFragment(verts=fverts - centre3, faces=faces,
                                 centre=centre3, retained=retained,
                                 impact_distance=dist))
        depths.append(depth)

    # Safety net: if the band still claimed too much of the pane (a very coarse
    # tessellation, or a large edge_retain on a small pane), keep only the
    # shallowest fragments — the ones actually hugging the rim — and release the
    # rest.  Without this the pane can end up with a collar that never falls.
    keep = int(len(out) * MAX_RETAINED_FRACTION)
    retained_idx = [i for i, f in enumerate(out) if f.retained]
    if len(retained_idx) > keep:
        retained_idx.sort(key=lambda i: depths[i])
        for i in retained_idx[keep:]:
            out[i].retained = False
    return out


def _polygon_area(poly: np.ndarray) -> float:
    x, y = poly[:, 0], poly[:, 1]
    return 0.5 * abs(float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))))


def fragment_count_for(severity: float, tier_shattered: bool,
                       base: int = 70, vertex_count: int = 0) -> int:
    """How many pieces a pane breaks into.

    Tempered glass dices into thousands of fragments; simulating that literally
    would add thousands of rigid bodies for something the eye reads as a
    glittering cloud.  The count is scaled by severity and capped — the fine
    particle spray carries the "thousands of chips" impression, while these
    fragments carry the structure.
    """
    if not tier_shattered:
        return 0
    n = int(round(base * (0.45 + 0.55 * float(np.clip(severity, 0.0, 1.0)))))
    # A pane with very few vertices (the windshield has 59) cannot support a
    # very dense tessellation meaningfully, but it is the outline that matters,
    # so only clamp the extreme low end.
    return int(np.clip(n, 12, 220))
