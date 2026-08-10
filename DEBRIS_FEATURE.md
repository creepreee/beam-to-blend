# BeamNG Debris Feature — Design, Architecture & Current State

> Detailed write-up of the impact-debris feature for the BeamNG Cache Importer.
> Explains how debris is shaped from the **real part geometry without ever
> touching the visual (animated) geometry**, how it is spawned, and exactly
> where the feature stands right now.

---

## 1. The core guarantee

**The car's animated geometry is NEVER modified.**

This is not an accident — it is the central constraint of the whole feature.
The BeamNG Cache Importer plays back a crash by rewriting ~550K mesh vertices
every frame from a `frame_change_pre` handler. Editing that mesh in any way
(particle systems on it, vertex sculpts, boolean modifiers) corrupts the
playback — the sequence crashes, the car's animation dies.

The debris system sidesteps this entirely:

| Constraint | How it is satisfied |
|---|---|
| No particle system on the animated mesh | Emitters are **new standalone objects** in their own collection |
| No vertex edits to the car | Debris geometry is cut from the **BVC cache file** (a read), not from live mesh data |
| No modifiers on the car | Rigid bodies, particle systems, MESH_CACHE all live on **new objects** |
| Animation stays intact during bake | `_frozen_handlers` detaches every frame handler for the duration of a bake |

---

## 2. How debris is shaped — "cookie-cutter from the real surface"

### 2.1 The idea

Never model debris from scratch. A generic cube/icosphere never reads as "that
bumper just exploded" — the eye reads the silhouette long before the physics.
Instead, every shard is **punched out of the part's own surface**, so it
inherits the real curvature, real panel thickness, and real material.

### 2.2 Where the geometry comes from

In `build_debris` (`runtime/debris_spawn.py`):

```python
verts = reader.frame_positions(event.part, event.cache_frame)  # read from .bvc
tris  = triangulate_indices(reader.base_indices(event.part))   # read from .bvc
```

- `reader.frame_positions()` — the part's **vertex positions** at the frame the
  part failed, read straight out of the BVC cache.
- `reader.base_indices()` — the part's **triangle indices**, read from the cache.

The live Blender meshes are never read for geometry. The only use of the live
scene is **material lookup** (`_resolve_source_material`) so shards carry the
car's own material slot.

### 2.3 Step-by-step shaping algorithm

All in `runtime/debris_shards.py` (pure numpy, no bpy in the geometry path):

1. **Area-weighted surface sampling** (`sample_surface_patches`)
   Picks `count` spawn points across the part's triangles, weighted by triangle
   area. Area weighting matters: uniform-by-triangle sampling clusters shards
   wherever the mesh is densely tessellated (bolt holes, trim) instead of
   spreading them over the panel. Returns `(point, normal)` pairs.

2. **Per-material fracture profile** (`FRACTURE_PROFILES`)

   | material | size (m) | thickness (m) | elongation | jitter | count bias | speed bias | reads as |
   |---|---|---|---|---|---|---|---|
   | glass | 0.012–0.055 | 0.004 | 3.6 | 0.45 | 2.4 | 1.35 | long angular splinters, thin, wide spread |
   | plastic | 0.025–0.110 | 0.010 | 1.8 | 0.40 | 1.3 | 1.00 | medium irregular ABS chunks, torn edge |
   | paint | 0.030–0.130 | 0.008 | 1.5 | 0.35 | 1.0 | 0.90 | sheet-metal flakes: broad, thin, slightly bent |
   | chrome | 0.015–0.070 | 0.005 | 2.6 | 0.40 | 0.8 | 1.10 | small bright slivers of trim |
   | steel | 0.025–0.095 | 0.014 | 1.4 | 0.30 | 0.7 | 0.80 | compact heavy fragments |
   | rubber | 0.030–0.100 | 0.018 | 1.2 | 0.25 | 0.5 | 0.65 | chunky blocks, barely spread |

   Materials do not break alike, so the fracture parameters are per material.

3. **Per-shard geometry** (`build_shard_geometry`)
   For each sampled `(point, normal)`:
   - Build an orthonormal basis `(u, v)` in the tangent plane of the surface.
   - Generate `n_side` (3–6) random angles, sorted, with each radius jittered
     by the material's `jitter` — so no two shards share a silhouette.
   - **Stretch** along one tangent axis by `elongation` and compress along the
     other. This is what makes glass read as splinters rather than confetti.
   - Apply a random in-plane rotation so elongation isn't axis-aligned across
     shards.
   - **Extrude** along the *surface* normal by half the material thickness in
     each direction → an irregular polygonal prism sitting in the panel it came
     from.
   - Centre on the origin (the object's own transform places it in the world,
     which is what both the rigid body solver and particle instancing expect).
   - **Winding is fixed so every face points OUTWARD.** The earlier version was
     watertight but inside-out (negative volume) — renders as a black hole and
     confuses convex-hull collision bounds. Verified: 0 non-manifold edges, all
     positive volumes.

4. **Fracture surface** — shards are flat-shaded by construction (reads as a
   fresh fracture). Each shard mesh reuses the part's material (or a generated
   per-material fallback PBR shader, e.g. glass gets transmission + IOR).

