# ORIENTATION & ANIMATION — ROOT CAUSE FOUND & FIX PLAN

> 📌 **UPDATE 2026-07-18 — see `docs/WORLD_TRANSFORM_STATUS.md` for verified
> current status.** The fix below (use `getClusterRotationSlow(getRefNodeId())`)
> **is in the code and works**: the `testfinal` capture records real 113 m
> translation and up to 165° tumble, and the builder reconstructs the world
> trajectory. Remaining work is switching from vertex-baking to a **parent
> empty** and confirming the tumble axis via a live ground-truth capture
> (`tools/groundtruth_dump.lua` + `tests/calibrate_world_transform.py`).
> Note: sparse frame sampling makes the airborne segment look like identity
> rotation — plot the full curve.

**Date:** 2026-07-17
**Author:** Claude (session working from `docs/ORIENTATION_ANIMATION_PROBLEM.md`)
**Status:** ROOT CAUSE CONFIRMED WITH DATA. Fix designed, being implemented.

> ⚠️ **This doc CORRECTS the previous handoff (`ORIENTATION_ANIMATION_PROBLEM.md`).**
> That doc's central conclusion — "getDirectionVector only gives heading, so we can't
> capture the tumble" — was **only half right, and the half it got wrong is the important
> half.** The tumble was never recorded because the Lua rotation code **silently accepts an
> identity quaternion** and never falls through to a working method. The data proves it.

---

## TL;DR (read this first)

Two independent bugs stacked on top of each other. Both are now understood exactly.

### BUG 1 — The captured quaternion is IDENTITY on every single frame.
The current `capture.lua` (v5-fullrot) tries three methods in order:
1. `veh:getRotation()`
2. rotation from `getNodePosition(0/1/2)`
3. `getDirectionVector()` fallback

**Method 1 succeeds but returns the identity quaternion `(0,0,0,1)`** for the softbody
vehicle. The guard `if len > 0.5 then ... gotRot = true` treats identity as a valid
rotation (its length is exactly 1.0), so it **locks in identity and never tries methods 2
or 3.** Verified directly from `mycap2/capture.bin`:

```
frames with non-identity quaternion: 0 / 650
```

Every frame's quaternion is `(0,0,0,1)`. There is **no rotation data in the capture at
all** — not even heading. The car cannot tumble because the file says it never rotates.

**Fix:** use the API BeamNG's *own* camera code uses for softbody world rotation:
```lua
veh:getClusterRotationSlow(veh:getRefNodeId())   -- returns a quat (x,y,z,w)
```
Grep of the game's `lua/` tree: this is the canonical call (`cameraModes/orbit.lua:67`,
`chase.lua`, etc.). `getRotation()` is NOT used anywhere for softbody orientation.

### BUG 2 — The builder's coordinate math is wrong even if the quaternion were correct.
`_build_from_capture_impl` does, per frame:
```python
pos_all = self._quat_rotate(q, pos_all) + t        # rotate pool, add veh pos
pos_all = self._world_to_blender(pos_all)          # (bx,by,bz)=(-wy,-wx,wz)
```
Three things are broken here:

1. **Double-rotation.** The pool positions are ALREADY in a fixed reference orientation
   (see "Coordinate proof" below — Kabsch shows the body pool barely rotates across
   frames: 0.1° at frame 100, 0.5° at frame 500). Multiplying by `q` *again* would rotate
   an already-oriented mesh a second time. The mesh must be rotated by the **delta** from
   its rest orientation, not by the absolute vehicle quaternion.
2. **Translation applied in the wrong space.** `t` (from `veh:getPosition()`) is BeamNG
   **physics space, Z-up**. The pool positions are **Y-up** (X=length, Y=height, Z=width —
   confirmed by span: 3.74 × 1.23 × 1.75 m). Adding a Z-up translation directly to Y-up
   positions puts vehicle height into the mesh's width axis. Garbage.
3. **`_world_to_blender` was hand-tuned** to compensate for the mess above, so it only ever
   "looked right" for one specific broken configuration and fell apart the moment anything
   changed. This is the "catch-22" the old doc described — it's an artifact of stacking two
   wrong transforms, not a real constraint.

**Fix:** compute everything in ONE consistent space, anchored to the frame-0 rest pose that
was already verified correct.

