from __future__ import annotations

"""Fake tyre contact-patch flattening for cached BeamNG playback.

BeamNG renders wheels with a *rigid*, perfectly round tyre mesh pinned to the
physics hub, so a loaded tyre never shows a contact patch — the round mesh just
sinks into the ground as the hub deflects.  This module fakes the missing
rubber deformation at **playback** time (and again at bake time, so Alembic
export matches the viewport), driven purely by how the cached wheel geometry
sits relative to the ground plane:

* **contact patch** — vertices below the ground plane are projected up onto it,
  so the flat patch grows and shrinks with the real physics load: the deeper
  the hub sinks, the wider the patch.  This part needs no tuning at all, it is
  the true clipped silhouette of a circle resting on a plane.
* **static deflection** (``extra``) — an additional squash of the lower carcass
  toward the ground, so a tyre that merely *rests* on the ground (hub exactly
  at the unloaded radius, no penetration to clip) still shows a patch.  Weighted
  by height below the axle so the tread moves and the bead near the rim doesn't.
* **sidewall bulge** (``bulge``) — the rubber displaced by the patch is pushed
  out sideways along the wheel's axle axis, strongest at the sidewall and at
  ground level, fading out with height.
* **lift-off release** (``release``) — every term is ramped by the tyre's
  distance to the ground and becomes exactly zero once the tyre is more than
  ``release`` above it, so a wheel leaving the ground returns to perfectly
  round.  Nothing here is persistent or accumulated: each frame is computed
  from that frame's geometry alone, so a tyre is never "already slightly flat"
  before it ever touches the ground.

Pure numpy, no ``bpy``.  Geometry is expressed as *heights above a horizontal
ground plane*, so the caller supplies the local→height basis (``up``,
``height_offset``) and this module stays space-agnostic — the same code works
for playback (basis from the object's parent chain) and for the Alembic bake
(basis from the cache's per-frame rigid transform).

Assumes a flat, horizontal ground at a single height.  Sloped terrain is out of
scope: set ``ground_z`` to the local ground height for the shot.
"""

from dataclasses import dataclass, field
from typing import Optional, Sequence, Tuple

import numpy as np


_DEFAULT_NAMES = ("tire", "tyre")

#: Hard cap on the contact patch depth, as a fraction of the tyre's radius.  A
#: real tyre bottoms out on its rim long before this; the cap exists so a wrong
#: ``ground_z`` degrades into a slightly over-squashed tyre rather than a
#: pancake.  Not exposed in the UI — it's a guard rail, not a look control.
MAX_SQUASH_RATIO = 0.35


@dataclass
class TyreSettings:
    """Tunables for :func:`flatten_tyre`, mirrored by the add-on's UI props.

    ``amount`` is the master switch: at ``0.0`` the deformation is skipped
    entirely (zero cost, byte-identical playback to before this feature), at
    ``1.0`` the tyre flattens fully onto the ground plane.
    """

    amount: float = 0.0        # 0..1 master strength
    extra: float = 0.02        # static deflection in metres when in contact
    bulge: float = 0.6         # sidewall bulge as a fraction of the squash
    release: float = 0.03      # metres above ground at which the effect is 0
    ground_z: float = 0.0      # world height of the ground plane
    names: Tuple[str, ...] = field(default_factory=lambda: _DEFAULT_NAMES)

    @property
    def enabled(self) -> bool:
        return self.amount > 1e-4

    def matches(self, obj_name: str) -> bool:
        """True if *obj_name* looks like a tyre (and not a rim/hub/brake)."""
        low = obj_name.lower()
        return any(tok and tok in low for tok in self.names)

    # --- (de)serialisation for scene storage / undo recovery -------------
    def update(self, **kwargs) -> "TyreSettings":
        for key, val in kwargs.items():
            if val is None or not hasattr(self, key):
                continue
            if key == "names":
                setattr(self, key, parse_names(val))
            else:
                setattr(self, key, float(val))
        return self

    def to_dict(self) -> dict:
        return {
            "amount": float(self.amount),
            "extra": float(self.extra),
            "bulge": float(self.bulge),
            "release": float(self.release),
            "ground_z": float(self.ground_z),
            "names": ",".join(self.names),
        }

    @classmethod
    def from_dict(cls, data) -> "TyreSettings":
        s = cls()
        if data:
            s.update(**{k: data[k] for k in dict(data)})
        return s


