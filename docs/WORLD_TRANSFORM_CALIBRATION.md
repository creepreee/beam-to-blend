# World Transform — Calibration Log & Findings

**Date:** 2026-07-18
**Author:** Claude (Opus 4.8)
**Status:** IN PROGRESS. Rotation source identified; axis-basis solve blocked by a
flawed ground-truth capture (my dumper sampled non-rigid nodes). Dumper fix and
re-run required. All raw findings recorded below so no analysis is re-derived.

Read `docs/WORLD_TRANSFORM_STATUS.md` first for the verified pipeline state.

---

## What we are solving

The GPU pool is vehicle-LOCAL; world placement comes from the per-frame engine
transform stored in the BMC (`getPosition()` + `getClusterRotationSlow(refNode)`).
The transform is captured correctly (113 m translation, up to 165–180° rotation
measured). The **open question** is the fixed axis/basis relationship between the
stored quaternion and the pool coordinate frame — i.e. does a physical pitch
render as a pitch (front-flip) or get mis-mapped to a roll (barrel-roll)?

We answer it with ground truth from a live capture, not by guessing.

---

## Run 1 — 1000-frame ground-truth capture (`gt_dump.json`)

Produced by `tools/groundtruth_dump.lua` (24 nodes sampled by index across the
vehicle) + analyzed by `tests/calibrate_world_transform.py`.

### Finding 1 — `getRotation()` is useless for softbodies (confirmed)
Its frame-to-frame traversal angle is **0.0° on every frame** — it returns a
constant for softbody vehicles. Matches BeamNG's own avoidance of it for
vehicle orientation. Do NOT use.

### Finding 2 — `clusterRot` IS the physically correct rotation
`getClusterRotationSlow(getRefNodeId())` traversal angle tracks the true
node-cloud rotation almost exactly:

```
frame   true    clusterRot
  200  144.4      144.4
  400  114.9      113.9
  600   51.7       48.8
  800  154.4      147.5
  999  173.5      178.7
```

The initial calibrator scored it "WRONG" only because it compared raw rotation
**matrices**; the rotation is the same magnitude in a **different axis basis**.
So `capture.lua` is using the right API. The remaining task is the basis change,
not the source.

### Finding 3 — the ground-truth node cloud is NON-RIGID (dumper flaw)
Attempts to solve the fixed basis `B` (via conjugacy `Dc·B = B·Dw`, via axis
alignment, and via `W = Rc·A·L`) all bottomed out at 25–82° / ~1–1.4 m residual.
Root cause found by checking pairwise node distances over time:

```
node0–node12 distance: min 3.00 m  max 6.40 m  range 3.40 m
pairwise distance RANGE over frames: mean 0.86 m  max 10.23 m
```

A rigid body has constant pairwise distances. Ranges of 3–10 m mean the sampled
node set includes **wheels / suspension / articulated nodes** that move
independently of the body shell. The frame-0 Kabsch residual was already 0.44 m
(should be ~0). Selecting the 6 most-rigid nodes only improved residual to
~0.9 m — still far too high to trust a rotation solve.

**Conclusion:** the `by-index` node sampling in `groundtruth_dump.lua` is the
flaw. Nodes are not all part of the rigid body frame. This dataset cannot
cleanly calibrate the rotation basis.

---

## The correct ground truth (for Run 2)

Use the actual render-mesh vertices, which is what we store and care about, and
which BeamNG exposes with an exact world formula (`veFlexbodyDebug.lua`):

```
worldVert(i) = flexbodyObj:getDebugVertexPos(i) + veh:getPosition()   -- line 260/400
```

`getDebugVertexPos(i)` is the render-mesh vertex in the vehicle frame; adding
`getPosition()` gives world. Requires `veh:setFlexmeshDebugMode(true)` and a
`getFlexmesh(fid)` handle. For a chosen flexmesh, dump for a handful of vertex
indices, per frame:
- `getDebugVertexPos(i)`  (local/vehicle-frame render vertex)
- `worldVert(i) = getDebugVertexPos(i) + getPosition()`
- the frame transform (`getPosition`, `getClusterRotationSlow(refNode)`)

Then the pool vertex we already store (same index within that flexmesh) must map
to `worldVert(i)` under `pool→blender axis swap` + `R(quat) + t`. Solving the
single fixed basis that makes stored-pool → worldVert match to sub-mm gives the
exact convention — with true per-vertex correspondence and NO articulation
contamination (render-mesh vertices follow the body shell rigidly except at
genuine deformation, and we can pick body-panel vertices).

Even simpler cross-check: pick vertices on the rigid body panel far from crumple
zones; their world positions must be a rigid transform of their rest positions,
and that transform must equal `T(getPosition, clusterRot)` in the correct basis.

---

## Numeric facts to reuse (don't re-derive)

- `getRotation()` traversal: 0° always (softbody → constant).
- `clusterRot` traversal ≈ true body rotation (see Finding 2 table).
- Node cloud pairwise-distance range: mean 0.86 m, max 10.23 m → non-rigid set.
- Frame-0 full-cloud Kabsch residual: 0.44 m.
- Best `W=Rc·A·L` residual (6 rigid nodes): mean 0.91 m — still too high.
- Worst-fit frames clustered at 91–95 (a suspension/wheel articulation event).
- `getNodePosition(id)` = node offset from ref node in **world** coords (ai.lua:415).
- `getInitialNodePosition(id)` = same node in **car-local** rest reference.
- Physics space is RH Z-up == Blender (`util/export.lua:411-412`); translation
  delta needs no handedness flip.

---

## Run 2 tooling — DONE, awaiting a live capture

Both tools are rewritten and self-tested on synthetic data (calibrator picks
`clusterRot` at 0.0000 m invariance error; rigidity gate confirmed):

- `tools/groundtruth_dump.lua` **v2** — dumps render-mesh vertices via
  `veh:getFlexmesh(fid):getDebugVertexPos(i)` for one flexmesh (body shell,
  `flexIndex` arg, default 0), 16 vertices, plus all rotation candidates and
  `getPosition()`. `getDebugVertexPos` is world-oriented & ref-relative, so it
  needs no translation handling. Requires `setFlexmeshDebugMode(true)` (the mod
  sets/clears it automatically).
- `tests/calibrate_world_transform.py` **v2** — translation-immune test: the
  correct world rotation `R` makes `R^T · debugVert(f)` frame-invariant. Reports
  per-vertex spread (m) for each candidate and a rigidity gate (rejects clouds
  with >0.3 m pairwise-distance drift, i.e. non-rigid vertex selections).

### How to run Run 2 (in BeamNG)
```
-- GE Lua console:
groundtruth_dump.start('tools/gt_dump.json', 300, 0)   -- flexmesh 0 = body shell
-- ...drive off a ledge / crash so the car TUMBLES (needs real rotation)...
groundtruth_dump.stop()      -- or it auto-flushes after 300 frames
```
Then:
```
python tests/calibrate_world_transform.py "<userpath>/tools/gt_dump.json"
```
Expected: `clusterRot` wins with invariance err < 0.02 m, rigidity gate [ok].
If rigidity gate fires [!], the chosen flexmesh has heavy local deformation —
re-run with a different `flexIndex` or capture a cleaner tumble.

## Next actions
1. Run 2 capture (above) — confirms rotation source & exposes any basis issue.
2. If tumble axis still wrong after using `clusterRot`, solve the fixed basis
   `B` between the quaternion frame and the pool frame via vertex correspondence
   (pool vert ↔ debugVert). The vertex data now supports this directly.
3. Feed the result into task #3 (parent-empty runtime placement).