---

## Coordinate proof (measured, not assumed)

Diagnostic: `tests/diag_orientation.py` (committed). Run:
```
python tests/diag_orientation.py
```

### Finding A — pool positions are LOCAL (vehicle-fixed), rotation baked OUT
Raw pool bbox center for the body across frames (NO transform applied):
```
frame    0: center=(+0.73,+0.57,+0.36) span=(3.74,1.23,1.75)
frame  100: center=(+0.73,+0.57,+0.36) span=(3.74,1.23,1.75)
frame  325: center=(+0.73,+0.57,+0.36) span=(3.74,1.23,1.75)
frame  500: center=(+0.73,+0.57,+0.36) span=(3.74,1.23,1.76)
frame  649: center=(+0.45,+0.56,+0.70) span=(3.77,1.29,3.28)  <- crushed on impact
```
The center is **constant** while the vehicle flies from Z=0.27 to Z=3.11 to Z=0.17 and
translates 93 m in X. So the pool is **NOT world space** — it's a fixed local frame. The
GPU flexmesh vertices are in vehicle-reference space; the rigid body motion is stripped out
and is *supposed* to live in the per-frame transform (which is broken — Bug 1).

Kabsch rotation of the body pool vs frame 0:
```
frame   50: rotation_angle=  0.14 deg
frame  100: rotation_angle=  0.11 deg
frame  325: rotation_angle=  0.11 deg
frame  500: rotation_angle=  0.52 deg
frame  649: rotation_angle= 34.67 deg  (soft-body deformation on impact, not rigid tumble)
```
→ The pool barely rotates. **All the tumble must come from the per-frame quaternion**,
which is currently identity. This is why the car "slides with frozen orientation."

### Finding B — physics space == Blender space (both right-handed Z-up)
From BeamNG's own exporter `util/export.lua:411-412`, props are converted to glTF
(Y-up, RH) as:
```lua
translation = {position.x, position.z, -position.y}   -- phys(x,y,z) -> gltf(x,z,-y)
rotation    = {-rotation.x, -rotation.z, rotation.y, rotation.w}
```
So **BeamNG physics is X=right, Y=forward, Z=up, right-handed** — identical axis meaning to
Blender. No handedness flip is needed between physics and Blender. (The old
`_world_to_blender` handedness flip was compensating for Bug 2's double-transform, not a
real coordinate difference.)

The pool, however, is **Y-up** (glTF-style: X=length/forward-ish, Y=up, Z=width). To move a
pool position into physics/Blender space: **swap Y and Z** → `(x, z, y)`.

### Finding C — the fixed math reproduces the true trajectory
`diag_orientation.py` tested the corrected transform (pool Y-up → swap YZ → +physics t,
identity rotation) and the resulting world center TRACKS the vehicle position:
```
f   0 worldCenter=(-0.05,+0.18,+0.85)  vehPos=(-0.77,-0.19,+0.27)
f 100 worldCenter=(+0.53,+0.25,+2.41)  vehPos=(-0.20,-0.11,+1.83)
f 325 worldCenter=(+4.06,+4.94,+3.68)  vehPos=(+3.33,+4.58,+3.11)
f 649 worldCenter=(+93.49,-1.27,+0.73)  vehPos=(+93.05,-1.97,+0.17)
```
Center ≈ vehPos + constant body offset, on every frame. The trajectory is correct; only the
tumble (rotation) is missing, and that's Bug 1 in the Lua mod.

---

## THE FIX

### Part 1 — Lua mod (`capture.lua`) — get REAL rotation

Replace the whole `Method 1/2/3` block (lines ~769-831) in `captureFrame`'s vehicle-append
section with a single authoritative call. Pseudocode:

```lua
-- Vehicle world position (physics Z-up)
local p = veh:getPosition()
buf[w]     = p.x
buf[w + 1] = p.y
buf[w + 2] = p.z

-- Vehicle world ROTATION via the softbody cluster API (what BeamNG's own
-- camera code uses).  getRotation() returns identity for softbodies, so it
-- must NOT be used.
local qx, qy, qz, qw = 0, 0, 0, 1
local okR, q = pcall(function()
    return veh:getClusterRotationSlow(veh:getRefNodeId())
end)
if okR and q then
    qx, qy, qz, qw = q.x, q.y, q.z, q.w
    local len = math.sqrt(qx*qx + qy*qy + qz*qz + qw*qw)
    if len > 1e-4 then qx,qy,qz,qw = qx/len,qy/len,qz/len,qw/len else qx,qy,qz,qw=0,0,0,1 end
end
buf[w + 3] = qx
buf[w + 4] = qy
buf[w + 5] = qz
buf[w + 6] = qw
```
`getClusterRotationSlow` returns the vehicle rotation in **physics space** (Z-up, RH) — same
space as `getPosition()`. Store raw; do all coordinate conversion in Python.

**IMPORTANT:** update ALL THREE copies of capture.lua:
- `D:\danish\Games\beamng\BeamNG.drive\lua\ge\extensions\beamng\capture.lua` (game dir)
- `C:\Users\ubaid_i2c\AppData\Local\BeamNG\BeamNG.drive\current\mods\unpacked\TelekinesisController\lua\ge\extensions\beamng\capture.lua` (user mods)
- `C:\Users\ubaid_i2c\Downloads\beamng-cache-importer\capture.lua` (repo)

Also bump the version string on line ~1244 and the header comment.

**A NEW CAPTURE MUST BE RE-RECORDED after this Lua change** — the existing `mycap2` has
identity quaternions and cannot be salvaged for tumble. The orientation/translation half
CAN be fixed on the existing capture (Part 2), so verify that half against `mycap2` first.

### Part 2 — Builder (`cache_builder.py`) — correct, single-space transform

Replace the per-frame transform in BOTH branches of `_build_from_capture_impl` (the `weld`
branch ~lines 1206-1221 and the per-primitive branch ~lines 1222-1232).

Define ONE new helper on `CacheBuilder` and use it everywhere:

```python
@staticmethod
def _pool_to_blender(pos: np.ndarray) -> np.ndarray:
    """Pool space (Y-up: X≈length, Y=up, Z=width) -> Blender/physics (Z-up, RH).
    Swap Y and Z.  No handedness flip (BeamNG physics is already RH Z-up,
    per util/export.lua)."""
    out = np.empty_like(pos)
    out[:, 0] = pos[:, 0]
    out[:, 1] = pos[:, 2]
    out[:, 2] = pos[:, 1]
    return out
```

Per-frame world placement, anchored to the frame-0 rest pose so orientation is correct by
construction:

```python
# --- precompute ONCE, before the frame loop ---
vtx0 = reader.frame_vehicle_transform(0)           # frame-0 transform
p0   = vtx0[:3].astype(np.float64)                 # physics position at rest
R0   = self._quat_to_matrix(vtx0[3:7])             # rest orientation (3x3)
R0inv = R0.T                                        # inverse (rotation matrix)

# --- inside the frame loop, for frame fi ---
vtx = reader.frame_vehicle_transform(fi)
pf  = vtx[:3].astype(np.float64)
Rf  = self._quat_to_matrix(vtx[3:7])
Rdelta = Rf @ R0inv                                 # rotation RELATIVE to rest pose

# for each object's pool positions `pool` (N x 3, Y-up):
blend = self._pool_to_blender(pool).astype(np.float64)   # rest pose, Blender space
# apply tumble about the vehicle's rest centroid, then world translation delta
world = (Rdelta @ blend.T).T + (pf - p0)            # (pf-p0) is physics == Blender delta
# frame 0: Rdelta=I, pf-p0=0  -> world == blend  (validated rest orientation preserved)
```

Notes / rationale:
- At frame 0, `Rdelta = I` and `pf - p0 = 0`, so the output is exactly the rest pose in
  Blender space — the orientation the user already confirmed as correct ("faces -X"). We
  never *regress* orientation; we only add tumble + translation on top.
- Rotating the whole scene by `Rdelta` about the ORIGIN is correct because every object
  shares the same vehicle rigid frame; the vehicle's own translation (`pf - p0`) then
  places it. (The rest centroid offset stays baked into `blend`, which is what we want —
  the car rotates as a rigid unit about its physics reference point.)
- `pf - p0` is a translation in physics space, which equals Blender space (Finding B), so
  no axis munging on the delta.
