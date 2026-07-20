# CLAUDE.md — BeamNG Cache Importer (Architecture v3)

Working memory for AI contributors. Read first; keep updated when architecture changes.

---

## What this project is

A Blender importer for **BeamNG crash sequences** that captures the deforming
mesh from the GPU vertex pool and the vehicle's rigid transform from the
physics thread, then replays both in Blender.

**Pipeline:**
```
BeamNG vehicle Lua (VLUA)  ──►  capture  ──►  capture.bmc  ──►  Python builder  ──►  BVC  ──►  Blender runtime
   verticesGet()  +                  (BMC v1)        (importer/)            (runtime/)
   obj:getPosition() +                                                            + addon/
   obj:getRotation()
```

Two subsystems, both **frozen**:

- **Architecture A — Mesh Capture** (✅ frozen, complete)
- **Architecture B — Rigid Motion** (✅ frozen v3, this document)

---

## Architecture A — Mesh Capture  ✅ FROZEN

Goal: capture the deforming mesh exactly as BeamNG generates it.

**Proven facts**
- Shared GPU vertex pool is topology-stable: vertex N always represents the
  same physical point across frames.
- `verticesGet()` returns **body-relative local-space** positions (GPU pool,
  left-handed Y-up: X=length/forward, Y=up, Z=width/right).
- Index buffer is byte-identical across all frames.
- Per-primitive extraction, `indicesMinMax`, and welding are NOT needed.
- Builder reconstructs topology from shared-pool index ranges.
- Shared-pool backend requires no weld; CPU backend abandoned.

**Status:** complete. No further changes.

---

## Architecture B — Rigid Motion  ✅ FROZEN v4 (2026-07-19)

Goal: capture the rigid transform BeamNG applies to the local-space mesh.

### Context rule (unchanged from v3)
The rigid transform must be read from the **vehicle Lua (VLUA) context**,
NOT the gameplay/GE Lua (GELUA) context.

- GELUA `SceneObject:getRotation()` is **static** during softbody simulation.
  It is the SceneObject wrapper, not the live physics transform.
- VLUA `obj:getPosition()` / `obj:getDirectionVector()` /
  `obj:getDirectionVectorUp()` (where `obj` is the vehicle in the physics
  thread) update **continuously** with the crash. These are the live rigid
  signals.

### Key correction (v4 — fixes over-rotation)
v3 used `obj:getRotation()` (a **quaternion**). That quaternion is bound to
the vehicle's **refNodes triangle** (a structural node frame), which is
offset from the GPU vertex pool's mesh frame. Applying it directly to the
pool-derived mesh caused an **over-rotation that compounded each frame**
(diagnosed 2026-07-19 via Blender bbox/orientation diagnostics).

v4 drops the quaternion entirely and captures **direction vectors** instead:
- `obj:getDirectionVector()`    → vehicle forward (world space, physics Z-up)
- `obj:getDirectionVectorUp()`  → vehicle up (world space, physics Z-up)

These vectors are **not** refNode-bound to the mesh frame; they describe the
vehicle's true world orientation. The builder reconstructs a clean
orthonormal basis and never compounds a stale offset.

### Proven facts (v4)
1. `verticesGet()` is vehicle-local deformation. ✅
2. Rigid translation is NOT baked into the vertices. ✅
3. GELUA `getRotation()` is static → unusable. ✅
4. VLUA `getRotation()` (quaternion) is refNode-bound → causes over-rotation,
   **dropped in v4**. ✅
5. VLUA `getDirectionVector()` / `getDirectionVectorUp()` are live and
   refNode-offset-free. ✅
6. VLUA `obj:getPosition()` is live and matches vehicle motion. ✅
7. BeamNG itself builds orientation via `quatFromDir(dir, up)` from these same
   two vectors (`ge/spawn.lua`, `gameplay/rally/*`), confirming the approach. ✅

### Capture frame (v4)
```
Frame
 ├── Position (vec3)      obj:getPosition()
 ├── Forward  (vec3)      obj:getDirectionVector()
 ├── Up       (vec3)      obj:getDirectionVectorUp()
 ├── Vertex Pool          verticesGet()
 └── (optional metadata)
```
Stored in BMC as 9 f32: `px,py,pz, fx,fy,fz, ux,uy,uz` (36 bytes). No quaternion.