def parse_names(value) -> Tuple[str, ...]:
    """Accept ``"tire, tyre"`` or a sequence; return a lowercase token tuple."""
    if value is None:
        return _DEFAULT_NAMES
    if isinstance(value, str):
        parts: Sequence[str] = value.split(",")
    else:
        parts = list(value)
    toks = tuple(p.strip().lower() for p in parts if str(p).strip())
    return toks or _DEFAULT_NAMES


def axle_axis(pos: np.ndarray, up: np.ndarray) -> Optional[np.ndarray]:
    """Estimate a wheel's spin axis from its vertex cloud.

    A tyre is a wide-diameter, narrow-width shell, so the axle is the axis of
    *smallest* variance — the smallest eigenvector of the vertex covariance.
    Spin doesn't change it (spinning is rotation *about* it), so this is stable
    frame to frame; steering and body roll are picked up automatically.

    Returns ``None`` when the estimate is untrustworthy (too few vertices, or a
    near-vertical result, which would mean the cloud isn't wheel-shaped).
    """
    if pos.shape[0] < 16:
        return None
    centred = pos - pos.mean(axis=0)
    cov = (centred.T @ centred) / float(pos.shape[0])
    try:
        _vals, vecs = np.linalg.eigh(cov.astype(np.float64))
    except np.linalg.LinAlgError:  # pragma: no cover - defensive
        return None
    axis = vecs[:, 0]
    norm = float(np.linalg.norm(axis))
    if norm < 1e-9:
        return None
    axis = (axis / norm).astype(np.float32)
    # A wheel's axle is roughly horizontal even with heavy camber; a vertical
    # result means we're not looking at a wheel, so decline to bulge.
    if abs(float(axis @ up)) > 0.85:
        return None
    return axis


