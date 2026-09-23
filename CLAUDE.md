# CLAUDE.md — BeamNG Cache Importer

Working memory for AI contributors. Read first.

---

## What this project does

Captures BeamNG.drive crash sequences (deforming mesh + rigid vehicle motion)
and replays them as vertex animation in Blender.

**One capture pipeline (BMC), as of 2026-09:** the GLB pipeline
(`capture_gltf_sequence.py` + `gltf_reader` + `parallel_reader`) was REMOVED
for publication — it depended on a patched third-party glTF Sequence Exporter
mod and duplicated the capture path. Deleted modules: `tools/capture_gltf_sequence.py`,
`importer/gltf_reader.py`, `importer/parallel_reader.py`, the GLB branch of
`CacheBuilder.build_from_gltf`, the `SequenceScanner` GLB walk, and the add-on's
scan operator + manifest stash. Old copies live in `archive/` (gitignored).

```
BeamNG (v5capture console mod)
    │  GPUMesh API + origin-relative rigid transform
    ▼
captures/<name>.bmc
    │
    ▼
cache_builder.py  build_from_capture()   (the ONLY build path)
    │
    ▼
<name>.bvc
    │
    ▼
Blender addon (import + playback)
  CacheReader → CachePlayback → frame_handler
```

---

## File formats

**BMC v1** (`*.bmc`): Fixed-size frames from the Lua capture mod. The
extension writes ONE flat file named after the `v5capture.start` path
(e.g. `v5capture.start("captures/my_crash", N)` → `<userfolder>/captures/
my_crash.bmc`). 40-byte header + static section (indices, UVs, primitives,
materials, optional prop section) + per-frame blocks (timestamp +
shared-pool positions + 9 f32 rigid transform). v5 flag `FLAG_WORLD_SPACE` =
vertices already in Blender Z-up world space.

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

The v5 Lua capture applies `world = (x, -z, y)` after `getRefNodeMatrix()`,
baking the Y-up→Z-up rotation into the stored coordinates — so `.bmc` vertex
data is already Blender-world and `_pool_to_blender` handles the final
permutation `blender = (pool_x, -pool_z, pool_y)`.

**Detached-part anti-drag lives in the BMC pipeline.** `tools/v5_capture.lua`
stores `(getPosition() - originWorld)` per frame; a part resting in the world
keeps `map(pool) + getPosition` constant, so it does not drag. Verified
(measured: exporter map puts the nose ~0.7° on heading; the old map yawed 90°
and dragged parts).

---

## Capture (v5capture mod)

