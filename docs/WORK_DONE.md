# Work done in this session (2026-08-10)

Project: BeamNG cache importer (`beamng-cache-importer`).

## Goal this session
Fix debris/glass behaviour: retained glass edge fringe must STAY STUCK IN THE
FRAME (ride the wreck, never fall/bury), dynamic fragments must settle on the
ground, no firework launch.

## What was already in place (from previous session, verified again)
- `tests/blender_debris_retention.py` — headless retention test (real cache
  `name.bvc`, ~2 min run; `SAVE_BLEND`/`USE_BLEND` for fast iteration).
- `runtime/debris_spawn.py` phase 0b — retained fringe fragments parented to the
  transform empty `<coll>__root` with `matrix_parent_inverse` resolved at the
  shatter frame; dynamic fragments remain free rigid bodies with bounciness.

## What I did this session
1. Re-ran the full retention test with the real cache (settle=260).
   Result: fringe drift across all 6 panes = 0.0000 (stuck in the frame) BUT
   15 dynamic backlight/trunkglass fragments ended at z=-107..-118 — they
   tunnelled straight through the 0.2 m ground slab.
2. Diagnosed the tunnel by inspecting baked F-curves of a tunneled fragment
   (`backlight_015`): it fell at a constant ~15 m/s (the damping-limited
   terminal velocity), i.e. gravity/damping active but NO ground collision.
   Compared against a doorglass fragment that settled fine at z=0.10.
3. Built a minimal headless repro of the exact ground+fragment+kinematic-launch
   recipe. A cube did NOT tunnel. Rebuilding with the REAL tunneled shard mesh
   (extracted from the saved blend) DID tunnel: z -> -4.5 within 23 frames,
   crossing the slab on the very first ACTIVE frame.
4. Root cause:
   - The kinematic launch keys clamped only the fragment's CENTRE to
     `ground_z + GROUND_CLEARANCE` (0.004 m).
   - Glass shards extend several cm BELOW their centre (measured mesh z-bounds
     -0.025..+0.017), so at the ACTIVE transition the hull was already ~2 cm
     INSIDE the slab.
   - Bullet resolves that initial penetration by ejecting the body through the
     nearest face — for a thin flat shard that face is DOWNWARD — and the
     fragment free-falls out of the world.
   - The hero-piece path already had this fix (`_lowest_point_offset`, docstring
     documents the identical bug: 21/200 hero pieces at z=-868..-912). The glass
     path, added later, reused the plain centre clamp and missed it.
5. Fix: `runtime/debris_spawn.py` `_spawn_glass_pane` — clamp the glass launch
   keys on the fragment's LOWEST POINT via
   `_lowest_point_offset(obj, (0,0,0), 1.0)` (glass fragments spawn
   world-aligned, identity rotation).
6. Verified:
   - Faithful repro (real shard): previously tunnelled to z=-4.5; after the fix
     it bounces and settles above the ground.
   - Full retention run (`DEBRIS_SETTLE=20`, real cache, 315 fragments):
     below-ground count **15 -> 0**; fringe drift still **0.0000**; all checks
     PASS.
   - `python -m pytest -q` — **81/81 green**.
   - Addon repacked: `dist/beamng_cache_importer.zip`.

## Non-fix verified as non-issue
The 0.2 m slab thickness (12 substeps) was NOT the cause — a 15 m/s body crosses
~2 cm/substep, far less than 0.2 m. The tunnel was purely spawn-penetration
ejection. The slab thickness was left unchanged (0.2 m).

## Testing rule (PERMANENT)
ALWAYS, NO MATTER WHAT, USE THE BLEND FILE
`C:\Users\ubaid_i2c\Downloads\vehicle materials ready made.blend`
FOR ANY TESTING. NO OTHER BLEND FILES ALLOWED.
