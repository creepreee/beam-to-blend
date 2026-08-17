"""1000-shard live diagnostic — Bullet simulation WITHOUT baking.

Creates 150 hero rigid-body shards + 850 fine NEWTON particles.
Uses production shard geometry from debris_shards.py.
Applies known, aggressive, deterministic launch velocities.
Logs per-shard trajectory data.
Does NOT bake — user visually inspects the live simulation.

Run from Blender:
    blender --background --python tests/diagnostic_live_sim.py
Or run interactively from the viewport via the Blender MCP.
"""

from __future__ import annotations

import sys
import os
import time

import numpy as np

try:
    import bpy
    import mathutils
except ImportError:
    print("This script must run inside Blender.")
    sys.exit(1)

# Ensure the project root is on sys.path so runtime modules resolve.
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from runtime.debris_shards import (
    FractureProfile,
    FRACTURE_PROFILES,
    profile_for,
    build_shard_geometry,
    _mesh_from_arrays,
    fallback_material,
    sample_surface_patches,
    _triangle_areas,
)
from runtime.debris_physics import (
    _ensure_ground,
    _ensure_rigidbody_world,
    _configure_ground_rigidbody,
    _verify_ground_alignment,
    link_ground_to_rigidbody_world,
    configure_rigidbody,
    _bounce_params,
    _local_verts,
    LAUNCH_FRAMES,
    GROUND_CLEARANCE,
    GROUND_NAME,
    DEBRIS_COLLECTION,
    SHARD_COLLECTION,
    LAUNCH_PROP,
)
from runtime.debris_spawn import (
    DebrisSettings,
    _cone_directions,
    _lowest_point_offset,
    _linearise,
    _key_visibility,
)

# ---------------------------------------------------------------------------
# Diagnostic parameters
# ---------------------------------------------------------------------------

HERO_COUNT = 150
FINE_COUNT = 850
GROUND_Z = 0.0
GROUND_THICKNESS = 0.20
GROUND_SIZE = 400.0
OUTPUT_FPS = 24.0
SEED = 99999

# Material to use for the diagnostic shards.
MATERIAL = "steel"

# Launch centre — a point in world space above the ground.
LAUNCH_CENTRE = np.array([0.0, 0.0, 0.8])

# Aggressive but known launch speed range (m/s), mostly horizontal.
LAUNCH_SPEED_MIN = 3.0
LAUNCH_SPEED_MAX = 12.0

# Vertical bias — positive means upward.
LAUNCH_UP_BIAS = 4.0

# Simulation range.
FRAME_START = 1
LAUNCH_FRAME = 100  # shards launch at this frame
FRAME_END = 600

# Path for the trajectory log.
LOG_PATH = os.path.join(_REPO, "tests", "diagnostic trajectories.log")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clear_scene():
    """Remove all objects, meshes, materials, collections (except Scene Collection)."""
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for mesh in list(bpy.data.meshes):
        bpy.data.meshes.remove(mesh)
    for mat in list(bpy.data.materials):
        bpy.data.materials.remove(mat)
    for coll in list(bpy.data.collections):
        bpy.data.collections.remove(coll)
    for action in list(bpy.data.actions):
        bpy.data.actions.remove(action)
    for ps in list(bpy.data.particles):
        bpy.data.particles.remove(ps)
    # Remove rigid body world
    if bpy.context.scene.rigidbody_world is not None:
        bpy.context.scene.rigidbody_world = None


