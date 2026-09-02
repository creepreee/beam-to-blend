from __future__ import annotations

"""Physics configuration for debris simulation.

Owns everything about HOW debris physically moves and collides: the rigid
body world, the ground colliders, per-body rigid body parameters, and the
particle/Newton physics configuration.

This module is also the shared *base layer* of the debris subsystem: it holds
the constants, the :class:`DebrisSettings` dataclass and the generic helpers
(``_local_verts``, ``_linearise``, ``_key_visibility``, ``_bounce_params``,
``_get_collection``, ``_viewport_context``, ``_frozen_handlers``,
``_safe_name``) that both :mod:`debris_spawn` and :mod:`debris_bake` need.
Keeping them here lets those two import from this module without creating an
import cycle, and ``debris_spawn`` re-exports them so existing
``from runtime.debris_spawn import ...`` call sites keep working.

It deliberately contains NO spawning logic and NO baking logic: it neither
creates debris objects nor converts simulation results into keyframes.
"""

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

try:  # pragma: no cover - only inside Blender
    import bpy
    import mathutils
except ImportError:  # pragma: no cover
    bpy = None
    mathutils = None

from .impact_detect import ImpactEvent


# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

DEBRIS_COLLECTION = "BeamNG Debris"
SHARD_COLLECTION = "BeamNG Debris Shards"
GROUND_NAME = "BeamNG_DebrisGround"
GLASS_COLLECTION = "BeamNG Debris Glass"

#: Frames a hero piece travels kinematically before the solver takes over.
#: This is how launch velocity is transferred.  Fewer than ~3 transfers no
#: measurable velocity.
LAUNCH_FRAMES = 3

#: Clearance kept between a body's LOWEST POINT and the ground collider.
#: Not between its origin and the ground — see :func:`_lowest_point_offset`.
GROUND_CLEARANCE = 0.004

#: Custom property holding the frame a hero piece becomes visible.  The bake
#: clears each body's animation data, so the visibility keys written at spawn
#: have to be re-derivable afterwards — see :func:`_key_visibility`.
LAUNCH_PROP = "_beamng_debris_launch"

#: Separate deflector for the fine particles, sunk below the visible ground.
PARTICLE_GROUND_NAME = "BeamNG_DebrisGround_Particles"

#: Collection of shard templates pre-scaled for the PARTICLE system.
PARTICLE_SHARD_COLLECTION = "BeamNG Debris Shards (Particles)"

#: Collection the baked particle meshes live in.  A child of the main debris
#: collection so ``clear_debris`` and ``debris_objects`` (retime) cover them
#: without extra wiring.
PARTICLE_BAKED_COLLECTION = "BeamNG Debris Particles"