### Builder (v4)
```
read frame
 ├── update mesh vertices (pool → Blender coord conversion, per frame)
 ├── fwd, up = normalize(direction vectors)
 ├── right = normalize(cross(fwd, up))
 ├── up    = cross(right, fwd)          # re-orthogonalize
 ├── R = [right | fwd | up]              # 3x3, cols = X,Y,Z in Blender
 ├── write transform = [position(3) | R.flat(9)]   # 12 f32 into BVC
```
The basis is already in Blender axes (X=right, Y=forward, Z=up) because
physics Z-up == Blender Z-up (same axes, same handedness). No handedness
flip, no quaternion guessing, no calibration.

### Coordinate conversion (mesh only)
The GPU pool is **left-handed Y-up**. Blender is **right-handed Z-up**.
Per-frame vertices get the static `pool→Blender` permutation
`(pool_z, pool_x, pool_y)`. The direction vectors need **no** conversion —
they are already physics Z-up == Blender Z-up.

### Removed from architecture
- ❌ `obj:getRotation()` (refNode-bound quaternion — over-rotation source)
- ❌ `getClusterRotationSlow()`
- ❌ quaternion guessing / calibration / `quatFromDir` in the builder
- ❌ frame-zero anchoring / `Rdelta`
- ❌ builder-side quaternion passthrough

### Remaining validation
Export pos + forward + up, rebuild BVC, run Blender smoke test + orientation
diagnostic (`tests/blender_debug_transform.py` / `diag_orient.py`). Expect:
trajectory, heading, roll, pitch match the real vehicle with NO compounding
over-rotation (bbox long-axis tracks the car body, not the refNode frame).

---

## File formats

- **BMC v1** (`capture.bmc`): single file, 40-byte header + static section
  (shared indices, UVs, primitive table, material table) + fixed-size
  per-frame blocks (timestamp + positions + transform). Format spec:
  `importer/capture_format.py`.
- **BVC v3** (`capture.bvc`): unchanged. Format spec: `importer/binary.py`.

### Coordinate conventions
| Space | Up | Handedness | Axes | Where |
|-------|----|-----------|------|-------|
| Pool (GPU vertex buffer) | Y | left | X=length/fwd, Y=up, Z=width/right | `verticesGet()` |
| Physics (BeamNG sim) | Z | right | X=right, Y=fwd, Z=up | `obj:getPosition()/getRotation()` |
| Blender | Z | right | X=right, Y=fwd, Z=up | runtime scene |

Pool→Blender permutation: `(pool_x, pool_y, pool_z)` → `(pool_z, pool_x, pool_y)`
i.e. blender `(X, Y, Z)` = `(pool_width, pool_length, pool_up)`.

---

## State of the build