Deployed to `%LOCALAPPDATA%\BeamNG\BeamNG.drive\current\mods\unpacked\v5capture\`:

```
info.json
lua\ge\extensions\v5capture.lua
```

1. Spawn a vehicle in BeamNG
2. GE console: `extensions.load("v5capture"); v5capture.start("captures/name", 300)`
3. Crash the car
4. `v5capture.stop()` (or let it run to the frame limit)

Output: `...\current\captures\<name>.bmc` at 60 fps (the `start` path is
used verbatim, so `v5capture.start("name", 300)` would write straight to the
user folder).

---

## State of the build

| Module | State | Notes |
|--------|-------|-------|
| `importer/binary.py` | ✅ | BVC v4 format, backward compat v2/v3 |
| `importer/cache_builder.py` | ✅ | BMC → BVC only; `build()` accepts a .bmc path OR the folder containing it; position-only weld (epsilon=1e-4, cross-frame-safe) |
| `importer/capture_format.py` | ✅ | BMC v1 pack/unpack, FLAG_HAS_TRANSFORM/PROPS/WORLD_SPACE |
| `importer/capture_reader.py` | ✅ | BMC reader, shared pool + per-frame positions |
| `importer/materials.py` | ⚠️ | Blender 4.0+ Specular socket issue; `find_beamng_install` scans env vars + all drive letters (no personal paths) |
| `importer/scanner.py` | ✅ | Manifest dataclasses + `_edge_count` only (GLB scanner removed) |
| `importer/topology.py` | ✅ | SHA-256 topology hashing |
| `runtime/cache_reader.py` | ✅ | BVC memmap reader |
| `runtime/mesh_update.py` | ✅ | Per-frame vertex update, sharp edge marking, smooth-stop swing tail; `CHUNK_MAP_E180` is a FALLBACK chunk map — other cars import per-object automatically |
| `runtime/debris_retime.py` | ✅ | Rescales baked debris/particle keys on live fps/start changes |
| `runtime/frame_handler.py` | ✅ | Timeline handler, undo/reload recovery, live start/fps/tyre/smooth-stop retune, render_pre path |
| `addon/operators.py` | ✅ | Build/import/export/texture/debris operators; build operator is BMC-only (no scan step) |
| `addon/ui.py` | ✅ | Panel + scene props + Tyre/Debris/Physics sub-panels; `workers` prop kept inert for old .blends |
| `tools/v5_capture.lua` | ✅ | BMC capture, origin-relative anti-drag |
| `tools/rebuild_cache.py` | ✅ | argparse: `--captures <dir> --name <name> [--no-swap]` |

### Removed for publication (2026-09-21)

- `tools/capture_gltf_sequence.py` — GLB capture driver (beamngpy + patched
  glTF SE mod). User decision: BMC pipeline only.
- `importer/gltf_reader.py`, `importer/parallel_reader.py`, `SequenceScanner.scan()`,
  `CacheBuilder.build_from_gltf()`, add-on scan operator + manifest stash.
- ~50 one-off diagnostic scripts from `tests/` and all private session docs
  (personal paths) → `archive/` (gitignored, still on disk).
- Synthetic test fixtures now build real `.bmc` captures via
  `tests/bmc_fixtures.py` (moving car, no BeamNG needed).

---

## Car compatibility

The BVC/BMC pipeline is car-agnostic. `validate_chunk_map()` drops names not
in the cache, so any vehicle imports; only *chunked* mode's merging benefits
from a car-specific map. `CHUNK_MAP_E180` (one dev vehicle) is the default;
other cars silently import per-object. To support chunking for a new car,
pass a custom map: `CachePlayback(reader, chunk_map={...})`.

---

## Live-retunable UI knobs (no re-import)

| Field | Entry point |
|-------|-------------|
| Start at Frame | `frame_handler.update_start_frame()` |
| Playback Speed / Output FPS | `frame_handler.update_fps()` |
| all Tyre Contact fields | `frame_handler.update_tyre()` |
| Smooth Car Stop / Stop Frames | `frame_handler.update_smooth_stop()` |

**The start offset is stored in FRAMES** (`_start_frame`). `update_start_second()`
converts seconds for older .blends. Tradeoff: an Output FPS change keeps the
frame NUMBER, so set the start after settling on Output FPS.

**Every retune must end in `_refresh_current_frame()`.** The playhead usually
does *not* move, so no frame-change handler fires and the viewport keeps the
previous settings. `_apply_timeline` also clamps the playhead inside the new
range — outside it, every frame maps to cache frame 0 (frozen car).

All values persist as `_beamng_*` scene custom props for undo/reload recovery.

## Reload recovery (`_try_recover`)

1. **The add-on must be enabled in saved preferences** — recovery hangs off
   `load_post`. A disabled add-on registers nothing; the .blend itself is fine.
2. **`_try_recover` must rebind EVERY piece of module state** — including
   `playback._transform_empty` (the `<collection>__root` Empty). Missing it
   gives "car deforms correctly but sits at the origin".

`tests/blender_reload_root.py` asserts both, per-object and chunked mode.

The recovery link is **soft**: the .blend stores only the BVC *path*.

### Background mode gotchas (fixed)

1. **`load_post` fires in background with the FILEPATH STRING as arg** (not a
   scene). `_on_load_post` resolves the scene defensively.
2. **Dual-module identity trap**: the add-on's `__init__` plants sys.modules
   aliases (`importer`, `runtime`, all submodules) BEFORE importing
   ui/operators, so `beamng_cache_importer.runtime.x` and `runtime.x` are one
   module object. Render scripts must import through the installed add-on.

## Renders move the car (`_on_render_pre`)

1. GUI renders run Python handlers on the WM JOB THREAD; the frame-change
   path refuses non-main threads (Mantaflow guard), so `_on_render_pre`
   applies `_cache_frame_for(scene.frame_current_float)` on WHATEVER thread
   it is invoked on.
2. `render_pre` fires once per rendered frame BEFORE depsgraph evaluation —
   authoritative for what reaches the engine.

- Double application is fine (CachePlayback early-returns on unchanged frame).
- Subframes map via `frame_current_float`.
- `_skip_n` never applies to renders.
- **View > Viewport Render Animation is NOT fixable from a handler** when the
  viewport is Rendered + Cycles (upstream never re-evaluates; a keyframed cube
  freezes there too). Workaround: Material Preview / Solid shading.

## Smooth car stop (`runtime/mesh_update.py` tail path)

Fitted damped-sine continuation of the car's residual oscillation
(`_compute_tail_fit` over `_TAIL_FIT_WINDOW = 24` cache frames; raised-cosine
envelope). The seam defaults to the last captured frame; **Start at Frame**
can pull it earlier. UI fields are timeline frames; conversions in
`frame_handler._smooth_stop_tail_cache()` / `_smooth_stop_start_cache()`.
A stopped car stops (below `_TAIL_AMPLITUDE_EPS` the tail is a static hold).

## Debris retime (`runtime/debris_retime.py`)

Debris keys are baked to absolute TIMELINE frames at build time;
`retime_debris` affinely rescales them about the start frame on live
fps/start changes (`f_new = start_new + (f_old - start_old) * scale`).
Incremental (record rewritten each call). Old builds without the record can
be recovered by fitting the `debris_<mat>_<spawn_frame>_NNN` name suffixes.
Particle *trajectories* are still solver-integrated at scene fps (limitation).

## Tyre ground contact (`runtime/tyre_deform.py`)

Fake contact patch + static deflection + horizontal sidewall bulge +
lift-off release, computed per frame from that frame's geometry (stateless —
a lifted tyre is bit-exactly round again). `amount=0` short-circuits.
Gotchas: bulge is horizontal (project axle into ground plane first), heights
in float64, chunked mode deforms per *member*, auto-ground lives in
`obj.location` and is passed to Alembic bake as `height_bias`.
`MAX_SQUASH_RATIO = 0.35` caps patch depth.

---

## Tests

- `python -m pytest -q` — pure-Python suite (no Blender needed).
- Headless Blender scripts (run with the repo FIRST on sys.path so they test
  the working tree, not the installed add-on):
  - `tests/blender_render_playback.py` — REAL Cycles renders incl. job-thread
    renders, stills, post-undo recovery (self-contained BMC fixture).
  - `tests/diag_render_thread.py`, `tests/diag_viewport_render.py` — thread
    instrumentation / viewport-render diagnostics (viewport one must run
    WINDOWED).
  - `tests/blender_smooth_stop.py`, `tests/blender_start_second.py`,
    `tests/blender_playback_fps.py`, `tests/blender_tyre_contact.py` — need a
    real capture `.bvc` (pass after `--`).
  - `tests/bmc_fixtures.py` — synthetic BMC writer (`moving_car_sequence`).
- After changes, repack: `python build_addon.py` → `dist/beamng_cache_importer.zip`.

## How to run
- Tests: `python -m pytest -q`
- Build addon: `python build_addon.py`
- Build BVC from BMC: `from importer.cache_builder import CacheBuilder; CacheBuilder(captures_dir, dst).build(bmc_path)` (or `.build()` with the folder)

## Conventions
- Python 3.10+, `from __future__ import annotations` at top of every module.
- Dataclasses + type hints. `importer/` must NOT import `bpy` (plain CPython
  for CI). `runtime/` and `addon/` may.
- No personal filesystem paths in tracked files — use env vars
  (`BEAMNG_HOME`, `BEAMNG_USER_DIR`, `BEAMNG_TEST_BVC`, `BEAMNG_TEST_BLEND`)
  or argparse.
- Private session notes/diag scripts live in `archive/` (gitignored).
