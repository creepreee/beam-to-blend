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

## Architecture B — Rigid Motion  ✅ FROZEN v3 (2026-07-19)

Goal: capture the rigid transform BeamNG applies to the local-space mesh.

### Key correction (v3)
The rigid transform must be read from the **vehicle Lua (VLUA) context**,
NOT the gameplay/GE Lua (GELUA) context.

- GELUA `SceneObject:getRotation()` is **static** during softbody simulation
  (measured: `0 0 1 ~0` across many frames). It is the SceneObject wrapper,
  not the live physics transform. **Unsuitable for capture.**
- VLUA `obj:getPosition()` + `obj:getRotation()` (where `obj` is the vehicle
  in the vehicle/physics thread) update **continuously** — roll/pitch/yaw
  change exactly with the vehicle during a crash. **This is the live rigid
  transform.**

The earlier investigation mixed GELUA and VLUA (same API names, different
state). Once the source moved to the vehicle physics thread, the rigid
transform became a live, continuously-updating signal.

### Proven facts (v3)
1. `verticesGet()` is vehicle-local deformation. ✅
2. Rigid translation is NOT baked into the vertices. ✅
3. GELUA `getRotation()` is static → unusable for softbody animation. ✅
4. VLUA `obj:getRotation()` is live and tracks vehicle motion. ✅
5. VLUA `obj:getPosition()` is live and matches vehicle motion. ✅

### Capture frame (v3)
```
Frame
 ├── Position (vec3)      obj:getPosition()
 ├── Rotation (quat)      obj:getRotation()
 ├── Vertex Pool          verticesGet()
 └── (optional metadata)
```
Nothing else: no direction vectors, no cluster rotation, no `quatFromDir`,
no frame-0 reconstruction, no calibration, no `Rdelta`.

### Builder (v3 — simple)
```
read frame
 ├── update mesh vertices (pool → Blender coord conversion, per frame)
 ├── set object location  = position
 └── set object quaternion = rotation
```
No rigid reconstruction, no quaternion guessing, no calibration.

### Coordinate conversion (required, not "reconstruction")
The GPU pool is **left-handed Y-up**. Blender is **right-handed Z-up**.
The per-frame vertices must be converted pool→Blender (a static axis
permutation + handedness flip) before the object transform is applied.
This is a one-time-per-vertex operation on the pool data, NOT a per-frame
rigid solve. The quaternion from VLUA is applied by Blender's object
transform directly; the quaternion component order (x,y,z,w vs w,x,y,z) is
handled at import (see `runtime/mesh_update.py`).

### Removed from architecture
- ❌ `getClusterRotationSlow()`
- ❌ `quatFromDir()`
- ❌ `getDirectionVector()`
- ❌ direction-vector reconstruction
- ❌ frame-zero anchoring / `Rdelta`
- ❌ builder-side rigid reconstruction
- ❌ quaternion guessing
- ❌ calibration-based transform solving

### Remaining validation
Export only `obj:getPosition()` + `obj:getRotation()`, animate a simple cube.
Expected: identical trajectory, heading, roll, pitch. If it passes, the rigid
pipeline is conclusively validated.

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
| `importer/capture_format.py` | ✅ | BMC v1 pack/unpack, frame seek |
| `importer/cache_builder.py` | ✅ | BMC → BVC builder |
| `importer/binary.py` | ✅ | BVC v3 format |
| `importer/capture_reader.py` | ✅ | BMC reader |
| `importer/{materials,topology,scanner,gltf_reader}.py` | ✅ | supporting modules |
| `runtime/cache_reader.py` | ✅ | BVC memmap reader |
| `runtime/mesh_update.py` | ✅ | per-frame vertex update + quaternion import |
| `runtime/{frame_handler,baker,abc_writer,animation}.py` | ✅ | playback, MDD/Alembic export |
| `addon/*` | ✅ | Blender UI panel + operators |
| `tools/diagnostic_dump.lua` | ✅ | **v3 capture**: world-space BMC via refNode matrix (or local BMC via VLUA pos+rot) |
| `capture.lua` + `mods/beamng_capture_v7/` | ✅ | Architecture A GPU-pool capture backend (frozen) |
| `tests/` | ✅ | pytest suite (run `python -m pytest -q`) |

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