def flatten_tyre(pos: np.ndarray,
                 up: np.ndarray,
                 height_offset: float,
                 s: TyreSettings) -> Tuple[np.ndarray, float]:
    """Deform one tyre's vertices for ground contact.

    Parameters
    ----------
    pos:
        ``(N, 3)`` float32 vertex positions in the space the cache stores.
    up:
        Unit vector, in that same space, pointing along world +Z.  Displacing a
        vertex by ``d * up`` raises it ``d`` metres in the world.
    height_offset:
        Added to ``pos @ up`` to get each vertex's height **above the ground
        plane** (so it already folds in the rigid transform's Z and
        ``ground_z``).
    s:
        The tunables.

    Returns
    -------
    ``(positions, squash)`` — the deformed positions (a fresh array whenever
    anything changed, or *pos* untouched when nothing did) and the total squash
    depth in metres, for logging.
    """
    if not s.enabled or pos.shape[0] == 0:
        return pos, 0.0

    up = np.asarray(up, dtype=np.float32).reshape(3)
    # Heights in float64: `height_offset` carries the vehicle's world position,
    # which can be hundreds of metres, while the deformation we care about is
    # sub-centimetre.  In float32 that cancellation costs ~0.5 mm of precision
    # and visibly roughens the contact patch, so measure wide and only narrow
    # back to float32 when writing displacements.
    h = pos @ up.astype(np.float64) + float(height_offset)
    h_min = float(h.min())

    release = max(float(s.release), 1e-6)
    if h_min >= release:
        return pos, 0.0  # airborne: perfectly round, no residual flattening

    h_max = float(h.max())
    radius = 0.5 * (h_max - h_min)
    if radius <= 1e-6:
        return pos, 0.0

    # Contact ramp: full effect once touching or penetrating, easing to zero as
    # the tyre lifts to `release` above the ground.  This is what makes a lifted
    # car's tyres round again instead of holding a flat spot.
    prox = 1.0 if h_min <= 0.0 else 1.0 - h_min / release

    # Safety clamp: a real tyre can't flatten past its own rim, so cap the patch
    # at MAX_SQUASH_RATIO of the radius.  Without this, a mis-set ``ground_z``
    # (or forgetting the importer's auto-ground) puts the whole wheel below the
    # plane and pancakes its entire lower half instead of degrading gracefully.
    # Raising the measured heights lifts the effective ground instead of the
    # tyre, so the deformation is bounded and the wheel stays recognisable.
    max_squash = MAX_SQUASH_RATIO * radius
    if h_min < -max_squash:
        shift = -max_squash - h_min
        h = h + shift
        h_min += shift
        h_max += shift

    out = np.array(pos, dtype=np.float32)  # cache positions are a read-only mmap view
    up64 = up.astype(np.float64)

    # --- static deflection: squash the lower carcass toward the ground ------
    # Weight by depth below the axle so the tread flattens while the bead (up
    # near the rim) barely moves, which is how a real sidewall deflects.
    drop_max = float(s.extra) * prox * float(s.amount)
    if drop_max > 1e-6:
        axle_h = 0.5 * (h_max + h_min)
        t = np.clip((axle_h - h) / radius, 0.0, 1.0)
        drop = (t * t) * drop_max
        out -= (up64 * drop[:, None]).astype(np.float32)
        h -= drop

    # --- contact patch: project everything below the ground onto it ---------
    depth = 0.0
    below = h < 0.0
    if bool(below.any()):
        lift = (-h[below]) * float(s.amount)
        out[below] += (up64 * lift[:, None]).astype(np.float32)
        h[below] += lift
        depth = float(lift.max())

    # Nothing meaningful moved (in contact range but no penetration and no
    # deflection configured), so report no squash and skip the bulge.  Reachable
    # only when drop_max <= 1e-6, i.e. `out` is still an untouched copy of *pos*.
    squash = depth + drop_max
    if squash <= 1e-6:
        return out, 0.0

    # --- sidewall bulge: displaced rubber pushed out along the axle ---------
    if float(s.bulge) > 1e-4:
        axle = axle_axis(out, up)
        if axle is not None:
            # Bulge HORIZONTALLY: a cambered axle tilts out of the ground plane,
            # and displacing along it would shove sidewall vertices back down
            # through the ground we just lifted them onto (measured ~3.5 mm of
            # re-penetration at full lock).  Rubber squeezed out of the contact
            # patch has nowhere to go but sideways, so drop the vertical part.
            axle = axle - up * float(axle @ up)
            n_axle = float(np.linalg.norm(axle))
            if n_axle < 1e-6:  # pragma: no cover - excluded by axle_axis guard
                return out, squash
            axle = (axle / n_axle).astype(np.float32)
            side = (out - out.mean(axis=0)) @ axle
            half = float(np.abs(side).max())
            if half > 1e-6:
                band_h = min(radius, max(squash * 5.0, 1e-6))
                band = np.clip(1.0 - h / band_h, 0.0, 1.0)
                lateral = np.abs(side).astype(np.float64) / half
                mag = (np.sign(side) * lateral * (band * band)
                       * (squash * float(s.bulge)))
                out += (axle.astype(np.float64) * mag[:, None]).astype(np.float32)

    return out, squash


def height_basis_from_transform(transform: Optional[np.ndarray],
                                ground_z: float = 0.0) -> Tuple[np.ndarray, float]:
    """Build ``(up, height_offset)`` from a BVC per-frame rigid transform.

    *transform* is the 12-float block from :meth:`CacheReader.frame_transform`
    (position(3) + row-major 3x3), which places the vehicle in the world as
    ``world = M @ local + p``.  A vertex's world height is therefore
    ``row_z · local + p_z``, so ``up`` is M's bottom row and the offset carries
    ``p_z`` minus the ground height.

    Objects that also carry their own translation (the importer's auto-ground
    shift lives in ``obj.location``) should add ``float(up @ obj.location)`` to
    the returned offset.

    Falls back to a world-Z basis when the cache has no transform data — then
    the stored positions are already world-oriented.
    """
    fallback = (np.array([0.0, 0.0, 1.0], dtype=np.float32), -float(ground_z))
    if transform is None or len(transform) < 12:
        return fallback
    row_z = np.asarray(transform[9:12], dtype=np.float32)
    norm = float(np.linalg.norm(row_z))
    if norm < 1e-9:  # pragma: no cover - defensive
        return fallback
    up = (row_z / np.float32(norm)).astype(np.float32)
    # Heights measured as `pos @ up` are scaled by 1/norm relative to true world
    # metres, so scale the constant term the same way (norm == 1 for a proper
    # rotation, which is what the builder writes).
    offset = (float(transform[2]) - float(ground_z)) / norm
    return up, offset
