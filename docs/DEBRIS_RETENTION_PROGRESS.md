# Debris/glass retention — work log (2026-08-10)

Requirement (user, via session log 2026-08-10-114341):
- Windshield edge fringe **should STAY STUCK IN THE FRAME** (only central vertices shatter out).
- "PARTICLES + DEBRIS BOUNCINESS" slider, 0 = collide once with ground then stay.
- No "firework boom" launch.

## Attempts and outcomes

### A. Original design (DEBRIS_FEATURE.md:250) — parent fringe to owning chunk
Parented retained fragments to the pane's owning chunk object with
`matrix_parent_inverse` resolved at the shatter frame.
FAILED: 82 retained fragments rode the wreck AND went through the launch/RB/bake
path. End frame 65/82 at z=-1.53 (below ground, "glass follows the car").

### B. Fix in earlier session — unparent everything
Removed ALL glass→car parenting; every fragment = independent rigid body, retained
ones got reduced throw (blow * edge_retain * 0.5, vel * 0.15).
Result: glass no longer buries (end z 0.00–0.28, below0=0) BUT fringe no longer
stuck in the frame.
USER: "the earlier fix you thought wasn't correct either. come up with a new fix
approach."

### C. Selective parenting, parent_inverse resolved at shatter frame — IMPLEMENTED
Retain the fringe AND keep it stuck in the frame by:
- parent retained fragments ONLY to the transform empty (`<coll>__root`)
- resolve `matrix_parent_inverse` at the SHATTER frame (not bake time)
- do NOT give retained fragments launch velocity, rigid body, or bake them
- dynamic (non-retained) fragments stay independent rigid bodies with bounciness

Code: `runtime/debris_spawn.py` `_spawn_glass_pane` phase 0b + `_find_transform_root`.

## Verification (real cache name.bvc, headless Blender)

`tests/blender_debris_retention.py` — 2 min headless run; `SAVE_BLEND` caches a
built scene so re-checks run in ~20 s via `USE_BLEND`.

FIRST FAILED RUNS (test bugs, not code bugs):
- ground detection used a name heuristic that matched nothing → ground_z read as
  -100, making every z-check vacuous. Fixed: read `BeamNG_DebrisGround` by name.
- drift check read `root.matrix_world` without a depsgraph update → root pose
  stale (read the END pose at the SPAWN frame) → fake 16 m drift. Fixed: add
  `bpy.context.view_layer.update()` after `scene.frame_set(...)`.
- "below ground" asserted on ALL fragments; pane fragments stuck in a door that
  shattered while crushed into the ground sit at z≈-0.3, which is CORRECT
  ("stuck in the frame"). Restricted the check to dynamic (unparented) fragments.

PASSING (final):
- 58 fringe fragments parented to `<collection>__root`; all non-fringe are free.
- **Max drift across all 6 panes = 0.0000** — each parented fragment's world
  position equals `root.M(end) @ root.M(spawn)^-1 @ P_spawn` exactly. They ride
  the wreck and can never fall/fly/be overwritten by the bake.
- Per-pane spawn heights match their apertures (windshield -0.03..0.13 —
  shattered while the wreck was nose-down; doorglass_RR 0.14..0.26; etc.).
- Fringe `matrix_basis` is constant across frames (no keyframes), and
  `parent_inverse = -root.M(spawn).translation` — the glue is exact.
- `python -m pytest -q` 74/74 green. Addon repacked `dist/beamng_cache_importer.zip`.

### D. Dynamic-fragment ground tunneling — ROOT CAUSE FOUND + FIXED (later same day)
The "remaining observation" above reproduced at full scale: with the real cache,
15 backlight/trunkglass dynamic fragments ended at z=-107..-118, falling straight
through the 0.2 m ground slab at constant ~15 m/s (their damping-limited terminal
velocity), while doorglass fragments settled fine.