#: Custom property stamped on every baked particle object.  The verify scripts
#: use it to tell a frozen chip (no particle system) from a hero shard.
PARTICLE_BAKED_PROP = "_beamng_debris_baked_particle"


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@dataclass
class DebrisSettings:
    """Tunables exposed on the add-on panel."""

    #: Global multiplier on how much debris every impact sheds.
    density: float = 1.0
    #: Global multiplier on shard size.
    scale: float = 1.0
    #: Pieces simulated as real rigid bodies, per impact, before bias.
    hero_count: int = 14
    #: Fine particles emitted per impact, before bias.
    fine_count: int = 90
    #: Launch speed in m/s at severity 1.0.
    #:
    #: DEFAULT 0 — deliberately.  A radial cone launch from a single point is
    #: precisely what reads as a firework: every piece leaves one spot at once,
    #: on a clean parabola, in an expanding shell.  Real crash debris is not
    #: *fired*, it is *shed* — it separates from the panel and falls, carrying
    #: only whatever momentum the panel already had.  With speed 0 the pieces
    #: drop from the impact and the solver does the rest.  Raise it if a
    #: particular shot wants material thrown.
    speed: float = 0.0
    #: How much of the part's own velocity the debris inherits.  This is NOT a
    #: blast: it is the momentum the material genuinely had when it broke off,
    #: and it is what makes debris trail behind a moving wreck instead of
    #: dropping in a vertical column.  Kept modest so a fast impact does not
    #: turn into a launch by the back door.
    inherit_velocity: float = 0.3
    #: Minimum sideways scatter (m/s) given to shed material regardless of
    #: ``speed``.  Without it every piece of an impact falls from the same
    #: point and lands in a single tight stack; with it they spread over a
    #: small patch the way material peeling off a panel does.  Far too small to
    #: read as a throw.
    scatter: float = 0.45
    #: Floor on the RANDOM launch velocity (m/s) for every impact, including
    #: below-blast grazes.  The blast machinery only hands out directed speed
    #: above :attr:`min_blast_severity`; below it the spray previously got
    #: whatever ``scatter`` alone provided and a ground-level graze puddled —
    #: its particles barely moved (measured: 25 of 121 emitters with centroid
    #: travel under 0.10 m).  This guarantees the cloud visibly travels in every
    #: direction.  Deliberately random, not a directed cone, so it reads as a
    #: puff instead of the firework the blast threshold exists to suppress.
    min_launch_speed: float = 1.0
    #: Cone half-angle (degrees) the debris sprays into.
    spread: float = 55.0
    #: Ground plane height.
    ground_z: float = 0.0
    #: PARTICLES + DEBRIS BOUNCINESS.  One dial for every piece of debris in the
    #: scene — hero rigid bodies, glass fragments and fine particles alike, both
    #: against the ground and against each other.
    #:
    #: 0.0 means literally no bounce: a piece that touches the ground stays
    #: there.  Bullet's restitution alone does not achieve that — a body with
    #: restitution 0 still skitters and rolls for a long time on residual
    #: tangential velocity — so at low bounciness the damping is raised in step
    #: (see :func:`_bounce_params`), which is what actually kills the motion.
    bounciness: float = 0.25
    friction: float = 0.72
    #: Only spawn for events at or above this severity.
    #:
    #: NOTE this is a weak capacity lever on real captures: detection normalises
    #: severity to [0, 1] and its relative floor (0.18 x peak) keeps the events
    #: clustered in a narrow band — measured 94 of 121 events still pass a 0.3
    #: threshold.  The effective cap is :attr:`max_hero_total`.
    min_severity: float = 0.12
    #: Blast threshold.  Impacts BELOW this severity still shed debris, but get
    #: NO launch blast — no cone spray, no inherited throw — the pieces simply
    #: fall straight from the impact point and let the physics settle them.
    #: Without this, even a gentle back-of-car brushing the ground (severity
    #: ~0.2) fired a full firework: the speed formula
    #: ``speed * (0.35 + 0.65 * severity)`` has a 0.35 floor, so every event
    #: sprayed at 1.6+ m/s.  Measured on the real capture the back-landing is
    #: 0.20-0.23 and the door-smash is 0.41-0.55, so 0.35 cleanly separates
    #: "fall straight" from "blast".
    min_blast_severity: float = 0.35
    #: Cap on total spawned rigid bodies, so a huge capture cannot hang Blender.
    #: 900 measured ~28 min to bake a 1200-frame capture (900 bodies x ~3000
    #: frames x 7 fcurves); 240 is the sane default.  The budget is allocated
    #: PROPORTIONALLY to severity, so the first crash dominates (measured ~133
    #: of 240) but follow-up impacts — the car slamming onto its side, then
    #: landing on its back — still shed a visible medium share instead of being
    #: starved to zero by a greedy budget (which let the opening crash swallow
    #: all 200).
    max_hero_total: int = 240
    #: Distinct shard meshes generated per (part, material).
    variants: int = 8
    #: Extra frames simulated past the last impact so debris comes to rest.
    settle_frames: int = 260
    #: Random seed, so a given scene always rebuilds identically.
    seed: int = 12345
    #: Shatter glass members out of the car when they break.
    shatter_glass: bool = True

    # --- realism -----------------------------------------------------------
    #: Air drag on fine particles (Blender's ``ParticleSettings.damping``).
    #: Zero drag is what produces the "firework" read: every chip flies a clean
    #: ballistic parabola and the whole spray stays a coherent expanding shell.
    #: Real debris is light with a large frontal area, so it decelerates fast,
    #: the spray loses its shape within a few metres, and small pieces fall
    #: short of big ones.
    air_drag: float = 0.35
    #: Solver subframes for fine particles.  Particles are launched at 10-25 m/s
    #: and at 60 fps that is up to 0.4 m per frame — several times a shard's own
    #: size — so a single-step solver lets them pass through the ground before
    #: it ever tests a collision.  4 subframes cut that to ~0.1 m per substep,
    #: which still lets a fast particle skip across the COLLISION surface; the
    #: rigid-body slab has real thickness to catch what tunnels, but NEWTON
    #: particles collide against the mesh and have no "inside".  10 subframes
    #: plus the collision thickness keeps the spray above the plane (measured:
    #: the old value dropped a visible share of shards to z=-40).
    particle_subframes: int = 10
    #: Emission window in frames.  A 2-frame window fires the entire spray as
    #: one shell, which is the other half of the firework look.  Spreading
    #: emission over a handful of frames staggers the departure the way real
    #: material peels off over the duration of the crush.
    emit_window: int = 6
    #: Fraction of the launch speed that is randomised per particle.  Real
    #: fragments leave at wildly different speeds; a tight distribution reads
    #: as a coordinated burst.
    speed_spread: float = 1.0
    #: Extra spread (degrees) added to the cone at full severity.  A harder hit
    #: throws material over a wider arc.
    spread_gain: float = 40.0
    #: Multiplier on how strongly severity drives particle count.  This is the
    #: "intensity" dial: at 0 every impact sheds the same amount, at 1 the
    #: count scales fully with how hard the hit was.
    intensity_gain: float = 1.0


# ---------------------------------------------------------------------------
# Generic helpers (shared base layer)
# ---------------------------------------------------------------------------


def _get_collection(name: str, parent=None) -> "bpy.types.Collection":
    coll = bpy.data.collections.get(name)
    if coll is None:
        coll = bpy.data.collections.new(name)
        (parent or bpy.context.scene.collection).children.link(coll)
    return coll


def _local_verts(obj: "bpy.types.Object",
                 _cache: Dict[str, np.ndarray] = {}) -> Optional[np.ndarray]:
    """Object-space vertices of ``obj`` as an ``(N, 3)`` float64 array.

    Cached by mesh name.  The bake calls this once per body per frame across
    hundreds of bodies and thousands of frames, and ``foreach_get`` on a fresh
    buffer every time is what turns the ground-snap pass from seconds into
    minutes.  Hero shards deliberately SHARE mesh data between instances, so
    the cache hit rate is high.
    """
    mesh = getattr(obj, "data", None)
    count = len(mesh.vertices) if mesh is not None else 0
    if not count:
        return None
    hit = _cache.get(mesh.name)
    if hit is not None and len(hit) == count:
        return hit
    co = np.empty(count * 3, dtype=np.float64)
    mesh.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3)
    _cache[mesh.name] = co
    return co


def _safe_name(text: str) -> str:
    """Datablock-name-safe form of ``text``, short enough to take a suffix.

    Part names come from the capture and can carry separators that make the
    resulting collection names hard to read; Blender also truncates names past
    63 characters, which would silently collide two libraries onto one
    deflector.
    """
    out = "".join(c if (c.isalnum() or c in "_-") else "_" for c in text)
    return out[:40]