- Add `_quat_to_matrix` static (xyzw → 3×3), same formula already in `_quat_rotate`.
- DELETE the old `_world_to_blender` usage from this path (leave the method for now to
  avoid breaking other callers, but this path must not call it).
- Keep `used_verts[name]` gather for per-primitive; keep `source_map` for weld branch. Only
  the transform math changes.

### Part 3 — Runtime does NO coordinate conversion
The BVC stores Blender-ready positions (per the hard rule in CLAUDE.md). `mesh_update.py`
must not convert. Confirm no `_world_to_blender`/`_gltf_to_blender` on the capture-runtime
path (there isn't — this is just a guardrail note).

---

## VERIFICATION PLAN

### Immediately (on existing mycap2 — tests Part 2 orientation/translation only):
Because mycap2 has identity quaternions, `Rdelta = I` for all frames, so the car will
translate + land correctly and stay UPRIGHT (no tumble). That's the expected partial result
and proves the coordinate math:
```
python tests/diag_orientation.py           # world center should track vehPos (already shown)
```
Rebuild BVC from mycap2 with the new builder, then check with a Blender headless script:
- Car faces -X at frame 0 (headlight -X, taillight +X)
- Car is upright (Z-up), Z rises then falls following vehPos
- No barrel roll

### After re-capture (tests Part 1 + Part 2 together — the real fix):
1. Apply Lua fix, repack is NOT needed for the mod (it's loaded from the game/mods dir),
   but DO restart BeamNG or reload the extension so the new Lua loads.
2. Re-record a capture (same crash) → new `capture.bin` with REAL quaternions.
   Confirm: `python -c "..."` that non-identity quaternion count > 0.
3. Rebuild BVC, import in Blender, scrub: the car should now **front-flip** (pitch about the
   lateral axis) as it flies. If it barrel-rolls instead, the `_pool_to_blender` axis swap
   or the `Rdelta` handedness needs one transpose/sign flip — but Finding B says no flip is
   needed, so this should be right on the first try.

### Regression:
```
python -m pytest -q          # MUST stay 24 passing (test_capture_roundtrip uses synthetic
                             # identity-ish transforms; verify the new math is a no-op there)
python build_addon.py        # repack the addon zip (non-negotiable per CLAUDE.md)
```
⚠️ `test_capture_roundtrip.py` builds a synthetic capture with RANDOM 7-float transforms
(see `write_synthetic_capture`, `vtx = rng.random(7)`). The new anchored math changes what
positions come out. If that test asserts exact position round-trip THROUGH the transform, it
may need updating to either (a) use identity transforms, or (b) assert the new anchored
formula. Check `tests/test_capture_roundtrip.py` before/after and adjust expectations to the
new (correct) math rather than reverting the fix.

---

## FILE / LINE REFERENCE (as of 2026-07-17)

| What | Where |
|---|---|
| Broken rotation chain | `capture.lua` lines ~766-837 (`Method 1/2/3` in `captureFrame`) |
| Fix target (weld branch) | `cache_builder.py` lines ~1202-1221 |
| Fix target (per-prim branch) | `cache_builder.py` lines ~1222-1232 |
| `_quat_rotate` (reuse formula) | `cache_builder.py` lines ~824-833 |
| `_world_to_blender` (STOP using on capture path) | `cache_builder.py` lines ~808-822 |
| `frame_vehicle_transform` (reader) | `capture_reader.py` lines ~274-285 |
| Diagnostic (committed) | `tests/diag_orientation.py` |
| Existing capture (identity quats) | `.../captures/mycap2/capture.bin` |

## KEY EVIDENCE (so next session doesn't re-derive it)
1. `0/650` frames have non-identity quaternion → rotation never captured (Bug 1).
2. Pool bbox center is CONSTANT across flight → pool is LOCAL, rotation baked out.
3. Kabsch body rotation ≤0.5° until impact → tumble MUST come from the (missing) quaternion.
4. BeamNG `util/export.lua:411` proves physics is RH Z-up == Blender; pool is Y-up → swap YZ.
5. Corrected transform (swap YZ + physics translation) makes world center track vehPos on
   every frame → coordinate math validated.
6. BeamNG camera code uses `veh:getClusterRotationSlow(veh:getRefNodeId())` for softbody
   world rotation → the correct API for Bug 1.