def _build_panel_surface(centre, size, n_tris):
    """Create a flat triangulated panel at the given centre, returning (verts, tris).

    The panel lies in the XY plane at the centre's Z, facing +Z.
    Used as the source surface for shard geometry generation.
    """
    half = size / 2.0
    z = centre[2]
    # Corners of a square panel.
    corners = np.array([
        [centre[0] - half, centre[1] - half, z],
        [centre[0] + half, centre[1] - half, z],
        [centre[0] + half, centre[1] + half, z],
        [centre[0] - half, centre[1] + half, z],
    ])
    # Subdivide into a grid of triangles.
    grid_n = int(np.ceil(np.sqrt(n_tris / 2)))
    step = size / grid_n
    verts_list = []
    tris_list = []
    vid = 0
    for ix in range(grid_n):
        for iy in range(grid_n):
            x0 = centre[0] - half + ix * step
            y0 = centre[1] - half + iy * step
            x1 = x0 + step
            y1 = y0 + step
            v0 = [x0, y0, z]
            v1 = [x1, y0, z]
            v2 = [x1, y1, z]
            v3 = [x0, y1, z]
            base = vid
            verts_list.extend([v0, v1, v2, v3])
            tris_list.extend([
                [base, base + 1, base + 2],
                [base, base + 2, base + 3],
            ])
            vid += 4
    return np.array(verts_list, dtype=np.float64), np.array(tris_list, dtype=np.int32)


def _generate_shard_variants(material, n_variants, seed):
    """Generate n_variants shard objects using the production generator."""
    profile = FRACTURE_PROFILES.get(material, FRACTURE_PROFILES["steel"])
    rng = np.random.default_rng(seed)

    # Build a synthetic panel surface to cut shards from.
    verts, tris = _build_panel_surface(LAUNCH_CENTRE, 6.0, n_variants * 4)

    # Sample surface patches.
    patches = sample_surface_patches(verts, tris, n_variants, rng)
    if not patches:
        raise RuntimeError("No surface patches sampled")

    mat = fallback_material(material)
    shard_coll = bpy.data.collections.new(SHARD_COLLECTION)
    bpy.context.scene.collection.children.link(shard_coll)

    objects = []
    for i, (point, normal) in enumerate(patches):
        sverts, sfaces = build_shard_geometry(point, normal, profile, rng)
        mesh = _mesh_from_arrays(f"diag_shard_{material}_{i:03d}", sverts, sfaces)
        mesh.materials.append(mat)
        for poly in mesh.polygons:
            poly.use_smooth = False
        obj = bpy.data.objects.new(f"diag_shard_{material}_{i:03d}", mesh)
        shard_coll.objects.link(obj)
        obj.hide_viewport = True
        obj.hide_render = True
        objects.append(obj)

    return objects, shard_coll, mat


def _build_ground():
    """Construct the ground slab matching the proven construction exactly."""
    settings = DebrisSettings(ground_z=GROUND_Z)
    ground = _ensure_ground(settings)
    return ground, settings


def _spawn_hero_shard(template, index, launch_frame, vel, settings, rng,
                      rb_coll):
    """Spawn one hero rigid-body shard with a known launch velocity.

    Returns (obj, launch_start, requested_vel).
    """
    profile = profile_for(MATERIAL)
    obj = template.copy()
    obj.data = template.data  # share mesh data
    obj.name = f"diag_hero_{MATERIAL}_{index:04d}"

    # Spawn position with small jitter.
    jitter = rng.normal(0.0, 0.05, 3)
    spawn_at = LAUNCH_CENTRE + jitter

    # Random rotation.
    rotation = tuple(rng.uniform(0.0, 2.0 * np.pi, 3))
    obj.rotation_euler = rotation
    s = float(rng.uniform(0.8, 1.2))
    obj.scale = (s, s, s)

    # Floor clamp.
    floor = settings.ground_z + GROUND_CLEARANCE - _lowest_point_offset(
        template, rotation, s)
    if spawn_at[2] < floor:
        spawn_at[2] = floor

    # Link into scene collection (phase 1).
    bpy.context.scene.collection.objects.link(obj)

    # Velocity — the known launch vector.
    vel = np.asarray(vel, dtype=np.float64)
    dt = 1.0 / max(1e-6, OUTPUT_FPS)
    launch_start = launch_frame - LAUNCH_FRAMES

    # Write kinematic location keyframes.
    for k in range(LAUNCH_FRAMES + 1):
        f = launch_start + k
        loc = spawn_at + vel * dt * k
        if loc[2] < floor:
            loc[2] = floor
        obj.location = tuple(loc)
        obj.keyframe_insert("location", frame=f)
    _linearise(obj, "location")

    # Visibility.
    obj[LAUNCH_PROP] = int(launch_start)
    _key_visibility(obj, launch_start)

    # Phase 2: link into RB collection.
    bpy.context.view_layer.update()
    rb_coll.objects.link(obj)
    if obj.rigid_body is not None:
        configure_rigidbody(
            obj, settings,
            mass=profile.thickness * 90.0 * s ** 3,
            is_blast=True,
            launch_start=launch_start)

    return obj, launch_start, vel


