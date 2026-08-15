from __future__ import annotations

"""Spawn and simulate impact debris in the Blender scene.

Hybrid simulation, because neither approach alone is good enough:

**Hero debris** — a few dozen larger pieces per impact — are real rigid bodies.
They tumble, bounce, collide with each other and come to rest in a pile.  That
settling behaviour is what sells a crash, and particles cannot fake it.

**Fine debris** — the hundreds of small chips — are a particle system.  At that
size the eye reads the spray, not the individual piece, so the cheaper solver is
indistinguishable and keeps the scene tractable.  The particles are then BAKED
into per-chip F-curve meshes by :func:`bake_particles` (with every chip
ground-clamped, since a solver-rested sphere cannot guarantee shards stay off
the ground), so the finished debris is pure keyframe animation either way.

THE BAKE-CORRUPTION RULE
------------------------
Both solvers are driven by the scene timeline, and this add-on has a
``frame_change_pre`` handler that rewrites ~550K vertices on every frame.
Baking while that handler is live corrupts the caches — the previous attempt at
this feature produced point caches misaligned by ~300 frames and had to be
re-baked by hand.  :func:`_frozen_handlers` detaches every frame handler for the
duration of a bake and restores them afterwards, so a bake can never race the
vertex playback.

After the rigid body bake, the debris is pure keyframe F-curves and the rigid
body world is torn down.  From then on the debris is inert data: it cannot be
re-simulated, cannot drift, and cannot be corrupted by the playback handler.
"""

from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

from .debris_shards import (
    FRACTURE_PROFILES,
    build_shard_library,
    profile_for,
    triangulate_indices,
)
from .glass_shatter import (
    fragment_count_for,
    shatter_pane,
)
from .impact_detect import (
    GLASS_CRACKED,
    GLASS_SHATTERED,
    MATERIAL_DEBRIS_BIAS,
    GlassSettings,
    ImpactEvent,
    local_to_world,
    resolve_glass_damage,
)

try:  # pragma: no cover - only inside Blender
    import bpy
    import mathutils
except ImportError:  # pragma: no cover
    bpy = None
    mathutils = None

import zlib


def _stable_hash(text: str) -> int:
    """Deterministic cross-session string hash.

    Python's built-in ``hash()`` is randomized per process (PYTHONHASHSEED),
    so a seed derived from it rebuilds a *different* scene every Blender
    session.  crc32 is stable everywhere, which is what "a given scene always
    rebuilds identically" actually requires.
    """
    return zlib.crc32(text.encode("utf-8", "surrogatepass"))


DEBRIS_COLLECTION = "BeamNG Debris"
SHARD_COLLECTION = "BeamNG Debris Shards"
GROUND_NAME = "BeamNG_DebrisGround"

#: Frames a hero piece travels kinematically before the solver takes over.
#: This is how launch velocity is transferred — see :func:`_spawn_hero_pieces`.
#: Fewer than ~3 transfers no measurable velocity.
LAUNCH_FRAMES = 3

#: Clearance kept between a body's LOWEST POINT and the ground collider.
#: Not between its origin and the ground — see :func:`_lowest_point_offset`.
GROUND_CLEARANCE = 0.004

#: Custom property holding the frame a hero piece becomes visible.  The bake
#: clears each body's animation data, so the visibility keys written at spawn
#: have to be re-derivable afterwards — see :func:`_key_visibility`.
LAUNCH_PROP = "_beamng_debris_launch"


def _lowest_point_offset(template: "bpy.types.Object",
                         rotation_euler: Sequence[float],
                         scale: float) -> float:
    """How far the template's lowest vertex sits BELOW its object origin.

    Returns a value <= 0.  Shards are centred on their origin at build time, so
    a shard whose origin is clamped to the ground plane is buried up to half its
    own depth — templates measure up to 0.138 m in radius, and at scale 1.4 that
    is nearly 0.2 m of the body starting *inside* the collision slab.  Bullet
    resolves that much initial penetration by ejecting the body along whichever
    face is nearest, which for a deeply buried shard is frequently downward: it
    pops out under the slab and free-falls forever (measured: 21 of 200 hero
    pieces ended between z=-868 and z=-912, and 19 of those 21 were penetrating
    the ground on their launch frame).

    Measuring the real rotated extent rather than a bounding sphere keeps flat
    shards — which are most of them — lying nearly ON the ground instead of
    hovering a radius above it.
    """
    mesh = getattr(template, "data", None)
    count = len(mesh.vertices) if mesh is not None else 0
    if not count:
        return 0.0
    co = np.empty(count * 3, dtype=np.float64)
    mesh.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3) * float(scale)
    basis = np.array(mathutils.Euler(tuple(rotation_euler)).to_matrix(),
                     dtype=np.float64)
    return float(min(0.0, (co @ basis.T)[:, 2].min()))