def _linearise(obj: "bpy.types.Object", data_path: str) -> None:
    """Force LINEAR interpolation on every key of ``data_path``.

    Used for the kinematic launch keys, which encode constant-velocity motion.
    Blender keys default to Bezier with auto-clamped handles, which eases into
    and out of every keyframe — so a body meant to travel at a steady v arrives
    at its release frame with a slope nowhere near v.
    """
    action = obj.animation_data.action if obj.animation_data else None
    if action is None:
        return
    for fc in action.fcurves:
        if fc.data_path != data_path:
            continue
        for kp in fc.keyframe_points:
            kp.interpolation = "LINEAR"


def _key_visibility(obj: "bpy.types.Object", launch_frame: int) -> None:
    """Hide a hero piece until the frame it is thrown.

    Without this every piece sits fully visible at its spawn point from frame 1
    — a static clump hanging in the air for the whole pre-crash run, while the
    debris that has already launched moves around it.  ``hide_render`` matters
    as much as ``hide_viewport``: the two are independent, and keying only the
    viewport leaves the clump in the render.
    """
    for attr in ("hide_viewport", "hide_render"):
        setattr(obj, attr, True)
        obj.keyframe_insert(attr, frame=launch_frame - 1)
        setattr(obj, attr, False)
        obj.keyframe_insert(attr, frame=launch_frame)
    # Constant interpolation: a float F-curve ramping between 1.0 and 0.0 is
    # read as visible the moment it drops below 0.5, so a default Bezier key
    # would pop the piece in a frame early.
    action = obj.animation_data.action if obj.animation_data else None
    if action is not None:
        for fc in action.fcurves:
            if fc.data_path in ("hide_viewport", "hide_render"):
                for kp in fc.keyframe_points:
                    kp.interpolation = "CONSTANT"


def _bounce_params(bounciness: float) -> Tuple[float, float, float]:
    """Map the single bounciness dial onto ``(restitution, lin_damp, ang_damp)``.

    Restitution alone does not give the behaviour the dial promises.  A Bullet
    body with ``restitution = 0`` still keeps every bit of its *tangential*
    velocity on contact, so a shard dropped onto the ground does not bounce but
    does skate and spin across it for hundreds of frames — which reads as
    ice-skating debris, not as "no bounce".  Damping is what actually removes
    the energy, so the dial drives both: as bounciness falls, damping rises.

    At 0.0 the damping is high enough that a piece touching the ground is dead
    within a few frames — one contact, then it stays put, which is exactly what
    the 0 end of the slider is documented to do.  At 1.0 damping is near zero
    and restitution is high, so pieces bounce several times.
    """
    b = float(np.clip(bounciness, 0.0, 1.0))
    restitution = 0.72 * b
    # Damping ramps the other way. The floor at b=1 stays slightly above 0 so a
    # fully bouncy scene still settles eventually instead of jittering forever.
    lin_damp = 0.85 - 0.79 * b
    ang_damp = 0.92 - 0.80 * b
    return restitution, lin_damp, ang_damp


@contextmanager
def _viewport_context():
    """Run an operator as if invoked from the 3D viewport.

    ``rigidbody.bake_to_keyframes`` is a Python operator that internally calls
    ``anim.keyframe_insert_by_name``, whose poll fails outside a VIEW_3D area.
    Called from a panel button the context is already right, but from a script,
    a timer, or a headless run it is not, and the bake dies partway through with
    "context is incorrect" — after having already created some keyframes.
    Overriding onto a real viewport area makes the bake work from anywhere.
    """
    if bpy is None:
        yield
        return
    win = getattr(bpy.context, "window", None)
    screen = getattr(win, "screen", None) if win else None
    area = next((a for a in screen.areas if a.type == "VIEW_3D"), None) if screen else None
    region = next((r for r in area.regions if r.type == "WINDOW"), None) if area else None

    if area is None or region is None:
        # Headless / no viewport: run unmodified and let the caller handle it.
        yield
        return
    with bpy.context.temp_override(window=win, area=area, region=region):
        yield


@contextmanager
def _frozen_handlers():
    """Detach every frame-change handler for the duration of a bake.

    Baking with the BeamNG vertex-playback handler live corrupts the caches —
    the previous attempt at this feature produced point caches misaligned by
    ~300 frames and had to be re-baked by hand.  Restores the exact handler
    list on the way out, including on exception.
    """
    if bpy is None:
        yield
        return
    pre = list(bpy.app.handlers.frame_change_pre)
    post = list(bpy.app.handlers.frame_change_post)
    bpy.app.handlers.frame_change_pre.clear()
    bpy.app.handlers.frame_change_post.clear()
    try:
        yield
    finally:
        bpy.app.handlers.frame_change_pre.clear()
        bpy.app.handlers.frame_change_post.clear()
        for h in pre:
            bpy.app.handlers.frame_change_pre.append(h)
        for h in post:
            bpy.app.handlers.frame_change_post.append(h)


# ---------------------------------------------------------------------------
# Rigid body world
# ---------------------------------------------------------------------------


def _ensure_rigidbody_world(scene, frame_start: int, frame_end: int) -> None:
    """Create/point the rigid body world at a collection we control."""
    if scene.rigidbody_world is None:
        bpy.ops.rigidbody.world_add()

    rbw = scene.rigidbody_world
    rb_coll = bpy.data.collections.get("RigidBodyWorld")
    if rb_coll is None:
        rb_coll = bpy.data.collections.new("RigidBodyWorld")
    rbw.collection = rb_coll

    if rbw.constraints is None:
        con = bpy.data.collections.get("RigidBodyConstraints")
        if con is None:
            con = bpy.data.collections.new("RigidBodyConstraints")
        rbw.constraints = con

    rbw.enabled = True
    # Final-quality settings matching Simply Shatter's "Final Physics" mode.
    # 30 substeps / 60 iterations gives clean collision for CONVEX_HULL debris.
    rbw.substeps_per_frame = 30
    rbw.solver_iterations = 60
    rbw.point_cache.frame_start = frame_start
    rbw.point_cache.frame_end = frame_end

    scene.use_gravity = True
    scene.gravity = (0.0, 0.0, -9.81)