def _spawn_fine_emitter(template, index, settings, rng, fine_coll, mat):
    """Spawn one NEWTON particle emitter for fine debris.

    Returns the emitter object.
    """
    # Tiny emitter face.
    mesh = bpy.data.meshes.new(f"diag_emit_{MATERIAL}_{index:03d}")
    r = 0.002
    mesh.from_pydata(
        [(-r, -r, 0.0), (r, -r, 0.0), (r, r, 0.0), (-r, r, 0.0)],
        [], [(0, 1, 2, 3)])
    mesh.update()

    emitter = bpy.data.objects.new(f"diag_emit_{MATERIAL}_{index:03d}", mesh)
    # Random position in a 4m radius around launch centre.
    angle = rng.uniform(0.0, 2.0 * np.pi)
    radius = rng.uniform(0.0, 4.0)
    pos = LAUNCH_CENTRE.copy()
    pos[0] += np.cos(angle) * radius
    pos[1] += np.sin(angle) * radius
    pos[2] = settings.ground_z + 0.5 + rng.uniform(0.0, 1.0)
    emitter.location = tuple(pos)
    emitter.hide_render = True
    fine_coll.objects.link(emitter)

    # Particle system.
    psys_mod = emitter.modifiers.new(name="debris", type="PARTICLE_SYSTEM")
    psys = psys_mod.particle_system
    st = psys.settings
    st.name = f"PS_diag_{MATERIAL}_{index:03d}"
    st.count = int(np.ceil(FINE_COUNT / max(1, 10)))  # ~85 per emitter
    st.frame_start = LAUNCH_FRAME
    st.frame_end = LAUNCH_FRAME + 10
    st.lifetime = FRAME_END - LAUNCH_FRAME + 60
    st.lifetime_random = 0.05
    st.emit_from = "FACE"
    st.use_emit_random = True

    # NEWTON physics.
    st.physics_type = "NEWTON"
    st.mass = 0.01
    st.particle_size = 0.02
    st.use_size_deflect = True
    st.use_multiply_size_mass = True

    # Initial velocity — outward from centre.
    emitter_dir = pos - LAUNCH_CENTRE
    n = np.linalg.norm(emitter_dir)
    if n > 1e-6:
        emitter_dir = emitter_dir / n
    else:
        emitter_dir = np.array([0.0, 0.0, 1.0])
    # Tangent + upward.
    tangent = np.array([-emitter_dir[1], emitter_dir[0], 0.0])
    tn = np.linalg.norm(tangent)
    if tn > 1e-6:
        tangent = tangent / tn
    else:
        tangent = np.array([1.0, 0.0, 0.0])
    launch_dir = tangent * 0.5 + np.array([0.0, 0.0, 0.8])
    ln = np.linalg.norm(launch_dir)
    if ln > 1e-6:
        launch_dir = launch_dir / ln
    speed = rng.uniform(1.0, 4.0)
    st.normal_factor = 0.0
    st.object_align_factor = (
        float(launch_dir[0] * speed),
        float(launch_dir[1] * speed),
        float(launch_dir[2] * speed),
    )
    st.factor_random = 2.0

    # Air drag.
    bounciness = float(np.clip(settings.bounciness, 0.0, 1.0))
    st.damping = float(np.clip(
        settings.air_drag + 0.45 * (1.0 - bounciness), 0.0, 1.0))
    st.subframes = int(max(0, settings.particle_subframes))
    st.effector_weights.gravity = 1.0
    st.use_dynamic_rotation = True

    # Render as the shard template collection.
    st.render_type = "OBJECT"
    st.instance_object = template
    st.use_rotation_instance = True
    st.use_scale_instance = True
    st.particle_size = rng.uniform(0.02, 0.08)
    st.size_random = 0.4

    # Assign collision modifier to the ground for particles.
    ground = bpy.data.objects.get(GROUND_NAME)
    if ground is not None:
        if not any(m.type == "COLLISION" for m in ground.modifiers):
            ground.modifiers.new(name="Collision", type="COLLISION")
        if getattr(ground, "collision", None) is not None:
            ground.collision.friction_factor = settings.friction
            ground.collision.permeability = 0.0
            ground.collision.damping_factor = 1.0 - 0.85 * bounciness
            ground.collision.damping_random = 0.15 * bounciness
            ground.collision.damping = 0.6
            ground.collision.thickness_outer = 0.12
            ground.collision.thickness_inner = 0.06

    return emitter


