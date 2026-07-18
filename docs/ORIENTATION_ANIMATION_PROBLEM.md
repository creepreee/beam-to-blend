# ORIENTATION & ANIMATION PROBLEM — Full Writeup for Handoff

**Date:** 2026-07-17
**Status:** UNRESOLVED — both orientation and animation are wrong in the latest attempt.
**Last working state:** Car orientation was correct (faces -X in Blender) but animation was frozen (no tumble). After attempting to fix animation, BOTH broke.

---

## TL;DR

The BeamNG capture exports pool (local) positions + a per-frame vehicle transform (position + quaternion). The Lua mod computes the quaternion from `getDirectionVector()` which only gives **heading (yaw)** — no pitch/roll. This means the car's tumble/flip in the air is never captured, only its ground-level heading.

Multiple attempts were made to get the full 3D rotation. Each approach either didn't work (API limitations) or broke the orientation when applied.

---

## What the user wants

1. **Correct orientation:** Car faces -X in Blender (this was ACHIEVED at one point).
2. **Correct animation:** Car should do a **front flip** (pitch/rotation around the lateral/right axis, i.e. Y-axis when facing -X). Instead, the car either:
   - Slides through space with frozen orientation (no tumble at all), OR
   - Does a barrel roll (rotation around the forward/X-axis), OR
   - After fixes, both orientation and animation are completely wrong.

**User's mathematical rule:** "If the car orientation is X+, then the animation occurs perpendicular to X+ and vice versa. Fix that." → The animation rotation axis is perpendicular to what it should be.

---

## The Coordinate Systems (verified from diagnostic data)

### Pool space (GPU vertex buffer, per flexmesh)
```
X = FORWARD/BACKWARD (longest axis = 3.74m = car length)
Y = UP/DOWN (shortest = 1.14m = car height)
Z = RIGHT/LEFT (middle = 1.72m = car width)
```
- Car faces pool **-X** (front at X≈-1.3, rear at X≈+2.4)
- Headlight_R at pool X=-0.47, Z=+1.49 (right side = +Z)
- Pool is **left-handed** (right = up × forward = +Z)

### BeamNG world space (Z-up, left-handed)
```
Z = UP
Y = FORWARD
X = RIGHT (or LEFT, depends on handedness)
```
- Vehicle position Z≈0.17 at ground level → Z=up confirmed
- Direction vector `getDirectionVector()` returns (-1, 0, 0) → car faces world -X? No...
- Actually: the rotation matrix maps pool +Z → world -X, pool +Y → world +Z, pool +X → world -Y

### Blender space (Z-up, right-handed)
```
X = RIGHT
Y = FORWARD (into screen from default camera)
Z = UP
```

---

## The Full Data Flow

```
capture.lua (Lua mod)
  → reads pool vertices from GPU mesh (vehicle-local space)
  → reads vehicle position from getPosition()
  → computes rotation quaternion from getDirectionVector() (HEADING ONLY)
  → writes per-frame: [pool_positions (Nx3 float32), vehicle_pos (3 float32), vehicle_rot (4 float32)]
  → capture.bin + capture.meta

cache_builder.py (Python)
  → reads capture.bin via CaptureReader
  → applies vehicle rotation + translation to pool positions
  → optionally applies _world_to_blender conversion
  → writes BVC file

runtime (Blender)
  → reads BVC via CacheReader
  → creates mesh, updates vertex positions per frame
```

**The critical bug is in step 1:** the Lua rotation quaternion only encodes heading, not pitch/roll.

---

## Verified Diagnostic Data

### Vehicle rotation matrix across frames (from capture.bin)
```
Frame 0:   poolX→world-Y  poolY→world+Z  poolZ→world-X   pos=(-0.67, 0.14, 0.27)
Frame 50:  poolX→world-Y  poolY→world+Z  poolZ→world-X   pos=(-0.63, 0.14, 1.11)
Frame 100: poolX→world-Y  poolY→world+Z  poolZ→world-X   pos=(-0.16, 0.20, 1.81)
Frame 200: poolX→world-Y  poolY→world+Z  poolZ→world-X   pos=(1.01, 0.83, 2.45)
Frame 325: poolX→world-Y  poolY→world+Z  poolZ→world-X   pos=(3.39, 4.89, 3.10)
Frame 500: poolX→world-Y  poolY→world+Z  poolZ→world-X   pos=(3.98, 4.92, 3.26)
Frame 649: poolX→world-X  poolY→world+Z  poolZ→world+Y   pos=(92.39, 1.63, 0.18)
```

