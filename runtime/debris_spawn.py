from __future__ import annotations

"""Spawn impact debris in the Blender scene.

Hybrid simulation, because neither approach alone is good enough:

**Hero debris** — a few dozen larger pieces per impact — are real rigid bodies.
They tumble, bounce, collide with each other and come to rest in a pile.  That
settling behaviour is what sells a crash, and particles cannot fake it.

**Fine debris** — the hundreds of small chips — are a particle system.  At that
size the eye reads the spray, not the individual piece, so the cheaper solver is
indistinguishable and keeps the scene tractable.

THE MODULE SPLIT.  This module is deliberately ONLY the *creation* layer: it
builds the shard libraries and places the debris objects in the scene (hero
rigid bodies, fine-particle emitters, glass fragments, cracked-pane materials).
It does NOT configure the solver — that lives in :mod:`debris_physics` — and it
does NOT convert anything into animation — the bake lives in :mod:`debris_bake`:

    debris_spawn  ->  debris_physics  ->  debris_bake
    (build_debris,   (solver, ground,     (bake_debris)
     spawn helpers)    rigid bodies,        native rigid-body bake)
                       particles)

:func:`build_debris` returns the spawned ``hero_objects`` / ``emitter_objects``
and the bake range; the caller hands them to :func:`debris_bake.bake_debris`.

Why the bake exists at all: the hero bodies are baked to keyframes so the
finished debris is pure animation data — Bullet owns their motion until then
and the baked F-curves are what the timeline plays back.  The fine-particle
NEWTON emitters are NOT baked: they stay live and solver-owned, and their
emission windows are re-timed by ``debris_retime`` when the playback sliders
move.  The bake runs with every frame handler detached — the add-on's
``frame_change_pre`` handler rewrites ~550K vertices per frame, and baking with
it live corrupts the point caches (the previous attempt produced caches
misaligned by ~300 frames and had to be re-baked by hand).
"""

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

import zlib

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
from .debris_physics import (
    DEBRIS_COLLECTION,
    GLASS_COLLECTION,
    GROUND_CLEARANCE,
    GROUND_NAME,
    LAUNCH_FRAMES,
    LAUNCH_PROP,
    PARTICLE_BAKED_COLLECTION,
    PARTICLE_BAKED_PROP,
    PARTICLE_GROUND_NAME,
    PARTICLE_SHARD_COLLECTION,
    SHARD_COLLECTION,
    DebrisSettings,
    _bounce_params,
    _disable_ground_particle_collision,
    _ensure_ground,
    _ensure_particle_ground,
    _ensure_particle_templates,
    _ensure_rigidbody_world,
    _frozen_handlers,
    _get_collection,
    _key_visibility,
    _linearise,
    _local_verts,
    _safe_name,
    _viewport_context,
    configure_glass_rigidbody,
    configure_particle_physics,
    configure_rigidbody,
    link_ground_to_rigidbody_world,
)

try:  # pragma: no cover - only inside Blender
    import bpy
    import mathutils
except ImportError:  # pragma: no cover
    bpy = None
    mathutils = None