def _log_header(f):
    f.write("=" * 80 + "\n")
    f.write("DIAGNOSTIC LIVE SIMULATION — TRAJECTORY LOG\n")
    f.write(f"Hero shards: {HERO_COUNT}  Fine particles: {FINE_COUNT}\n")
    f.write(f"Ground Z: {GROUND_Z}  Ground thickness: {GROUND_THICKNESS}\n")
    f.write(f"Launch centre: {LAUNCH_CENTRE}\n")
    f.write(f"Launch frame: {LAUNCH_FRAME}  Sim range: {FRAME_START}-{FRAME_END}\n")
    f.write(f"Speed range: {LAUNCH_SPEED_MIN}-{LAUNCH_SPEED_MAX} m/s\n")
    f.write(f"Output FPS: {OUTPUT_FPS}\n")
    f.write("=" * 80 + "\n\n")


def main():
    t0 = time.time()

    scene = bpy.context.scene
    scene.frame_start = FRAME_START
    scene.frame_end = FRAME_END
    scene.frame_current = FRAME_START
    scene.render.fps = int(OUTPUT_FPS)

    print("\n" + "=" * 60)
    print("  DIAGNOSTIC LIVE SIMULATION — 1000 SHARDS")
    print("  150 hero rigid bodies + 850 NEWTON particles")
    print("  NO BAKE — inspect the live simulation")
    print("=" * 60 + "\n")

    # --- Phase 0: clear and set up -------------------------------------------
    print("[1/7] Clearing scene...")
    _clear_scene()

    settings = DebrisSettings(
        ground_z=GROUND_Z,
        bounciness=0.25,
        friction=0.72,
        scatter=0.45,
        speed=0.0,
        inherit_velocity=0.3,
        spread=55.0,
        air_drag=0.35,
        particle_subframes=10,
    )

    # --- Phase 1: ground ----------------------------------------------------
    print("[2/7] Building ground slab...")
    ground, settings = _build_ground()

    # --- Phase 2: rigid body world ------------------------------------------
    print("[3/7] Setting up rigid body world...")
    _ensure_rigidbody_world(scene, FRAME_START, FRAME_END)
    rb_coll = scene.rigidbody_world.collection

    # Link ground into the RB world.
    link_ground_to_rigidbody_world(ground, rb_coll, settings)

    # Verify ground alignment.
    try:
        ok = _verify_ground_alignment(ground, settings)
        print(f"  Ground alignment: {'PASS' if ok else 'FAIL'}")
    except RuntimeError as e:
        print(f"  Ground alignment FAIL: {e}")

    # --- Phase 3: generate shard variants -----------------------------------
    n_variants = 24
    print(f"[4/7] Generating {n_variants} shard variants ({MATERIAL})...")
    templates, shard_coll, mat = _generate_shard_variants(
        MATERIAL, n_variants, SEED)

    # Measure shard extents for logging.
    print(f"  Generated {len(templates)} templates")
    for i, tpl in enumerate(templates[:3]):
        v = _local_verts(tpl)
        if v is not None:
            ext = v.max(axis=0) - v.min(axis=0)
            print(f"  Template {i}: extents {ext[0]*1000:.1f} x {ext[1]*1000:.1f} x {ext[2]*1000:.1f} mm")

    # --- Phase 4: spawn hero rigid bodies -----------------------------------
    print(f"[5/7] Spawning {HERO_COUNT} hero rigid bodies...")
    rng = np.random.default_rng(SEED)
    hero_objects = []
    hero_data = []  # (obj, launch_start, requested_vel)

    for i in range(HERO_COUNT):
        tpl = templates[i % len(templates)]

        # Random direction in XY plane + upward bias.
        angle = rng.uniform(0.0, 2.0 * np.pi)
        horizontal_speed = rng.uniform(LAUNCH_SPEED_MIN, LAUNCH_SPEED_MAX)
        vx = np.cos(angle) * horizontal_speed
        vy = np.sin(angle) * horizontal_speed
        vz = rng.uniform(1.0, LAUNCH_UP_BIAS)
        vel = np.array([vx, vy, vz])

        obj, launch_start, req_vel = _spawn_hero_shard(
            tpl, i, LAUNCH_FRAME, vel, settings, rng, rb_coll)
        hero_objects.append(obj)
        hero_data.append((obj, launch_start, req_vel))

    print(f"  Spawned {len(hero_objects)} hero shards")

    # --- Phase 5: spawn fine particle emitters ------------------------------
    n_emitters = 10
    fine_coll = bpy.data.collections.new("Diag Fine Particles")
    bpy.context.scene.collection.children.link(fine_coll)

    print(f"[6/7] Spawning {n_emitters} fine particle emitters ({FINE_COUNT} total)...")
    emitters = []
    for i in range(n_emitters):
        tpl = templates[i % len(templates)]
        emitter = _spawn_fine_emitter(
            tpl, i, settings, rng, fine_coll, mat)
        emitters.append(emitter)
    print(f"  Spawned {len(emitters)} emitters")

    # --- Phase 6: evaluate the scene so Bullet registers everything ---------
    bpy.context.view_layer.update()
    print("  Depsgraph updated — Bullet bodies registered")

    # --- Phase 7: log initial state and trajectory plan --------------------
    log_lines = []
    import io
    log_f = io.StringIO()
    _log_header(log_f)

    log_f.write("HERO SHARD TRAJECTORY DATA\n")
    log_f.write("-" * 80 + "\n")
    log_f.write(f"{'Index':<6} {'LaunchZ':<10} {'VelX':<8} {'VelY':<8} {'VelZ':<8} "
                f"{'|Vel|':<8} {'Name'}\n")

    for i, (obj, launch_start, req_vel) in enumerate(hero_data):
        vn = float(np.linalg.norm(req_vel))
        log_f.write(
            f"{i:<6} {spawn_at_z(obj):<10.4f} "
            f"{req_vel[0]:<8.3f} {req_vel[1]:<8.3f} {req_vel[2]:<8.3f} "
            f"{vn:<8.3f} {obj.name}\n")

    log_f.write("\n" + "-" * 80 + "\n")
    log_f.write("GROUND VERIFICATION\n")
    log_f.write("-" * 80 + "\n")
    gw = _ground_world_bounds(ground)
    if gw:
        log_f.write(f"  Ground mesh world Z: [{gw[0]:.6f}, {gw[1]:.6f}]\n")
    log_f.write(f"  Ground location Z: {ground.location.z:.6f}\n")
    log_f.write(f"  Ground dimensions: {tuple(round(d, 4) for d in ground.dimensions)}\n")
    half = ground.dimensions.z / 2.0
    log_f.write(f"  BOX collider Z range: [{ground.location.z - half:.6f}, "
                f"{ground.location.z + half:.6f}]\n")
    log_f.write(f"  Expected Z range: [{GROUND_Z - GROUND_THICKNESS:.6f}, {GROUND_Z:.6f}]\n")
    log_f.write(f"  Top matches GROUND_Z: {abs(ground.location.z + half - GROUND_Z) < 1e-4}\n")

    log_f.write("\n" + "-" * 80 + "\n")
    log_f.write("RIGID BODY WORLD SETTINGS\n")
    log_f.write("-" * 80 + "\n")
    rbw = scene.rigidbody_world
    if rbw:
        log_f.write(f"  Substeps per frame: {rbw.substeps_per_frame}\n")
        log_f.write(f"  Solver iterations: {rbw.solver_iterations}\n")
        log_f.write(f"  Point cache: {rbw.point_cache.frame_start}-{rbw.point_cache.frame_end}\n")
    log_f.write(f"  Gravity: {tuple(scene.gravity)}\n")

    # Sample body config.
    if hero_objects:
        sample = hero_objects[0]
        rb = sample.rigid_body
        if rb:
            log_f.write(f"\n  Sample body config ({sample.name}):\n")
            log_f.write(f"    type: {rb.type}\n")
            log_f.write(f"    collision_shape: {rb.collision_shape}\n")
            log_f.write(f"    mass: {rb.mass:.4f}\n")
            log_f.write(f"    restitution: {rb.restitution:.4f}\n")
            log_f.write(f"    friction: {rb.friction:.4f}\n")
            log_f.write(f"    linear_damping: {rb.linear_damping:.4f}\n")
            log_f.write(f"    angular_damping: {rb.angular_damping:.4f}\n")
            log_f.write(f"    use_margin: {rb.use_margin}\n")
            log_f.write(f"    collision_margin: {rb.collision_margin:.6f}\n")
            log_f.write(f"    use_deactivation: {rb.use_deactivation}\n")
            log_f.write(f"    deactivate_linear_velocity: {rb.deactivate_linear_velocity:.4f}\n")
            log_f.write(f"    deactivate_angular_velocity: {rb.deactivate_angular_velocity:.4f}\n")

    log_f.write("\n" + "-" * 80 + "\n")
    log_f.write("POST-SIMULATION CHECKS\n")
    log_f.write("  (Run after scrubbing to FRAME_END or playing the simulation)\n")
    log_f.write("-" * 80 + "\n\n")

    log_content = log_f.getvalue()
    log_lines.append(log_content)

    # Write initial log.
    with open(LOG_PATH, "w", encoding="utf-8") as f:
        f.write(log_content)
    print(f"\n  Initial log written to: {LOG_PATH}")

    # --- Done — let user play/scrub ----------------------------------------
    scene.frame_set(LAUNCH_FRAME)
    bpy.context.view_layer.update()

    elapsed = time.time() - t0
    print(f"\n{'=' * 60}")
    print(f"  SETUP COMPLETE in {elapsed:.1f}s")
    print(f"  Hero shards: {len(hero_objects)}")
    print(f"  Fine emitters: {len(emitters)}")
    print(f"  Fine particles: ~{FINE_COUNT}")
    print(f"  Total visible debris: ~{HERO_COUNT + FINE_COUNT}")
    print(f"")
    print(f"  PLAY the timeline to see the live Bullet simulation.")
    print(f"  Scrub to FRAME_END ({FRAME_END}) to see final positions.")
    print(f"  Then run the post-check (below) to log trajectory data.")
    print(f"{'=' * 60}\n")

    # --- Post-simulation trajectory log -------------------------------------
    # This runs AFTER the user has played the simulation.
    # We step through key frames and record positions.
    print("Running post-simulation trajectory scan...")
    _post_sim_log(hero_objects, hero_data, ground)