**Key finding:** The rotation is IDENTICAL for frames 0–500 (only heading, no pitch/roll). Only at frame 649 does it change. This proves `getDirectionVector()` returns a constant heading when the car is airborne — it does NOT capture the tumble.

### Pitch/Roll across frames (from BVC positions)
```
Frame   0: pitch_z=-0.0385  roll_z=+0.0637  height=0.92
Frame 100: pitch_z=-0.0346  roll_z=+0.0618  height=2.46
Frame 325: pitch_z=-0.0349  roll_z=+0.0621  height=3.73
Frame 649: pitch_z=+0.0342  roll_z=+0.0655  height=0.85
```

Pitch and roll are **nearly constant** across the entire sequence. The car doesn't tumble in the BVC — it just translates through space with the same orientation.

---

## Approaches Tried (Chronological)

### Attempt 1: getRotation() — "Returns identity for softbody vehicles"
**What was tried:** Use `veh:getRotation()` in the Lua mod to get the per-frame orientation quaternion.
**Result:** Returns identity quaternion (0,0,0,1) for all frames. BeamNG's softbody vehicles don't populate this API.
**Verdict:** DOES NOT WORK for softbody vehicles.

### Attempt 2: getDirectionVectorUp() — "Returns wrong axis"
**What was tried:** Use `veh:getDirectionVectorUp()` to get the vehicle's up vector, then construct rotation from forward + up.
**Result:** Returns the local Z direction (which is forward, not up). Comment in code: "getDirectionVectorUp() returns the WRONG axis (local Z = forward, not up)".
**Verdict:** DOES NOT WORK — returns forward instead of up.

### Attempt 3: getDirectionVector() + Gram-Schmidt (original approach)
**What was tried:** Use `getDirectionVector()` for forward, Gram-Schmidt with world up (0,0,1) to get up vector, cross product for right. Convert matrix to quaternion.
**Result:** 
- Rotation only captures **heading (yaw)** — the Z component of the direction vector is always 0
- Car faces -X correctly in Blender (after _world_to_blender compensation)
- **But the rotation is frozen** — no pitch, no roll, no tumble
**Verdict:** Orientation OK, animation BROKEN (frozen).

### Attempt 4: _world_to_blender conversion (fixing pool→Blender mapping)
**What was tried:** After Lua rotation + translation, apply `_world_to_blender` to convert world positions to Blender space.
- Initially tried: simple Y↔Z swap `By=Z, Bz=Y` (for glTF→Blender)
- Then tried: `(bx, by, bz) = (-wy, -wx, wz)` — a 90° rotation around Z + handedness correction

**Diagnostic output:**
```
Blender space after _world_to_blender:
  X: span=3.738 (car length = forward axis) ✓
  Y: span=1.736 (car width) 
  Z: span=1.236 (car height, mean=0.924 above ground) ✓
Headlight at X=-1.70, Taillight at X=+2.10 → car faces -X ✓
```

**Result:** Orientation correct (car faces -X, upright). But animation was still frozen because the Lua rotation was wrong.

### Attempt 5: Remove _world_to_blender from runtime
**What was tried:** Move coordinate conversion from runtime to builder (BVC stores Blender-ready positions at write time). Runtime reads directly with no conversion.
**Result:** Cleaner architecture, but didn't fix the animation issue (still frozen rotation from getDirectionVector).

### Attempt 6: GLB builder _gltf_to_blender at write time  
**What was tried:** Apply glTF→Blender conversion in `cache_builder.py` at BVC write time instead of at runtime.
**Result:** Correct for GLB path. Not relevant to the capture path animation issue.

### Attempt 7: getNodePosition() for reference nodes (v5 Lua mod)
**What was tried:** Replace getDirectionVector() rotation with rotation computed from 3 reference node world positions via `veh:getNodePosition(0)`, `veh:getNodePosition(1)`, `veh:getNodePosition(2)`.
**Lua code:**
```lua
local ok1, n0 = pcall(function() return veh:getNodePosition(0) end)
local ok2, n1 = pcall(function() return veh:getNodePosition(1) end)
local ok3, n2 = pcall(function() return veh:getNodePosition(2) end)
-- Build rotation from: forward = n1-n0, right = forward × (n2-n0)
-- Convert matrix to quaternion
```
**Result:** "Both orientation and animation screwed up" per user. The rotation from reference nodes may give a different axis mapping than the old getDirectionVector approach, breaking the _world_to_blender compensation.

