# World Transform — Verified Status & Plan

**Date:** 2026-07-18
**Author:** Claude (Opus 4.8)
**Status:** Capture + reconstruction VERIFIED WORKING on real data. Remaining
work is (a) move world placement from vertex-baking to a **parent empty**, and
(b) confirm the tumble **axis** convention via a live ground-truth capture.

> This doc supersedes the "rotation is identity" narrative in
> `ORIENTATION_ANIMATION_PROBLEM.md`. The rotation-source fix prescribed in
> `ORIENTATION_ANIMATION_SOLVED.md` (2026-07-17) — use
> `veh:getClusterRotationSlow(veh:getRefNodeId())` — **is in the code and it
> works.** Measured below.

---

## The core question, answered

**"Can we get vertex positions in WORLD coordinates instead of vehicle-local,
without guessing the transform, and without writing a C++ mod?"**

**Yes — and it is already implemented.** Three facts settle it:

1. **The GPU vertex pool is vehicle-LOCAL by design.** BeamNG bakes the rigid
   body motion out of the shared flexmesh pool for numerical stability (max pool
   drift 0.10 m over a 93 m tumble). World motion can therefore *never* be
   recovered from the vertices alone — it was subtracted upstream. This is not a
   limitation to fight; it's why we read the transform separately.

2. **The engine exposes the authoritative per-frame world transform in Lua.**
   No C++ needed. BeamNG's own code uses these:
   - `veh:getPosition()` — ref-node world position (Z-up physics space).
   - `veh:getClusterRotationSlow(veh:getRefNodeId())` — softbody world
     orientation quaternion. This is what BeamNG's camera code and `techCore.lua`
     use for vehicles; `getRotation()` returns identity for softbodies.
   - (Cross-check sources: `quatFromDir(-getDirectionVector(),
     getDirectionVectorUp())` at `veFlexbodyDebug.lua:319`.)

3. **We are not guessing.** These are the same values the renderer draws with.
   The reconstruction is `world_vertex = R_world · local_vertex + world_pos`,
   which is exactly BeamNG's own formula (`veFlexbodyDebug.lua:260,318-320`).

**No C++ capture mod is warranted.** It would re-read the same engine state the
Lua API already returns.

---

## Measured evidence (real capture: `testfinal/capture.bmc`, 700 frames)

Per-frame transform stored in the BMC (position + quaternion vs frame 0):

```
frame   pos                      rot_vs_f0
    0   (   -0.0, +0.0, +0.3)       0.0 deg   <- rest
  100   (   +0.1, +0.0, +1.5)       0.2 deg   } airborne, launching —
  350   (   +4.0, +4.7, +3.1)       0.2 deg   } little rotation yet
  500   (   +4.7, +4.8, +3.3)       0.3 deg
  550   (  +19.3, +4.9, +6.6)     142.9 deg   <- TUMBLING
  600   (  +74.5, +2.9, +1.8)      49.1 deg
  650   ( +103.4, +2.8, +2.1)     165.3 deg   <- 113 m downrange, fully tumbled
```

- **Translation is captured:** 0 → 113 m.
- **Rotation is captured:** up to 165° during the tumble.

> ⚠️ **Sampling trap (I hit this).** Sampling only frames 0/175/350/525/699
> lands on the low-rotation airborne segment and makes the quaternion look
> frozen at identity. It is NOT. Always plot the FULL curve before concluding
> anything about rotation.

The builder (`cache_builder.build_from_capture`) already applies this transform;
reconstructing the body centroid gives `(+112.9, +3.35, +0.60)` at frame 699 —
the car really does travel and tumble through the world in the built BVC.

---

## If the imported result still animates "at the origin"

Most likely you are looking at a **stale BVC** built before the transform code
landed (e.g. `test_captures/testfinal.bvc`, 4.1 GB, may predate it). **Rebuild
the BVC from the BMC before debugging further.** The capture and builder are
correct on current code.

---

## Remaining work

### 1. Parent empty instead of vertex-baking (chosen approach)

Today `build_from_capture` bakes `R·local + t` **into the BVC vertex positions**
(`cache_builder.py:848-866`). That works but couples deformation and placement,
and bloats per-frame deltas. Per the design decision, switch to:

- **Builder:** write per-frame *local* deformation into the BVC (no transform
  baked), and carry the per-frame `float[7]` (px,py,pz,qx,qy,qz,qw) as a new
  optional per-frame transform stream in the BVC (mirrors the BMC field). The
  format already precedents optional sections (dynamic dir/data).
- **Runtime:** create **one parent empty per object** (or a single shared root
  empty that all part meshes parent to). Each frame, set the empty's
  `location` + `rotation_quaternion` from the captured transform (converted to
  Blender space). Meshes keep deforming locally; the empty places them in the
  world. Clean separation, world motion toggle-able by hiding/zeroing the empty.

Axis conversion is FIXED (not per-frame): pool Y-up → Blender Z-up via
`_pool_to_blender` (Bx=poolZ, By=poolX, Bz=poolY); BeamNG physics is already
right-handed Z-up == Blender (per `util/export.lua:411-412`), so the
translation delta needs no handedness flip. The quaternion basis change is the
one thing to confirm empirically (below).

### 2. Confirm the tumble AXIS (front-flip vs barrel-roll)

The rotation magnitude is correct; the open question is whether the quaternion's
axes line up with the pool-space axis swap — i.e. does a physical pitch show up
as a pitch in Blender, or as a roll? Settle it with ground truth, no guessing:

- `tools/groundtruth_dump.lua` — run in BeamNG during a crash. For ~24 nodes it
  records, per frame: `getNodePosition(id)` (node offset from ref, **world**
  coords) and `getInitialNodePosition(id)` (same node, **car-local**), plus
  candidate rotations (`getRotation`, `getClusterRotationSlow`,
  `getDirectionVector`/`Up`). Because `world_offset = R_world · local_offset`
  with exact node↔node correspondence, this pins R_world unambiguously.
- `tests/calibrate_world_transform.py` — Kabsch-solves the true R_world from the
  node clouds, then scores each candidate quaternion against it. Output names the
  correct source and exposes any basis mismatch to sub-degree accuracy.

---

## File / API reference

| What | Where |
|---|---|
| Capture writes transform | `capture.lua:325-333` (f0), `:392-400` (fN) |
| Rotation API used | `veh:getClusterRotationSlow(veh:getRefNodeId())` |
| BMC transform field | `capture_format.py` — flag bit 2, per-frame `float32[7]` |
| Reader | `capture_reader.py:183` `frame_vehicle_transform` |
| Builder applies transform (vertex-bake — to be replaced) | `cache_builder.py:848-866` |
| Axis swap | `cache_builder.py:_pool_to_blender` (626) |
| Ground-truth dumper (calibration) | `tools/groundtruth_dump.lua` |
| Calibrator | `tests/calibrate_world_transform.py` |
| BeamNG's own vertex→world formula | `veFlexbodyDebug.lua:260, 318-320` |
| BeamNG physics = RH Z-up proof | `util/export.lua:411-412` |