def spawn_at_z(obj):
    """Read the Z of the first location keyframe."""
    ad = obj.animation_data
    if ad and ad.action:
        for fc in ad.action.fcurves:
            if fc.data_path == "location" and fc.array_index == 2:
                if fc.keyframe_points:
                    return fc.keyframe_points[0].co.y
    return obj.location.z


def _ground_world_bounds(ground):
    """World-space (min_z, max_z) of the ground slab vertices."""
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


def _lowest_world_z(obj, frame):
    """Lowest world-space vertex Z at a given frame."""
    co = _local_verts(obj)
    if co is None:
        return None
    old_frame = bpy.context.scene.frame_current
    bpy.context.scene.frame_set(frame)
    bpy.context.view_layer.update()
    mw = np.asarray(obj.matrix_world, dtype=np.float64)
    zrow = mw[2]
    z = co @ zrow[:3] + zrow[3]
    return float(z.min())


def _post_sim_log(hero_objects, hero_data, ground):
    """After the simulation plays, log per-shard trajectory data."""
    scene = bpy.context.scene
    ground_z = GROUND_Z

    # Sample frames.
    sample_frames = [LAUNCH_FRAME, LAUNCH_FRAME + 1, LAUNCH_FRAME + 5,
                     LAUNCH_FRAME + 30, LAUNCH_FRAME + 60,
                     FRAME_END]

    lines = []
    lines.append("\n" + "=" * 80)
    lines.append("POST-SIMULATION TRAJECTORY DATA")
    lines.append("=" * 80 + "\n")

    # Per-shard log.
    lines.append(f"{'Idx':<5} {'Name':<35} "
                 f"{'f@release':<11} {'f@rel+1':<10} {'f@rel+5':<10} "
                 f"{'f@end':<10} {'minZ':<10} {'maxDist':<10} {'Status'}\n")
    lines.append("-" * 120 + "\n")

    penetrating = 0
    max_pen = 0.0

    for i, (obj, launch_start, req_vel) in enumerate(hero_data):
        # Record trajectory.
        positions = {}
        for f in sample_frames:
            scene.frame_set(f)
            bpy.context.view_layer.update()
            positions[f] = np.asarray(obj.location, dtype=np.float64)

        # Min Z across all sampled frames.
        min_z = float("inf")
        max_dist = 0.0
        launch_pos = positions.get(launch_start, positions.get(LAUNCH_FRAME))
        for f, pos in positions.items():
            if pos[2] < min_z:
                min_z = pos[2]
            dist = float(np.linalg.norm(pos - launch_pos))
            if dist > max_dist:
                max_dist = dist

        # Check final position.
        final_pos = positions.get(FRAME_END, positions.get(LAUNCH_FRAME))
        pen_depth = ground_z - min_z if min_z < ground_z else 0.0
        if pen_depth > 0.0:
            penetrating += 1
            max_pen = max(max_pen, pen_depth)

        status = "PENETRATES" if pen_depth > 0.001 else "OK"
        if pen_depth > 0.001:
            status += f" ({pen_depth*1000:.1f}mm)"

        # Check return-to-origin: did the shard end up close to launch centre?
        dist_from_launch = float(np.linalg.norm(final_pos - launch_pos))
        if dist_from_launch < 0.05 and float(np.linalg.norm(req_vel)) > 2.0:
            status += " RETURN?"

        lines.append(
            f"{i:<5} {obj.name:<35} "
            f"{positions.get(launch_start, [0,0,0])[2]:<11.4f} "
            f"{positions.get(launch_start + 1, [0,0,0])[2]:<10.4f} "
            f"{positions.get(launch_start + 5, [0,0,0])[2]:<10.4f} "
            f"{final_pos[2]:<10.4f} "
            f"{min_z:<10.4f} {max_dist:<10.4f} {status}\n")

    # Summary.
    lines.append("\n" + "-" * 80 + "\n")
    lines.append("SUMMARY\n")
    lines.append("-" * 80 + "\n")
    lines.append(f"  Total hero shards: {len(hero_objects)}\n")
    lines.append(f"  Ground-penetrating: {penetrating}\n")
    lines.append(f"  Max penetration depth: {max_pen * 1000:.1f} mm\n")

    # Ground check at final frame.
    scene.frame_set(FRAME_END)
    bpy.context.view_layer.update()
    gw = _ground_world_bounds(ground)
    if gw:
        lines.append(f"\n  Ground mesh Z range at frame {FRAME_END}: [{gw[0]:.6f}, {gw[1]:.6f}]\n")

    lines.append("\n" + "=" * 80 + "\n")
    lines.append("  SCROLL UP for per-shard data.\n")
    lines.append("  Check for 'PENETRATES' and 'RETURN?' flags.\n")
    lines.append("=" * 80 + "\n")

    # Write to file.
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.writelines(lines)

    # Also print to console.
    for line in lines:
        print(line, end="")

    print(f"\n  Full log: {LOG_PATH}")


if __name__ == "__main__":
    main()
