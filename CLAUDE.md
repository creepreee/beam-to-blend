# CLAUDE.md — Working memory for AI contributors

This file is the shared brain for any AI working on this repo. Read it first. Keep it
updated when you change architecture, conventions, or the current state of the build.
Humans are **not** contributing code here — AI models are. Write for the next model.

---

## What this project is

A Blender importer for **BeamNG crash sequences** that avoids the RAM blow-up of the
naive "one GLB per frame → one mesh datablock per frame" workflow.

**Pipeline:** BeamNG GPU readback → `capture.bmc` (BMC v1) → Python builder → BVC → Blender runtime.

The whole project rests on one validated finding: the shared GPU vertex pool is topology-stable
(vertex N always represents the same physical point). So we store the mesh **once** and cache
only per-frame vertex positions.

### Ground-truth (2364-frame real test)

- Shared pool: **533,885 verts**, **1,658,349 indices**, **byte-identical across all frames**
- 96/97 objects topology-stable; only `flanje_e180_tierod_F` changes (115↔156 at frame 168)
- Max pool drift: 0.10 m over a 93 m tumble (rotation/translation baked out by engine)

---

## Architecture

```
BeamNG Backend                Python Builder                Blender Runtime
(GPU readback via             (BMC v1 → BVC v3)             (BVC → mesh playback)
 bng_getGPUMesh)                     │                              │
       │                    capture_format.py                cache_reader.py
       │                    capture_reader.py                mesh_update.py
  capture.lua               cache_builder.py                 frame_handler.py
       │                                                      baker.py
       ▼                                                      │
  capture.bmc ───────────►  BVC file ─────────────────────►  Blender
```

- **Backend rule (non-negotiable):** capture only copies bytes. No per-primitive decomposition,
  no `indicesMinMax`, no reconstruction, no weld, no repair.
- **Builder rule:** groups primitives by flexmesh index, deduplicates via `np.unique` on
  shared-pool index ranges, converts pool space (Y-up) → Blender space (Z-up). No weld/clamp/repair.
- **Runtime** is unchanged from the GLB pipeline — reads BVC files, doesn't know their provenance.

### File formats

- **BMC v1** (`capture.bmc`): single file, 40-byte header + static section (shared indices, UVs,
  primitive table, material table) + fixed-size per-frame blocks (timestamp + positions).
  All integers little-endian. Format spec: `importer/capture_format.py`.
- **BVC v3** (`capture.bvc`): unchanged. Format spec: `importer/binary.py`.

### Capture rate

GPU readback driven by `onUpdate()` (sim callback, fires every tick at ~60 Hz).
`updateGFX(dt)` is NOT invoked for user extensions — only for built-in modules.
Validation uses a sample-based hash (first/last 1000 uint32 values, not byte-by-byte).
Full byte-loop hash (`hashBytes` with `ffi.cast` per iteration) caused access violation
on large buffers (6.6M iterations); replaced with `hashSample`.

---

## Current state of the build

| Module | State | Notes |
|---|---|---|
| `importer/capture_format.py` | ✅ | BMC v1 pack/unpack, frame seek, round-trip tested |
| `capture.lua` | ✅ | Shared-pool GPU readback, BMC v1 output, per-frame hash validation |
| `importer/capture_reader.py` | ✅ | BmcReader — groups primitives by flexmesh, dedup via np.unique. Standalone prop primitives (flexmesh=-1) get uniquified names to avoid dict overwrite |
| `importer/cache_builder.py` | ✅ | build_from_capture (BMC→BVC) — clean, no weld/clamp/repair. GLB build() kept for legacy |
| `importer/binary.py` | ✅ | BVC v3 format (unchanged) |
| `importer/gltf_reader.py` | ✅ | GLB parser (legacy — for GLB pipeline only) |
| `importer/scanner.py` | ✅ | Sequence scanner (legacy — for GLB pipeline only) |
| `importer/topology.py` | ✅ | TopologyHasher.hash_indices |
| `importer/materials.py` | ✅ | BeamNG .materials.json parser |
| `runtime/cache_reader.py` | ✅ | BVC memmap reader (unchanged) |
| `runtime/mesh_update.py` | ✅ | CachePlayback — position attribute API, UVs, materials (unchanged) |
| `runtime/frame_handler.py` | ✅ | Timeline handler (unchanged) |
| `runtime/baker.py` | ✅ | MDD writer + Alembic export (unchanged) |
| `addon/*` | ✅ | UI panel + operators (unchanged) |
| `tests/` | ✅ | 19 pytest tests passing |
| `tests/scripts/` | ✅ | Verification scripts (see "Verification" section) |

---

## Conventions

- **Python 3.10+**, `from __future__ import annotations` at top of every module.
- Dataclasses for structured data. Type hints everywhere.
- `importer/` must **not** import `bpy` — plain CPython for CI tests. `runtime/` and `addon/` may.
- Tests live in `tests/`, run with `pytest`. Keep new logic testable without Blender.
- **After every change, repack:** `python build_addon.py` → `dist/beamng_cache_importer.zip`.

---

## How to run