# ---------------------------------------------------------------------------
# Ground colliders
# ---------------------------------------------------------------------------


def _ensure_ground(settings: DebrisSettings) -> "bpy.types.Object":
    """A flat passive collision plane for debris to land on.

    Always created — both hero rigid bodies and fine NEWTON particles need a
    ground to collide with.  Particles use COLLISION modifiers (sphere-based),
    rigid bodies use PASSIVE rigid body (mesh-based).  Both are on this one
    object.

    Modeled after Simply Shatter's approach: a simple zero-thickness Plane
    with CONVEX_HULL shape and a small margin.  The Plane sits at
    ``ground_z`` with its face normal pointing up (+Z).
    """
    ground = bpy.data.objects.get(GROUND_NAME)
    if ground is None:
        mesh = bpy.data.meshes.new(GROUND_NAME)
        ground = bpy.data.objects.new(GROUND_NAME, mesh)
        _get_collection(DEBRIS_COLLECTION).objects.link(ground)

    size = 400.0
    mesh = ground.data
    mesh.clear_geometry()
    verts = [(-size, -size, 0.0), (size, -size, 0.0),
             (size, size, 0.0), (-size, size, 0.0)]
    faces = [(0, 1, 2, 3)]
    mesh.from_pydata(verts, [], faces)
    mesh.update()
    ground.location = (0.0, 0.0, settings.ground_z)
    ground.rotation_euler = (0.0, 0.0, 0.0)
    ground.scale = (1.0, 1.0, 1.0)
    ground.hide_render = True
    ground.display_type = "WIRE"

    # COLLISION modifier for NEWTON particles.  Particles collide as spheres
    # (radius = particle_size) against this surface.  thickness_outer creates
    # a detection zone ABOVE the mesh so particles are deflected before they
    # reach the surface — critical for preventing sphere-centre penetration.
    if not any(m.type == "COLLISION" for m in ground.modifiers):
        ground.modifiers.new(name="Collision", type="COLLISION")
    if getattr(ground, "collision", None) is not None:
        col = ground.collision
        bounciness = float(np.clip(settings.bounciness, 0.0, 1.0))
        col.damping_factor = 1.0 - 0.85 * bounciness
        col.damping_random = 0.15 * bounciness
        col.friction_factor = float(np.clip(
            settings.friction + 0.6 * (1.0 - bounciness), 0.0, 5.0))
        col.permeability = 0.0
        col.damping = 0.6
        # thickness_outer pushes the collision detection zone UP so the
        # particle sphere (radius = particle_size) stops well above the mesh
        # surface.  The instanced shard extends ~particle_size below the
        # sphere centre, so thickness_outer must be >= particle_size to keep
        # every shard above ground.  0.05 m is the calibrated minimum for the
        # largest shards (~5 cm radius).
        col.thickness_outer = 0.05
        col.thickness_inner = 0.001
    return ground


def _ensure_particle_ground(settings: DebrisSettings, radius: float,
                            drop: float, suffix: str = "",
                            ) -> Optional["bpy.types.Object"]:
    """Deflector for the particles, sunk so their SHARDS land on ``ground_z``.

    Colliding on a radius stops chips sinking, but it introduces the opposite
    error: the solver rests the particle's SPHERE on the plane, while the flat
    shard drawn inside that sphere extends less than a radius downward, so the
    piece floats by the difference (measured ~20 mm — visible, and the contact
    shadow still detaches).  The radius cannot simply be shrunk to fix it: it is
    also the anti-tunnelling buffer, and a radius smaller than the shard's own
    extent puts the piece back under the ground.

    The two jobs come apart by moving the PLANE instead of the radius.  This
    deflector sits ``drop`` below the visible ground, where ``drop`` is the
    systematic hover measured from the geometry, so a chip resting on it lands
    its shard on ``ground_z``.  It cannot be the same object as the rigid-body
    ground — the hero shards must keep colliding with the true surface, and they
    are baked and ground-snapped separately.

    ONE PLANE PER MATERIAL.  ``drop`` is derived from the shard size, and the
    libraries span a 10x range (glass 12-55 mm, paint 30-130 mm), so a single
    shared plane would be right for one material and wrong for the rest.  Blender
    deflects a particle against EVERY collider in the scene, so these cannot
    simply be stacked at different heights — a glass chip would stop on the
    paint plane if that one happened to be higher.  Each is therefore restricted
    to its own emitter's particles via the collision collection set on the
    particle system.

    ``hide_render``/``hide_viewport`` are both left on: a COLLISION modifier
    keeps deflecting while the object is hidden from the render, and this slab
    is a physics proxy that must never appear in a shot.
    """
    if bpy is None or radius <= 0.0:
        return None
    name = f"{PARTICLE_GROUND_NAME}{suffix}"
    ground = bpy.data.objects.get(name)
    if ground is None:
        mesh = bpy.data.meshes.new(name)
        size = 400.0
        mesh.from_pydata([(-size, -size, 0.0), (size, -size, 0.0),
                          (size, size, 0.0), (-size, size, 0.0)],
                         [], [(0, 1, 2, 3)])
        mesh.update()
        ground = bpy.data.objects.new(name, mesh)
        _get_collection(DEBRIS_COLLECTION).objects.link(ground)
    ground.location = (0.0, 0.0, settings.ground_z - drop)
    ground.hide_render = True
    ground.display_type = "WIRE"

    if not any(m.type == "COLLISION" for m in ground.modifiers):
        ground.modifiers.new(name="Collision", type="COLLISION")
    col = getattr(ground, "collision", None)
    if col is not None:
        bounciness = float(np.clip(settings.bounciness, 0.0, 1.0))
        col.damping_factor = 1.0 - 0.85 * bounciness
        col.damping_random = 0.15 * bounciness
        col.friction_factor = float(np.clip(
            settings.friction + 0.6 * (1.0 - bounciness), 0.0, 5.0))
        col.permeability = 0.0
        col.damping = 0.6
        # The radius now supplies the standoff that the thick skin used to, so
        # the skin drops to a thin safety margin.  See _ensure_particle_ground,
        # which builds the per-material deflector that replaces this one.
        col.thickness_outer = max(0.001, min(0.02, radius * 0.25))
        col.thickness_inner = max(0.001, col.thickness_outer * 0.5)
    return ground