### 2.4 Template reuse

`build_shard_library` generates only **`variants` (default 8) template meshes**
per `(part, material)` and hides them. Those templates are then either:
- duplicated into rigid bodies (hero debris), or
- instanced by a particle system (fine debris).

Reuse keeps the scene light — a few thousand unique shard meshes would bloat
the .blend for no visible gain.

---

## 3. How debris is spawned

### 3.1 The hybrid model

Two solvers, because neither alone is enough:

| Type | Count / impact | Method | Why |
|---|---|---|---|
| **Hero debris** | ~dozens of larger pieces | **Real rigid bodies** | Tumble, bounce, collide with each other, settle in a pile. Particles cannot fake settling, and settling sells the crash. |
| **Fine debris** | hundreds of small chips | **Particle system** | At that size the eye reads the spray, not individual pieces; the cheaper solver is indistinguishable and keeps the scene tractable. |

### 3.2 Impact detection (`runtime/impact_detect.py`)

Pure numpy, no bpy — unit-testable outside Blender. Reads the BVC cache
directly (it carries strictly more info than the old side-car NPZ telemetry).

- **Deformation is the ONLY trigger.** Debris is shed when material actually
  fails, and material failure *is* shape change. Velocity/ground signals only
  annotate a deformation event (raising severity); a rigid brake disc that
  slams the ground without deforming sheds nothing.
- **Rotation-invariant deformation** via Kabsch/SVD residual
  (`kabsch_residual`). Cache-local positions remove the vehicle's rigid motion
  but not each part's own rotation (doors swing, wheels spin, the speedometer
  needle sweeps). Measured on real data: parts rotate 13–50° locally, so raw
  local motion over-reads deformation ~2× and catastrophically on parts that
  only rotate. The worst case: `needle_speedo` reads 0.73 m/frame raw but only
  0.009 m of true shape change. The Kabsch residual returns 0.0000 for every
  part in free flight and non-zero only where material actually fails.
- Each event carries a **hotspot**: the world-space weighted centroid of the
  most-displaced vertices. Debris spawns at the corner of the bumper that hit
  the kerb, not the middle of the part.
- **Severity** blends normalized deformation + speed + a bonus per confirming
  signal; a `relative_floor` drops the settling-noise tail; interior parts are
  excluded by name so debris doesn't spawn inside the cabin shell.
- Material is classified from the part name (`classify_material`) to pick the
  fracture profile and debris-count bias.

### 3.3 Spawning (`runtime/debris_spawn.py`)

`build_debris` orchestrates:

1. Filter events by `min_severity`.
2. Create `"BeamNG Debris"` + `"BeamNG Debris Shards"` collections.
3. Build the ground plane (`"BeamNG_DebrisGround"`, large passive plane).
4. Build one shard-template library per `(part, material)`.
5. Create the rigid body world, pointing it at a controlled collection, with
   high substeps/iterations (small fast shards otherwise tunnel through the
   ground).
6. For each event (sorted by severity):
   - `_blender_frame_for` maps the cache impact frame to a Blender frame using
     the live `frame_start` / `playback_fps` / `output_fps` (inverse of
     `frame_handler._cache_frame_for`).
   - **Hero pieces** (`_spawn_hero_pieces`): duplicate a template, random scale
     0.7–1.4, random spin, position at hotspot + jitter, and give it a real
     launch velocity.
   - **Fine debris** (`_spawn_fine_particles`): one quad emitter object with a
     `PARTICLE_SYSTEM` modifier instancing the shard collection. Rotation is ON
     (shards tumble in flight — the single biggest fix over the earlier dead
     attempt) and the emitter inherits the part's velocity so the spray is
     thrown *off the part*, not dropped from a fixed point.

### 3.4 Launch velocity — how a shard is "thrown"

Blender's rigid body API exposes no initial-velocity setting. The trick:
**hand velocity to the solver as motion.** A kinematic body follows its
keyframes, and when released the solver adopts the velocity it was already
travelling at.

- `LAUNCH_FRAMES = 3` frames of genuine kinematic travel before the body goes
  dynamic.
- Measured: two keyframes transfer **exactly zero** velocity — the shard is
  released at rest and drops straight down (peak rise 0.000 m). With 3 launch
  frames the same shard arcs 2.0 m up and 2.0 m downrange.
- Hero pieces are hidden until their launch frame so they don't sit in mid-air
  from frame 1.

### 3.5 Baking (`bake_debris`)

Simulates the rigid bodies and freezes the result into keyframes.

- **`_frozen_handlers`** detaches every frame handler for the duration — the
  addon's playback handler rewrites ~550K verts/frame, and baking while it is
  live produces corrupt point caches (the earlier attempt was misaligned by
  ~300 frames).
- Steps the solver **one frame at a time from the point cache's start frame** —
  Bullet integrates incrementally; jumping the playhead into the middle leaves
  the cache invalid and every body reads out at its rest position.
- Starts a few frames ahead of the earliest launch — simulating 2000 idle frames
  made the solver integrate a huge phantom velocity out of long-static
  kinematic bodies (debris shot 90 m sideways, fell to z=-96).