- Unit tests (no Blender): `python -m pytest -q` (19 tests).
- Build add-on: `python build_addon.py` → `dist/beamng_cache_importer.zip`.
- Blender smoke test: `blender.exe --background --python tests/blender_smoke.py`
- Master verification (BMC→BVC): `python tests/scripts/run_all_checks.py <capture.bmc> <cache.bvc>`
- Individual verifiers live in `tests/scripts/`.

---

## Verification scripts (`tests/scripts/`)

All scripts are **fully independent** of the builder code (no imports from `cache_builder.py`).
Each defines its own `quat_multiply` and `_Q_AXIS`. Additionally, every script has a
**scipy-only** verification path that uses zero shared math constants — true cross-check.

| Script | What it checks | Independence strategy |
|--------|---------------|----------------------|
| `verify_bmc_to_bvc.py` | Body vertex positions (sampled), transform frames (all 700) | Own quat_multiply + scipy-only path |
| `verify_exhaustive.py` | ALL 88 objects' vertices (sampled), ALL 700 transform frames, scipy cross-check, raw byte-level transform comparison | Three paths: manual, scipy-vs-BVC, byte-level (zero math) |
| `animation_summary.py` | Motion stats from BVC only (no BMC needed) | Reads BVC only — no builder math at all |
| `blender_orientation_only.py` | Frame-0 orientation in Blender scene | Blender's own matrix/quaternion engine — fully independent code path |
| `run_all_checks.py` | Runs all above + pytest, outputs FINAL VERDICT | Shell orchestrator only |

### What "independent" means

1. **No shared imports:** zero scripts import `cache_builder.py`.
2. **Own rotation math:** each defines `quat_multiply` from scratch using basic numpy.
3. **Scipy-only path:** computes expected quaternion using ONLY `scipy.spatial.transform.Rotation` — no manual `_Q_AXIS` constant involved. Compares scipy result directly against BVC stored data.
4. **Byte-level check:** reads raw bytes of transform data from BMC, computes expected bytes after axis conversion, compares against BVC raw bytes. Zero conceptual math — pure bit comparison.
5. **Blender path:** uses Blender's C++ matrix/quaternion engine to import BVC and read back transforms. Completely independent codebase.

---

## Coordinate conventions (critical — get this right every time)

### Spaces

| Space | Up | Handedness | Axes | Where used |
|-------|----|-----------|------|------------|
| **Pool** (GPU vertex buffer) | Y | left-handed | X=forward-ish, Y=up, Z=right-ish | `capture.lua` readback; `capture.bmc` vertex positions |
| **Physics** (BeamNG sim) | Z | right-handed | X=right, Y=forward, Z=up | Vehicle transform in `capture.bmc` |
| **Blender** | Z | right-handed | X=right, Y=forward, Z=up | Runtime scene |

### Conversions applied by builder (`importer/cache_builder.py`)

1. **Positions** — pool Y-up → Blender Z-up via `_pool_to_blender`:
   `(z_pool, x_pool, y_pool)` = `(x_blender, y_blender, z_blender)`.
   - Pool: X=length (car forward), Y=up, Z=width (car right).
   - Blender: X=right, Y=forward, Z=up.
   - So pool Z (width) → blender X (right), pool X (length) → blender Y (forward), pool Y (up) → blender Z (up).

2. **Transform position** — physics Z-up → Blender Z-up via cyclic perm:
   `p_blender = [p_z, p_x, p_y]` — maps physics Z→Blender X, X→Y, Y→Z.

3. **Transform quaternion** — physics → Blender via conjugation:
   `q_blender = Q_AXIS * q_phys * Q_AXIS_INV` where `Q_AXIS = [0.5, 0.5, 0.5, 0.5]` = 120° around `(1,1,1)/√3` = cyclic perm mapping physics X→Blender Y, Y→Z, Z→X.

### Rest orientation (validated)

- Car model faces **−X in pool space** (front vertices at most negative X).
- After `_pool_to_blender` (z,x,y): pool X→blender Y, pool Z→blender X, pool Y→blender Z. So car front = −Y_blender, car right = +X_blender, car up = +Z_blender.
- At rest (q_phys ≈ identity): q_blender ≈ identity, car's local frame aligns with world (car's +X=+X_world, +Y=+Y_world, +Z=+Z_world). The MODEL's front is at the −Y extreme of its local frame.
- In plain English: car's nose points −Y_world, roof points +Z_world, right side points +X_world at rest.

### Pool and physics spaces use the same (z,x,y) perm

Both `_pool_to_blender` and the position transform use `(z, x, y)` — this is NOT a coincidence. The pool stores vertices in world-space positions (Y-up) after physics deformation. The builder converts pool Y-up → Blender Z-up via `(z,x,y)`, and the vehicle transform also converts physics Z-up → Blender Z-up via `(z,x,y)`. They are consistent because pool space Y-up and physics space Z-up describe the same world, just with different up-axes.

---

## Environment

Primary dev machine is **Windows** (PowerShell). Repo under
`C:\Users\ubaid_i2c\Downloads\beamng-cache-importer`.
Blender 4.5.9 at `C:\Users\ubaid_i2c\Downloads\blender-4.5.9-windows-x64\`.