def _disable_ground_particle_collision() -> None:
    """Stop the MAIN ground deflecting particles, once the sunk one exists.

    The two deflectors are at different heights, and a falling chip stops at
    whichever it meets first — which is the higher, main one.  Leaving both
    active silently defeats the sunk plane: the particles rest a radius above
    ``ground_z`` exactly as they did before, and the offset looks like it had no
    effect at all.

    Only the COLLISION modifier is removed.  The main ground keeps its PASSIVE
    rigid body, which is what the hero shards and glass fragments collide with —
    those are baked and ground-snapped, and must land on the true surface.
    """
    ground = bpy.data.objects.get(GROUND_NAME) if bpy is not None else None
    if ground is None:
        return
    for mod in list(ground.modifiers):
        if mod.type == "COLLISION":
            ground.modifiers.remove(mod)


def link_ground_to_rigidbody_world(ground: "bpy.types.Object",
                                   rb_coll: "bpy.types.Collection",
                                   settings: DebrisSettings) -> None:
    """Make the ground plane a PASSIVE rigid body in the simulation world.

    This link is what stops the debris falling out of the world.  It has to
    happen AFTER :func:`_ensure_rigidbody_world`.
    """
    if ground.name not in rb_coll.objects:
        rb_coll.objects.link(ground)
    if ground.rigid_body is None:
        with _viewport_context():
            bpy.context.view_layer.objects.active = ground
            bpy.ops.rigidbody.object_add(type="PASSIVE")
    if ground.rigid_body is None:
        return
    _configure_ground_rigidbody(ground, settings)


def _configure_ground_rigidbody(ground: "bpy.types.Object",
                                settings: DebrisSettings) -> None:
    """Apply the ground's rigid-body collider configuration.

    Uses CONVEX_HULL with a small margin, matching the Simply Shatter
    approach.  The margin creates an invisible buffer that prevents
    penetration, while CONVEX_HULL is fast for a flat quad.
    """
    rb = ground.rigid_body
    rb.type = "PASSIVE"
    rb.collision_shape = "CONVEX_HULL"
    rb.use_margin = True
    rb.collision_margin = 0.01
    rb.friction = settings.friction
    rb.restitution = _bounce_params(settings.bounciness)[0]


def _ground_world_bounds(ground: "bpy.types.Object") -> Optional[Tuple[float, float]]:
    """World-space ``(min_z, max_z)`` of the ground slab's actual mesh vertices."""
    mesh = getattr(ground, "data", None)
    verts = getattr(mesh, "vertices", None) if mesh is not None else None
    if verts is None or not len(verts):
        return None
    co = np.empty(len(verts) * 3, dtype=np.float64)
    verts.foreach_get("co", co)
    local = co.reshape(-1, 3)
    mw = np.asarray(ground.matrix_world, dtype=np.float64)
    world = np.hstack([local, np.ones((len(local), 1))]) @ mw.T
    return float(world[:, 2].min()), float(world[:, 2].max())


def _verify_ground_alignment(ground: "bpy.types.Object",
                             settings: DebrisSettings,
                             eps: float = 1e-4) -> bool:
    """Prove the visible mesh and the BOX collider share one world surface.

    Bullet's BOX shape is centred on the object origin with half-extents of
    ``object.dimensions / 2``, so with the canonical construction both the
    mesh's world Z extent and the box's world Z extent must be exactly
    ``[ground_z - thickness, ground_z]``.  Returns True when aligned and raises
    ``RuntimeError`` when not — this is the tripwire that catches any future
    drift between the visible ground and the physical collision surface.
    """
    mesh_bounds = _ground_world_bounds(ground)
    if mesh_bounds is None:
        return False
    mesh_min, mesh_max = mesh_bounds
    half = ground.dimensions.z / 2.0
    box_min = ground.location.z - half
    box_max = ground.location.z + half
    top = settings.ground_z
    bottom = settings.ground_z - ground.dimensions.z
    aligned = (abs(mesh_min - bottom) <= eps and abs(mesh_max - top) <= eps
               and abs(box_min - bottom) <= eps and abs(box_max - top) <= eps)
    if not aligned:
        raise RuntimeError(
            f"ground collider misaligned with the visible mesh: "
            f"mesh world Z [{mesh_min:.4f}, {mesh_max:.4f}], "
            f"BOX world Z [{box_min:.4f}, {box_max:.4f}], "
            f"expected [{bottom:.4f}, {top:.4f}]")
    return True


# ---------------------------------------------------------------------------
# Rigid body configuration
# ---------------------------------------------------------------------------


