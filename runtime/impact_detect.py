from __future__ import annotations

"""Detect per-part impact events directly from a BVC cache.

The previous approach worked from a side-car ``.npz`` of scalar telemetry
(min-z, centroid, speed) and detected *ground strikes*.  That misses the thing
that actually makes a crash look like a crash: the frame a panel gets **crushed**.

This module reads the cache itself, which carries strictly more information, and
keys off the one signal that is unambiguous:

**Cache-local vertex motion IS deformation.**  The BVC stores each part's
positions in vehicle-local space with the rigid body motion factored out into a
separate per-frame transform (see :meth:`CacheReader.frame_transform`).  So a
part flying down the road at 30 m/s has *zero* local vertex motion until
something hits it.  ``mean |p[f] - p[f-1]|`` over a part's vertices is therefore
a direct read-out of "how hard is this part being deformed right now" — no
thresholding of world velocity, no guessing.

Three signals are combined:

``deform``   peak local vertex motion — a panel being crushed
``ground``   world min-z crossing the ground plane while moving down
``impulse``  a sharp change in the part's world velocity (it hit *something*)

and each surviving event carries the **hotspot**: the world-space centroid of
the vertices that actually moved, weighted by how far they moved.  That is where
the debris gets spawned, so shards come off the corner of the bumper that hit
the kerb rather than the middle of the part.

No ``bpy`` import — this is plain numpy so it can be unit-tested outside Blender.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

# ---------------------------------------------------------------------------
# Material classification
# ---------------------------------------------------------------------------

# Order matters: the first matching rule wins, so the specific patterns
# ("headlightglass") must precede the general ones ("headlight").
_MATERIAL_RULES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("glass", ("windshield", "backlight", "doorglass", "glass")),
    ("rubber", ("tire_", "tyre")),
    ("plastic", ("bumper", "headlight", "taillight", "licenseplate",
                 "mirror", "dash", "shifter", "signalstalk", "pedal",
                 "needle", "gauges", "wheelcap")),
    ("paint", ("body", "hood", "trunk", "door_", "fender", "roof")),
    ("chrome", ("bumperbar", "exhaust", "brake_disc", "brake_caliper")),
    ("steel", ()),  # default
)

#: Rough density-ish weights used only to bias debris counts per material, so a
#: crushed steel subframe throws fewer, heavier pieces than a shattered window.
MATERIAL_DEBRIS_BIAS: Dict[str, float] = {
    "glass": 2.2,
    "plastic": 1.3,
    "paint": 1.0,
    "chrome": 0.7,
    "steel": 0.6,
    "rubber": 0.4,
}


def classify_material(part_name: str) -> str:
    """Map a cache part name to a debris material key."""
    low = part_name.lower()
    for material, needles in _MATERIAL_RULES:
        if any(n in low for n in needles):
            return material
    return "steel"


def is_glass(part_name: str) -> bool:
    return classify_material(part_name) == "glass"


# ---------------------------------------------------------------------------
# Event model
# ---------------------------------------------------------------------------


@dataclass
class ImpactEvent:
    """One detected impact on one part."""

    part: str
    material: str
    cache_frame: int
    #: World-space point the debris should originate from (deformation hotspot).
    position: Tuple[float, float, float]
    #: Unit vector the debris should be thrown along, world space.
    direction: Tuple[float, float, float]
    #: Part's world velocity at impact, in METRES PER SECOND.
    #: Stored in real units rather than per-sample deltas: the sampling stride
    #: is an internal detail of detection, and a consumer that assumed
    #: "per cache frame" silently launched debris at double speed.
    velocity: Tuple[float, float, float]
    #: Peak local deformation for this event (metres/frame).
    deform: float
    #: Speed of the part at impact (metres/frame).
    speed: float
    #: Height of the hotspot above the ground plane.
    height: float
    #: Which signals fired: any of "deform", "ground", "impulse".
    kinds: Tuple[str, ...] = ()
    #: Relative severity in [0, 1], normalised across the whole detection run.
    severity: float = 0.0
    #: How deep the part's LOWEST vertex went below the ground plane, in metres
    #: (0.0 when it never crossed).  Distinct from :attr:`height`, which tracks
    #: the deformation hotspot: a windshield can be crushed at its top edge
    #: while its bottom edge is buried in the tarmac.  This is what separates
    #: "the glass cracked" from "the glass hit the road", and it is measured
    #: rather than inferred — on the real capture the windshield reaches
    #: -0.124 m and the backlight -0.037 m at cache frame 652.
    ground_depth: float = 0.0
    #: Absolute (un-normalised) impact energy proxy, ``deform * speed``.  The
    #: normalised :attr:`severity` is relative to the strongest event in the
    #: run, so a capture with no big crash still produces severity-1.0 events;
    #: this keeps an absolute scale for the glass tiers.
    energy: float = 0.0

    def to_dict(self) -> dict:
        return {
            "part": self.part,
            "material": self.material,
            "cache_frame": int(self.cache_frame),
            "position": [float(v) for v in self.position],
            "direction": [float(v) for v in self.direction],
            "velocity": [float(v) for v in self.velocity],
            "deform": float(self.deform),
            "speed": float(self.speed),
            "height": float(self.height),
            "kinds": list(self.kinds),
            "severity": float(self.severity),
            "ground_depth": float(self.ground_depth),
            "energy": float(self.energy),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ImpactEvent":
        return cls(
            part=d["part"],
            material=d["material"],
            cache_frame=int(d["cache_frame"]),
            position=tuple(d["position"]),
            direction=tuple(d["direction"]),
            velocity=tuple(d.get("velocity", (0.0, 0.0, 0.0))),
            deform=float(d.get("deform", 0.0)),
            speed=float(d.get("speed", 0.0)),
            height=float(d.get("height", 0.0)),
            kinds=tuple(d.get("kinds", ())),
            severity=float(d.get("severity", 0.0)),
            ground_depth=float(d.get("ground_depth", 0.0)),
            energy=float(d.get("energy", 0.0)),
        )


@dataclass
class DetectSettings:
    """Tunables for :func:`detect_impacts`."""

    #: Sample every Nth cache frame.  2 keeps sub-100 ms timing resolution at
    #: 24 fps capture while halving the read cost.
    stride: int = 2
    #: Cap on vertices sampled per part.  Deformation is a bulk statistic, so a
    #: few hundred vertices track it as well as 16 000 and read ~40x faster.
    max_verts: int = 384
    #: Local deformation (m/frame) below which a part is considered undamaged.
    #: Comfortably above float noise on a rigid part.
    deform_threshold: float = 0.004
    #: Ground plane height in world space.
    ground_z: float = 0.0
    #: Hotspot must be within this of the ground for a "ground" classification.
    ground_tolerance: float = 0.35
    #: Minimum world speed (m/frame) for a ground strike to count.
    min_ground_speed: float = 0.02
    #: Velocity change (m/frame) that counts as hitting something.
    impulse_threshold: float = 0.05
    #: Minimum cache frames between two events on the SAME part, so one long
    #: crush registers once instead of every frame it is being crushed.
    min_separation: int = 12
    #: Drop events weaker than this fraction of the strongest event found.
    #: The tail below this is a settling car creaking, not material failing.
    relative_floor: float = 0.18
    #: Fraction of a part's vertices that define the deformation hotspot.
    hotspot_fraction: float = 0.15
    #: Skip parts whose name contains any of these (case-insensitive).
    #: Cabin parts deform heavily in a rollover but are *enclosed* — debris
    #: from them would spawn inside the shell and rain out through the floor.
    exclude: Tuple[str, ...] = (
        "dash", "seats", "steer", "shifter", "needle", "gauges",
        "signalstalk", "pedal", "interior", "_bake", "_intense",
    )


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------


def local_to_world(local: np.ndarray, transform: Optional[np.ndarray],
                   ground_shift: float = 0.0) -> np.ndarray:
    """Map cache-local positions to Blender world space.

    Mirrors what playback does at draw time: the chunk object carries the
    auto-ground offset in ``obj.location`` (z only) and the root Empty carries
    the per-frame rigid transform, so

        world = M @ (local + [0, 0, ground_shift]) + P

    with ``transform`` the 12-float block ``[px, py, pz, m00..m22]`` (row-major).
    Verified against Blender's own ``matrix_world`` to 1e-3 m.
    """
    pos = np.asarray(local, dtype=np.float64)
    if ground_shift:
        pos = pos + np.array((0.0, 0.0, float(ground_shift)))
    if transform is None:
        return pos
    tf = np.asarray(transform, dtype=np.float64).reshape(-1)
    if tf.shape[0] < 12:
        return pos
    m = tf[3:12].reshape(3, 3)
    return pos @ m.T + tf[0:3]


def kabsch_residual(a: np.ndarray, b: np.ndarray) -> Tuple[float, np.ndarray]:
    """True deformation between two poses of the same vertex set.

    **This is the correction that makes detection trustworthy.**  Cache-local
    positions have the *vehicle's* rigid motion removed, but not each part's own
    motion within the vehicle: a door swings on its hinge, a wheel spins, the
    speedometer needle sweeps.  Measured on real capture data, parts rotate
    13-50 degrees locally between adjacent sampled frames, so raw local motion
    over-reads deformation by roughly 2x on genuinely crushed panels — and
    catastrophically on parts that only rotate.

    The worst case found: ``needle_speedo`` reads 0.73 m/frame of raw local
    motion at the impact, but only 0.009 m of actual shape change.  Detecting on
    raw motion would spawn a burst of metal debris out of the *speedometer
    needle*.  A rigid-body-aligned residual reads it as the rigid part it is.

    So: superimpose ``b`` onto ``a`` with the optimal rotation (Kabsch/SVD,
    reflection-corrected), then measure what is left over.  Rotation and
    translation cancel exactly; only real shape change survives.

    The residual must be measured by rotating ``a`` ONTO ``b`` and comparing
    against ``b``.  Rotating ``b`` and differencing against ``a`` applies the
    rotation in the wrong direction, which *doubles* it instead of cancelling
    it — that reads exactly ``2 x`` the unaligned motion and reports a rigid,
    free-flying car as continuously deforming.  Verified on capture data: the
    correct orientation returns 0.0000 for every part while the car is airborne
    and non-zero only on the frames material actually fails.

    Returns ``(mean_residual, per_vertex_residual)``.
    """
    ac = a - a.mean(axis=0)
    bc = b - b.mean(axis=0)
    try:
        u, _s, vt = np.linalg.svd(ac.T @ bc)
    except np.linalg.LinAlgError:  # pragma: no cover - degenerate geometry
        per_vert = np.linalg.norm(b - a, axis=1)
        return float(per_vert.mean()), per_vert
    # Correct for a reflection, which SVD alone can produce.
    d = np.sign(np.linalg.det(vt.T @ u.T))
    rot = vt.T @ np.diag((1.0, 1.0, d)) @ u.T
    per_vert = np.linalg.norm(ac @ rot.T - bc, axis=1)
    return float(per_vert.mean()), per_vert


def sample_delta_to_ms(delta: np.ndarray, playback_fps: float,
                       stride: int) -> np.ndarray:
    """Convert a per-sample position delta into metres per second.

    The detector samples every ``stride`` cache frames, so one sample delta
    spans ``stride`` cache frames.  The whole capture of ``n`` cache frames
    plays back over ``n / playback_fps`` seconds (see
    ``frame_handler._cache_frame_for``), so one cache frame is
    ``1 / playback_fps`` scene-seconds and the scene-space velocity is::

        delta * playback_fps / stride

    This is the single source of truth for the units of
    :attr:`ImpactEvent.velocity`.  Consumers (``debris_spawn``) take the field
    as-is and must NOT re-scale it — the earlier code multiplied by 24.0 *and*
    ignored the stride, launching debris at roughly double speed (55 m/s
    instead of 28 m/s on a stride-2 scan).
    """
    return np.asarray(delta, dtype=np.float64) * (
        max(1e-6, float(playback_fps)) / max(1, int(stride)))


def _pick_vertex_sample(count: int, max_verts: int) -> Optional[np.ndarray]:
    """Evenly spaced vertex indices, or None to mean "use all of them"."""
    if count <= max_verts:
        return None
    return np.linspace(0, count - 1, max_verts).astype(np.intp)


def _find_peaks(values: np.ndarray, threshold: float,
                min_separation: int) -> List[int]:
    """Greedy peak picking: strongest first, suppressing neighbours.

    Simpler and more predictable than scipy's find_peaks (which we cannot
    depend on inside Blender), and it gives exactly the behaviour wanted here —
    one event per crush, located at the crush's most violent frame.
    """
    idx = [i for i in np.argsort(-values) if values[i] >= threshold]
    chosen: List[int] = []
    for i in idx:
        if all(abs(i - c) >= min_separation for c in chosen):
            chosen.append(int(i))
    return sorted(chosen)


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def _scan_part(reader, name: str, frames: Sequence[int],
               settings: DetectSettings, ground_shift: float,
               transforms: Dict[int, Optional[np.ndarray]]):
    """Read one part across ``frames`` and derive its per-sample signals."""
    try:
        obj = reader.get_object(name)
        vertex_count = int(obj.vertex_count)
    except Exception:
        return None
    if vertex_count == 0:
        return None

    sample = _pick_vertex_sample(vertex_count, settings.max_verts)

    local: List[np.ndarray] = []
    world: List[np.ndarray] = []
    for f in frames:
        try:
            p = reader.frame_positions(name, f)
        except Exception:
            return None
        if p.shape[0] != vertex_count:
            return None
        p = p[sample] if sample is not None else p
        p = np.asarray(p, dtype=np.float64)
        local.append(p)
        world.append(local_to_world(p, transforms.get(f), ground_shift))

    local_arr = np.stack(local)          # (T, V, 3)
    world_arr = np.stack(world)          # (T, V, 3)

    # Deformation: shape change only.  The vehicle's rigid motion is already
    # factored out of the cache, but each part still rotates *within* the
    # vehicle, so the residual after best-fit alignment is what we want.  See
    # :func:`kabsch_residual` for why raw local motion is not usable here.
    t_count, v_count = local_arr.shape[0], local_arr.shape[1]
    per_vert_deform = np.zeros((t_count, v_count))
    deform = np.zeros(t_count)
    for t in range(1, t_count):
        mean_resid, per_vert = kabsch_residual(local_arr[t - 1], local_arr[t])
        deform[t] = mean_resid
        per_vert_deform[t] = per_vert

    centroid = world_arr.mean(axis=1)                    # (T, 3)
    vel = np.zeros_like(centroid)
    vel[1:] = centroid[1:] - centroid[:-1]
    speed = np.linalg.norm(vel, axis=1)

    accel = np.zeros_like(speed)
    accel[1:] = np.linalg.norm(vel[1:] - vel[:-1], axis=1)

    min_z = world_arr[:, :, 2].min(axis=1)

    return {
        "local": local_arr,
        "world": world_arr,
        "per_vert_deform": per_vert_deform,
        "deform": deform,
        "centroid": centroid,
        "vel": vel,
        "speed": speed,
        "accel": accel,
        "min_z": min_z,
    }


def _hotspot(world_frame: np.ndarray, weights: np.ndarray,
             fraction: float) -> Tuple[np.ndarray, np.ndarray]:
    """Weighted centroid of the most-displaced vertices, plus a spread axis.

    Returns ``(point, spread_direction)``.  Falls back to the plain centroid
    when nothing moved, so a ground strike on a rigid part still gets a
    sensible spawn point.
    """
    n = world_frame.shape[0]
    if weights.max() <= 0.0:
        return world_frame.mean(axis=0), np.array((0.0, 0.0, 1.0))
    k = max(1, int(round(n * fraction)))
    top = np.argsort(-weights)[:k]
    w = weights[top]
    w = w / w.sum()
    point = (world_frame[top] * w[:, None]).sum(axis=0)
    # Direction the crushed material is being pushed, as the offset of the
    # hotspot from the part's centre.
    spread = point - world_frame.mean(axis=0)
    norm = np.linalg.norm(spread)
    spread = spread / norm if norm > 1e-6 else np.array((0.0, 0.0, 1.0))
    return point, spread


def detect_impacts(reader, settings: Optional[DetectSettings] = None,
                   ground_shift: float = 0.0,
                   part_names: Optional[Sequence[str]] = None,
                   playback_fps: float = 24.0,
                   progress=None) -> List[ImpactEvent]:
    """Find impact events across every part in ``reader``.

    ``reader`` is a :class:`~.cache_reader.CacheReader`.  ``ground_shift`` is the
    scene's ``_beamng_ground_shift`` (the auto-ground offset playback folds into
    ``obj.location``), needed so world positions line up with what is on screen.

    ``playback_fps`` is the capture rate, used to convert the per-sample
    position deltas into real m/s on the emitted events.

    ``progress`` is an optional ``callable(done, total, part_name)`` for UI.
    """
    settings = settings or DetectSettings()
    names = list(part_names) if part_names is not None else list(reader.object_names())
    if settings.exclude:
        low = tuple(e.lower() for e in settings.exclude)
        names = [n for n in names if not any(e in n.lower() for e in low)]

    n_frames = int(reader.frame_count)
    frames = list(range(0, n_frames, max(1, settings.stride)))
    if len(frames) < 3:
        return []

    # Read the rigid transform once per sampled frame, shared by all parts.
    transforms: Dict[int, Optional[np.ndarray]] = {}
    for f in frames:
        try:
            transforms[f] = reader.frame_transform(f)
        except Exception:
            transforms[f] = None

    events: List[ImpactEvent] = []
    for i, name in enumerate(names):
        if progress is not None:
            progress(i, len(names), name)
        scan = _scan_part(reader, name, frames, settings, ground_shift, transforms)
        if scan is None:
            continue

        material = classify_material(name)
        deform = scan["deform"]
        min_z = scan["min_z"]
        speed = scan["speed"]
        accel = scan["accel"]

        # --- candidate frames --------------------------------------------
        # DEFORMATION IS THE ONLY TRIGGER.  Debris is shed when material
        # actually fails, and material failure *is* shape change.  Velocity and
        # ground signals cannot open an event on their own: a part that slams
        # the ground without deforming (a rigid brake disc, the speedometer
        # needle riding along inside the cabin) sheds nothing, and letting those
        # signals trigger spawned debris out of the dashboard instruments.
        # They only annotate a deformation event, where they raise severity.
        sep = max(1, settings.min_separation // max(1, settings.stride))
        cand: Dict[int, List[str]] = {}

        for t in _find_peaks(deform, settings.deform_threshold, sep):
            cand.setdefault(t, []).append("deform")

        if cand:
            # Ground strike: crossing into the ground plane while descending.
            gz = settings.ground_z + settings.ground_tolerance
            below = min_z < gz
            for t in range(1, len(frames)):
                if below[t] and not below[t - 1] and speed[t] > settings.min_ground_speed:
                    near = min(cand, key=lambda o: abs(o - t))
                    if abs(near - t) <= sep:
                        cand[near].append("ground")

            for t in _find_peaks(accel, settings.impulse_threshold, sep):
                near = min(cand, key=lambda o: abs(o - t))
                if abs(near - t) <= sep:
                    cand[near].append("impulse")

        # --- build events -------------------------------------------------
        for t, kinds in sorted(cand.items()):
            world_frame = scan["world"][t]
            point, spread = _hotspot(world_frame,
                                     scan["per_vert_deform"][t],
                                     settings.hotspot_fraction)
            height = float(point[2] - settings.ground_z)

            # Throw direction: off the ground if this is a ground strike,
            # otherwise along the crush direction.  Always biased upward so
            # debris arcs rather than sliding along the floor.
            if "ground" in kinds or height < settings.ground_tolerance:
                direction = np.array((spread[0], spread[1], 0.0))
                n = np.linalg.norm(direction)
                direction = (direction / n * 0.6 if n > 1e-6
                             else np.array((0.0, 0.0, 0.0)))
                direction = direction + np.array((0.0, 0.0, 0.8))
            else:
                direction = spread + np.array((0.0, 0.0, 0.35))
            dn = np.linalg.norm(direction)
            direction = direction / dn if dn > 1e-6 else np.array((0.0, 0.0, 1.0))

            events.append(ImpactEvent(
                part=name,
                material=material,
                cache_frame=int(frames[t]),
                position=tuple(float(v) for v in point),
                direction=tuple(float(v) for v in direction),
                velocity=tuple(float(v) for v in sample_delta_to_ms(
                    scan["vel"][t], playback_fps, settings.stride)),
                deform=float(deform[t]),
                speed=float(speed[t]),
                height=height,
                kinds=tuple(dict.fromkeys(kinds)),
                ground_depth=float(max(0.0, settings.ground_z - min_z[t])),
                energy=float(deform[t] * max(speed[t], 1e-6)),
            ))

    if progress is not None:
        progress(len(names), len(names), "")

    return _score_and_filter(events, settings)


def _score_and_filter(events: List[ImpactEvent],
                      settings: DetectSettings) -> List[ImpactEvent]:
    """Assign a 0..1 severity and drop the noise floor.

    Severity blends deformation and speed because the two failure modes look
    different: a windshield shatters with little centroid motion, a wheel
    slamming the kerb barely deforms.  Normalising each term by the run's own
    maximum keeps the scale meaningful whatever the capture.
    """
    if not events:
        return []

    max_deform = max(e.deform for e in events) or 1.0
    max_speed = max(e.speed for e in events) or 1.0
    for e in events:
        d = e.deform / max_deform
        s = e.speed / max_speed
        bonus = 0.15 * (len(e.kinds) - 1)
        e.severity = float(min(1.0, 0.7 * d + 0.3 * s + bonus))

    peak = max(e.severity for e in events) or 1.0
    kept = [e for e in events if e.severity >= peak * settings.relative_floor]
    kept.sort(key=lambda e: (e.cache_frame, e.part))
    return kept


# ---------------------------------------------------------------------------
# Glass damage tiers
# ---------------------------------------------------------------------------

#: Glass does not behave like the other materials, so it gets its own ladder.
#: Automotive glass is *laminated* (windshield) or *tempered* (side/rear): it
#: does not chip a few pieces off the way a bumper sheds plastic.  Below the
#: shatter threshold it crazes — the crack runs across the WHOLE pane and the
#: pane stays in its frame.  Only a genuinely violent hit, or the pane striking
#: the road directly, empties it out of the car.
GLASS_INTACT = "intact"
GLASS_CRACKED = "cracked"
GLASS_SHATTERED = "shattered"


@dataclass
class GlassSettings:
    """Thresholds separating the three glass damage tiers.

    Calibrated against the real capture (``name.bvc``), where the windshield
    peaks at 0.0313 m deformation and reaches -0.124 m below the ground plane
    at cache frame 652, and the backlight peaks at 0.0345 m / -0.037 m.
    """

    #: Deformation (m/frame) at which a pane starts to craze.  Below this the
    #: pane is untouched.
    crack_deform: float = 0.006
    #: Deformation at which the pane leaves the car and breaks up.
    shatter_deform: float = 0.022
    #: A pane whose lowest vertex goes at least this far below the ground plane
    #: has struck the road face-on.  That shatters it regardless of the measured
    #: deformation: laminated glass hitting tarmac at speed always empties out,
    #: and the Kabsch residual under-reads it because the pane is being pushed
    #: bodily rather than bent.
    shatter_ground_depth: float = 0.03
    #: Fraction of the pane's half-extent, measured inward from the outline,
    #: within which a fragment stays welded into the frame.  Real cars keep a
    #: fringe of glass in the rubber seal; a perfectly empty aperture reads as
    #: "the glass object was deleted", which is exactly what it would be.
    #:
    #: Retained is decided by the CELL CENTROID'S distance to the outline (see
    #: ``glass_shatter.shatter_pane``), so this is the physical width of the
    #: fringe band.  Measured on the real windshield (51 fragments): 0.05 keeps
    #: ~4-8% of the pane — a clean thin ring around the full perimeter — while
    #: 0.15 keeps ~24-39% and reads as a still-glazed window.  Small panes keep
    #: a larger share at the same value because a thin band is a bigger fraction
    #: of a small aperture.
    edge_retain: float = 0.05


def classify_glass_damage(event: "ImpactEvent",
                          settings: Optional[GlassSettings] = None) -> str:
    """Which damage tier a glass impact falls into.

    Returns one of :data:`GLASS_INTACT`, :data:`GLASS_CRACKED`,
    :data:`GLASS_SHATTERED`.

    The ground test is deliberately an OR rather than a contribution to a
    blended score: "the windshield hit the road" is a categorically different
    event from "the roof pillar bent and stressed the glass", and averaging the
    two lets a hard face-on road strike read as a mere crack because the pane
    moved rigidly instead of deforming.
    """
    settings = settings or GlassSettings()
    if (event.ground_depth >= settings.shatter_ground_depth
            or event.deform >= settings.shatter_deform):
        return GLASS_SHATTERED
    if event.deform >= settings.crack_deform:
        return GLASS_CRACKED
    return GLASS_INTACT


#: Tier ordering, worst last.  Used to collapse repeated hits on one pane.
_GLASS_RANK = {GLASS_INTACT: 0, GLASS_CRACKED: 1, GLASS_SHATTERED: 2}


def resolve_glass_damage(events: Sequence["ImpactEvent"],
                         settings: Optional[GlassSettings] = None
                         ) -> Dict[str, Tuple[str, int, "ImpactEvent"]]:
    """Collapse every glass event into ONE outcome per pane.

    Returns ``{part_name: (tier, cache_frame, event)}``.

    Damage to glass is monotonic and irreversible, but detection reports a pane
    once per crush peak — the real capture fires the windshield at cache frames
    652, 686 and 784 as the car rolls and keeps grinding its roof along the
    road.  Treating each of those as an independent event would re-shatter an
    already-empty frame twice more, spawning three full sets of shards from a
    pane that left the car on the first hit.

    So each pane keeps its WORST tier, and the frame recorded is the FIRST
    frame that reaches that tier — the moment it actually broke, not the last
    time the wreck scraped along.  A pane that cracks and later shatters is
    reported as shattered at the shatter frame; the earlier crack is implied by
    it and is not spawned separately.
    """
    settings = settings or GlassSettings()
    out: Dict[str, Tuple[str, int, "ImpactEvent"]] = {}
    for event in sorted(events, key=lambda e: e.cache_frame):
        if not is_glass(event.part):
            continue
        tier = classify_glass_damage(event, settings)
        if tier == GLASS_INTACT:
            continue
        prev = out.get(event.part)
        if prev is None or _GLASS_RANK[tier] > _GLASS_RANK[prev[0]]:
            out[event.part] = (tier, int(event.cache_frame), event)
    return out


def summarise(events: Sequence[ImpactEvent]) -> str:
    """Human-readable summary for the operator report / console."""
    if not events:
        return "no impacts detected"
    from collections import Counter
    by_mat = Counter(e.material for e in events)
    frames = [e.cache_frame for e in events]
    parts = len({e.part for e in events})
    return (f"{len(events)} impacts on {parts} parts, "
            f"cache frames {min(frames)}-{max(frames)}, "
            + ", ".join(f"{m}:{c}" for m, c in by_mat.most_common()))