def _stable_hash(text: str) -> int:
    """Deterministic cross-session string hash.

    Python's built-in ``hash()`` is randomized per process (PYTHONHASHSEED),
    so a seed derived from it rebuilds a *different* scene every Blender
    session.  crc32 is stable everywhere, which is what "a given scene always
    rebuilds identically" actually requires.
    """
    return zlib.crc32(text.encode("utf-8", "surrogatepass"))


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

    # Safety: remove any leftover debris emitters that might not have been
    # cleaned up by a previous interrupted build.
    for obj in list(bpy.data.objects):
        if obj.name.startswith("debris_emit_"):
            bpy.data.objects.remove(obj, do_unlink=True)
            removed += 1

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

        # Increase spawn jitter significantly: 4.5cm was too small, pieces overlapped
        # at birth and got stuck together.  30-40cm gives proper initial separation
        # so pieces don't collide and stick at birth.
        jitter = rng.normal(0.0, 0.35, 3)
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
        # so pieces don't fall as a tight cluster.  Increased significantly:
        # at default scatter=0.45, severity≈0.5 → scatter≈0.315.
        # Need much stronger kick (5-8×) to overcome clumping from single-point radial spawn.
        base_sep = scatter * float(rng.uniform(5.0, 8.0))
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
    # The registration itself happens on the next evaluation, which reads the
    # phase-1 world matrix — not origin.  Mass is derived from the material
    # profile and the piece's scale here (the caller owns the profile lookup);
    # the actual body configuration — kinematic keys, body type, collision
    # shape, restitution/damping, deactivation — is delegated to
    # configure_rigidbody in debris_physics.
    bpy.context.view_layer.update()
    with _viewport_context():
        for obj, launch_start, s, is_blast in placed:
            rb_coll.objects.link(obj)
            if obj.rigid_body is None:
                # Linking into the rigid body world collection does NOT create
                # a rigid_body component — only bpy.ops.rigidbody.object_add()
                # does.  Without this the piece has no Bullet physics and sits
                # at its spawn position forever (the "stuck in air" bug).
                bpy.context.view_layer.objects.active = obj
                bpy.ops.rigidbody.object_add(type="ACTIVE")
            if obj.rigid_body is None:
                continue
            configure_rigidbody(
                obj, settings,
                mass=profile.thickness * 90.0 * s ** 3,
                is_blast=is_blast,
                launch_start=launch_start)

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

    The NEWTON solver settings, the collision radius (from the pre-scaled
    templates), the sunk per-material deflector and the drag/rotation physics
    are configured by :func:`debris_physics.configure_particle_physics`; this
    function owns the EMISSION (count, window, lifetime, velocities) and the
    RENDER (which instanced collection, rotation instancing) side.
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

    # NEWTON solver + collision physics: physics type, mass, the collision
    # radius carried by ``particle_size`` (from the pre-scaled templates), the
    # sunk per-material deflector, drag, subframes, radius deflection and
    # dynamic rotation.  Returns the pre-scaled template collection the RENDER
    # side needs below.
    pcoll = configure_particle_physics(
        st, event, settings, particle_templates,
        mass=profile.thickness * 40.0)

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
    # Scatter velocity: scale aggressively with settings.scatter so that
    # scatter=5 gives a massive random velocity kick, while scatter=0.45
    # remains gentle.  The old linear mapping capped at ~3.5 m/s even at
    # scatter=5; now it scales quadratically so scatter=5 gives ~50 m/s
    # random velocity, enough to overcome radial clumping.
    base_scatter = max(0.0, float(settings.scatter))
    intensity_factor = 0.4 + 0.6 * intensity
    # Quadratic scaling: scatter=0.45 -> ~0.2, scatter=1.0 -> ~1.0, scatter=5.0 -> ~25
    effective_scatter = base_scatter * base_scatter * intensity_factor
    # Floor: even at low scatter, ensure minimum separation
    effective_scatter = max(effective_scatter,
                            settings.min_launch_speed * intensity_factor)
    st.factor_random = (effective_scatter
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

    # Hide the emitter quad until the impact, so it does not sit as a tiny
    # fixed square at each impact point from frame 1 (measured: 121 quads
    # visible at frame 1).  Emission itself was always gated by frame_start;
    # this hides the emitter OBJECT.
    #
    # BOTH hide_viewport AND hide_render are keyed (not just viewport).  An
    # emitter with ``hide_render = True`` suppresses its instanced particles in
    # the FINAL render even though they show in the viewport — the collection
    # instances are evaluated from the depsgraph, which drops render-hidden
    # emitters, so the emitted debris vanished from final output.  Keying
    # hide_render to 0 from the spawn frame keeps the particles in real renders.
    #
    # It is re-hidden once every particle is dead so it does not linger after
    # the spray settles.  With the lifetime now running past the end of the
    # scene this key lands beyond the timeline and never fires, which is
    # correct: hiding the emitter drops it out of the depsgraph and takes its
    # instanced particles with it, so re-hiding it while any chip is still on
    # the ground would delete the settled debris. The key is kept for the case
    # where a short settle window genuinely does outlive the spray.
    end_hide = int(spawn_frame) + 2 + int(st.lifetime) + 1
    for attr in ("hide_viewport", "hide_render"):
        setattr(emitter, attr, True)
        emitter.keyframe_insert(attr, frame=spawn_frame - 1)
        setattr(emitter, attr, False)
        emitter.keyframe_insert(attr, frame=spawn_frame)
        setattr(emitter, attr, True)
        emitter.keyframe_insert(attr, frame=end_hide)

    return emitter


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
                      material: Optional["bpy.types.Material"],
                      rng: np.random.Generator,
                      output_fps: float = 24.0,
                      frame_end: int = 0,
                      rb_coll: Optional["bpy.types.Collection"] = None,
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

    # Compute pane normal from fragment centres (all lie on the pane plane).
    centres = np.array([f.centre for f in fragments], dtype=np.float64)
    centred = centres - centres.mean(axis=0)
    _, _, vt = np.linalg.svd(centred.T @ centred)
    pane_normal = vt[2]  # smallest singular vector = plane normal
    if pane_normal @ np.array(event.velocity, dtype=np.float64) < 0:
        pane_normal = -pane_normal  # point outward from car

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
        dynamic.append(obj)

        # Disable auto-keying for this object's animation.
        obj.animation_data_clear()

        # Launch frame parameters.
        launch_start = spawn_frame - LAUNCH_FRAMES
        death = frame_end  # glass lives until scene end
        low_off = _lowest_point_offset(obj, (0.0, 0.0, 0.0), 1.0)
        floor = settings.ground_z + GROUND_CLEARANCE - low_off
        # Position jitter: offset start SIGNIFICANTLY along pane plane to prevent clumping.
        # Was 3cm, now 0.5-2.0m to ensure fragments start separated.
        jitter_scale = float(rng.uniform(0.5, 2.0))
        jitter_dir = np.array([float(rng.uniform(-1, 1)),
                               float(rng.uniform(-1, 1)), 0.0])
        jitter_dir = jitter_dir / (np.linalg.norm(jitter_dir) + 1e-9)
        base = np.array(frag.centre, dtype=np.float64) + jitter_dir * jitter_scale
        # Clamp the initial position so the fragment's lowest vertex sits
        # above ground.  Without this, fragments spawned on the lower half of
        # a glass pane extend below ground_z from birth and the bake captures
        # those positions.
        if base[2] < floor:
            base[2] = floor
        obj.location = tuple(float(c) for c in base)

        # Compute velocity: outward from impact, with jitter, forced downward.
        blow = float(np.exp(-2.4 * frag.impact_distance))
        away = np.array(frag.centre, dtype=np.float64) - np.array(event.position, dtype=np.float64)
        n = np.linalg.norm(away)
        away = away / n if n > 1e-6 else np.array((0.0, 0.0, 1.0))

        # Direction jitter: rotate away around pane_normal (±60°) and tilt (±30°).
        # Increased from ±35°/±15° to ensure real angular spread.
        theta = float(rng.uniform(-1.05, 1.05))  # ±60°
        c, s = np.cos(theta), np.sin(theta)
        away_rot = (away * c + np.cross(pane_normal, away) * s +
                    pane_normal * (pane_normal @ away) * (1 - c))
        tilt = float(rng.uniform(-0.52, 0.52))  # ±30°
        away_rot = away_rot * np.cos(tilt) + pane_normal * np.sin(tilt)
        away = away_rot / np.linalg.norm(away_rot)

        scatter = max(0.0, float(settings.scatter)) * (0.4 + 0.6 * intensity)
        # Increased base_sep multiplier to ensure fragments separate properly at high scatter values
        base_sep = scatter * float(rng.uniform(12.0, 25.0))
        launch_speed = (settings.speed * profile_for("glass").speed_bias
                        * (0.3 + 1.4 * intensity))
        vel = away * (launch_speed + base_sep) * blow * float(rng.uniform(0.55, 1.4))
        vel = vel + part_vel * settings.inherit_velocity * intensity
        vel[2] = min(vel[2], -3.0)  # force downward more aggressively

        # Horizontal velocity (XY only; Z forced to -3 m/s).
        vel_xy = vel[:2].copy()
        dt = 1.0 / max(1e-6, float(output_fps))

        # Build the action manually - no keyframe_insert at all.
        import uuid
        action = bpy.data.actions.new(f"{obj.name}__action_{uuid.uuid4().hex[:8]}")

        # Visibility: hidden until launch frame, then visible.
        birth = launch_start
        death = frame_end
        vis_keys = [(birth - 1, 1.0), (birth, 0.0)]
        if death < frame_end:
            vis_keys.append((death + 1, 1.0))
        for data_path in ("hide_render", "hide_viewport"):
            fc = action.fcurves.new(data_path=data_path)
            fc.keyframe_points.add(len(vis_keys))
            for k, (fr, val) in enumerate(vis_keys):
                kp = fc.keyframe_points[k]
                kp.co.x = float(fr)
                kp.co.y = float(val)
                kp.interpolation = "LINEAR"

        # Launch keyframes with forced downward velocity.
        nf = LAUNCH_FRAMES + 1
        loc_fcs = [action.fcurves.new(data_path="location", index=i) for i in range(3)]
        for fc in loc_fcs:
            fc.keyframe_points.add(nf)
        for k in range(nf):
            f = launch_start + k
            loc = base.copy()
            loc[:2] += vel_xy * dt * k
            loc[2] += -2.0 * dt * k
            if loc[2] < floor:
                loc[2] = floor
            for idx, fc in enumerate(loc_fcs):
                kp = fc.keyframe_points[k]
                kp.co.x = float(f)
                kp.co.y = float(loc[idx])
                kp.interpolation = "LINEAR"
        for fc in action.fcurves:
            fc.update()
        obj.animation_data_create().action = action
        placed.append((obj, launch_start))

    # Glass used to stop being simulated after its three launch keys.  That
    # made the scatter velocity effectively meaningless after the launch: the
    # object simply held its last keyframe for the rest of the shot.  Hand the
    # fragment from kinematic launch motion to Bullet, then let bake_debris
    # freeze the real trajectory and ground-snap the final result.
    if rb_coll is not None and placed:
        bpy.context.view_layer.update()
        with _viewport_context():
            for obj, launch_start in placed:
                if obj.name not in rb_coll.objects:
                    rb_coll.objects.link(obj)
                if obj.rigid_body is None:
                    bpy.context.view_layer.objects.active = obj
                    bpy.ops.rigidbody.object_add(type="ACTIVE")
                if obj.rigid_body is None:
                    continue
                configure_glass_rigidbody(
                    obj, settings, launch_start,
                    mass=profile_for("glass").thickness * 70.0)

    # Glass is now a genuine rigid-body launch/simulation path.  bake_debris
    # converts it to ordinary F-curves, so the final scene contains no live
    # Bullet state and the frame playback handler cannot move it underneath the
    # ground after the bake.
    #
    # The original implementation intentionally left glass out of Bullet, but
    # that was only safe if the launch motion itself continued for the whole
    # shot.  Three keyframes do not provide that continuation.

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
    """Create shard libraries and spawn the debris into the scene.

    Returns a summary dict for the operator report, including the spawned
    ``hero_objects`` / ``emitter_objects`` and the ``bake_start`` / ``bake_end``
    range.  The caller passes those to :func:`debris_bake.bake_debris` to bake
    the finished simulation into native F-curves.
    """
    if bpy is None:
        raise RuntimeError("build_debris requires Blender (bpy)")

    settings = settings or DebrisSettings()
    glass_settings = glass_settings or GlassSettings()
    crack_settings = crack_settings or GlassCrackSettings()
    scene = bpy.context.scene
    rng = np.random.default_rng(settings.seed)

    # Disable auto-keying during the build to prevent spurious keyframes
    # from being inserted when object locations are set during spawn.
    ts = scene.tool_settings
    auto_key_was_on = ts.use_keyframe_insert_auto
    ts.use_keyframe_insert_auto = False

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
    # Pre-scaled particle templates, built lazily and memoised here.
    # All particles now collide with the single main ground (Simply Shatter
    # approach) — no per-material particle grounds.
    particle_cache: Dict[str, Tuple] = {}

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
                glass_settings, glass_coll,
                _glass_material_for(part, event.material, source_objects),
                rng, output_fps=output_fps, frame_end=scene.frame_end,
                rb_coll=rb_coll)
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
        link_ground_to_rigidbody_world(ground, rb_coll, settings)

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
# Restore auto-keying setting
    ts.use_keyframe_insert_auto = auto_key_was_on
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


# ---------------------------------------------------------------------------
# Re-exports (backward compatibility)
# ---------------------------------------------------------------------------
# Names that moved to :mod:`debris_physics` but are re-exported here so
# existing ``from runtime.debris_spawn import ...`` call sites keep working:
# the shared constants (DEBRIS_COLLECTION, SHARD_COLLECTION, GROUND_NAME,
# GLASS_COLLECTION, LAUNCH_FRAMES, GROUND_CLEARANCE, LAUNCH_PROP,
# PARTICLE_GROUND_NAME, PARTICLE_SHARD_COLLECTION, PARTICLE_BAKED_COLLECTION,
# PARTICLE_BAKED_PROP), the DebrisSettings dataclass, and the generic helpers
# (_safe_name, _local_verts, _linearise, _key_visibility, _bounce_params,
# _viewport_context, _frozen_handlers, _get_collection).
#
# ``bake_debris`` is deliberately NOT re-exported: it lives in
# :mod:`debris_bake` now, and call sites have been updated to import it from
# there.