def configure_rigidbody(obj: "bpy.types.Object",
                        settings: DebrisSettings,
                        mass: float,
                        is_blast: bool,
                        launch_start: int = 0) -> None:
    """Configure a hero debris piece as a Bullet rigid body.

    Handles the kinematic-launch keys, the body type, collision shape, mass,
    restitution/damping (via :func:`_bounce_params`), friction, collision
    margin and deactivation thresholds.  ``mass`` is the body mass in kg as
    computed from the material profile — the caller owns the profile lookup.

    ``is_blast`` selects HOW the body is released: with motion to transfer the
    piece is kinematic for :data:`LAUNCH_FRAMES` and keyframed, then released
    ACTIVE so the solver adopts the travel velocity.  Without a launch the body
    is ACTIVE from the start and simply rests where it was placed.
    """
    rb = obj.rigid_body
    if rb is None:
        return

    if is_blast:
        # Kinematic keyframes ride on the RIGID BODY, so they have to be
        # inserted here, after the body exists.  The registration itself
        # happens on the next evaluation, which reads the phase-1 world
        # matrix — not origin.
        for k in range(LAUNCH_FRAMES + 1):
            rb.kinematic = k < LAUNCH_FRAMES
            rb.keyframe_insert("kinematic", frame=launch_start + k)
        rb.type = "ACTIVE"
    else:
        rb.type = "ACTIVE"
    # Convex hull is both cheaper and more stable than mesh collision for
    # small chunky solids, and our shards are convex by construction.
    rb.collision_shape = "CONVEX_HULL"
    rb.mass = max(0.004, mass)
    # One dial drives restitution AND damping — see _bounce_params for why
    # restitution alone cannot deliver "0 = no bounce at all".
    rest, lin_damp, ang_damp = _bounce_params(settings.bounciness)
    rb.restitution = rest
    rb.friction = settings.friction
    rb.linear_damping = lin_damp
    rb.angular_damping = ang_damp
    rb.use_margin = True
    rb.collision_margin = 0.002
    # Let a settled piece fall asleep instead of jittering on the ground
    # forever; a sleeping body is also what makes the bake's tail cheap.
    rb.use_deactivation = True
    rb.use_start_deactivated = False
    rb.deactivate_linear_velocity = 0.06 + 0.14 * (
        1.0 - float(np.clip(settings.bounciness, 0.0, 1.0)))
    rb.deactivate_angular_velocity = 0.12 + 0.28 * (
        1.0 - float(np.clip(settings.bounciness, 0.0, 1.0)))


def configure_glass_rigidbody(obj: "bpy.types.Object",
                              settings: DebrisSettings,
                              launch_start: int,
                              mass: float) -> None:
    """Configure a glass fragment as a Bullet rigid body.

    Glass always launches: fragments are keyframed kinematically for
    :data:`LAUNCH_FRAMES` and then handed to Bullet, which integrates the real
    trajectory until :func:`bake_debris` freezes it.  The material profile
    lookup is the caller's (``mass`` is passed in).
    """
    rb = obj.rigid_body
    if rb is None:
        return
    for k in range(LAUNCH_FRAMES + 1):
        rb.kinematic = k < LAUNCH_FRAMES
        rb.keyframe_insert("kinematic", frame=launch_start + k)
    rb.type = "ACTIVE"
    rb.collision_shape = "CONVEX_HULL"
    rest, lin_damp, ang_damp = _bounce_params(settings.bounciness)
    rb.mass = max(0.001, mass)
    rb.restitution = rest
    rb.friction = settings.friction
    rb.linear_damping = min(0.99, lin_damp + 0.08)
    rb.angular_damping = min(0.99, ang_damp + 0.04)
    rb.use_margin = True
    rb.collision_margin = 0.002
    rb.use_deactivation = True
    rb.use_start_deactivated = False
    rb.deactivate_linear_velocity = 0.05 + 0.12 * (
        1.0 - float(np.clip(settings.bounciness, 0.0, 1.0)))
    rb.deactivate_angular_velocity = 0.10 + 0.24 * (
        1.0 - float(np.clip(settings.bounciness, 0.0, 1.0)))


# ---------------------------------------------------------------------------
# Particle collision geometry (radius / scaled templates / sunk deflectors)
# ---------------------------------------------------------------------------

#: Percentile of the shards' DOWNWARD extent used as the particle collision
#: radius.  See :func:`_shard_radius` for why it is not the bounding radius and
#: not the median.
_SHARD_RADIUS_PCT = 70.0

#: Percentile of the extents used to compute the deflector ``drop`` — the
#: systematic hover the radius leaves, which the sunk plane cancels.  See the
#: comment block inside :func:`_ensure_particle_templates`.
_SHARD_DROP_PCT = 65.0

#: Random orientations sampled per template when measuring that extent.  A
#: particle lands at an arbitrary angle, so the extent has to be averaged over
#: orientations rather than read off the template's rest pose.
_RADIUS_ORIENTATIONS = 24


def _shard_extents(objs: Sequence["bpy.types.Object"],
                   seed: int = 0) -> np.ndarray:
    """How far each template extends BELOW its own centre, over random landings.

    A particle comes to rest at an arbitrary orientation, so the quantity that
    decides whether the shard drawn around it looks buried or floating is this
    per-orientation downward extent — not anything measurable from the
    template's rest pose.  Returned as a flat array over (template x
    orientation) so callers can take whatever statistic they need.
    """
    rng = np.random.default_rng(seed)
    out: List[float] = []
    for obj in objs:
        co = _local_verts(obj)
        if co is None:
            continue
        # Random rotations via normalised quaternions (uniform on SO(3)).
        q = rng.normal(size=(_RADIUS_ORIENTATIONS, 4))
        q /= np.linalg.norm(q, axis=1, keepdims=True)
        w, x, y, z = q[:, 0], q[:, 1], q[:, 2], q[:, 3]
        # Only the matrix's Z row matters — it is what projects onto the ground
        # normal — so build that directly instead of the full basis.
        zrow = np.stack([2 * (x * z - y * w),
                         2 * (y * z + x * w),
                         1 - 2 * (x * x + y * y)], axis=1)   # (K, 3)
        out.extend((-(co @ zrow.T).min(axis=0)).tolist())
    return np.asarray(out, dtype=np.float64)