### Attempt 8: Priority chain (getRotation → getNodePosition → getDirectionVector)
**What was tried:** Try getRotation() first, then getNodePosition(), then fallback to getDirectionVector().
**Result:** If getRotation() returns non-identity (which it might for some vehicles), it uses a different axis convention than what _world_to_blender expects. If getNodePosition() works, same issue. The _world_to_blender was tuned for the getDirectionVector rotation mapping specifically.

---

## Root Cause Analysis

There are TWO interrelated problems:

### Problem A: The Lua rotation doesn't capture pitch/roll
`getDirectionVector()` projects the forward vector onto the ground plane (Z component is always 0). When the car is flying through the air and tumbling, the heading stays constant but the car is pitching/rolling. The Lua mod only captures the heading → rotation is frozen → animation shows translation without rotation.

**This is the fundamental data problem.** The vehicle transform in the capture doesn't contain the actual per-frame orientation.

### Problem B: The coordinate pipeline is fragile
The `_world_to_blender` conversion was derived to compensate for the SPECIFIC axis mapping of the getDirectionVector rotation:
- getDirectionVector maps pool +Z → world -X (because it treats pool Z as forward, but pool X is actually forward)
- `_world_to_blender (bx=-wy, by=-wx, bz=wz)` compensates for this axis swap

If ANYTHING changes in the Lua rotation computation (different API, different axis convention), the _world_to_blender mapping breaks and both orientation and animation are wrong.

### The catch-22:
- Using getDirectionVector: orientation works (with _world_to_blender), but animation is frozen
- Using getNodePosition/getRotation: animation might work (full 3D rotation), but orientation breaks because _world_to_blender was tuned for the old axis mapping

---

## What Needs to Happen (for next attempt)

### Step 1: Fix the Lua rotation to capture full 3D orientation
Try (in order):
1. `veh:getRotation()` — may or may not work
2. `veh:getNodePosition(0/1/2)` — compute rotation from 3 reference nodes
3. If both fail, try other APIs: `obj.nodePositions`, `core_vehicle_manager` data, physics API

**IMPORTANT:** Whichever method gives a working rotation, you MUST also re-derive `_world_to_blender` for that method's axis convention.

### Step 2: Re-derive _world_to_blender
The `_world_to_blender` conversion depends on HOW the Lua rotation maps pool axes to world axes. 

For the getDirectionVector approach:
```
pool +X → world -Y  (car forward mapped to world right/left)
pool +Y → world +Z  (car up mapped to world up)
pool +Z → world -X  (car right mapped to world forward)
_world_to_blender: (bx=-wy, by=-wx, bz=wz)
```

For a hypothetical correct rotation (pool forward → world forward):
```
pool -X → world forward  (car forward correctly mapped)
pool +Y → world +Z      (car up correctly mapped)
pool +Z → world right    (car right correctly mapped)
_world_to_blender would be different — likely just a handedness flip
```

**You MUST verify the axis mapping by running the diagnostic script and checking that pool axes map to the expected world axes.**

### Step 3: Verify
Run `tests/blender_smoke.py` or equivalent to check:
1. Car faces -X (headlight at -X, taillight at +X)
2. Car is upright (Z mean > 0)
3. Pitch changes across frames (front_z - rear_z varies over time)
4. No barrel roll (left_z - right_z stays roughly constant during a pitch)

---

## File Locations

| File | Purpose |
|------|---------|
| `D:\danish\Games\beamng\BeamNG.drive\lua\ge\extensions\beamng\capture.lua` | **Game dir** — Lua mod (source of truth for capture) |
| `C:\Users\ubaid_i2c\AppData\Local\BeamNG\BeamNG.drive\current\mods\unpacked\TelekinesisController\lua\ge\extensions\beamng\capture.lua` | **User mods dir** — Lua mod copy |
| `C:\Users\ubaid_i2c\Downloads\beamng-cache-importer\capture.lua` | **Repo** — Lua mod copy |
| `C:\Users\ubaid_i2c\Downloads\beamng-cache-importer\importer\cache_builder.py` | Builder — applies Lua rotation + _world_to_blender |
| `C:\Users\ubaid_i2c\Downloads\beamng-cache-importer\runtime\mesh_update.py` | Runtime — creates/updates meshes in Blender |
| `C:\Users\ubaid_i2c\Downloads\beamng-cache-importer\runtime\cache_reader.py` | BVC reader — memmap-based |
| `C:\Users\ubaid_i2c\Downloads\beamng-cache-importer\importer\capture_reader.py` | Capture reader — reads capture.bin/meta |
| `C:\Users\ubaid_i2c\AppData\Local\BeamNG\BeamNG.drive\current\captures\mycap2\capture.bin` | Current capture data |
| `C:\Users\ubaid_i2c\AppData\Local\BeamNG\BeamNG.drive\current\captures\mycap2\out.bvc` | Current BVC |

