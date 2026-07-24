# CLAUDE.md — BeamNG Cache Importer

Working memory for AI contributors. Read first.

---

## What this project does

Captures BeamNG.drive crash sequences (deforming mesh + rigid vehicle motion)
and replays them as vertex animation in Blender.

Two capture pipelines, both working:

1. **GLB pipeline** (`capture_gltf_sequence.py` → `build_from_gltf`): pause+step
   or slowmo capture via beamngpy → directory of `.glb` frames → BVC cache.
   Per-object GLB exports give drag-free detached parts. Superior path.

2. **BMC pipeline** (`v5_capture.lua` → `build_from_capture`): GE console Lua
   mod captures GPU pool vertices + rigid transform to `.bmc` → BVC cache.
   Origin-relative translation prevents detached-part drag.

---

## Pipeline

```
BeamNG (GLB)                        BeamNG (BMC)
    │                                   │
    ▼                                   ▼
capture_gltf_sequence.py            v5_capture.lua
    │  (beamngpy + glTF SE)             │  (GE console, GPUMesh API)
    ▼                                   ▼
D:\animation_test\                 captures/test.bmc
  frame_00000.glb ...                (local pool verts +
  frame_00001.glb ...                 origin-relative transform)
    │                                   │
    ▼                                   ▼
cache_builder.py                   cache_builder.py
  build_from_gltf()                  build_from_capture()
    │                                   │
    ▼                                   ▼
animation_test.bvc                test.bvc
    │                                   │
    └──────────┬────────────────────────┘
               ▼
    Blender addon (import + playback)
      CacheReader → CachePlayback → frame_handler
```

---

## File formats

**BMC v1** (`capture.bmc`): Fixed-size frames from the Lua capture mod.
40-byte header + static section (indices, UVs, primitives, materials) +
per-frame blocks (timestamp + local pool positions + 9 f32 rigid transform).

**BVC v4** (`capture.bvc`): Blender vertex cache for runtime playback.
Header + object table + base meshes + frame directory + per-frame position
streams + optional transform data + dynamic object section.

---

## Coordinate conventions

| Space | Up | Handedness | Axes |
|-------|----|-----------|------|
| GPU pool (verticesGet) | Y | left | X=length, Y=up, Z=width |
| BeamNG physics | Z | right | X=right, Y=fwd, Z=up |
| Blender | Z | right | X=right, Y=fwd, Z=up |

Pool→Blender permutation: `(pool_z, pool_x, pool_y)` → `(X, Y, Z)`.
Physics Z-up == Blender Z-up — direction vectors need no permutation.

---

## Capture modes (capture_gltf_sequence.py)

**Deterministic** (`--mode deterministic`): Pause + set 2000 Hz deterministic
physics + step(N) per frame. Precise but changes crash behavior (pausing
alters the BKS Controller mod timing). `--frames 2400` for ~40s crash.

**Slowmo** (`--mode slowmo`): Set timescale via `simTimeAuthority.setInstant()`.
Game runs naturally but slowly. Real physics, no pause artifacts.
`--timescale 0.1` (10x slower), `--frames 2400` for ~40s crash.

Always captures at 60fps (hardcoded `TARGET_FPS = 60`).

---

## Deployed mods

**v5capture** (BMC capture):
```
C:\Users\ubaid_i2c\AppData\Local\BeamNG\BeamNG.drive\current\
  mods\unpacked\v5capture\
    info.json
    lua\ge\extensions\v5capture.lua
```

**glTF Sequence Exporter** (patched for re-export):
```
C:\Users\ubaid_i2c\Downloads\beamng\BeamNG.drive\current\
  mods\unpacked\glTF_Sequence_Exporter\
    lua\ge\extensions\gltfSequenceExporter\export.lua
```
Patched with `beamng_capture_quiet` flag to suppress GE console spam.

---

## State of the build

| Module | State | Notes |
|--------|-------|-------|
| `importer/binary.py` | ✅ | BVC v4 format, backward compat v2/v3 |
| `importer/cache_builder.py` | ✅ | GLB + BMC → BVC, position-only weld (epsilon=1e-5) |
| `importer/capture_format.py` | ✅ | BMC v1 pack/unpack, FLAG_HAS_TRANSFORM |
| `importer/capture_reader.py` | ✅ | BMC reader, shared pool + per-frame positions |
| `importer/gltf_reader.py` | ✅ | GLB reader, glTF→Blender coord conversion |
| `importer/scanner.py` | ✅ | Topology classification (stable/dynamic) |
| `importer/parallel_reader.py` | ✅ | Parallel GLB reading with remap caching |
| `importer/topology.py` | ✅ | SHA-256 topology hashing |
| `importer/materials.py` | ⚠️ | Blender 4.0+ Specular socket issue |
| `runtime/cache_reader.py` | ✅ | BVC memmap reader |
| `runtime/mesh_update.py` | ✅ | Per-frame vertex update, sharp edge marking |
| `runtime/frame_handler.py` | ✅ | Timeline handler, undo/reload recovery |
| `addon/operators.py` | ✅ | Scan/build/import/export/texture operators |
| `addon/ui.py` | ✅ | Panel + scene properties |
| `tools/capture_gltf_sequence.py` | ✅ | beamngpy driver, slowmo + deterministic |
| `tools/v5_capture.lua` | ✅ | BMC capture, origin-relative anti-drag |

### Validation
- 13/13 tests pass (`python -m pytest -q`)
- GLB pipeline: 2000-frame capture at 10x slowmo, 97 objects, 7.4 GB BVC — verified
- BMC pipeline: 700-frame capture, 88 objects, 533K verts, 4.5 GB BVC — verified
- Detached-part drag fixed via origin-relative translation in v5_capture.lua

---

## Conventions
- Python 3.10+, `from __future__ import annotations` at top of every module.
- Dataclasses + type hints. `importer/` must NOT import `bpy` (plain CPython
  for CI). `runtime/` and `addon/` may.
- Tests in `tests/`, run with `pytest`. Keep logic testable without Blender.
- After changes, repack: `python build_addon.py` → `dist/beamng_cache_importer.zip`.

## How to run
- Tests: `python -m pytest -q`
- Build addon: `python build_addon.py`
- Build BVC from GLB: `from importer.cache_builder import CacheBuilder; CacheBuilder(src, dst).build()`
- Build BVC from BMC: same, pass `.bmc` path as src

## Capture
1. Open BeamNG with vehicle spawned
2. GLB: `python tools\capture_gltf_sequence.py --mode slowmo --timescale 0.1 --frames 2400 --out D:\animation_test`
3. BMC: GE console `extensions.load("v5capture"); v5capture.start("captures/name", 300)` → crash → `v5capture.stop()`
4. Build BVC, import in Blender via addon panel

### BeamNG paths
- Game install: `D:\danish\Games\beamng\BeamNG.drive`
- User dir: `C:\Users\ubaid_i2c\AppData\Local\BeamNG\BeamNG.drive\current\`
- Mods: `...\current\mods\unpacked\<name>\`
- Captures: `...\current\captures\`
- Deploy mods to `mods\unpacked\<name>\` with `info.json` + `lua\ge\extensions\<name>.lua`. Restart required.