def _shard_radius(objs: Sequence["bpy.types.Object"],
                  seed: int = 0) -> float:
    """Collision radius that lands a tumbling shard closest to flat on the plane.

    A NEWTON particle collides as a SPHERE, but a shard is a flat, jagged plate
    — so no single radius is right for every landing.  What the radius should
    approximate is how far the piece extends BELOW ITS OWN CENTRE once it comes
    to rest at whatever angle it happened to land at, which is measured here by
    sampling random orientations of each template.

    Why not the bounding radius (origin to farthest vertex).  That is the
    distance to a CORNER, and a flat plate resting on its face extends only a
    fraction of that downward, so the sphere holds the piece up in the air by
    the difference: measured 16-21 mm of median hover across the materials, and
    up to 46 mm at p90.  Trading "half sunk" for "visibly floating" is not a
    fix — the shadow still detaches.

    Why not the median extent either.  It centres the error on zero, but the
    distribution is skewed: half the pieces then sink, by up to 23-30 mm at p90.
    A shard poking through the ground reads as a bug, while the same distance of
    hover reads merely as a small piece resting on a chip below it, so the
    percentile is pushed above the median to buy sink-resistance cheaply.  At
    p70 the worst-case sink drops to 13-21 mm and hover stays under 23 mm —
    both roughly HALF the current point-collision burial (17-27 mm median,
    40-57 mm at p90), which is what makes this worth doing at all.
    """
    extents = _shard_extents(objs, seed=seed)
    if not len(extents):
        return 0.02
    return float(np.percentile(extents, _SHARD_RADIUS_PCT))


def _ensure_particle_templates(
        settings: DebrisSettings,
        key: str = "",
        templates: Optional[Sequence["bpy.types.Object"]] = None,
        cache: Optional[Dict[str, Tuple]] = None,
) -> Tuple[Optional["bpy.types.Collection"], float, float]:
    """Shard templates pre-scaled so particles can collide with a real radius.

    THE HALF-SUBMERGED PARTICLE BUG.  ``ParticleSettings.particle_size`` is
    ONE number doing TWO jobs: it is the render scale applied to the instanced
    object AND (with ``use_size_deflect``) the collision radius.  The shard
    templates are modelled at their true size, so the render job pins it at
    ~1.0 — and a 1 m collision radius is absurd, which is why the previous fix
    attempt (just switching ``use_size_deflect`` on) left chips resting at
    z = 0.35-0.75, hovering half a metre up.  Turning it back OFF collides the
    particle as a POINT, so the solver rests the shard's CENTRE on the ground
    and the instance drawn around that centre is exactly half buried — which is
    precisely the "not a quarter, not too little, exactly half submerged" in the
    reference screenshot.

    The two jobs are separable by changing the templates instead of the number.
    A copy of each template scaled UP by ``1/r`` renders at true size when
    ``particle_size = r``::

        rendered = template_scale x particle_size = (size/r) x r = size

    while the collision radius becomes ``r`` — the shard's actual radius — so a
    settled chip rests TOUCHING the plane.  The copies must live in their own
    collection: the hero pieces instance the unscaled originals and would be
    inflated by ``1/r`` (a 50x blow-up) if this reused SHARD_COLLECTION.

    PER (part, material), not once globally.  The libraries span a 10x size
    range — glass shards are 12-55 mm, paint flakes 30-130 mm — and one radius
    across all of them fits none: it buries the glass and floats the paint.  The
    emitters are already per-material, so each gets templates scaled to its own
    material's radius.  ``cache`` memoises by ``key`` so the work is done once
    per library rather than once per impact.

    Returns ``(collection, radius, drop)``.  ``drop`` is the residual hover the
    radius leaves — the solver rests the SPHERE on the plane while the flatter
    shard inside reaches less far down — which :func:`_ensure_particle_ground`
    cancels by sinking the particles' deflector.  The collection is ``None``
    when there are no templates to scale, in which case the caller keeps point
    collision.
    """
    if cache is not None and key in cache:
        return cache[key]

    src = list(templates) if templates else None
    if not src:
        coll_src = bpy.data.collections.get(SHARD_COLLECTION)
        src = list(coll_src.objects) if coll_src is not None else []
    if not src:
        return None, 0.0, 0.0

    scale = max(1e-6, float(settings.scale))
    extents = _shard_extents(src, seed=settings.seed) * scale
    if not len(extents):
        return None, 0.0, 0.0
    radius = float(np.percentile(extents, _SHARD_RADIUS_PCT))
    if radius <= 1e-6:
        return None, 0.0, 0.0
    # THE REST HEIGHT IS EXACTLY ``particle_size``.  Measured in Blender 4.5:
    # a settled particle's CENTRE comes to rest at radius + 0.1 mm, and that is
    # invariant under both the deflector's ``thickness_outer`` (tried 1-20 mm:
    # rest height did not move) and the particle's own orientation — the solver
    # deflects a SPHERE and neither the skin thickness nor the instanced mesh
    # enters the calculation.
    #
    # So the hover is not a statistical effect to be minimised, it is a constant
    # to be cancelled: the centre sits at ``radius`` while the shard drawn
    # around it reaches ``median(extents)`` below that centre.  Sinking the
    # deflector by the difference puts the typical shard's lowest point exactly
    # on ``ground_z``.  Only the spread of extents remains, which is what
    # _SHARD_RADIUS_PCT trades off.
    #
    # The percentile trades the two directions off.  Measured sink/hover at p90
    # of the extents (mm, glass / paint / steel):
    #
    #     p50   13.7/7.4   18.9/16.5   13.3/11.4
    #     p65   10.2/11.0  13.0/22.4    9.4/15.4   <- balanced
    #     p80    5.8/15.4   5.8/29.5    4.7/20.0
    #
    # p65 sits where the two are about equal for glass — the material with the
    # most pieces on screen — and it keeps the worst-case sink well under the
    # 16-30 mm the piece is buried by today.  Biased slightly ABOVE the median
    # because a shard poking through the ground reads as a bug while the same
    # distance of hover reads as the piece resting on a chip underneath it.
    drop = max(0.0, radius - float(np.percentile(extents, _SHARD_DROP_PCT)))

    # One collection per library, since each is scaled by its own 1/r.
    coll_name = (f"{PARTICLE_SHARD_COLLECTION} {key}" if key
                 else PARTICLE_SHARD_COLLECTION)
    coll = bpy.data.collections.get(coll_name)
    if coll is None:
        coll = bpy.data.collections.new(coll_name)
    else:
        for obj in list(coll.objects):
            coll.objects.unlink(obj)
            if obj.users == 0:
                bpy.data.objects.remove(obj)

    # The user's Shard Scale is folded in here rather than left to
    # ``particle_size``, which is already carrying the collision radius.
    inv = scale / radius
    for tpl in src:
        dup = tpl.copy()          # share mesh data; only the scale differs
        dup.data = tpl.data
        dup.name = f"{tpl.name}_pcoll"
        dup.scale = (inv, inv, inv)
        coll.objects.link(dup)

    # Never linked into the scene: an instance collection only has to exist in
    # bpy.data, and linking it would drop a pile of 50x shards at the origin.
    # (Contrast _detach_template_collection, which has to UNLINK the originals —
    # they are linked so build_shard_library can evaluate them.)
    out = (coll, radius, drop)
    if cache is not None:
        cache[key] = out
    return out