- Writes the F-curves itself instead of `bpy.ops.rigidbody.bake_to_keyframes`
  (that operator depends on the active keying set + VIEW_3D context and dies
  with "No suitable context info for active keying set").
- Drops the rigid body world **before** writing keys (while it exists, the
  solver overrides object transforms and the keys don't stick).
- Keeps successive quaternions on the same hemisphere so interpolation never
  takes the long way round (no wild shard spins).
- **After the bake the debris is inert animation data**: keyframes only, solver
  torn down. It cannot be re-simulated, cannot drift, and cannot be corrupted
  by the playback handler.

### 3.6 Ground collision

- The ground plane must be linked into the rigid body collection **after** the
  rigid body world exists — linking is what gives an object its
  `obj.rigid_body`. A ground created before the world has none, contributes no
  collision, and every shard drops to z=-100.
- Passive body, `MESH` collision shape, high solver substeps so small shards
  don't tunnel.

---

## 4. "Shatter glass out of the car" — implemented

`DebrisSettings.shatter_glass: bool = True` is now wired end to end. When the
toggle is on, a glass pane that reaches its shatter tier breaks out of the car
and spawns fragments, and the intact window then collapses out of the playback
so the broken opening reads correctly. This is the ONE place the debris path
writes to the car's geometry, and it is opt-in via the toggle.

Damage classification (`runtime/impact_detect.py`):
- Three monotonic tiers: `GLASS_INTACT` / `GLASS_CRACKED` / `GLASS_SHATTERED`.
- `classify_glass_damage` shatters an event when its deformation exceeds
  `GlassSettings.shatter_deform` (0.022 m) or its ground-depth exceeds
  `shatter_ground_depth` (0.03 m — a face-on tarmac hit); below that it crazes
  (crack threshold `crack_deform`, 0.006 m).
- `resolve_glass_damage` collapses every event per pane to the worst tier
  reached, keeping the **first** shatter frame (glass damage is irreversible).

Spawn side (`build_debris` → `_spawn_glass_pane`):
- Pane cache-local verts are mapped to world space (`local_to_world` with the
  pane's frame transform + ground shift) before meshing.
- Dynamic fragments spawn as camera-facing quad wedges with a glass material,
  get `hide_viewport`/`hide_render` keys until their launch frame, and are
  baked alongside the hero pieces (they are appended to `hero_objects`).
- A retained fringe (`edge_retain` ≈ 0.15 → ~13% of shards) stays stuck to the
  pane's owning chunk object — parented with `matrix_parent_inverse` resolved
  at the shatter frame, so it travels with the wreck instead of launching.
- `clear_debris` removes the `BeamNG Debris Glass` collection with the rest.

Playback side (`CachePlayback.set_shattered_panes`, called by the operator
after baking): from the shatter frame on, the pane's vertex range broadcasts to
its centroid, so the broken window collapses out of the car. The pane map is
persisted as the `_beamng_shattered_panes` scene prop and re-applied on reload
recovery (`frame_handler._try_recover`).

---

## 5. Current state of the feature

### 5.1 Wired, shipped and installed

The feature is fully wired and installed (verified current in the deployed
addon copy — the source tree and
`AppData\Roaming\Blender Foundation\Blender\4.5\scripts\addons\beamng_cache_importer\`
are in sync):

| Module | Lines | Status |
|---|---|---|
| `runtime/impact_detect.py` | ~677 | Detection core + glass tiers, pure numpy, **tested** (`tests/test_impact_detect.py`) |
| `runtime/debris_shards.py` | ~431 | Shard generation, numpy + thin bpy layer |
| `runtime/debris_spawn.py` | ~1276 | Spawner + bake + glass pane spawn, bpy-dependent |
| `runtime/glass_shatter.py` | ~340 | Pane fragmentation (Voronoi), retained-fringe selection |
| `runtime/mesh_update.py` | ~931 | Per-frame vertex update + shattered-pane collapse |
| `runtime/frame_handler.py` | ~626 | Timeline handler + shatter-pane persistence/recovery |
| `addon/operators.py` | ~887 | `beamng.build_debris` (line 578) + `beamng.clear_debris` (line 679) |
| `addon/ui.py` | ~576 | "Build Debris" / "Clear Debris" buttons + Glass sub-box (panel lines 477–478) |

### 5.2 Velocity units — fixed (m/s contract)

`detect_impacts` now takes `playback_fps` and emits `ImpactEvent.velocity` in
**real metres per second** (converted at the source, stride-aware). The stale
`× 24.0` / `× 60.0` assumptions were removed from `debris_spawn.py`. A consumer
that assumed "per cache frame" launched debris at ~2× speed (the 55 m/s instead
of 28 m/s bug); the contract is now explicit and pinned by the m/s unit test.

### 5.3 Realism gap unchanged

See "The honest weakness" (section 7): all shards are flat, convex,
straight-edged prisms. Reads well at a glance, betrays itself at close range.
This is a quality gap, not a correctness bug — deferred.

### 5.3 Other gap — flat convex prisms

See "The honest weakness" below: all shards are flat, convex, straight-edged
prisms. Reads well at a glance, betrays itself at close range.

---

## 6. Completion status

Everything in the old plan is done and verified:

1. ✅ **Velocity m/s contract** — converted inside `detect_impacts`, `× 24.0`
   removed from the spawner, unit test pins the contract.
2. ✅ **Operators wired** — `beamng.build_debris` (open `CacheReader` → `detect_impacts`
   → `build_debris` → `bake_debris`, feeding live `frame_start` / `playback_fps` /
   `output_fps` and `_beamng_ground_shift`) and `beamng.clear_debris`.
3. ✅ **Settings in the UI** — Debris sub-panel with Build Debris / Clear Debris.
4. ✅ **Tests** — 81/81 `pytest`; detection unit tests cover Kabsch residual, the
   m/s contract, and the glass damage tiers / pane resolution / world-space
   `local_to_world` mapping.
5. ✅ **Built + reinstalled + verified** — rebuilt the zip, reinstalled, ran the
   full operator headless: **82–88 s** for the whole detect → spawn → bake cycle,
   **200 hero** rigid bodies, **121 emitters**, keyframes **2020 → 2750**, no
   edits to the car's animated geometry.
6. ✅ **Live-GUI confirmation** — clicked **Build Debris** in the running GUI
   on the real shot scene (`vehicle materials ready made.blend`, cache
   `name.bvc`). No hang: progress streamed to the console, the bake completed.
   Verified in the live scene: **200 hero** bodies baked to **1,153,600 F-curve
   keys over frames 2345–3168**, **121** quad emitters, ground collider present,
   rigid body world torn down (debris is inert keyframes), particle systems exist
    **only** on the 121 emitters — the car's animated geometry is untouched.
7. ✅ **Emitter quads hidden before spawn** — the 121 emitter quads used to be
   visible as tiny fixed squares at impact points from frame 1. Emitters now get
   `hide_viewport` keyframes at `spawn_frame-1` (True), `spawn_frame` (False),
   `spawn_frame + 2 + lifetime + 1` (True); `hide_render` stays True. Verified
   live: **0 visible emitter quads at frame 1**.
8. ✅ **Fine particles collide with the ground** — fine particles (NEWTON) only
   collide with objects carrying a **COLLISION** modifier; the rigid-body ground
   slab is invisible to them, so particles fell through it. `BeamNG_DebrisGround`
   now gets a `COLLISION` modifier with `friction_factor = settings.friction`,
   `damping = 0.6`, `permeability = 0.0`. Verified live after a full re-bake
   (frames 2345–3168, 200 hero): 6736 particles at rest sit ON the slab
    (`min_z -0.196` vs resting-center ≈ `surface 0.1 − radius 0.25 = −0.15`), no
    sinking. *Gotcha:* `CollisionSettings` has **no `friction`** attribute — the
    correct field is `friction_factor`.
9. ✅ **Hero pieces hidden until launch** — the 200 hero rigid bodies used to be
   visible from frame 1 as a static clump of shards hanging at the impact point
   (while launched debris moved around them). Root cause: `bake_debris` calls
   `animation_data_clear()`, which wipes the spawn-time `hide_viewport`/
   `hide_render` keys. Fix: `_key_visibility()` re-keys both after the clear,
   using `LAUNCH_PROP` (the launch frame stashed on the object at spawn),
   with CONSTANT interpolation so the piece never half-fades in. Verified live
   after re-bake: **0 of 200 heroes visible at frame 1**; 800 visibility keys
   preserved across the bake (200 × 2 fcurves × 2 keyframes).
10. ✅ **Hero pieces no longer sink through the ground** — some hero pieces
    (measured: 21 of 200) ended at z between −868 and −912, free-falling out of
    the world. Two causes, both fixed:
    - *Spawn clamp was origin-based.* Shards are centred on their origin, so
      clamping `spawn_at.z` to `ground_z + 0.005` buried up to half the body
      (templates up to 0.138 m radius × scale 1.4 ≈ 0.2 m) inside the collision
      slab; Bullet then resolved that penetration by ejecting the body downward
      through the slab. `_lowest_point_offset()` computes the real rotated
      lowest-vertex extent and the clamp now keeps that point `GROUND_CLEARANCE`
      (0.004 m) above ground.
    - *Launch path was kinematic.* The 3-frame kinematic launch ignores
      collisions, so a strong downward inherited velocity carried the shard
      straight through the ground and released it below the (single-sided)
      collider. The launch clamp now uses the same lowest-point `floor`.
    Verified live after re-bake: **0 heroes below z=−0.5**; settled z ≈ 0.10
    (the slab's top surface) for all 200.
11. ✅ **Emitter quads shrunk to near-points** — even with item 7's hide keys,
    each emitter quad (0.12 × 0.12 m) was visible as a small static "square"
    sitting at its impact point from its spawn frame until its particles died,
    while the actual shard particles flew around it — read by the user as "why
    are there squares that don't move?". The quad cannot be hidden in the
    viewport without dropping the object out of the depsgraph (which kills
    particle display entirely), and Blender 4.5 no longer has
    `ParticleSettings.show_emitter`. Fix: emit from a 0.004 × 0.004 m face
    (`r = 0.002`) — the object stays in the depsgraph so the baked particles
    still display, but the emitter is imperceptible. Spawn spread is dominated
    by velocity/jitter, not the 0.12 m face, so the impact looks identical.
    Verified live after re-bake: emitter verts are ±0.002, 121/121 particle
    systems baked, debris still rests on the slab (med z ≈ 0.115, none below
     −0.3), hero pieces animate (z 0.50 → 0.10 at launch → settle).
12. ✅ **Hero budget now allocated proportionally to severity** — the old greedy
    sort (`ordered = sorted(events, key=severity desc)`, take until the
    `max_hero_total` budget runs out) let the *first* crash swallow all 200
    pieces, so the follow-up impacts the user actually cares about — the car
    slamming onto its side (doors crushed), then landing on its back — spawned
    **zero** hero shards (only fine particles, which read as invisible). Root
    cause proven by simulation: raw demand is 1048 heroes across 121 events, so
    budget exhaustion at the first crash starved everything after. Fix:
    `build_debris` now sums every event's raw `hero_n`, scales the whole set by
    `max_hero_total / raw_total`, and rounds half-even (lines ~677–700). Budget
    default raised 200 → 240. Measured allocation: first crash 133 (still the
    dominant bang), side-impact band 2450–2500 → 46 pieces (was 0), back-landing
    band 2750 → 6 pieces (was 0). The door-smash specifically gets
    door_FL=1, doorglass_FL=7, windshield=8 — visible medium shards matching the
    user's "medium intensity" expectation.
13. ✅ **Blast threshold — no firework for gentle touches** — even a low-intensity
    touch (the car's back brushing the ground while rolling, severity ~0.2)
    fired a full "firework": every event's launch speed is
    `settings.speed * (0.35 + 0.65 * severity)`, so the 0.35 floor gave even
    the weakest impact a 1.6+ m/s cone spray plus the part's inherited
    velocity. New setting `min_blast_severity` (default 0.35) gates the blast:
    impacts BELOW it still shed debris (hero shards + fine particles) but with
    `speed = 0` and zero inherited throw — the kinematic launch just holds each
    piece in place for `LAUNCH_FRAMES` and releases it at rest, so it drops
    straight from the impact point and the rigid-body physics settles it.
    Angular velocity (fine particles) also zeros below threshold. Applied in
    `_spawn_hero_pieces` (vel += inherit gated on `blast`) and
    `_spawn_fine_particles` (`normal_factor`/`factor_random`/`object_align_factor`
    zeroed). Measured on the real capture: back-landing band 2752–2908 is
    severity 0.18–0.23 → all fall straight (was firework); door-smash 2458 is
    0.41–0.55 → still blasts at medium strength; first crash 2350–2362 is
    0.40–1.0 → full blast unchanged. 38 of 121 events are now no-blast.
14. ✅ **Phantom velocity eliminated for below-threshold drops** — the first
    implementation of item 13 zeroed the *spray* but kept the kinematic launch,
    and the kinematic hold is what caused the "back-landing heroes shoot
    straight up" bug. A body that sits kinematic-static for the whole pre-launch
    run (a late event at frame ~2752 is held static from the sim start at ~2346
    — ~400 idle frames) picks up a solver-integrated phantom velocity when it is
    released. Measured on the real bake: back-landing shards rose ~9.6 m/s
    straight up (`debris_glass_2752_000` z: 0.0306 static 2345→2751, then 0.183
    at 2752 → 1.39 at 2760 → 3.62 at 2780), instead of resting on the ground.
    The `bake_start = first_spawn - LAUNCH_FRAMES - 2` fix (item 9) only helps
    the FIRST event — every later event still sat static for hundreds of frames
    and accumulated the phantom. **Fix:** below-threshold events skip the
    kinematic launch entirely. In `_spawn_hero_pieces` the launch block now
    branches on `blast`:
    - `blast=True` → unchanged 3-frame kinematic launch (`LAUNCH_PROP =
      launch_start`).
    - `blast=False` → no kinematic keys at all; the piece is placed at its
      floor-clamped spawn position, set ACTIVE from the start, and hidden until
      its spawn frame. It simply rests on the ground all run and is revealed
      already settled — a straight drop, no velocity handed to the solver.
    Verified live after a clean re-bake: `debris_glass_2752_000` Z stays flat at
    0.104 from 2748→2950 (was rising past 2.6); `debris_glass_2356_000` (a real
    blast, severity ≥ 0.35) still arcs to Z≈7.15 at 2450 and settles — the
    firework path is untouched.
    **Gotcha for developers:** the addon's `runtime/` package imports as a
    *top-level* module (`runtime.debris_spawn`, not
    `beamng_cache_importer.runtime.debris_spawn`). Reloading the addon by
    purging only `beamng_cache_importer*` from `sys.modules` leaves the stale
    pre-fix bytecode live in memory (`inspect.getsource` reads from disk and
    shows the NEW code, so it lies about what is actually running). A full
    reload must also purge `runtime*` / `importer*` before re-enabling the
    addon, or the re-bake silently reproduces the bug.
15. ✅ **Glass shatters out of the car with a retained edge fringe** — pane
    damage tiers (`GLASS_INTACT` / `GLASS_CRACKED` / `GLASS_SHATTERED`) are
    classified per impact in `impact_detect.py` and collapsed to the worst tier
    per pane, keeping the first shatter frame. At bake, `build_debris` spawns
    dynamic fragments for shattered panes (world-space verts via
    `local_to_world` with the pane's frame transform + ground shift) and leaves
    a `~edge_retain` (0.15) fringe of shards parented to the pane's owning chunk
    object — `matrix_parent_inverse` resolved at the shatter frame — so they
    stick to the window frame and travel with the wreck. The intact pane then
    collapses out of the car from its shatter frame: `CachePlayback` broadcasts
    the pane's vertex range to its centroid (`set_shattered_panes`), driven by
    the `_beamng_shattered_panes` scene prop persisted across undo/reload via
    `frame_handler`. UI: "Shatter Glass" toggle + thresholds in the Debris
    panel's Glass box. Verified: unit tests for tiers/resolution/world-space
    mapping; addon rebuilt to `dist/beamng_cache_importer.zip`.

---

## 7. Honest limitations (for the design review)

### 7.1 Flat convex prisms

Every shard is an irregular convex prism — straight edges, flat faces, no bend.

| Material | Reads well because... | Betrays itself because... |
|---|---|---|
| glass | thin + 3.6× elongation = splintery silhouette | edges are straight, real fracture edges are stepped/curved |
| paint / plastic | panel thickness + jittered silhouette | flakes are perfectly flat; real sheet warps |
| steel | compact, chunky | convex only; real fragments have concave breaks |

### 7.2 Realistic upgrades (in order of cost)

- **A. Sharpen the profiles (cheapest, ~1 module):** concave star-shaped
  cross-sections for glass (indented radii → jagged edges); a subtle bend/warp
  along the elongation axis for paint/plastic flakes; occasional tapered
  "wedge" shards; vary vertex count 3–7.
- **B. Bend the real geometry (medium):** cut actual triangle clusters out of
  the part's deformed mesh and give them thickness — debris carries real weld
  seams, cut lines, and 3D curvature. Heaviest realism gain for sheet parts.
- **C. Boolean / voronoi fracture (heaviest, avoid):** boolean a cell pattern
  against the real part. Genuine fracture look, but expensive per part and
  fights the "no touching car geometry + keep it unit-testable" constraints.

**Recommendation:** do A now; only if it reads fake in the viewport, add B
later. C is a trap here.

### 7.3 Performance notes

- Shard geometry is built once per `(part, material)` and reused via templates
  (8 variants) — not per shard instance.
- Fine debris is a single particle system per impact (cheap solver).
- Hero debris is capped (`max_hero_total = 200`) so a huge capture cannot hang
  Blender — see the debugging log for the 900 → 200 story.
- The rigid body bake walks the timeline once, writing F-curves directly rather
  than a double pass.

---

## 8. Where the pieces live (file map)

| Concern | File |
|---|---|
| Impact detection (pure numpy) | `runtime/impact_detect.py` |
| Shard geometry + materials (numpy + thin bpy) | `runtime/debris_shards.py` |
| Spawner + bake (bpy) | `runtime/debris_spawn.py` |
| Live playback handler (must not be corrupted by bakes) | `runtime/frame_handler.py` |
| Cache read API used by detection/shards | `runtime/cache_reader.py` |
| Addon panel (needs a Debris sub-panel) | `addon/ui.py` |
| Addon operators (need build_debris / clear_debris) | `addon/operators.py` |
| Build script (copies all of `runtime/`, so new modules ship on rebuild) | `build_addon.py` |

---

## 9. Debugging log — what was tried, what went wrong, what didn't work

Every significant mistake that was actually made (in the rough order it
happened), what the wrong behaviour looked like, and the fix that stuck.

### 9.1 Impact detection

1. **Tried raw local vertex motion as "deformation".** Cache-local positions
   remove the *vehicle's* rigid motion but not each part's own rotation — doors
   swing, wheels spin, the speedometer needle sweeps (measured 13–50° locally
   between frames). Raw motion read a part that only rotates as deforming.
   **Went wrong:** the needle would have spawned metal debris out of the
   dashboard; panels read ~2× too deforming. **Didn't work:** any fixed raw
   threshold.
2. **Tried Kabsch/SVD residual — first attempt rotated the wrong operand.**
   `per_vert = norm(bc @ rot.T - ac)` applies the best-fit rotation to `b` and
   differs from `a`, which *doubles* the rotation instead of cancelling it.
   **Went wrong:** residual was exactly 2× the raw motion; a rigid, free-flying
   car read as continuously deforming 0.45 m/step (bounded AABB span changed
   3.74 → 2.99 → 3.49 mid-air, which physically cannot happen).
   **Didn't work:** no amount of thresholding could separate signal from that.
   **Fix:** rotate `a` ONTO `b` (`norm(ac @ rot.T - bc)`) — after that every part
   reads **0.0000** in free flight and non-zero only where material fails.
3. **Tried letting impulse/ground signals trigger events independently.** A
   rigid brake disc slams the ground without deforming, and it must shed
   nothing. **Went wrong:** 7 spurious events from cabin parts. **Fix:**
   deformation is the **only** trigger; velocity/ground only *annotate* a
   deformation event (raise severity).
4. **Tried `relative_floor = 0.06`.** **Went wrong:** 831 events — the tail was
   a settling car creaking, not material failing. **Fix:** `0.18` → 121 events,
   tightly clustered at the real impacts (bf 2010–2130), zero during free
   flight.
5. **Interior parts leaked.** A rollover deforms the cabin shell, and debris
   from it would spawn *inside* the shell and rain through the floor.
   **Fix:** explicit `exclude` name list (dash, seats, steer, needle, gauges,
   pedal, interior…) → zero interior leakage.

### 9.2 Shard geometry

6. **Winding was inside-out.** The first prism was watertight but all volumes
   negative — renders as a black hole and confuses convex-hull collision bounds.
   **Fix:** top ring counter-clockwise from +normal, bottom reversed, side quads
   ordered to match → 0 non-manifold edges, all positive volumes.

### 9.3 Spawning / ground

7. **Ground plane created before the rigid body world existed.**
   Linking an object into the RBW collection is what gives it `obj.rigid_body` —
   a ground made before the world exists silently has none. **Went wrong:** no
   collision; every shard dropped to z=-100. **Fix:** link the ground (and
   force a `PASSIVE` body if still missing) *after* `_ensure_rigidbody_world`.

### 9.4 Launch velocity

8. **Tried two location keyframes as a kinematic launch.** Place the piece one
   frame upstream along its launch vector so it "arrives" on the spawn frame.
   **Went wrong:** Blender does not infer velocity from two keyframes — the
   shard was released at rest and dropped straight down (peak rise **0.000 m**).
   **Fix:** `LAUNCH_FRAMES = 3` frames of genuine kinematic travel; the solver
   then adopts the velocity it was travelling at (same shard arcs 2.0 m up and
   2.0 m downrange).
9. **Baked from `scene.frame_start`.** The scene starts at frame 1 but impacts
   are ~2030; simulating ~2000 idle frames integrated a huge phantom velocity
   out of the long-static kinematic bodies. **Went wrong:** debris shot **90 m
   sideways**, fell to z=-96. **Didn't work:** tweaking the sim window length.
   **Fix:** `bake_start = max(scene.frame_start, first_spawn - LAUNCH_FRAMES - 2)`
   — a few frames ahead of the earliest launch. Also: **always step forward one
   frame at a time from the point cache's start frame** — jumping the playhead
   into the middle leaves the cache invalid and every body reads out at rest
   (all debris teleported to z=0).
10. **Hardcoded `× 24.0` on velocity** (assumed "per cache frame"). Detection
    samples every `stride=2` frames. **Went wrong:** debris launched at ~2× speed
    — 55 m/s instead of 28 m/s. **Fix:** convert to m/s at the source in
    `detect_impacts` (via `playback_fps`), delete the magic constants, pin the
    contract with a unit test.

### 9.5 The bake itself

11. **Tried `bpy.ops.rigidbody.bake_to_keyframes`.** It drives keyframe
    insertion through `anim.keyframe_insert_by_name`, which needs a VIEW_3D
    context *and* the active keying set. **Went wrong:** "No suitable context
    info for active keying set", dying partway after creating some keyframes
    (and a hidden body can't be selected, so it silently baked nothing).
    **Didn't work:** context overrides and setting the keying set globally.
    **Fix:** write the F-curves directly — step the solver once, record each
    body's world matrix, drop the rigid body world, then write keys.
12. **Baked while frame handlers were live.** The playback handler rewrites
    ~550K verts/frame; baking with it live corrupted the point caches.
    **Went wrong:** the earlier attempt was misaligned by ~300 frames.
    **Fix:** `_frozen_handlers` detaches every frame handler for the bake.
13. **Wrote keys while the solver still existed.** The solver keeps overriding
    object transforms, so the keys didn't stick. **Fix:** remove the rigid body
    world *before* writing F-curves; the debris becomes inert keyframe data.
14. **Raw quaternion interpolation took the long way round.** Visually
    identical orientations interpolated across the full sphere → wild spins.
    **Fix:** keep successive quaternions on the same hemisphere
    (negate when `prev.dot(quat) < 0`).

### 9.6 The GUI hang — `max_hero_total` 900 → 200

15. **Symptom:** clicking **Build Debris** in the *GUI* Blender looked like a
    hard hang — the UI froze, no progress, the whole Blender window appeared
    dead. (This is the current open item and the reason a live-GUI pass still
    matters.)
16. **Root cause:** `max_hero_total = 900`. The real 1200-frame capture hit the
    cap: **900 rigid bodies × ~3000 baked frames × 7 F-curves each ≈ 28 minutes
    of bake**, and the operator has no progress bar in the GUI — the window just
    sits there. The code itself was not stuck in an infinite loop; it was doing
    ~half an hour of real work while the UI showed nothing. Headless runs masked
    this (a CLI script "running" for 30 min is unremarkable).
17. **Tried (and rejected):** leaving the cap at 900 and adding a progress bar.
    Even a bar doesn't make a 28-minute non-cancellable operator acceptable from
    a panel button; the work is far more than a debris effect needs.
18. **What didn't fully solve it:** the `bake_start` fix alone (item 9). It cut
    the idle-frame waste and the phantom velocity, but the dominant cost — the
    body count — was untouched.
19. **Fix that stuck:** `max_hero_total: int = 900 → 200` (still a global budget,
    consumed across all impacts in severity order, so the strongest events keep
    their hero pieces). Measured on the real capture, headless: full detect →
    spawn → bake in **82–88 s**, **200 hero / 121 emitters**, keyframes
    **2020 → 2750**, 78/78 pytest, installed copy rebuilt and redeployed.
20. **GUI verification (now done):** the button was clicked in the live GUI on
    the real shot scene — progress streamed, the bake completed, no hang
    (verified live: 200 hero / 121 emitters, 1,153,600 keys over frames
    2345–3168, solver torn down, car untouched). If a future huge capture still
    feels slow, raise the cap for that capture rather than removing it — the
    ceiling is the safety net against a real hang.
21. **Symptom:** after the GUI fix, "particles" were visible at every impact
    point from frame 1 — small fixed squares that never moved. Emission timing
    was already correct (0 live particles at frame 1); the squares were the 121
    **emitter quads** themselves, never hidden.
    **Fix:** keyframe `hide_viewport` on each emitter — True at
    `spawn_frame-1`, False at `spawn_frame`, True at
    `spawn_frame + 2 + lifetime + 1` (a particle born at `frame_end` dies at
    `frame_end + lifetime`, so this hides the quad exactly one frame after the
    last particle expires). `hide_render` stays True throughout. Verified live:
    `visible_quads_at_frame1: 0`.
22. **Symptom:** some fine particles sank through the ground. **Root cause:**
    NEWTON particles only collide with objects that carry a **COLLISION**
    modifier — the debris ground was a rigid-body slab with `mods: []`, so it
    was invisible to particles (its rigid body was also gone after the bake
    teardown). **Fix:** `BeamNG_DebrisGround` gets a `COLLISION` modifier with
    `friction_factor = settings.friction` (0.72), `damping = 0.6`,
    `permeability = 0.0`. Verified live after a full re-bake: 6736 particles at
    rest, `min_z -0.196`, which is exactly a particle of size 0.5 (radius 0.25)
    resting on the slab whose top surface sits at z ≈ 0.1 (center at
    `0.1 − 0.25 = −0.15`); no penetration.
    **Gotcha:** first attempt used `settings.collision.friction` →
    `AttributeError: 'CollisionSettings' object has no attribute 'friction'`.
    Probing the object revealed the real names: `friction_factor`, `damping`,
    `permeability`, `absorption`, `damping_factor`, `friction_random`,
    `thickness_inner`, `thickness_outer`.

### 9.7 The phantom velocity — below-threshold heroes shot straight up

23. **Symptom:** the no-blast ("drop straight") path from item 13 didn't drop —
    it shot upward. Baked back-landing heroes rose ~9.6 m/s straight off the
    ground (`debris_glass_2752_000` z 0.0306 → 1.39 at 2760 → 3.62 at 2780).
    The blast-path heroes (e.g. the 2356 crash) were fine; only the gentle
    events were broken.
24. **Root cause:** the kinematic launch itself. With `speed = 0` the shard's
    3 launch keyframes all sat at the same position, but the body was still
    **kinematic from the sim start** — the Boolean fcurve extrapolates backwards,
    so `rb.kinematic` was True for the entire pre-launch run (~2346→2751, ~400
    idle frames). On release the solver handed the body the velocity it had
    "integrated" while being held static: a phantom proportional to hold
    duration. The `bake_start` fix (item 9) only trims the window around the
    FIRST launch; a late event still sat held for hundreds of frames.
    **Didn't work:** tweaking `bake_start`, longer/shorter launch windows,
    separate hold distances — any kinematic-hold-then-release scheme re-accumulates
    the phantom for late events.
25. **Fix that stuck:** remove the kinematic hold from the below-threshold path
    entirely. In `_spawn_hero_pieces`, `if blast:` keeps the 3-frame launch;
    `else:` (below threshold) sets `obj.location = spawn_at` (floor-clamped),
    `LAUNCH_PROP = spawn_frame`, `_key_visibility(obj, spawn_frame)`, and
    `rb.type = "ACTIVE"` — the body is ACTIVE (dynamic) from the start, resting
    on the ground, hidden until its spawn frame, revealed already settled.
    Verified on the real scene after a clean rebuild: back-landing heroes flat
    at Z≈0.104 the whole run; blast heroes still arc and settle.
26. **Reload gotcha (cost an hour):** after editing the source, the re-bake
    still showed the bug. The addon's `runtime/` is imported as a top-level
    module (`runtime.debris_spawn`), so purging only `beamng_cache_importer*`
    from `sys.modules` before `addon_disable`/`addon_enable` left the OLD
    bytecode live. `inspect.getsource` reads the file from disk and displayed
    the NEW code, making the running module *look* fixed while it wasn't.
    **Fix:** purge `beamng_cache_importer*`, `runtime*`, AND `importer*` from
    `sys.modules`, then re-enable. Verify with an actual
    below-threshold `_spawn_hero_pieces` call (`LAUNCH_PROP` must equal
    `spawn_frame`, not `spawn_frame - LAUNCH_FRAMES`) rather than by reading
    source.