def _local_verts(obj: "bpy.types.Object",
                 _cache: Dict[str, np.ndarray] = {}) -> Optional[np.ndarray]:
    """Object-space vertices of ``obj`` as an ``(N, 3)`` float64 array.

    Cached by mesh name.  The bake calls this once per body per frame across
    hundreds of bodies and thousands of frames, and ``foreach_get`` on a fresh
    buffer every time is what turns the ground-snap pass from seconds into
    minutes.  Hero shards deliberately SHARE mesh data between instances (see
    :func:`_spawn_hero_pieces`), so the cache hit rate is high.
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


def _lowest_world_z(obj: "bpy.types.Object", mat) -> Optional[float]:
    """World Z of the body's LOWEST vertex under world matrix ``mat``.

    The spawn-time :func:`_lowest_point_offset` only understands an euler plus a
    uniform scale, which is all a freshly-placed shard has.  A BAKED body has an
    arbitrary rotation from the solver, so its lowest point has to be measured
    from the full 4x4 — the offset under the spawn rotation says nothing about
    the offset after the piece has tumbled.
    """
    co = _local_verts(obj)
    if co is None:
        return None
    m = np.array(mat, dtype=np.float64)  # 4x4, row-major
    # Only the Z row is needed: z = m[2,0]x + m[2,1]y + m[2,2]z + m[2,3]
    z = co @ m[2, :3] + m[2, 3]
    return float(z.min())


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
    #: plus the collision thickness in _ensure_ground keeps the spray above the
    #: plane (measured: the old value dropped a visible share of shards to
    #: z=-40).
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
# Handler safety
# ---------------------------------------------------------------------------


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

    See the module docstring: baking with the BeamNG vertex-playback handler
    live produces corrupt point caches.  Restores the exact handler list on the
    way out, including on exception.
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
# Scene helpers
# ---------------------------------------------------------------------------


def _get_collection(name: str, parent=None) -> "bpy.types.Collection":
    coll = bpy.data.collections.get(name)
    if coll is None:
        coll = bpy.data.collections.new(name)
        (parent or bpy.context.scene.collection).children.link(coll)
    return coll


#: Prefix of every material :func:`_apply_glass_crack` builds.  Used to find
#: them again at clear time — they sit in the CAR's material slots, so they
#: cannot be found by walking the debris collections.
CRACK_MATERIAL_PREFIX = "BeamNG Crack "


def _clear_crack_materials() -> int:
    """Strip crack materials off the car and delete them.

    Returns how many materials were removed.  Faces carrying a crack slot are
    reset to slot 0 (the pane's original glass), then the now-unused slot is
    left in place — removing a slot re-indexes every face above it, which would
    silently repaint unrelated panes in the same merged chunk.
    """
    if bpy is None:
        return 0
    cracks = [m for m in bpy.data.materials
              if m.name.startswith(CRACK_MATERIAL_PREFIX)]
    if not cracks:
        return 0
    crack_set = set(cracks)
    for mesh in bpy.data.meshes:
        slots = list(mesh.materials)
        if not slots or not crack_set.intersection(
                m for m in slots if m is not None):
            continue
        cracked_slots = {i for i, m in enumerate(slots) if m in crack_set}
        for poly in mesh.polygons:
            if poly.material_index in cracked_slots:
                poly.material_index = 0
        for i in cracked_slots:
            mesh.materials[i] = None
    for mat in cracks:
        bpy.data.materials.remove(mat)
    return len(cracks)


def clear_debris() -> int:
    """Remove every object this module created.  Returns how many were removed."""
    if bpy is None:
        return 0
    removed = 0
    for coll_name in (DEBRIS_COLLECTION, SHARD_COLLECTION,
                      PARTICLE_SHARD_COLLECTION, GLASS_COLLECTION,
                      PARTICLE_BAKED_COLLECTION):
        coll = bpy.data.collections.get(coll_name)
        if coll is None:
            continue
        for obj in list(coll.objects):
            bpy.data.objects.remove(obj, do_unlink=True)
            removed += 1
        bpy.data.collections.remove(coll)

    for name in (GROUND_NAME, PARTICLE_GROUND_NAME):
        ground = bpy.data.objects.get(name)
        if ground is not None:
            bpy.data.objects.remove(ground, do_unlink=True)
            removed += 1

    # Drop the rigid body world so a re-run starts from a clean solver.
    scene = bpy.context.scene
    if scene.rigidbody_world is not None:
        try:
            bpy.ops.rigidbody.world_remove()
        except Exception:
            scene.rigidbody_world = None

    for mesh in list(bpy.data.meshes):
        if mesh.users == 0 and mesh.name.startswith("shard_"):
            bpy.data.meshes.remove(mesh)

    # Un-paint cracked panes.  The crack lives on the CAR's own mesh (a slot on
    # the merged glass chunk), not on a debris object, so removing the debris
    # collections cannot reach it — without this a cracked pane stays cracked
    # after Clear Debris, and a rebuild with different thresholds paints a
    # second crack over the first.  Restores each affected face to slot 0, the
    # pane's original glass shader.
    removed += _clear_crack_materials()

    # Un-collapse the car's glass panes.  A shattered pane is collapsed out of
    # the car during playback (and that state is persisted on the scene), so
    # clearing the fragments must also clear the collapse map — otherwise the
    # car's glass stays gone even after Clear Debris.
    try:
        from runtime import frame_handler
        frame_handler.set_shattered_panes({})
    except Exception:
        pass
    return removed


def _ensure_ground(settings: DebrisSettings) -> "bpy.types.Object":
    """A large passive collision slab for the debris to land on.

    The collider has real thickness (not a zero-thickness plane): a shard
    released fast enough to skip across the top surface in one solver substep
    still lands *inside* the slab and is pushed back up.  With a plain quad the
    same shard tunnels straight through and falls out of the world (measured on
    real capture data: glass at z=-50, 20+ m of sideways drift).
    """
    ground = bpy.data.objects.get(GROUND_NAME)
    if ground is None:
        mesh = bpy.data.meshes.new(GROUND_NAME)
        size = 400.0
        thickness = 0.2
        verts = [(-size, -size, 0.0), (size, -size, 0.0),
                 (size, size, 0.0), (-size, size, 0.0),
                 (-size, -size, -thickness), (size, -size, -thickness),
                 (size, size, -thickness), (-size, size, -thickness)]
        faces = [(0, 1, 2, 3), (4, 5, 6, 7),
                 (0, 1, 5, 4), (1, 2, 6, 5),
                 (2, 3, 7, 6), (3, 0, 4, 7)]
        mesh.from_pydata(verts, [], faces)
        mesh.update()
        ground = bpy.data.objects.new(GROUND_NAME, mesh)
        _get_collection(DEBRIS_COLLECTION).objects.link(ground)
    ground.location = (0.0, 0.0, settings.ground_z)
    ground.hide_render = True
    ground.display_type = "WIRE"

    # NEWTON particles only collide with objects carrying a COLLISION modifier;
    # the rigid-body slab is invisible to them, so fine debris used to fall
    # straight through (measured: shards dropped under the ground).  The same
    # ground can be both a rigid body (hero pieces) and a COLLISION collider
    # (fine debris) — the two systems are independent.
    if not any(m.type == "COLLISION" for m in ground.modifiers):
        ground.modifiers.new(name="Collision", type="COLLISION")
    if getattr(ground, "collision", None) is not None:
        col = ground.collision
        col.friction_factor = settings.friction
        col.permeability = 0.0
        # PARTICLE bounce lives on ``damping_factor``, NOT on ``damping``.
        # ``damping`` is the soft-body/cloth field and has no effect whatsoever
        # on a NEWTON particle system, so the old value here was doing nothing
        # and every chip bounced with Blender's default elasticity — half of why
        # the fine debris pinballed instead of settling.  ``damping_factor`` is
        # inverted relative to restitution: 1.0 absorbs all the impact energy.
        bounciness = float(np.clip(settings.bounciness, 0.0, 1.0))
        col.damping_factor = 1.0 - 0.85 * bounciness
        col.damping_random = 0.15 * bounciness
        # Kill the tangential skate at low bounciness for the same reason the
        # rigid bodies get extra damping: restitution 0 stops the bounce but not
        # the slide, and a chip sliding across the road forever reads as wrong.
        col.friction_factor = float(np.clip(
            settings.friction + 0.6 * (1.0 - bounciness), 0.0, 5.0))
        col.damping = 0.6
        # A thin collision surface lets a fast particle cross the top face
        # inside one substep and fall out of the world — the COLLISION system,
        # unlike the rigid-body slab, has no thickness to catch it once past.
        # Thickening the outer zone deflects the particle while it is still
        # above the plane (measured: shards at z=-40 with the default 0.02).
        #
        # ``thickness_outer`` is an absolute distance in METRES, and it stacks on
        # top of the collision radius: a particle rests at ``radius + outer``
        # above the plane.  With radius collision enabled (see
        # :func:`_ensure_particle_templates`) 0.12 m of it would park every chip
        # a hand's width in the air — the exact hovering failure the radius
        # change exists to remove — so :func:`_disable_ground_particle_collision`
        # removes this modifier entirely once the sunk particle deflector exists.
        # The value here is the
        # point-collision default, which is what a build with no scaled
        # templates falls back to.
        ground.collision.thickness_outer = 0.12
        ground.collision.thickness_inner = 0.06
    return ground


#: Separate deflector for the fine particles, sunk below the visible ground.
#: See :func:`_ensure_particle_ground`.
PARTICLE_GROUND_NAME = "BeamNG_DebrisGround_Particles"


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
    particle system (see ``_spawn_fine_particles``).

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


#: Collection of shard templates pre-scaled for the PARTICLE system.  See
#: :func:`_ensure_particle_templates` for why this cannot be the same collection
#: the hero pieces instance from.
PARTICLE_SHARD_COLLECTION = "BeamNG Debris Shards (Particles)"


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


def _detach_template_collection(scene) -> None:
    """Hide the shard templates without breaking particle instancing.

    THIS IS A TRAP WORTH SPELLING OUT.  The templates must not be visible — 352
    shards stacked at the world origin — but the obvious ways to hide them all
    silently destroy the fine debris:

    ==========================  ==========  =========
    template flag               viewport    render
    ==========================  ==========  =========
    ``hide_viewport = True``    0 instances 0 pixels
    ``hide_render = True``      instances   0 pixels
    ``hide_set(True)`` (eye)    instances   pixels
    collection unlinked         instances   pixels
    ==========================  ==========  =========

    Measured with a 50-particle system rendering a single template: fully
    visible gives 136 lit pixels, ``hide_render = True`` gives 1.  Blender
    evaluates a particle system's instanced geometry from the *depsgraph*, and
    an object hidden from an evaluation is simply not there to instance — so
    the debris silently renders as nothing at all.  Because the emitters, the
    particle counts and the point caches all still look correct, this reads as
    "the particles are not showing up" with no obvious cause.

    Unlinking the collection from the scene keeps the objects alive in
    ``bpy.data`` — which is all ``instance_collection`` needs — while removing
    them from both the viewport and the render.
    """
    coll = bpy.data.collections.get(SHARD_COLLECTION)
    if coll is None:
        return
    for parent in list(bpy.data.collections) + [scene.collection]:
        if coll.name in parent.children:
            parent.children.unlink(coll)


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
    # Substeps/iterations well above default: shards are small and fast, and the
    # default 10 lets them tunnel straight through the ground plane.
    rbw.substeps_per_frame = 12
    rbw.solver_iterations = 24
    rbw.point_cache.frame_start = frame_start
    rbw.point_cache.frame_end = frame_end

    scene.use_gravity = True
    scene.gravity = (0.0, 0.0, -9.81)


# ---------------------------------------------------------------------------
# Sampling
# ---------------------------------------------------------------------------


def _cone_directions(axis: np.ndarray, half_angle_deg: float, n: int,
                     rng: np.random.Generator) -> np.ndarray:
    """``n`` unit vectors uniformly sampled in a cone about ``axis``."""
    axis = np.asarray(axis, dtype=np.float64)
    norm = np.linalg.norm(axis)
    axis = axis / norm if norm > 1e-9 else np.array((0.0, 0.0, 1.0))

    ref = np.array((0.0, 0.0, 1.0)) if abs(axis[2]) < 0.9 else np.array((1.0, 0.0, 0.0))
    u = np.cross(axis, ref)
    u /= max(np.linalg.norm(u), 1e-9)
    v = np.cross(axis, u)

    cos_max = np.cos(np.radians(max(1.0, min(179.0, half_angle_deg))))
    cos_t = rng.uniform(cos_max, 1.0, n)
    sin_t = np.sqrt(np.clip(1.0 - cos_t ** 2, 0.0, 1.0))
    phi = rng.uniform(0.0, 2.0 * np.pi, n)

    return (axis * cos_t[:, None]
            + u * (sin_t * np.cos(phi))[:, None]
            + v * (sin_t * np.sin(phi))[:, None])


def impact_intensity(event: ImpactEvent, settings: DebrisSettings) -> float:
    """How violent this impact is, on a continuous 0..1 scale.

    THE THRESHOLD SYSTEM.  Everything the debris does — how many pieces it
    sheds, how fast and how wide it throws them, how hard they tumble, how long
    they stay airborne — is driven by this one number, so a graze and a
    head-on smash produce visibly different events rather than the same canned
    burst at two sizes.

    The mapping is a ramp, not a switch.  ``min_blast_severity`` used to gate
    the launch as a hard boolean, which meant an impact just over the line fired
    with the full 0.35-floor speed while one just under it dropped straight
    down — a visible discontinuity across a threshold the user cannot see.  Here
    the severity is remapped so intensity reaches 0 exactly AT the threshold and
    climbs smoothly to 1 at maximum severity:

        intensity = ((severity - min_blast) / (1 - min_blast)) ** 1.35

    The exponent biases the curve down, so mid-strength impacts stay modest and
    only genuinely hard hits blast — a linear ramp made everything look big.
    Below the threshold the result is 0.0 and the caller drops the piece rather
    than throwing it.
    """
    lo = float(np.clip(settings.min_blast_severity, 0.0, 0.999))
    t = (float(event.severity) - lo) / max(1e-6, 1.0 - lo)
    return float(np.clip(t, 0.0, 1.0) ** 1.35)


def _counts_for(event: ImpactEvent, settings: DebrisSettings) -> Tuple[int, int]:
    """How many hero and fine pieces this event sheds.

    Count is driven by intensity rather than raw severity, so the amount of
    material shed tracks how hard the hit was.  ``intensity_gain`` blends
    between "every impact sheds the same" (0) and "count scales fully with
    intensity" (1); a floor keeps even a light touch shedding something, since
    an impact that registers at all did break some material.
    """
    bias = MATERIAL_DEBRIS_BIAS.get(event.material, 1.0)
    profile = profile_for(event.material)
    gain = float(np.clip(settings.intensity_gain, 0.0, 1.0))
    graded = (1.0 - gain) + gain * impact_intensity(event, settings)
    # Severity still contributes directly: a below-threshold event sheds a small
    # amount of material that simply falls rather than being thrown.
    strength = max(0.18 * event.severity, graded) * bias * profile.count_bias
    strength *= settings.density
    hero = int(round(settings.hero_count * strength))
    fine = int(round(settings.fine_count * strength))
    return max(0, hero), max(0, fine)


# ---------------------------------------------------------------------------
# Spawning
# ---------------------------------------------------------------------------


def _blender_frame_for(cache_frame: int, frame_start: int,
                       playback_fps: float, output_fps: float) -> int:
    """Inverse of frame_handler._cache_frame_for."""
    if playback_fps <= 0:
        return int(frame_start + cache_frame)
    return int(round(frame_start + cache_frame * (output_fps / playback_fps)))


def _spawn_hero_pieces(event: ImpactEvent, count: int,
                       templates: Sequence["bpy.types.Object"],
                       spawn_frame: int, settings: DebrisSettings,
                       coll: "bpy.types.Collection",
                       rng: np.random.Generator,
                       rb_coll: "bpy.types.Collection",
                       output_fps: float = 24.0) -> List["bpy.types.Object"]:
    """Instance ``count`` rigid-body shards at an impact."""
    if not templates or count <= 0:
        return []

    profile = profile_for(event.material)
    origin = np.array(event.position, dtype=np.float64)
    axis = np.array(event.direction, dtype=np.float64)
    dirs = _cone_directions(axis, settings.spread, count, rng)

    # event.velocity is already metres per second — impact_detect emits it in
    # scene time via sample_delta_to_ms.  Do NOT re-scale (the old `* 24.0`
    # plus a stride-2 scan launched debris ~2x too fast).
    part_vel = np.array(event.velocity, dtype=np.float64)

    # NO BLAST BY DEFAULT.  ``speed`` is 0 unless the user dials it up, so this
    # resolves to scatter + inherited momentum: the pieces separate from the
    # panel, drift apart slightly and fall.  The cone spray remains available
    # (``speed`` > 0) but it is no longer the default behaviour, because a radial
    # cone from a point is the firework.
    #
    # ``blast`` now only selects HOW the velocity is handed to the solver: with
    # motion to transfer we need the kinematic launch frames, without any we can
    # skip them entirely (which also skips the phantom-velocity failure mode).
    speed = settings.speed * (0.35 + 0.65 * event.severity) * profile.speed_bias
    if event.severity < settings.min_blast_severity:
        speed = 0.0
    # Scatter: a small, mostly-horizontal separation velocity so shed material
    # spreads over a patch instead of stacking in one column.  Deliberately
    # capped well below anything that reads as a throw.
    scatter = max(0.0, float(settings.scatter)) * (0.4 + 0.6 * event.severity)
    inherited = part_vel * settings.inherit_velocity
    spawned: List["bpy.types.Object"] = []
    # (object, launch_start, scale, is_blast) — placed into the scene in phase
    # 1, then registered as rigid bodies in phase 2 (see the note below).
    placed: List[Tuple["bpy.types.Object", int, float, bool]] = []

    for i in range(count):
        tpl = templates[int(rng.integers(0, len(templates)))]
        obj = tpl.copy()
        obj.data = tpl.data  # share mesh data; the transform makes it unique
        obj.name = f"debris_{event.material}_{spawn_frame}_{i:03d}"

        jitter = rng.normal(0.0, 0.045, 3)
        spawn_at = origin + jitter
        rotation = tuple(rng.uniform(0.0, 2.0 * np.pi, 3))
        obj.rotation_euler = rotation
        s = float(rng.uniform(0.7, 1.4)) * settings.scale
        obj.scale = (s, s, s)

        # Keep the shard's LOWEST POINT above the ground collider, not its
        # origin.  Shards are centred on their origin, so clamping the origin
        # buries half the piece inside the slab; Bullet then resolves the
        # penetration by ejecting it through the nearest face, often downward,
        # and the piece free-falls out of the world.
        floor = settings.ground_z + GROUND_CLEARANCE - _lowest_point_offset(
            tpl, rotation, s)
        if spawn_at[2] < floor:
            spawn_at[2] = floor

        coll.objects.link(obj)
        spawned.append(obj)

        # Separation velocity.  Scatter is flattened toward horizontal (the Z
        # component scaled right down) so shed material spreads across the road
        # rather than being lobbed upward — an upward component on every piece is
        # the single strongest firework cue, because it puts the whole spray on
        # matching rising arcs.
        #
        # At speed=0 (default) the only separation was scatter ~0.18 m/s,
        # which leaves pieces clumped.  Add a base separation kick and per-piece
        # direction jitter so the spray fans naturally even without an explicit
        # blast.  The cone direction already gives some spread; we rotate each
        # piece's vector by a random angle around the cone axis (±25°) plus a
        # small out-of-cone tilt (±12°) to break the radial pattern.
        axis = np.array(event.direction, dtype=np.float64)
        axis_norm = np.linalg.norm(axis)
        axis = axis / axis_norm if axis_norm > 1e-9 else np.array((0.0, 0.0, 1.0))
        theta = float(rng.uniform(-0.44, 0.44))  # ±25°
        c, s = np.cos(theta), np.sin(theta)
        # Rodrigues rotation of dirs[i] around axis by theta
        dir_rot = (dirs[i] * c +
                   np.cross(axis, dirs[i]) * s +
                   axis * (axis @ dirs[i]) * (1 - c))
        # Small out-of-cone tilt
        tilt = float(rng.uniform(-0.21, 0.21))  # ±12°
        # Find an axis perpendicular to dir_rot for the tilt
        ref = np.array((0.0, 0.0, 1.0)) if abs(dir_rot[2]) < 0.9 else np.array((1.0, 0.0, 0.0))
        tilt_axis = np.cross(dir_rot, ref)
        tn = np.linalg.norm(tilt_axis)
        if tn > 1e-9:
            tilt_axis = tilt_axis / tn
            dir_rot = (dir_rot * np.cos(tilt) +
                       np.cross(tilt_axis, dir_rot) * np.sin(tilt) +
                       tilt_axis * (tilt_axis @ dir_rot) * (1 - np.cos(tilt)))
        dir_rot = dir_rot / np.linalg.norm(dir_rot)

        # Base separation speed: even at speed=0, give a random outward kick
        # so pieces don't fall as a tight cluster.
        base_sep = scatter * float(rng.uniform(0.7, 1.6))
        speed_mult = speed * float(rng.uniform(0.6, 1.35)) if speed > 0 else 0.0
        sep = dir_rot * (base_sep + speed_mult)
        sep[2] = abs(sep[2]) * 0.25
        vel = sep + inherited
        blast = bool(np.linalg.norm(vel) > 1e-4)

        # LAUNCH VELOCITY.  Blender's rigid body API exposes no initial
        # velocity, so it has to be handed to the solver as *motion*: a
        # kinematic body follows its keyframes, and when it is released the
        # solver adopts the velocity it was already travelling at.
        #
        # Two keyframes are NOT enough.  Measured: a single pre-frame offset
        # transfers exactly zero velocity — the shard is released at rest and
        # simply drops straight down (peak rise 0.000 m).  The solver needs
        # several frames of genuine kinematic travel to establish the velocity;
        # with LAUNCH_FRAMES=3 the same shard arcs 2.0 m upward and 2.0 m
        # downrange, which is what a thrown fragment should do.
        #
        # NOTE on phase 1/phase 2: the object is placed and keyed in the SCENE
        # collection first, and only linked into the rigid body collection
        # below.  The rigid body system captures a body's initial transform on
        # its FIRST depsgraph evaluation, and a freshly-linked object reads as
        # identity there — registering before the real position was established
        # pinned every body at the world origin (measured: all 543 baked bodies
        # frozen at (0,0,0), glass under the car).  The explicit evaluation
        # between the two phases makes the registration read the real matrices.
        dt = 1.0 / max(1e-6, float(output_fps))

        if blast:
            launch_start = spawn_frame - LAUNCH_FRAMES
            for k in range(LAUNCH_FRAMES + 1):
                f = launch_start + k
                loc = spawn_at + vel * dt * k
                # The launch phase is KINEMATIC — it ignores collisions — so a
                # strong downward inherited velocity would carry the shard
                # straight through the ground plane and release it below the
                # collider, where it falls out of the world (measured: glass at
                # z=-50 after a -19 m/s backlight strike).  Skimming the ground
                # instead of burying into it is the correct behaviour: the
                # shard keeps its horizontal speed, slides, and settles in the
                # pile.  Clamp on the lowest point, for the same reason the
                # spawn position does.
                if loc[2] < floor:
                    loc[2] = floor
                obj.location = tuple(loc)
                obj.keyframe_insert("location", frame=f)
            # The launch keys describe CONSTANT-velocity travel, so they must be
            # interpolated LINEARLY.  Blender's default is Bezier, which eases in
            # and out of every key: the curve leaves the first key at zero slope
            # and arrives at the last one at zero slope too, so the velocity the
            # solver reads at release is not the velocity that was computed — the
            # piece is handed a near-zero or wildly overshooting speed depending
            # on where the ease lands.  That is what made the launch unpredictable
            # after the previous edits.
            _linearise(obj, "location")

            # Hide until the launch begins, so debris does not sit in mid-air
            # from frame 1 waiting to be thrown.  The frame is stashed on the
            # object because bake_debris clears animation data and has to
            # rewrite these.
            obj[LAUNCH_PROP] = int(launch_start)
            _key_visibility(obj, launch_start)
            placed.append((obj, launch_start, s, True))
        else:
            # Below-threshold drop: no kinematic launch at all.  A kinematic
            # body that sits static for the pre-launch run and is then released
            # picks up a phantom solver velocity proportional to how long it was
            # held (measured: back-landing shards ~9.6 m/s straight up after a
            # ~400-frame hold).  The bake-start fix in bake_debris only helps
            # the FIRST event; every later event still accumulates the phantom.
            # Instead the piece stays ACTIVE from the start at its floor-clamped
            # spawn position, hidden until its spawn frame, so it just rests on
            # the ground and is revealed already settled — a straight drop.
            obj.location = tuple(spawn_at)
            obj[LAUNCH_PROP] = int(spawn_frame)
            _key_visibility(obj, spawn_frame)
            placed.append((obj, spawn_frame, s, False))

    # Phase 2: let the depsgraph evaluate the freshly-placed pieces so their
    # world matrices are real before the rigid bodies register against them.
    bpy.context.view_layer.update()
    for obj, launch_start, s, is_blast in placed:
        rb_coll.objects.link(obj)
        rb = obj.rigid_body
        if rb is None:
            continue

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
        rb.mass = max(0.004, profile.thickness * 90.0 * s ** 3)
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

    return spawned


def _spawn_fine_particles(event: ImpactEvent, count: int,
                          templates: Sequence["bpy.types.Object"],
                          spawn_frame: int, settings: DebrisSettings,
                          coll: "bpy.types.Collection",
                          rng: np.random.Generator,
                          particle_templates: Optional[
                              Tuple[Optional["bpy.types.Collection"],
                                    float, float]] = None,
                          ) -> Optional["bpy.types.Object"]:
    """One small emitter whose particles instance the shard templates.

    Unlike the earlier attempt, rotation is ON (so shards tumble in flight
    rather than sliding along like decals) and the emitter carries the part's
    own velocity, so the spray is thrown *off the part* instead of merely
    falling from a fixed point.
    """
    if not templates or count <= 0:
        return None

    profile = profile_for(event.material)
    name = f"debris_emit_{event.material}_{spawn_frame}"

    mesh = bpy.data.meshes.new(name)
    # Near-point emitter.  A visible 0.12 m quad read as a static "square" at
    # every impact in the viewport (measured), and it cannot be hidden without
    # dropping the object out of the depsgraph (kills particle display).  A
    # tiny face keeps the object + particle evaluation alive while being
    # imperceptible in the viewport; spawn spread comes from velocity/jitter.
    r = 0.002
    mesh.from_pydata([(-r, -r, 0.0), (r, -r, 0.0), (r, r, 0.0), (-r, r, 0.0)],
                     [], [(0, 1, 2, 3)])
    mesh.update()

    emitter = bpy.data.objects.new(name, mesh)
    pos = np.array(event.position, dtype=np.float64)
    if pos[2] < settings.ground_z:
        pos[2] = settings.ground_z + 0.005
    emitter.location = tuple(pos)
    axis = np.array(event.direction, dtype=np.float64)
    emitter.rotation_mode = "QUATERNION"
    emitter.rotation_quaternion = mathutils.Vector(
        (0.0, 0.0, 1.0)).rotation_difference(mathutils.Vector(tuple(axis)))
    emitter.hide_render = True
    coll.objects.link(emitter)

    psys_mod = emitter.modifiers.new(name="debris", type="PARTICLE_SYSTEM")
    psys = psys_mod.particle_system
    st = psys.settings
    st.name = f"PS_{event.material}_{spawn_frame}"
    st.count = int(count)

    # THRESHOLD -> INTENSITY.  One number grades everything below, so a graze
    # and a smash differ in kind, not just in particle count.
    intensity = impact_intensity(event, settings)

    st.frame_start = spawn_frame
    # Stagger emission over a window that widens with intensity.  Firing the
    # whole spray in 2 frames is what reads as a firework: one coherent shell
    # leaving at one instant.  Real material peels off across the crush.
    window = max(1, int(round(settings.emit_window * (0.5 + 0.5 * intensity))))
    st.frame_end = spawn_frame + window
    # LIFETIME must outlast the shot.  A particle whose lifetime expires is
    # deleted where it stands, so a lifetime shorter than the remaining timeline
    # makes settled debris blink out of the pile — and any particle still
    # falling when it expires vanishes in mid-air, which is indistinguishable
    # from "the debris hangs in the air and then disappears".  Run it to the end
    # of the scene so every chip lands and stays landed.
    st.lifetime = max(60, int(bpy.context.scene.frame_end) - spawn_frame
                      + settings.settle_frames)
    # Near-zero lifetime variation: the variation existed to desynchronise the
    # spray, but emission is already staggered over `window`, and randomising
    # lifetime only adds the mid-air pop-out described above.
    st.lifetime_random = 0.05
    st.emit_from = "FACE"
    st.use_emit_random = True
    st.physics_type = "NEWTON"
    st.mass = max(0.002, profile.thickness * 40.0)

    # SIZE.  ``particle_size`` is both the render scale for instanced objects
    # AND the collision radius.  The old 0.5 gave a ~0.18 m collision radius
    # around shards that render at ~0.014 m, so particles rested a hand's width
    # above the ground and bounced off each other like beach balls.  The shard
    # SIZE / COLLISION RADIUS.  These are the same field, so the templates are
    # pre-scaled to separate them — see _ensure_particle_templates.  With the
    # scaled collection in play ``particle_size`` becomes the shard's real
    # radius (~1-3 cm) and the instances still render at true size; without it
    # this falls back to the old true-size/point-collision pairing.
    pcoll, pradius, pdrop = (particle_templates if particle_templates is not None
                             else _ensure_particle_templates(settings))
    # With the scaled collection the Shard Scale already lives in the template
    # scale, so this field carries ONLY the collision radius.
    st.particle_size = pradius if pcoll is not None else 1.0 * settings.scale
    # SIZE VARIATION scales the collision radius per particle as well as the
    # render size. The sunk deflector is calibrated for ONE radius, so a spread
    # would re-scatter rest heights during the live simulation — but the bake
    # step (bake_particles) clamps every particle to the ground per-frame,
    # fixing any penetration/hover from size variation. This gives the visual
    # variety of a statistical spray without the half-submerged bug.
    st.size_random = 0.35 if pcoll is not None else 0.7
    # Mass follows size, so big fragments carry momentum and small chips are
    # stopped by drag — without this every piece decelerates identically.
    st.use_multiply_size_mass = True

    # THIS EMITTER'S OWN DEFLECTOR, sunk by the drop its shard size implies.
    # ``collision_collection`` restricts the system to that one plane, which is
    # what makes per-material drops possible: Blender otherwise deflects a
    # particle against EVERY collider in the scene, so a glass chip would stop
    # on whichever material's plane happened to sit highest.
    # Keyed by LIBRARY — the same (part, material) key the scaled templates and
    # therefore the radius come from, so the plane a system collides with is
    # always the one calibrated for the shards it is instancing.  Two parts of
    # the same material have different shard sizes and so need different planes.
    # This is per library, not per impact: ~a dozen planes, not one per event.
    if pcoll is not None:
        tag = _safe_name(f"{event.part}_{event.material}")
        pground = _ensure_particle_ground(settings, pradius, pdrop,
                                          suffix=f"_{tag}")
        if pground is not None:
            holder = _get_collection(f"{PARTICLE_GROUND_NAME}_{tag}",
                                     parent=_get_collection(DEBRIS_COLLECTION))
            if pground.name not in holder.objects:
                holder.objects.link(pground)
            st.collision_collection = holder

    # AIR DRAG.  The single biggest cue that debris is light: it sheds speed
    # fast, so the spray loses its shape instead of holding a clean parabola.
    # At low bounciness the drag is raised too, so a chip that has landed loses
    # its remaining speed instead of sliding away across the road.
    bounciness = float(np.clip(settings.bounciness, 0.0, 1.0))
    st.damping = float(np.clip(settings.air_drag + 0.45 * (1.0 - bounciness),
                               0.0, 1.0))
    # Sub-stepping the solver: at 10-25 m/s a particle covers up to 0.4 m per
    # frame, several times its own size, and would tunnel through the ground.
    st.subframes = int(max(0, settings.particle_subframes))
    # RADIUS collision, now that ``particle_size`` carries the shard's true
    # radius instead of its render scale.  Point collision (the previous
    # behaviour, kept as the fallback) rests the shard's CENTRE on the ground,
    # so the instance drawn around it is exactly half buried — the reported
    # bug.  Deflecting on the radius rests the shard's SURFACE on the ground.
    #
    # The earlier attempt at this flag failed only because ``particle_size`` was
    # still ~1.0, giving a half-metre collision radius and chips hovering at
    # z=0.35-0.75; see _ensure_particle_templates for how the render scale and
    # the collision radius were separated.
    st.use_size_deflect = pcoll is not None

    # NORMAL_FACTOR is emission ALONG THE EMITTER'S NORMAL — i.e. every particle
    # leaving in the same direction at the same speed from the same point.  That
    # is the firework, exactly: a coherent shell on matching arcs.  It is 0
    # unless the user explicitly dials Launch Speed up.
    speed = (settings.speed * profile.speed_bias
             * (0.25 + 1.55 * intensity))
    st.normal_factor = speed
    # The scatter goes in as RANDOM velocity instead, so the pieces separate
    # from each other rather than all flying outward together.  Random velocity
    # has no preferred direction, so it cannot produce a shell.
    scatter = max(0.0, float(settings.scatter)) * (0.4 + 0.6 * intensity)
    # FLOOR so even a grazing impact sheds material that visibly travels
    # (see ``min_launch_speed``): a puff in every direction, not a cone.
    scatter = max(scatter,
                  settings.min_launch_speed * (0.4 + 0.6 * intensity))
    st.factor_random = (scatter
                        + speed * float(np.clip(settings.speed_spread, 0.0, 2.0)))
    # Inherit the part's momentum so the spray trails the moving wreck. This is
    # the only *directed* velocity left by default, and it is the correct one:
    # the material was travelling with the panel when it broke off.
    st.object_align_factor = tuple(
        np.array(event.velocity) * settings.inherit_velocity * intensity)

    # Tumbling: the single biggest reason the earlier debris read as fake.
    st.use_rotations = True
    st.rotation_mode = "VEL"
    st.rotation_factor_random = 1.0
    st.angular_velocity_mode = "RAND"
    # Spin scales with intensity — a hard hit shreds material and sets it
    # spinning; a light one lets it flutter down.
    st.angular_velocity_factor = 2.0 + 22.0 * intensity

    st.render_type = "COLLECTION"
    # The PRE-SCALED collection, so the 1/r template scale cancels the radius
    # in particle_size and the chip renders at its true size.  Falls back to
    # the unscaled originals when no scaled set could be built.
    shard_coll = pcoll or bpy.data.collections.get(SHARD_COLLECTION)
    if shard_coll is not None:
        st.instance_collection = shard_coll
        st.use_collection_pick_random = True
    st.use_rotation_instance = True

    st.effector_weights.gravity = 1.0
    st.use_dynamic_rotation = True

    # Hide the emitter quad until the impact, so it does not sit as a tiny
    # fixed square at each impact point from frame 1 (measured: 121 quads
    # visible at frame 1).  Emission itself was always gated by frame_start;
    # this hides the emitter OBJECT.  It is re-hidden once every particle is
    # dead so it does not linger after the spray settles.  (hide_render stays
    # True throughout — the emitter quad is never rendered, only its instances.)
    #
    # With the lifetime now running past the end of the scene this key lands
    # beyond the timeline and never fires, which is correct: hiding the emitter
    # drops it out of the depsgraph and takes its instanced particles with it,
    # so re-hiding it while any chip is still on the ground would delete the
    # settled debris. The key is kept for the case where a short settle window
    # genuinely does outlive the spray.
    end_hide = int(spawn_frame) + 2 + int(st.lifetime) + 1
    emitter.hide_viewport = True
    emitter.keyframe_insert("hide_viewport", frame=spawn_frame - 1)
    emitter.hide_viewport = False
    emitter.keyframe_insert("hide_viewport", frame=spawn_frame)
    emitter.hide_viewport = True
    emitter.keyframe_insert("hide_viewport", frame=end_hide)

    return emitter


GLASS_COLLECTION = "BeamNG Debris Glass"


@dataclass
class GlassCrackSettings:
    """How a CRACKED pane is decorated.

    Separate from :class:`GlassSettings` (which owns the tier thresholds)
    because this is purely a look, with no effect on classification.
    """

    #: Master switch for the crack decoration.
    enabled: bool = True
    #: Paint the damage from a user-supplied image instead of the procedural
    #: web.  The Image Texture node is left empty when no path is set, so the
    #: user can drop their own crack PNG into it and place it by hand.
    use_image: bool = True
    #: Absolute path to the crack texture.  Empty = leave the node unassigned.
    image_path: str = ""
    #: How wide (m) the crack image spans across the pane.
    image_span: float = 1.2
    #: Hole size scale, fed to ``glass_crack.hole_radius_for`` with severity.
    #: Only used by the procedural path.
    scale: float = 0.05


def _glass_material_for(part: str, material: str,
                        source_objects: Optional[Dict[str, "bpy.types.Object"]]
                        ) -> Optional["bpy.types.Material"]:
    """Reuse the car's own glass material when it is available.

    Falling back to a fresh material is fine (debris still renders), but the
    real pane's material carries its actual shading, transmission and
    roughness — and matching it means the fragments read as the same glass the
    viewer watched the whole crash through.
    """
    src = (source_objects or {}).get(part)
    if src is not None and src.data is not None and src.data.materials:
        for m in src.data.materials:
            if m is not None and "glass" in m.name.lower():
                return m
        return src.data.materials[0]
    candidate = bpy.data.materials.get(material)
    if candidate is not None:
        return candidate
    candidate = bpy.data.materials.get("glass")
    if candidate is not None:
        return candidate
    return None


def _spawn_glass_pane(part: str, tier: str, event: ImpactEvent,
                      verts: np.ndarray, spawn_frame: int,
                      settings: DebrisSettings,
                      glass_settings: GlassSettings,
                      coll: "bpy.types.Collection",
                      rb_coll: Optional["bpy.types.Collection"],
                      material: Optional["bpy.types.Material"],
                      rng: np.random.Generator,
                      output_fps: float = 24.0,
                      ) -> Tuple[List["bpy.types.Object"], int]:
    """Break one pane into fragments and place them in the scene.

    Returns ``(dynamic_fragments, retained_count)``.  Retained cells (the
    pane's edge fringe) are NOT spawned as objects: the pane mesh's rim band
    is kept alive by ``mesh_update`` (via ``set_shattered_panes``) and IS the
    fringe, so it stays welded in the aperture and follows the pane's per-frame
    vertex deformation exactly — where a parented fragment could only ride the
    wreck's rigid transform and drift off a deforming pane.  The non-retained
    fragments are independent rigid bodies that break outward from the impact.

    ``verts`` must be in WORLD space (the same space as ``event.position`` —
    see ``impact_detect.local_to_world``).

    Only ``GLASS_SHATTERED`` panes reach here — a cracked pane keeps its glass
    and is handled by the crack material in :func:`_apply_glass_crack`.
    """
    if tier != GLASS_SHATTERED:
        return [], 0

    count = fragment_count_for(event.severity, True, vertex_count=len(verts))
    fragments = shatter_pane(
        verts,
        impact_point=np.array(event.position, dtype=np.float64),
        fragments=count,
        thickness=profile_for("glass").thickness,
        edge_retain=glass_settings.edge_retain,
        seed=settings.seed + (_stable_hash(str(part)) % 100000),
    )
    if not fragments:
        return [], 0

    intensity = impact_intensity(event, settings)
    part_vel = np.array(event.velocity, dtype=np.float64)
    dt = 1.0 / max(1e-6, float(output_fps))
    dynamic: List["bpy.types.Object"] = []
    retained = 0
    # (object, launch_start) placed in phase 1, registered as a rigid body in
    # phase 2 — see the phase note in _spawn_hero_pieces.
    placed: List[Tuple["bpy.types.Object", int]] = []

    for i, frag in enumerate(fragments):
        if frag.retained:
            # Edge fringe — never spawned as an object.  The pane mesh's rim
            # band is kept alive by mesh_update and IS the fringe: it stays
            # welded in the aperture and follows the pane's own per-frame
            # vertex animation, where a separate parented fragment could only
            # ride the wreck's rigid transform and drift off the deforming
            # pane.  Counted so the build report still shows the rim.
            retained += 1
            continue

        mesh = bpy.data.meshes.new(f"glassfrag_{part}_{i:03d}")
        mesh.from_pydata([tuple(float(c) for c in v) for v in frag.verts], [],
                         [list(f) for f in frag.faces])
        mesh.validate(verbose=False)
        for poly in mesh.polygons:
            poly.use_smooth = False
        if material is not None:
            mesh.materials.append(material)
        mesh.update()

        obj = bpy.data.objects.new(f"glassfrag_{part}_{i:03d}", mesh)
        obj.location = tuple(float(c) for c in frag.centre)
        coll.objects.link(obj)

        # Fragments closest to the impact are thrown hardest — the strike drives
        # them out while the far side of the pane merely falls away.
        blow = float(np.exp(-2.4 * frag.impact_distance))
        away = np.array(frag.centre, dtype=np.float64) - np.array(
            event.position, dtype=np.float64)
        n = np.linalg.norm(away)
        away = away / n if n > 1e-6 else np.array((0.0, 0.0, 1.0))

        # ``away`` is the outward direction from the impact.  With ``speed`` 0
        # by default the launch term vanishes and glass should simply fall out
        # of the aperture.  Scatter alone (0.45 m/s) is too small to break the
        # clump — we add per-fragment direction jitter and a small base
        # separation velocity so fragments fan out naturally instead of staying
        # in a tight ball.
        scatter = max(0.0, float(settings.scatter)) * (0.4 + 0.6 * intensity)

        # Per-fragment direction variation: rotate the radial ``away`` vector by
        # a random angle around the pane normal (up to ±35°) plus a small
        # out-of-plane tilt (±15°).  This fans the spray into a proper cloud.
        pane_normal = np.array(event.normal, dtype=np.float64)
        nrm = np.linalg.norm(pane_normal)
        pane_normal = pane_normal / nrm if nrm > 1e-6 else np.array((0.0, 0.0, 1.0))
        # Random axis in the pane plane
        theta = float(rng.uniform(-0.61, 0.61))  # ±35°
        c, s = np.cos(theta), np.sin(theta)
        # Rotate ``away`` around pane_normal by theta (Rodrigues)
        away_rot = (away * c +
                    np.cross(pane_normal, away) * s +
                    pane_normal * (pane_normal @ away) * (1 - c))
        # Small out-of-plane tilt
        tilt = float(rng.uniform(-0.26, 0.26))  # ±15°
        away_rot = away_rot * np.cos(tilt) + pane_normal * np.sin(tilt)
        away = away_rot / np.linalg.norm(away_rot)

        # Base separation speed: even at speed=0, give each fragment a small
        # random outward kick so they don't fall as a solid sheet.
        base_sep = scatter * float(rng.uniform(0.8, 1.8))
        launch_speed = (settings.speed * profile_for("glass").speed_bias
                        * (0.3 + 1.4 * intensity))
        vel = away * (launch_speed + base_sep) * blow * float(rng.uniform(0.55, 1.4))

        # Flatten the outward throw: glass falling out of a window should not be
        # lobbed upward off the car.
        vel[2] = min(vel[2], abs(vel[2]) * 0.2)
        vel = vel + part_vel * settings.inherit_velocity * intensity

        # Per-fragment position jitter: offset the start position slightly along
        # the pane plane so fragments don't all begin at the exact same point.
        # Scale by fragment size (~2-5 cm) so the jitter is subtle but breaks
        # the perfect grid alignment of Voronoi centroids.
        jitter_scale = 0.03 * float(rng.uniform(0.5, 1.5))
        jitter_dir = np.array([float(rng.uniform(-1, 1)),
                               float(rng.uniform(-1, 1)), 0.0])
        jitter_dir = jitter_dir / (np.linalg.norm(jitter_dir) + 1e-9)
        base = np.array(frag.centre, dtype=np.float64) + jitter_dir * jitter_scale
        # Clamp on the fragment's LOWEST POINT, not its origin — exactly as the
        # hero pieces do (see _lowest_point_offset).  The launch phase is
        # KINEMATIC and ignores collisions, so a strong inherited downward
        # velocity carries the centre down to the ground_z + GROUND_CLEARANCE
        # floor while the hull, which extends several cm BELOW the centre, is
        # already buried in the slab.  Bullet resolves that initial penetration
        # by ejecting the body through the nearest face — for a thin flat shard
        # that is downward — and the fragment free-falls out of the world
        # (measured on the real cache: 15 backlight/trunkglass fragments ended
        # at z=-107 to -118 with the centre-only clamp).  Skimming on the lowest
        # point keeps the hull clear of the collider at release.
        low_off = _lowest_point_offset(obj, (0.0, 0.0, 0.0), 1.0)
        floor = settings.ground_z + GROUND_CLEARANCE - low_off
        for k in range(LAUNCH_FRAMES + 1):
            loc = base + vel * dt * k
            if loc[2] < floor:
                loc[2] = floor
            obj.location = tuple(loc)
            obj.keyframe_insert("location", frame=launch_start + k)
        # Constant-velocity keys need LINEAR interpolation — see _linearise.
        _linearise(obj, "location")

        obj[LAUNCH_PROP] = int(launch_start)
        _key_visibility(obj, launch_start)
        placed.append((obj, launch_start))

    # Phase 2: evaluate the placed fragments in the scene, then register their
    # rigid bodies against the real world matrices.
    bpy.context.view_layer.update()
    for obj, launch_start in placed:
        if rb_coll is not None:
            rb_coll.objects.link(obj)
        dynamic.append(obj)

        rb = obj.rigid_body
        if rb is None:
            continue
        rb.type = "ACTIVE"
        rb.collision_shape = "CONVEX_HULL"
        rb.mass = 0.02
        # Same single bounciness dial as the hero pieces, scaled down: glass
        # chips are the least bouncy debris in a crash, they mostly just skitter
        # and stop.
        rest, lin_damp, ang_damp = _bounce_params(settings.bounciness)
        rb.restitution = rest * 0.6
        rb.friction = settings.friction
        rb.linear_damping = lin_damp
        rb.angular_damping = ang_damp
        rb.use_margin = True
        rb.collision_margin = 0.002
        rb.use_deactivation = True
        rb.deactivate_linear_velocity = 0.06 + 0.14 * (
            1.0 - float(np.clip(settings.bounciness, 0.0, 1.0)))
        rb.deactivate_angular_velocity = 0.12 + 0.28 * (
            1.0 - float(np.clip(settings.bounciness, 0.0, 1.0)))
        for k in range(LAUNCH_FRAMES + 1):
            rb.kinematic = k < LAUNCH_FRAMES
            rb.keyframe_insert("kinematic", frame=launch_start + k)

    return dynamic, retained


def _find_pane_target(part: str) -> Tuple[Optional["bpy.types.Object"],
                                          Optional[Tuple[int, int]]]:
    """Locate the object that actually DRAWS ``part``, and its face slice.

    Returns ``(object, face_range)``.  ``face_range`` is None when the pane owns
    a whole mesh; otherwise it is the ``[start, end)`` polygon slice of the pane
    inside a merged chunk mesh.

    This lookup exists because the real capture imports CHUNKED: the windshield
    is not an object, it is 59 vertices and their faces inside a merged
    ``glass`` mesh shared by all 13 panes.  A standalone
    ``flanje_e180_windshield`` object does still exist (in the hidden Source
    collection), and it is a trap — playback never writes to it, so anything
    applied there is invisible.  Ask the live playback which object it actually
    updates, and fall back to the scene object only when there is no chunking.
    """
    if bpy is None:
        return None, None
    try:
        from . import frame_handler
        pb = frame_handler._active
    except Exception:  # pragma: no cover - defensive
        pb = None

    if pb is not None:
        faces = getattr(pb, "_chunk_member_faces", None) or {}
        chunks = getattr(pb, "_chunks", None) or {}
        for chunk_name, members in faces.items():
            if part in members and chunk_name in chunks:
                return chunks[chunk_name], members[part]
        # Non-chunked playback drives the per-part objects directly.
        objs = getattr(pb, "_objects", None) or {}
        if part in objs and not chunks:
            return objs[part], None

    return bpy.data.objects.get(part), None


def _assign_material_to_faces(obj: "bpy.types.Object",
                              mat: "bpy.types.Material",
                              face_range: Optional[Tuple[int, int]]) -> int:
    """Put ``mat`` on ``obj``, restricted to ``face_range`` when given.

    Returns the number of polygons switched to the new material.  Appending a
    slot (rather than replacing slot 0) is what keeps the other twelve panes in
    a merged glass chunk on their original shader — replacing the slot would
    crack every window in the car at once.
    """
    mesh = getattr(obj, "data", None)
    if mesh is None or mat is None:
        return 0
    slot = -1
    for i, existing in enumerate(mesh.materials):
        if existing is mat:
            slot = i
            break
    if slot < 0:
        mesh.materials.append(mat)
        slot = len(mesh.materials) - 1

    polys = mesh.polygons
    if face_range is None:
        start, end = 0, len(polys)
    else:
        start, end = face_range
        start = max(0, int(start))
        end = min(len(polys), int(end))
    for i in range(start, end):
        polys[i].material_index = slot
    return max(0, end - start)


def _apply_glass_crack(part: str, event: ImpactEvent, cache_frame: int,
                       reader, crack_frame: int,
                       settings: "GlassCrackSettings",
                       ground_shift: float = 0.0,
                       ) -> Optional[dict]:
    """Paint a cracked pane: it keeps its glass and keeps animating.

    A cracked pane is NOT fragmented and NOT collapsed — the decoration is a
    material, because the pane is still part of the vertex-cache animation and
    that animation writes mesh vertices BY INDEX.  Re-topologising the pane to
    give a hole real edges would misalign every frame of the car (see the
    ``glass_crack`` module docstring).

    Returns a summary dict, or None when the pane could not be decorated.
    """
    if bpy is None:
        return None
    from .glass_crack import (
        build_crack_image_material, build_crack_material, crack_placement,
        hole_radius_for, keyframe_crack, load_crack_image,
        world_to_cache_local,
    )

    obj, face_range = _find_pane_target(part)
    if obj is None or getattr(obj, "data", None) is None:
        return None

    # The shader works in OBJECT space — the same cache-local space playback
    # writes into the mesh — so both the pane and the impact must be mapped out
    # of world space, including the ground shift playback applies OUTSIDE the
    # mesh (on obj.location).
    try:
        local = np.asarray(reader.frame_positions(part, cache_frame),
                           dtype=np.float64)
    except Exception:
        return None
    if len(local) < 3:
        return None
    transform = reader.frame_transform(cache_frame)
    impact_local = world_to_cache_local(
        np.asarray(event.position, dtype=np.float64), transform,
        ground_shift=ground_shift)

    try:
        placement = crack_placement(
            local, impact_local,
            hole_radius_for(settings.scale, event.severity))
    except ValueError:
        return None

    name = f"BeamNG Crack {part}"
    if settings.use_image:
        image = load_crack_image(settings.image_path)
        mat = build_crack_image_material(
            name, placement, obj, image=image, span=settings.image_span)
    else:
        mat = build_crack_material(
            name, placement, web_intensity=float(np.clip(event.severity, 0.0, 1.0)),
            seed=_stable_hash(str(part)) % 100000, obj=obj)
    if mat is None:
        return None

    n_faces = _assign_material_to_faces(obj, mat, face_range)
    # The driver reads this off the OBJECT that carries the material, which in
    # chunked mode is the shared chunk — so several panes cracking would fight
    # over one property.  Ramp it once; a second pane on the same chunk simply
    # re-keys the same fade, which is correct because they crack together.
    keyframe_crack(obj, int(crack_frame))
    return {"part": part, "object": obj.name, "material": mat.name,
            "faces": n_faces, "frame": int(crack_frame),
            "chunked": face_range is not None}


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _template_key(event: ImpactEvent) -> str:
    return f"{event.part}|{event.material}"


def _safe_name(text: str) -> str:
    """Datablock-name-safe form of ``text``, short enough to take a suffix.

    Part names come from the capture and can carry separators that make the
    resulting collection names hard to read; Blender also truncates names past
    63 characters, which would silently collide two libraries onto one
    deflector.
    """
    out = "".join(c if (c.isalnum() or c in "_-") else "_" for c in text)
    return out[:40]


def build_debris(reader, events: Sequence[ImpactEvent],
                 settings: Optional[DebrisSettings] = None,
                 glass_settings: Optional[GlassSettings] = None,
                 crack_settings: Optional[GlassCrackSettings] = None,
                 frame_start: int = 0,
                 playback_fps: float = 24.0,
                 output_fps: float = 24.0,
                 ground_shift: float = 0.0,
                 source_objects: Optional[Dict[str, "bpy.types.Object"]] = None,
                 progress=None) -> dict:
    """Create shard libraries, spawn debris and bake it to keyframes.

    Returns a summary dict for the operator report.
    """
    if bpy is None:
        raise RuntimeError("build_debris requires Blender (bpy)")

    settings = settings or DebrisSettings()
    glass_settings = glass_settings or GlassSettings()
    crack_settings = crack_settings or GlassCrackSettings()
    scene = bpy.context.scene
    rng = np.random.default_rng(settings.seed)

    events = [e for e in events if e.severity >= settings.min_severity]
    if not events:
        return {"events": 0, "hero": 0, "emitters": 0, "shards": 0}

    debris_coll = _get_collection(DEBRIS_COLLECTION)
    shard_coll = _get_collection(SHARD_COLLECTION)

    _ensure_ground(settings)

    # --- shard libraries, one per (part, material) ------------------------
    templates: Dict[str, List["bpy.types.Object"]] = {}
    for event in events:
        key = _template_key(event)
        if key in templates:
            continue
        try:
            verts = reader.frame_positions(event.part, event.cache_frame)
            tris = triangulate_indices(reader.base_indices(event.part))
        except Exception:
            continue
        objs = build_shard_library(
            event.part, event.material,
            np.asarray(verts, dtype=np.float64), tris,
            variants=settings.variants,
            seed=settings.seed + (_stable_hash(str(key)) % 100000),
            source_objects=source_objects,
            collection=shard_coll,
        )
        templates[key] = objs

    # Pre-scaled copies for the particle system, built ONCE now that every
    # library exists — it is derived from the whole template set, and rebuilding
    # it per emitter would redo the work for all 100+ impacts.
    # Pre-scaled particle templates + a sunk deflector PER LIBRARY, built lazily
    # and memoised here (see _ensure_particle_templates for the per-material
    # rationale).  The main ground must stop deflecting particles or they never
    # reach the sunk planes at all.
    particle_cache: Dict[str, Tuple] = {}
    if templates:
        _disable_ground_particle_collision()

    # --- spawn -------------------------------------------------------------
    _ensure_rigidbody_world(scene, scene.frame_start, scene.frame_end)
    rb_coll = scene.rigidbody_world.collection

    hero_objects: List["bpy.types.Object"] = []
    emitters: List["bpy.types.Object"] = []
    last_frame = scene.frame_start
    first_spawn: Optional[int] = None

    # Allocate the hero budget PROPORTIONALLY to severity, not greedily.
    # Greedy (sort by severity, take until the budget runs out) lets the first
    # crash swallow all ``max_hero_total`` pieces, so the follow-up impacts the
    # user actually notices — the car slamming onto its side (doors crushed),
    # then landing on its back — spawn ZERO hero shards.  Proportional scaling
    # keeps the biggest crash dominant while every later impact sheds a share
    # that reads as its true (medium) intensity instead of nothing.
    ordered = sorted(events, key=lambda e: -e.severity)
    raw_counts = [_counts_for(e, settings) for e in ordered]
    raw_hero_total = sum(h for h, _ in raw_counts)
    scale = (settings.max_hero_total / raw_hero_total
             if raw_hero_total > settings.max_hero_total else 1.0)
    hero_allot = [int(round(h * scale)) for h, _ in raw_counts]

    for i, event in enumerate(ordered):
        if progress is not None:
            progress(i, len(ordered), event.part)
        tpl = templates.get(_template_key(event))
        if not tpl:
            continue

        spawn_frame = _blender_frame_for(event.cache_frame, frame_start,
                                         playback_fps, output_fps)
        spawn_frame = max(scene.frame_start + 1, spawn_frame)
        last_frame = max(last_frame, spawn_frame)
        first_spawn = spawn_frame if first_spawn is None else min(first_spawn, spawn_frame)

        hero_n = hero_allot[i]
        fine_n = raw_counts[i][1]

        hero_objects.extend(_spawn_hero_pieces(
            event, hero_n, tpl, spawn_frame, settings, debris_coll, rng, rb_coll,
            output_fps=output_fps))
        # Scaled templates + sunk deflector for THIS library's shard size.
        key = _template_key(event)
        ptpl = _ensure_particle_templates(settings, key=key, templates=tpl,
                                          cache=particle_cache)
        emitter = _spawn_fine_particles(
            event, fine_n, tpl, spawn_frame, settings, debris_coll, rng,
            particle_templates=ptpl)
        if emitter is not None:
            emitters.append(emitter)

    if progress is not None:
        progress(len(ordered), len(ordered), "")

    # --- glass shatter ------------------------------------------------------
    # Resolved per PANE (not per impact): damage is monotonic and detection
    # fires a pane once per crush peak, so resolve_glass_damage collapses the
    # repeated hits into one break with the FIRST frame that reached the worst
    # tier.  Only shattered panes spawn; cracked panes keep their glass.
    glass_objects: List["bpy.types.Object"] = []
    shattered_panes: Dict[str, int] = {}
    cracked_panes: List[dict] = []
    retained_total = 0
    if settings.shatter_glass:
        glass_coll = _get_collection(GLASS_COLLECTION)
        for part, (tier, cache_frame, event) in resolve_glass_damage(
                events, glass_settings).items():
            # A CRACKED pane keeps its glass: no fragments, and deliberately no
            # entry in shattered_panes, so mesh_update never collapses it and
            # the pane's vertices keep animating with the car.
            if tier == GLASS_CRACKED:
                if crack_settings.enabled:
                    crack_frame = _blender_frame_for(
                        cache_frame, frame_start, playback_fps, output_fps)
                    crack_frame = max(scene.frame_start + 1, crack_frame)
                    info = _apply_glass_crack(
                        part, event, cache_frame, reader, crack_frame,
                        crack_settings, ground_shift=ground_shift)
                    if info is not None:
                        cracked_panes.append(info)
                continue
            if tier != GLASS_SHATTERED:
                continue
            try:
                local = reader.frame_positions(part, cache_frame)
            except Exception:
                continue
            # The pane geometry and event.position must live in the SAME space
            # for shatter_pane to bias fragments correctly.  frame_positions is
            # cache-local, so map it into world space at the shatter frame.
            world = local_to_world(
                local,
                reader.frame_transform(cache_frame),
                ground_shift=ground_shift)
            spawn_frame = _blender_frame_for(
                cache_frame, frame_start, playback_fps, output_fps)
            spawn_frame = max(scene.frame_start + 1, spawn_frame)
            last_frame = max(last_frame, spawn_frame)
            first_spawn = spawn_frame if first_spawn is None else min(first_spawn, spawn_frame)
            frags, retained = _spawn_glass_pane(
                part, tier, event, world, spawn_frame, settings,
                glass_settings, glass_coll, rb_coll,
                _glass_material_for(part, event.material, source_objects),
                rng, output_fps=output_fps)
            glass_objects.extend(frags)
            retained_total += retained
            shattered_panes[part] = int(cache_frame)

    # Templates are instancing sources only.  Detach AFTER every emitter has
    # been created (they only need the collection to exist in bpy.data) — see
    # _detach_template_collection for why hiding them any other way silently
    # renders the fine debris as nothing.
    _detach_template_collection(scene)

    # --- ground collider participates in the solver ------------------------
    # This link is what stops the debris falling out of the world.  It has to
    # happen AFTER _ensure_rigidbody_world: linking an object into the rigid
    # body collection is what gives it an ``obj.rigid_body``, so a ground plane
    # created before the world exists silently has none, contributes no
    # collision, and every shard drops to z=-100.
    ground = bpy.data.objects.get(GROUND_NAME)
    if ground is not None:
        if ground.name not in rb_coll.objects:
            rb_coll.objects.link(ground)
        if ground.rigid_body is None:  # pragma: no cover - defensive
            with _viewport_context():
                bpy.context.view_layer.objects.active = ground
                bpy.ops.rigidbody.object_add(type="PASSIVE")
        if ground.rigid_body is not None:
            ground.rigid_body.type = "PASSIVE"
            # BOX (computed from the slab's bounds) is the most robust collider
            # for a fast-moving shard spray: it is a closed volume, so a shard
            # that skips across the top surface in a substep is caught inside
            # rather than slipping through a zero-thickness mesh.
            ground.rigid_body.collision_shape = "BOX"
            # Bullet combines the two restitutions, so the ground has to follow
            # the dial as well — a bouncy floor under a non-bouncy shard still
            # bounces it.
            ground.rigid_body.friction = settings.friction
            ground.rigid_body.restitution = _bounce_params(
                settings.bounciness)[0]

    # The bake must start just before the FIRST launch, not at scene.frame_start.
    # Simulating the idle run from frame 1 (or from a start offset far ahead of
    # the first impact) both wastes minutes on empty frames and integrates a
    # phantom velocity out of long-static kinematic bodies (debris shot 90 m
    # sideways and fell to z=-96).  LAUNCH_FRAMES before the first spawn leaves
    # the point cache valid and the velocities honest.
    first_spawn = first_spawn if first_spawn is not None else last_frame
    bake_start = max(scene.frame_start, first_spawn - LAUNCH_FRAMES - 2)

    # EXTEND THE SCENE so the settle actually fits.  The bake end used to be
    # ``min(scene.frame_end, ...)``, which silently truncated the simulation
    # whenever the last impact landed near the end of the timeline — and a
    # truncated bake is the "all the debris is stuck in mid-air" symptom: the
    # F-curves simply stop while the pieces are still falling, so every piece
    # holds its last baked position forever.  The debris needs the settle window
    # to reach the ground, so the timeline is grown to fit rather than the
    # simulation cut to fit.
    bake_end = last_frame + settings.settle_frames
    if bake_end > scene.frame_end:
        scene.frame_end = int(bake_end)
    if scene.rigidbody_world is not None:
        scene.rigidbody_world.point_cache.frame_end = int(bake_end)

    # Re-stretch every emitter's particle lifetime now that the timeline's real
    # end is known.  The lifetimes were set during spawning, when scene.frame_end
    # was still the pre-extension value — leaving them short would delete
    # particles mid-flight or mid-pile, which reads as debris hanging in the air
    # and then vanishing.  Unlike the rigid bodies, particles are never baked, so
    # this setting IS the final behaviour and there is no later pass to fix it.
    for emitter in emitters:
        for mod in emitter.modifiers:
            psys = getattr(mod, "particle_system", None)
            if psys is None:
                continue
            st = psys.settings
            st.lifetime = max(int(st.lifetime),
                              int(scene.frame_end) - int(st.frame_start) + 2)
    return {
        "events": len(events),
        "hero": len(hero_objects),
        "emitters": len(emitters),
        "shards": sum(len(v) for v in templates.values()),
        "glass": len(glass_objects),
        "retained": retained_total,
        "shattered_panes": shattered_panes,
        "cracked_panes": cracked_panes,
        "bake_start": bake_start,
        "bake_end": bake_end,
        "hero_objects": hero_objects + glass_objects,
        "emitter_objects": emitters,
    }


#: Residual per-frame vertical motion (metres) below which a baked body counts
#: as having come to REST, so the ground-snap may seat it exactly on the plane.
#: Above this it is still moving and only the penetration clamp applies — a
#: piece mid-bounce must be allowed to leave the ground.
_REST_EPS = 2.5e-4

#: Frames over which the rest seat is ramped in, so seating a settled body does
#: not produce a single-frame vertical step just before it comes to rest.
_SEAT_BLEND = 6


def _snap_matrices_to_ground(objs: Sequence["bpy.types.Object"],
                             matrices: List[List], ground_z: float) -> dict:
    """Lift every baked pose so NO vertex ever sits below ``ground_z``.

    Two distinct corrections, because the two failure modes in the reference
    screenshot are different bugs wearing the same costume:

    * **Penetration clamp** (every frame).  Bullet resolves contacts against the
      body's CONVEX HULL plus a collision margin, and it is happy to leave a
      hull corner a millimetre or two inside the slab — plus the hull is a
      convex approximation, so a concave notched shard has real geometry
      *outside* the hull that the solver never tested at all.  Any frame whose
      lowest vertex is under the plane is lifted by exactly the shortfall.  This
      is what removes the half-submerged pieces.

    * **Rest seat** (settled tail).  A body that has gone to sleep is left
      wherever the solver's margin parked it, which is typically a hair ABOVE
      the plane (``collision_margin`` = 2 mm) — the ``debris_glass_1924_004`` at
      z = 0.104 in the report.  Once a body stops moving vertically its whole
      settled tail is translated so its lowest vertex sits exactly ON the plane,
      so the pile beds down and casts contact shadows instead of floating.

    The correction is a pure Z TRANSLATION of the whole tail, never a
    re-orientation: rotating a settled piece flat would destroy the natural
    jumbled lie of the pile.  Because the same offset is applied to every frame
    from the rest frame onward, a settled body does not drift or pop.

    Mutates ``matrices`` in place.  Returns a summary for the operator report.
    """
    if not objs or not matrices:
        return {"clamped": 0, "seated": 0, "max_lift": 0.0}

    n_frames = len(matrices)
    clamped = seated = 0
    max_lift = 0.0

    for j, obj in enumerate(objs):
        co = _local_verts(obj)
        if co is None:
            continue

        # --- per-frame lowest vertex, vectorised over the whole bake --------
        # Stacking the Z rows lets one matmul do every frame for this body,
        # instead of a Python loop over (frames x vertices).
        zrow = np.empty((n_frames, 4), dtype=np.float64)
        for i in range(n_frames):
            m = matrices[i][j]
            zrow[i] = (m[2][0], m[2][1], m[2][2], m[2][3])
        lows = co @ zrow[:, :3].T + zrow[:, 3]      # (V, F)
        lows = lows.min(axis=0)                      # (F,)

        # --- rest detection: first frame of the settled tail ----------------
        # Scanning BACKWARD from the end finds the moment the body last moved,
        # so a piece that bounces late is not seated on an early lull.
        rest_from = n_frames
        for i in range(n_frames - 1, 0, -1):
            if abs(lows[i] - lows[i - 1]) > _REST_EPS:
                rest_from = i
                break
        else:
            rest_from = 0

        # --- build the per-frame lift --------------------------------------
        lift = np.zeros(n_frames, dtype=np.float64)
        # Penetration: everything below the plane comes up to it.
        under = lows < ground_z
        lift[under] = ground_z - lows[under]
        if under.any():
            clamped += 1
        # Rest seat: the settled tail is translated to sit exactly on the plane.
        if rest_from < n_frames:
            seat = ground_z - lows[rest_from]
            lift[rest_from:] = seat
            if abs(seat) > _REST_EPS:
                seated += 1

        if not lift.any():
            continue
        max_lift = max(max_lift, float(np.abs(lift).max()))

        # Blend the seat in over the approach so a body that settles from a
        # small residual hover does not step vertically on its rest frame.
        # Only the frames immediately before the rest frame are touched, and
        # only up to their own existing lift, so this can never push a moving
        # piece into the ground.
        blend = min(_SEAT_BLEND, rest_from)
        if blend > 0 and rest_from < n_frames:
            ramp = np.linspace(0.0, 1.0, blend + 1)[:-1]
            head = slice(rest_from - blend, rest_from)
            lift[head] = np.maximum(lift[head], lift[rest_from] * ramp)

        for i in range(n_frames):
            if lift[i]:
                # Index the translation column explicitly rather than going
                # through ``.translation``, which returns a COPY on a Matrix —
                # mutating that copy would silently discard the lift.
                matrices[i][j][2][3] += lift[i]

    return {"clamped": clamped, "seated": seated, "max_lift": max_lift}


def bake_debris(hero_objects: Sequence["bpy.types.Object"],
                frame_start: int, frame_end: int,
                ground_z: float = 0.0, snap_ground: bool = True) -> dict:
    """Simulate the rigid bodies and freeze the result into keyframes.

    Runs with all frame handlers detached (see :func:`_frozen_handlers`), then
    converts the simulation to F-curves and removes the rigid body world.  The
    debris afterwards is inert animation data — the playback handler cannot
    disturb it and it never needs re-simulating.
    """
    if bpy is None or not hero_objects:
        return {"baked": 0}

    scene = bpy.context.scene
    alive = [o for o in hero_objects if o.name in bpy.data.objects]
    if not alive:
        return {"baked": 0}

    frame_start = int(frame_start)
    frame_end = int(frame_end)
    frame_orig = scene.frame_current

    with _frozen_handlers():
        if scene.rigidbody_world is not None:
            scene.rigidbody_world.point_cache.frame_end = frame_end

        # Step the solver and record each body's world matrix.  This is what
        # bpy.ops.rigidbody.bake_to_keyframes does internally, minus its
        # dependency on the active keying set — that operator drives keyframe
        # insertion through ``anim.keyframe_insert_by_name``, which needs both a
        # VIEW_3D context and a configured keying set, and dies with "No
        # suitable context info for active keying set" when either is missing.
        # Writing the F-curves ourselves is both more robust and faster, since
        # it walks the timeline once instead of twice.
        #
        # The walk MUST advance one frame at a time from the point cache's own
        # start frame.  Bullet integrates incrementally: jumping the playhead
        # into the middle of the range leaves the cache invalid and every body
        # reads out at its rest position (measured: all debris teleported to
        # z=0 instead of falling).
        #
        # Equally, the run must NOT start far before the first launch.  The
        # scene starts at frame 1 but impacts happen around frame 2030, and
        # simulating those 2000 idle frames made the solver integrate a huge
        # phantom velocity out of the long-static kinematic bodies — debris
        # shot 90 m sideways and fell to z=-96.  Starting a few frames ahead of
        # the earliest launch keeps the cache valid and the velocities honest.
        matrices: List[List] = []
        sim_start = int(max(scene.frame_start, frame_start - LAUNCH_FRAMES - 2))
        rbw = scene.rigidbody_world
        if rbw is not None:
            rbw.point_cache.frame_start = sim_start
        for f in range(sim_start, frame_end + 1):
            scene.frame_set(f)
            if f >= frame_start:
                matrices.append([o.matrix_world.copy() for o in alive])

        # GROUND SNAP.  Applied to the sampled matrices, BEFORE they become
        # keyframes, so what gets written is already correct — there is no
        # second pass over the F-curves and nothing can re-introduce the
        # penetration later.  See _snap_matrices_to_ground for why the solver
        # leaves pieces both buried and hovering.
        snap = ({"clamped": 0, "seated": 0, "max_lift": 0.0} if not snap_ground
                else _snap_matrices_to_ground(alive, matrices, float(ground_z)))

        # Drop the rigid body world BEFORE writing keys: while it exists the
        # solver keeps overriding object transforms and the keys do not stick.
        if scene.rigidbody_world is not None:
            with _viewport_context():
                try:
                    bpy.ops.rigidbody.world_remove()
                except Exception:
                    scene.rigidbody_world = None

        # animation_data_clear() drops the location/kinematic launch keys, which
        # is intended — but it also drops the hide_viewport/hide_render keys, so
        # they MUST be rewritten below.  Without that every hero piece is
        # visible from frame 1: a static clump of shards hanging at the impact
        # point for the whole pre-crash run while other debris flies past it.
        for obj in alive:
            obj.rotation_mode = "QUATERNION"
            obj.animation_data_clear()
            launch = obj.get(LAUNCH_PROP)
            _key_visibility(obj, int(launch) if launch is not None else frame_start)

        for i, f in enumerate(range(frame_start, frame_end + 1)):
            row = matrices[i]
            for j, obj in enumerate(alive):
                mat = row[j]
                if obj.parent:
                    mat = (obj.matrix_parent_inverse.inverted()
                           @ obj.parent.matrix_world.inverted() @ mat)
                obj.location = mat.to_translation()
                quat = mat.to_quaternion()
                prev = obj.rotation_quaternion
                # Keep successive quaternions on the same hemisphere, otherwise
                # interpolation takes the long way round and the shard spins
                # wildly between two visually identical orientations.
                obj.rotation_quaternion = -quat if prev.dot(quat) < 0.0 else quat
                obj.keyframe_insert("location", frame=f)
                obj.keyframe_insert("rotation_quaternion", frame=f)

        # The bake writes a key on EVERY frame, so the curve between two keys
        # should be a straight line — there is no gap to interpolate across.
        # Blender's default Bezier handles still fit a curve through them, and
        # auto-clamped handles OVERSHOOT wherever the sampled motion changes
        # direction sharply: exactly what a bouncing shard does at each contact.
        # The result is debris that visibly dips through the ground and pops back
        # on impact frames, and jitters while resting.  LINEAR reproduces the
        # simulation exactly and is cheaper to evaluate.
        for obj in alive:
            _linearise(obj, "location")
            _linearise(obj, "rotation_quaternion")

        scene.frame_set(frame_orig)

    return {"baked": len(alive), "frames": frame_end - frame_start + 1,
            "ground_clamped": snap["clamped"], "ground_seated": snap["seated"],
            "ground_max_lift": snap["max_lift"]}


#: Collection the baked particle meshes live in.  A child of the main debris
#: collection so :func:`clear_debris` and :func:`debris_objects` (retime) cover
#: them without extra wiring.
PARTICLE_BAKED_COLLECTION = "BeamNG Debris Particles"

#: Custom property stamped on every baked particle object.  The verify scripts
#: use it to tell a frozen chip (no particle system) from a hero shard.
PARTICLE_BAKED_PROP = "_beamng_debris_baked_particle"


def _particle_ground_lift(co: np.ndarray, zrows: np.ndarray,
                          ground_z: float) -> np.ndarray:
    """Per-frame +Z lift that keeps ONE particle's shard on the ground.

    The per-body half of :func:`_snap_matrices_to_ground`, but for a single
    particle whose ``(frame, matrix)`` series is not aligned to the bake
    window: penetration clamp (any frame whose lowest vertex is under the plane
    comes up to it) plus the rest seat (the settled tail is translated so it
    sits exactly ON the plane instead of a solver-margin hair above it).
    ``zrows`` is that particle's stacked Z matrix rows ``(F, 4)``; ``co`` is
    its local vertices ``(V, 3)``.
    """
    n = len(zrows)
    if not n:
        return np.zeros(0, dtype=np.float64)
    lows = co @ zrows[:, :3].T + zrows[:, 3]
    lows = lows.min(axis=0)
    lift = np.zeros(n, dtype=np.float64)
    under = lows < ground_z
    lift[under] = ground_z - lows[under]
    rest_from = n
    for i in range(n - 1, 0, -1):
        if abs(lows[i] - lows[i - 1]) > _REST_EPS:
            rest_from = i
            break
    else:
        rest_from = 0
    if rest_from < n:
        seat = ground_z - lows[rest_from]
        lift[rest_from:] = seat
        blend = min(_SEAT_BLEND, rest_from)
        if blend > 0 and rest_from < n:
            ramp = np.linspace(0.0, 1.0, blend + 1)[:-1]
            head = slice(rest_from - blend, rest_from)
            lift[head] = np.maximum(lift[head], lift[rest_from] * ramp)
    # THE GUARANTEE.  The rest seat translates a settled tail so its REST frame
    # sits exactly on the plane, but a resting body can still wander up to
    # _REST_EPS off that frame — so the seat can overshoot a neighbouring frame
    # a fraction of a millimetre into the ground.  Clamp once more against the
    # FINAL lows so NO frame of ANY particle is ever under the plane.
    final = lows + lift
    below = final < ground_z
    if below.any():
        lift[below] = ground_z - lows[below]
    return lift


def bake_particles(emitters: Sequence["bpy.types.Object"],
                   frame_start: int, frame_end: int,
                   ground_z: float = 0.0, snap_ground: bool = True) -> dict:
    """Freeze the fine-particle emitters into ground-clamped F-curve meshes.

    WHY THIS IS THE ONLY WAY TO KEEP PARTICLES OFF THE GROUND.  A NEWTON
    particle collides as a SPHERE and the solver rests the sphere's centre a
    radius above the deflector.  The flat shard drawn around that centre
    reaches LESS than a radius downward on a face landing but MORE on an edge
    landing, so no matter how the collision radius / deflector-drop percentiles
    are tuned some settled pieces poke through the ground (measured: 36% of
    instances with a vertex below z=0, worst 98 mm).  The two error directions
    are tied to the same number: a drop large enough to hide the biggest shard
    leaves every small chip floating by the same amount.  The only guarantee
    that NO vertex ever sits under the ground is to stop the solver from owning
    the transforms: bake each particle into its own mesh and clamp, mirroring
    :func:`_snap_matrices_to_ground` on the hero bodies.

    Baking also lifts the particle limitation in ``debris_retime``: the frozen
    chips are ordinary F-curve objects, so the live fps/start sliders slow the
    spray down with the car like the hero shards.

    HOW.  Each emitter is walked FRAME BY FRAME (jumping the playhead reads
    garbage from the NEWTON integrator — measured to z=-1e27), and every live
    particle's world matrix is sampled from the evaluated depsgraph.  A particle
    keeps the template the solver picked for it, so the baked geometry is
    bit-identical to what the system would have rendered.  Each particle's
    series is then penetration-clamped and rest-seated, written as
    location/rotation keyframes with LINEAR interpolation (auto-clamped Bezier
    overshoots at every bounce), and the emitters plus their per-material
    deflectors are deleted.  The result is inert animation data, like the hero
    bake.

    Returns a summary for the operator report.
    """
    if bpy is None or not emitters:
        return {"baked": 0}
    scene = bpy.context.scene
    alive = [o for o in emitters if o.name in bpy.data.objects]
    alive = [o for o in alive if any(
        getattr(m, "particle_system", None) is not None for m in o.modifiers)]
    if not alive:
        return {"baked": 0}
    frame_end = int(frame_end)
    frame_orig = scene.frame_current

    names = {o.name for o in alive}
    starts: List[int] = []
    for o in alive:
        for m in o.modifiers:
            ps = getattr(m, "particle_system", None)
            if ps is not None:
                starts.append(int(ps.settings.frame_start))
    walk_start = max(scene.frame_start,
                     (min(starts) if starts else int(frame_start)) - 2)

    # emitter -> particle index -> [(frame, float32 (4,4) matrix)].
    # The index (depsgraph persistent_id[0]) is stable across frames; the
    # template is whatever the solver assigned at birth.  Matrices are pulled
    # off the depsgraph instance immediately and stored by VALUE — keeping the
    # instance reference across evaluations reads as "StructRNA removed".
    series: Dict[str, Dict[int, List[Tuple[int, np.ndarray]]]] = {}
    sources: Dict[str, Dict[int, str]] = {}

    with _frozen_handlers():
        for f in range(walk_start, frame_end + 1):
            scene.frame_set(f)
            bpy.context.view_layer.update()
            dg = bpy.context.evaluated_depsgraph_get()
            for inst in dg.object_instances:
                if not inst.is_instance:
                    continue
                parent = inst.parent
                if parent is None or parent.name not in names:
                    continue
                pid = int(inst.persistent_id[0])
                series.setdefault(parent.name, {}).setdefault(pid, []).append(
                    (f, np.asarray(inst.matrix_world,
                                   dtype=np.float32).copy()))
                sources.setdefault(parent.name, {}).setdefault(
                    pid, inst.object.name)

    coll = _get_collection(
        PARTICLE_BAKED_COLLECTION,
        parent=_get_collection(DEBRIS_COLLECTION))
    baked_total = 0
    clamped = seated = 0
    max_lift = 0.0
    for ename in sorted(series):
        for pid in sorted(series[ename]):
            mats = series[ename][pid]
            src = bpy.data.objects.get(sources[ename].get(pid, ""))
            if src is None or src.data is None or not mats:
                continue
            co = _local_verts(src)
            if co is None:
                continue
            frames = np.asarray([m[0] for m in mats], dtype=np.int64)
            mat4 = np.asarray([m[1] for m in mats], dtype=np.float64)
            lift = (_particle_ground_lift(co, mat4[:, 2], float(ground_z))
                    if snap_ground
                    else np.zeros(len(mats), dtype=np.float64))
            if (lift > 0.0).any():
                clamped += 1
            # The rest seat can be negative (seat down) and only moves the
            # settled tail, so count it from the seat region, not the lift.
            # max_lift reports the magnitude of the biggest correction.
            if np.abs(lift).max() > 0.0:
                max_lift = max(max_lift, float(np.abs(lift).max()))
            if len(lift) and abs(lift[-1]) > _REST_EPS:
                seated += 1

            tag = ename.replace("emit_", "", 1) if "emit_" in ename else ename
            baked = bpy.data.objects.new(f"{tag}_part_{pid}", src.data)
            baked.scale = (1.0, 1.0, 1.0)
            baked[PARTICLE_BAKED_PROP] = True
            baked.rotation_mode = "QUATERNION"
            baked.animation_data_clear()
            coll.objects.link(baked)

            birth = int(frames[0])
            death = int(frames[-1])
            baked.hide_render = True
            baked.hide_viewport = True

            # Build the action completely, THEN bind it to the object.  An
            # action that gains F-curves after it is assigned does not drive
            # the object: the depsgraph binds the property→fcurve mapping at
            # assignment time and never sees the later curves.
            action = bpy.data.actions.new(f"{baked.name}__action")

            # Visibility: hidden until its birth frame, hidden again after its
            # death (particles are deleted when their lifetime expires, so a
            # baked chip must blink out the same way).
            vis_keys = [(birth - 1, 1.0), (birth, 0.0)]
            if death < frame_end:
                vis_keys.append((death + 1, 1.0))
            vis_fcs = []
            for data_path in ("hide_render", "hide_viewport"):
                fc = action.fcurves.new(data_path=data_path)
                fc.keyframe_points.add(len(vis_keys))
                for k, (fr, val) in enumerate(vis_keys):
                    kp = fc.keyframe_points[k]
                    kp.co.x = float(fr)
                    kp.co.y = float(val)
                    kp.interpolation = "LINEAR"
                vis_fcs.append(fc)

            # Transform keyframes written straight into the F-curves rather than
            # via keyframe_insert: ~3M keys across the scene, and the operator
            # round-trip would make the bake minutes longer.
            nf = len(frames)
            loc_fcs = [action.fcurves.new(data_path="location", index=i)
                       for i in range(3)]
            quat_fcs = [action.fcurves.new(data_path="rotation_quaternion",
                                           index=j) for j in range(4)]
            for fc in loc_fcs + quat_fcs:
                fc.keyframe_points.add(nf)
            prev_q = None
            for i, f in enumerate(frames):
                m = mat4[i]
                m[2][3] += lift[i]
                mat = mathutils.Matrix(m)
                loc = mat.to_translation()
                q = mat.to_quaternion()
                if prev_q is not None and prev_q.dot(q) < 0.0:
                    q = -q
                prev_q = q
                for idx, fc in enumerate(loc_fcs):
                    kp = fc.keyframe_points[i]
                    kp.co.x = float(f)
                    kp.co.y = float(loc[idx])
                    kp.interpolation = "LINEAR"
                for idx, fc in enumerate(quat_fcs):
                    kp = fc.keyframe_points[i]
                    kp.co.x = float(f)
                    kp.co.y = float(q[idx])
                    kp.interpolation = "LINEAR"
            for fc in vis_fcs + loc_fcs + quat_fcs:
                fc.update()
            baked.animation_data_create().action = action
            baked_total += 1

    # The emitters are dead once their particles are frozen, and their
    # per-material deflectors have no systems left to deflect for.
    for o in alive:
        if o.name in bpy.data.objects:
            bpy.data.objects.remove(o, do_unlink=True)
    prefix = PARTICLE_GROUND_NAME + "_"
    for o in list(bpy.data.objects):
        if o.name.startswith(prefix):
            bpy.data.objects.remove(o, do_unlink=True)

    scene.frame_set(frame_orig)
    return {"baked": baked_total, "frames": frame_end - walk_start + 1,
            "ground_clamped": clamped, "ground_seated": seated,
            "ground_max_lift": max_lift}