# ---------------------------------------------------------------------------
# Particle physics configuration
# ---------------------------------------------------------------------------


def configure_particle_physics(
        st, event: ImpactEvent, settings: DebrisSettings,
        particle_templates: Optional[Tuple[Optional["bpy.types.Collection"],
                                            float, float]],
        mass: float) -> Optional["bpy.types.Collection"]:
    """Configure the NEWTON solver and collision physics for one emitter.

    Sets the particle system's physics type, mass, collision radius (from the
    pre-scaled templates), damping (air drag), solver subframes, radius
    deflection, gravity and dynamic rotation.  Also wires the per-material sunk
    deflector as the system's ``collision_collection``.

    Returns the pre-scaled template collection (``None`` when point collision
    is used), which the caller needs for the render side of the emitter.
    """
    pcoll, pradius, pdrop = (particle_templates if particle_templates is not None
                             else _ensure_particle_templates(settings))

    st.physics_type = "NEWTON"
    st.mass = max(0.002, mass)

    # SIZE.  ``particle_size`` is both the render scale for instanced objects
    # AND the collision radius.  With the scaled collection in play
    # ``particle_size`` becomes the shard's real radius (~1-3 cm) and the
    # instances still render at true size; without it this falls back to the
    # old true-size/point-collision pairing.
    st.particle_size = pradius if pcoll is not None else 1.0 * settings.scale
    # SIZE VARIATION scales the collision radius per particle as well as the
    # render size. The sunk deflector is calibrated for ONE radius, so a spread
    # re-scatters rest heights slightly during the live simulation — accepted
    # for the visual variety of a statistical spray; the particles stay live.
    st.size_random = 0.7 if pcoll is not None else 0.85
    # Mass follows size, so big fragments carry momentum and small chips are
    # stopped by drag — without this every piece decelerates identically.
    st.use_multiply_size_mass = True

    # THE MAIN GROUND is the single collision target for all particles.
    # Simply Shatter uses one ground plane for everything — particles and rigid
    # bodies alike.  Per-material sunk deflectors were an over-engineering that
    # created 49+ ground objects; a single ground with COLLISION modifier works
    # well when use_size_deflect and subframes handle the contact properly.
    ground_coll = _get_collection(DEBRIS_COLLECTION)
    st.collision_collection = ground_coll

    # AIR DRAG.  The single biggest cue that debris is light: it sheds speed
    # fast, so the spray loses its shape instead of holding a clean parabola.
    # At low bounciness the drag is raised too, so a chip that has landed loses
    # its remaining speed instead of sliding away across the road.
    #
    # SCALING GOTCHA.  The original formula ``air_drag + 0.45*(1-bounciness)``
    # fed a 0..1 dial into Blender's ``damping``, which is a *velocity decay
    # factor*, not a "percent drag" dial.  With the defaults (0.35 + 0.45*0.75)
    # that produced an effective damping of ~0.69, capping terminal velocity at
    # a measured ~0.24 m/s — every chip fell like a snowflake (measured 1.6 m
    # of fall in 1.8 s at 60 fps; damping 0.1 covers 14.8 m in the same time).
    # The dial is now remapped onto a sane 0..0.3 band so debris still sheds
    # speed a little (light chips land short of heavy ones) while falling at
    # near-real gravity.
    bounciness = float(np.clip(settings.bounciness, 0.0, 1.0))
    st.damping = float(np.clip(settings.air_drag * 0.3 + 0.06 * (1.0 - bounciness),
                               0.0, 0.3))
    # Sub-stepping the solver: at 10-25 m/s a particle covers up to 0.4 m per
    # frame, several times its own size, and would tunnel through the ground.
    st.subframes = int(max(0, settings.particle_subframes))
    # RADIUS collision, now that ``particle_size`` carries the shard's true
    # radius instead of its render scale.  Point collision (the previous
    # behaviour, kept as the fallback) rests the shard's CENTRE on the ground,
    # so the instance drawn around it is exactly half buried — the reported
    # bug.  Deflecting on the radius rests the shard's SURFACE on the ground.
    st.use_size_deflect = pcoll is not None

    st.effector_weights.gravity = 1.0
    st.use_dynamic_rotation = True

    return pcoll