| Module | State | Notes |
|---|---|---|
| `importer/capture_format.py` | ✅ | BMC v1 pack/unpack, frame seek; `FLAG_HAS_TRANSFORM` + `has_transform`; `FLAG_WORLD_SPACE` retained for legacy v3run_world |
| `importer/cache_builder.py` | ✅ | BMC → BVC builder. **Local-pool path (canonical):** pool→Blender perm `(pool_z,pool_x,pool_y)` on verts + per-frame transform block from direction vectors via the PROVEN calibrator basis (`x=fwd, z=up, y=z×x`, re-orthogonalize, stored 3×3 row-major). Legacy world-space path still copies frames verbatim when `FLAG_WORLD_SPACE` set. |
| `importer/binary.py` | ✅ | BVC v3 format |
| `importer/capture_reader.py` | ✅ | BMC reader (`frame_positions(frame, name)`) |
| `importer/{materials,topology}.py` | ✅ | supporting modules |
| `runtime/cache_reader.py` | ✅ | BVC memmap reader; `frame_transform` returns None when `transform_data_offset == 0` |
| `runtime/mesh_update.py` | ✅ | per-frame `foreach_set("co", ...)`; transform applied to a parent empty from the 12-f32 transform block (pos + 3×3). Legacy world-space dumps: no transform, empty stays at origin |
| `runtime/{frame_handler,baker,abc_writer,animation}.py` | ✅ | playback, MDD/Alembic export |
| `addon/*` | ✅ | Blender UI panel + operators |
| `tools/v5_capture.lua` | ✅ | **v5 capture mod source**: LOCAL pool vertices (`verticesGet`, verbatim) + per-frame rigid transform (`getPosition` / `getDirectionVector` / `getDirectionVectorUp`) via `FLAG_HAS_TRANSFORM` (name `v5capture`, NO underscore). Deployed to `...\current\mods\unpacked\v5capture\` |
| `tests/` | ✅ | pytest suite (run `python -m pytest -q`) |

### ✅ Real-capture validation (v5, 2026-07-19)
- Live capture `v3run_world.bmc`: **531 frames**, 88 objects, 533,885 verts,
  `...\current\captures\v3run_world.bmc` (3.25 GB). Produced by `v5capture` mod.
  Header `flags = 17` (`FLAG_HAS_UVS | FLAG_WORLD_SPACE`). Coordinates verified
  sane: frame 0 centroid ~origin, Z extent 1.48 m (height) = Z-up world;
  frame 530 centroid displaced **104 m** (real crash trajectory), no NaN.
- Built to `v3run_world_v2.bvc` (3.4 GB, 88 objects) via
  `CacheBuilder.build_from_capture`. World-space path applies **identity**
  world→Blender (BeamNG world == Blender world: right-handed Z-up, same axes),
  so the BVC is a flat verbatim array stream — no parent empty, no per-frame
  matrix, no quaternion.
- Blender headless smoke test **PASSES** on `v3run_world_v2.bvc`:
  `531 frames, 88 stable, 0 dynamic`; 88 mesh datablocks, no per-frame
  explosion; vertices match cache at frames 0/265/530; body moved 104.36 m.

### ⚠️ CANONICAL design rule (2026-07-20, calibrator-proven — do NOT regress)
Strategy B: **local pool vertices + separable per-frame rigid transform.**
- `tools/v5_capture.lua` dumps LOCAL pool vertices (`verticesGet`, verbatim,
  left-handed Y-up) + a per-frame transform block
  (px,py,pz, fx,fy,fz, ux,uy,uz) from `getPosition()` / `getDirectionVector()` /
  `getDirectionVectorUp()`. `FLAG_HAS_TRANSFORM`. NO world baking, NO
  `getRefNodeMatrix():mulP3F`.
- Builder applies pool→Blender perm `(pool_z, pool_x, pool_y)` to VERTICES only,
  and builds orientation from the direction vectors with the **calibrator-proven
  basis** (validated on the real 533k-vert mesh — drives nose-first, roof-up):
      x = fwd; z = up; y = cross(z,x); z = cross(x,y)   # re-orthogonalize
      rot = [x|y|z] (columns); store rot.T row-major (Blender reconstructs
      columns = x,y,z). det = +1, no mirror.
  Direction vectors need NO permutation (physics Z-up == Blender Z-up).
- Runtime parents all meshes to an empty and applies the 3×3 directly as
  `matrix_basis`. **NO** PCA, **NO** `diag(1,-1,-1)` flip, **NO** quaternion
  guessing, **NO** spawn-flip unwrap (all removed 2026-07-20).
- The abandoned approaches: refNode-bound quaternion (over-rotation), world-space
  verbatim dump (`FLAG_WORLD_SPACE`, kept only for legacy `v3run_world.bmc`), and
  the PCA `L`/`R_true @ L.T` builder path.


---

## Conventions
- Python 3.10+, `from __future__ import annotations` at top of every module.
- Dataclasses + type hints. `importer/` must NOT import `bpy` (plain CPython
  for CI). `runtime/` and `addon/` may.
- Tests live in `tests/`, run with `pytest`. Keep logic testable without Blender.
- After changes, repack: `python build_addon.py` → `dist/beamng_cache_importer.zip`.

## How to run
- Unit tests: `python -m pytest -q`
- Build add-on: `python build_addon.py` → `dist/beamng_cache_importer.zip`
- Blender smoke test: `blender.exe --background --python tests/blender_smoke.py`

## Capture (BeamNG side)
Deploy `tools/diagnostic_dump.lua` (or `capture.lua`) to a mod, load in GE
console, run `start()`, crash, `stop()`. Produces `capture.bmc` (+ `.json`
debug for `diagnostic_dump.lua`).

### ⚠️ REAL mods folder (deploy target)
BeamNG's user path on this machine is **NOT** `Documents\BeamNG.drive\`. The
live mods folder that `core_modmanager.initDB()` actually reads is:
```
C:\Users\ubaid_i2c\AppData\Local\BeamNG\BeamNG.drive\current\mods\
```
Deploy unpacked extension mods to `...\current\mods\unpacked\<name>\` with:
```
unpacked\<name>\info.json
unpacked\<name>\lua\ge\extensions\<name>.lua
```
Anything placed in `Documents\BeamNG.drive\mods\` is IGNORED. `initDB` reads a
VFS snapshot built at boot, so a **full BeamNG restart** is required after
adding a new unpacked mod folder (mid-session `initDB` will not see it).
`extensions.load("<name>")` resolves by extension name against the VFS; `_` in
the name maps to `/` (dir separator), so use names without underscores
(e.g. `v3capture`, not `v3_capture`).

---

## API trial history (what was tried, why it failed, what works)

Full record of every vehicle-transform API call attempted across the v4 and v5
efforts. Read this before touching the capture mod.

### v4 direction-vector / quaternion architecture (BUILT + VERIFIED, then ABANDONED)
- `SceneObject:getRotation()` (GELUA / gameplay VM) — **STATIC** during softbody
  sim; the SceneObject wrapper, not live physics. Unusable.
- `obj:getRotation()` (VLUA, quaternion) — bound to the vehicle **refNodes
  triangle** (structural node frame), offset from the GPU pool mesh frame.
  Applying it compounded an over-rotation every frame (diagnosed 2026-07-19 via
  Blender bbox/orientation diagnostics). **Dropped.**
- `obj:getDirectionVector()` + `obj:getDirectionVectorUp()` (VLUA) — built an
  orthonormal basis `R=[right|fwd|up]`, with PCA mesh-axis alignment (L) and a
  spawn-flip yaw-unwrap. VERIFIED (det=+1, nose→fwd, spawn→-Y, roof→+Z). **Worked
  but architecturally rejected** by the user's hard zero-matrix requirement.
- `obj:getClusterRotationSlow(refNodeId)` (VLUA) — refNode-bound, same offset
  problem. **Dropped.**

### v5 absolute world-space vertex dump (CURRENT)
- `obj:getRenderTransform()` (VLUA) — **NOT EXPOSED**; returns `nil` (0 hits in
  the entire game Lua tree). Because `pcall` returned `ok=true` with a nil
  matrix, the fallback check never fired and `renderMat:transformPosition` was
  called on nil → `attempt to call method 'transformPosition' (a nil value)`.
  **Discarded completely.**
- `MatrixF:transformPosition()` — **DOES NOT EXIST** on the engine MatrixF type
  (0 occurrences in all game Lua). Would reproduce the same nil-method crash.
  The real method is `mulP3F`.
- `obj:getRefNodeMatrix()` (VLUA) **✅ WORKING** — pulled **dynamically inside
  the per-frame loop** (never cached at start), then `renderMat:mulP3F(lx,ly,lz)`
  transforms each pool vertex to absolute world space. Confirmed available &
  live via the `diagtransform` mod (`v:getRefNodeMatrix()` → `:getColumn(3)` for
  position, `:toQuatF()` for rotation). Exposed to the core physics thread:
  initialized instantly, stays active through catastrophic crashes. This is the
  authoritative, crash-proof transform used by `tools/v5_capture.lua`.

### Non-API blockers hit during v5 mod development
- `FLAG_WORLD_SPACE` referenced in `start()` but never declared →
  `bit.bor(nil,...)` crash. Fixed: `local FLAG_WORLD_SPACE = 16`.
- `writeI32` called by `writePrimitive` before its `local` definition → Lua
  scope made it a nil global. Fixed: moved `writeI32` above `writePrimitive`.
- `verticesGet()` (GPU pool) works only in the GE VM via
  `GPUMesh.bng_getGPUMesh`; the capture mod runs in GE and calls it directly. OK.
- Python side: added `FLAG_WORLD_SPACE` + a world-space fast path in
  `cache_builder.py` (no `_pool_to_blender` perm, no transform block) and a
  `has_world_space` property in `capture_format.py`.

### Bottom line
| API | Result |
|---|---|
| GELUA `getRotation()` | static, useless |
| VLUA `getRotation()` (quat) | refNode-offset, compounding over-rotation |
| VLUA `getDirectionVector/Up` | worked; rejected by user (zero-matrix rule) |
| VLUA `getClusterRotationSlow` | refNode-bound, dropped |
| VLUA `getRenderTransform()` | not exposed (returns nil) |
| `MatrixF:transformPosition` | method does not exist |
| **VLUA `getRefNodeMatrix()` + `MatrixF:mulP3F`** | **✅ only correct, live, crash-proof** |