---

## Test Commands

```bash
# Unit tests (no Blender)
cd C:\Users\ubaid_i2c\Downloads\beamng-cache-importer
python -m pytest -q

# Build addon
python build_addon.py

# Diagnostic: check pool axes and rotation mapping
python C:\Users\ubaid_i2c\AppData\Local\Temp\diag_coords.py

# Diagnostic: check Blender orientation
python C:\Users\ubaid_i2c\AppData\Local\Temp\diag_blender.py

# Blender smoke test (headless)
C:\Users\ubaid_i2c\Downloads\blender-4.5.9-windows-x64\blender-4.5.9-windows-x64\blender.exe --background --python tests/blender_smoke.py
```

---

## What the Capture Format Looks Like

### capture.meta (JSON)
```json
{
  "version": 2,
  "objectCount": 247,
  "objectNames": ["flanje_e180_body", "flanje_e180_body_flanje_e180_paint", ...],
  "totalVertexCount": 533885,
  "totalIndexCount": 1658349
}
```

### capture.bin layout
```
[STATIC HEADER]
  Per-object: index_count (uint32), vertex_count (uint32), indices (uint32[]), UVs (float32[])

[PER-FRAME DATA] × frameCount
  Per-object: positions (float32[vertexCount × 3])
  Vehicle transform: position (3 × float32) + quaternion (4 × float32)
```

The vehicle transform is **7 floats** at the end of each frame block. The quaternion is `(qx, qy, qz, qw)` — the Lua `getDirectionVector` rotation (heading only).

---

## Key Constraints

1. **Pool positions are in vehicle-local space** — they do NOT include the rigid body rotation
2. **The vehicle transform provides the rotation** from local → world
3. **Softbody vehicles in BeamNG** make many rotation APIs unreliable (identity/heading-only)
4. **The builder must convert world→Blender** at write time — the runtime should not need coordinate conversion
5. **The BVC stores per-frame position blocks** — the rotation is baked into the positions at build time
6. **24 unit tests pass** — don't break the GLB path or the capture roundtrip test

---

## What NOT to Do

1. **Don't remove _world_to_blender without re-deriving it** — the current mapping is tuned for the getDirectionVector axis convention
2. **Don't assume getRotation() works** — it returns identity for softbody vehicles
3. **Don't assume getNodePosition() works** — it may or may not be available in the capture callback context
4. **Don't change the pool position capture** — pool positions are correct; only the vehicle rotation is wrong
5. **Don't break the GLB path** — `importer/` must stay bpy-free, GLB builder applies `_gltf_to_blender` independently
6. **Always repack the addon** with `python build_addon.py` after changes
7. **Always update ALL 3 copies** of capture.lua (game dir, user mods dir, repo)
8. **Don't forget the log message** on line 1244 — it's hardcoded separately from the file header comment

---

## Summary of the Fundamental Challenge

The BeamNG capture captures positions in vehicle-local space and a vehicle transform (position + quaternion). The quaternion is SUPPOSED to provide the per-frame orientation, converting local → world. But for softbody vehicles, the only reliable API (`getDirectionVector`) gives only heading (yaw), not the full 3D orientation (pitch + roll + yaw).

This means when the car is launched into the air and tumbles/flips, the capture records the car's path through space but NOT its rotation. The car slides through space with a frozen orientation.

To fix this, we need a way to get the actual per-frame 3D rotation quaternion from BeamNG's physics engine for softbody vehicles. All standard APIs (`getRotation`, `getDirectionVectorUp`) either return identity or heading-only for softbodies.

**Possible approaches not yet tried:**
1. Read the physics engine's node world positions directly and compute rotation from 3+ non-collinear structural nodes using Kabsch algorithm
2. Use `core_vehicle_manager.getVehicleData()` or similar higher-level API
3. Access the simulation state through `obj:getSimData()` or physics extension APIs
4. Compute rotation by comparing pool positions at frame N vs frame 0 for a subset of non-deforming body vertices (Procrustes analysis) — but this only works if the body doesn't deform much during the flight
5. Store raw world positions instead of local + transform — bypass the rotation problem entirely by having the Lua mod read node world positions instead of pool local positions