Faithful headless repro (baking the REAL tunneled shard mesh against a fresh RB
slab) showed the tunnel happens AT SPAWN, not from fast motion:
- The kinematic launch keys clamp only the fragment's CENTRE to
  `ground_z + GROUND_CLEARANCE` (0.004 m). Glass shards extend several cm BELOW
  their centre (measured mesh z-bounds: -0.025..+0.017), so at the moment the
  body is released ACTIVE its hull is already ~2 cm INSIDE the slab.
- Bullet resolves that initial penetration by ejecting the body through the
  nearest face — for a thin flat shard that face is DOWNWARD — and the fragment
  free-falls out of the world.
- The hero-piece path already had this fix (`_lowest_point_offset`, see its
  docstring: 21 of 200 hero pieces at z=-868..-912 from the same bug). The glass
  path, added later, reused the plain centre clamp and missed it.

FIX (`runtime/debris_spawn.py` `_spawn_glass_pane`): clamp the glass launch keys
on the fragment's LOWEST POINT via `_lowest_point_offset(obj, (0,0,0), 1.0)`
(glass fragments spawn world-aligned, identity rotation). Verified first in the
faithful repro (real shard: previously z→-4.5 in 23 frames, now bounces and
settles), then in the full retention run.

VERIFIED (`DEBRIS_SETTLE=20`, real cache, 315 fragments):
- below-ground count **15 → 0**
- fringe drift across all 6 panes still **0.0000**
- `python -m pytest -q` **81/81 green**; addon repacked.

The slab thickness (0.2 m, 12 substeps) was never the issue for these fragments
— a 15 m/s body crosses only ~2 cm/substep, nowhere near 0.2 m. The measured
tunnel was purely the spawn-penetration ejection.

### E. Rim-band collapse — REPLACES approach C (2026-08-11)

The 58 parented fringe objects from C had a ceiling: parenting follows only the
root empty's RIGID transform, and the pane keeps DEFORMING during the crash
(the aperture crumples as the car rolls/crushes), so the fringe drifted off the
deforming frame. The code's own comment in `_spawn_glass_pane` called the right
design "don't parent the fringe at all — the fringe is one or more fixed verts
on the pane, so at frame f its world position SHOULD BE the pane verts at frame
f". Following the pane's verts was blocked, though, because `mesh_update`
collapsed EVERY vertex of a shattered member to the pane centroid from the
shatter frame on — no live rim existed to follow.

FIX: the pane mesh itself keeps its rim band alive and acts as the fringe.
- `glass_shatter.rim_mask_and_anchors()`: per-vertex keep-mask (vertices within
  `edge_retain` of the outline, capped at `MAX_RETAINED_FRACTION` like the cell
  test) + per-vertex glue-anchor index: every interior vertex rides its nearest
  rim vertex's CURRENT-frame position, so the collapsed faces stay degenerate
  along the break edge even while the pane keeps deforming (a static collapse
  target would stretch metres off the moving rim — measured rim rows move up to
  3.8 m of pure deformation over the crash).
- `mesh_update.set_shattered_panes(panes, edge_retain)`: stores keep/anchor
  arrays per pane; `_collapse_shattered` replaces only the interior rows with
  their anchor's live position. Rim rows keep their per-frame positions → rim
  follows the pane's vertex animation EXACTLY.
- `_spawn_glass_pane`: no fringe objects, no phase 0b, no `_find_transform_root`
  (deleted). Retained cells are counted but not spawned.
- `frame_handler` persists `_beamng_shatter_edge_retain` so reload recovery
  rebuilds the same rim; operator passes `glass_settings.edge_retain`.

VERIFIED (`tests/blender_debris_retention.py`, real cache, 208 dynamic
fragments):
- **0 parented fragments** (the 58 fringe objects are gone; outliner is clean)
- 6 panes shattered, map + rim-band width persisted, reload recovery rebuilds
  the masks
- rim rows welded to the pane **0.0000 m** deviation at the end frame; interior
  rows ride their rim anchors **0.0000 m** deviation and sit up to 3.8 m off
  their own live positions (proving real collapse)
- below-ground dynamic fragments: **0**
- `python -m pytest -q` 81/81 green; addon repacked.
- NOTE: a previously saved SAVE_BLEND predates this change and must be
  regenerated (it still holds the old parented fringe).
