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
| `runtime/mesh_update.py` | ✅ | Per-frame vertex update, sharp edge marking, smooth-stop swing tail |
| `runtime/debris_retime.py` | ✅ | Rescales baked debris/particle keys on live fps/start changes |
| `runtime/frame_handler.py` | ✅ | Timeline handler, undo/reload recovery, live start/fps/tyre/smooth-stop retune |
| `addon/operators.py` | ✅ | Scan/build/import/export/texture operators |
| `addon/ui.py` | ✅ | Panel + scene properties + Tyre Contact sub-panel |
| `tools/capture_gltf_sequence.py` | ✅ | beamngpy driver, slowmo + deterministic |
| `tools/v5_capture.lua` | ✅ | BMC capture, origin-relative anti-drag |

---

## Live-retunable UI knobs (no re-import)

These panel fields have `update=` callbacks that push straight into the running
`frame_handler`, so dragging them retunes the imported cache in place:

| Field | Entry point |
|-------|-------------|
| Start at Frame | `frame_handler.update_start_frame()` |
| Playback Speed / Output FPS | `frame_handler.update_fps()` |
| all Tyre Contact fields | `frame_handler.update_tyre()` |
| Smooth Car Stop / Stop Frames | `frame_handler.update_smooth_stop()` |

**The start offset is stored in FRAMES** (`_start_frame`), and `_frame_start` is
just a copy of it. It used to be seconds, so that cache frame 0 held its *time*
across an Output FPS change — but typing 500 then meant 500 *seconds* (frame
30000 at 60 fps). The timeline shows frames, so the field means frames. The
deliberate tradeoff: an Output FPS change now keeps the frame NUMBER, so set the
start after settling on Output FPS. `update_start_second()` still converts, for
older .blends.

**Every retune must end in `_refresh_current_frame()`.** This is the one rule
that gets forgotten, and it fails identically for all three knobs: the playhead
usually does *not* move, so no frame-change handler fires and the viewport keeps
showing the previous settings. The field then looks dead and the only apparent
fix is a re-import — which costs re-linking all the materials.

Both fps values are inputs to `_cache_frame_for`, so this bites `update_fps` as
hard as the others: at frame 300, Playback Speed 24 → 15 remaps cache frame
120 → 75. Refresh in place rather than sliding the playhead to hold the current
cache frame — that also gives the slider feedback while dragging.

`_apply_timeline` also clamps the playhead back inside the new range — outside
it, every frame maps to cache frame 0 and the car looks frozen.

Everything is persisted as `_beamng_*` scene custom props so undo/reload
recovery restores the *live* values, not the ones present at import.

## Reload recovery (`_try_recover`)

Two separate failure modes, both of which look like "the animation vanished":

1. **The add-on must be enabled in saved preferences.** Recovery hangs off
   `load_post`, so a disabled add-on registers nothing and the reopened file has
   97 objects, an empty `frame_change_pre`, and zero motion. Nothing is wrong
   with the .blend. Verified: with the add-on ticked, both the double-click and
   File > Open paths recover fully.
2. **`_try_recover` must rebind EVERY piece of module state**, not just the mesh
   dicts. It rebuilds `_objects` / `_chunks` / `_chunk_member_ranges`, and it
   must also re-bind `playback._transform_empty` to the `<collection>__root`
   Empty. That field is module state; the Empty and the parenting *are* saved in
   the .blend, so missing it produces the deceptive symptom **"car deforms
   correctly but sits at the origin"** — `_apply_transform` returns early on
   `_transform_empty is None` while vertex playback carries on normally.

Deformation and rigid motion travel through different paths, so **a
deformation-only assertion cannot see a dead root**. `tests/blender_reload_root.py`
asserts both, in per-object and chunked mode (measured 8.828 m of root travel;
0.000 m before the fix).

The recovery link is **soft**: the .blend stores only the BVC *path* (46 MB, not
4.27 GB). Move or rename the cache and `_try_recover` returns False silently.

## Smooth car stop (`runtime/mesh_update.py` tail path)

Without it, the crash ends with the car frozen mid-pose the instant the last
captured frame plays. With "Smooth Car Stop" on, the timeline is extended by
`Stop Frames` past the last captured frame and the car **keeps the little swing
it was still rocking through when the capture ended, and that swing gradually
decreases and then stops**. The tail is a *fitted damped-sine continuation* of
the car's residual oscillation — NOT a rigid decelerate-and-halt.

- **Frames vs cache units.** The UI field is in *timeline* frames; the playback
  needs *cache* frames (`tail_cache = tail_frames * playback_fps / output_fps`).
  `frame_handler._smooth_stop_tail_cache()` does the conversion; both `attach()`
  and the live `update_smooth_stop()` pass the result to
  `CachePlayback.set_smooth_stop()`, and the scene prop
  `_beamng_smooth_stop_frames` (TIMELINE frames) persists it for undo/reload
  recovery.
- **The fit.** `_compute_tail_fit()` (called by `set_smooth_stop`) fits a damped
  sine to the last `_TAIL_FIT_WINDOW = 24` cache frames of the ROOT translation
  (real captures end mid-swing: the test cache rocks at period ~10 frames,
  amplitude ~3 mm at capture end). A frequency grid
  (`_TAIL_PERIOD_GRID = np.linspace(4.0, 30.0, 40)`) minimises joint SSE; if the
  series is too short or the variance is below `_TAIL_AMPLITUDE_EPS = 1e-4`
  (`_fit_sine_at` returns None) there is no swing to continue and the tail just
  holds the final rigid pose. Each axis is fitted as
  `c + Ac·cos(ωt) + As·sin(ωt)` with an absolute phase `t = (frame - start)`;
  rotation is the vector part of `q_n · q_mean⁻¹` per component, plus the sign-
  aligned mean quaternion `_average_quaternion(qs)`.
- **The continuation.** For cache position `s` past the end: `u = s / tail`,
  `env = (1-u) · exp(-_TAIL_LAMBDA · u)` with `_TAIL_LAMBDA = 1.5`. The root
  pose is `c + (Ac·cos(ωt) + As·sin(ωt)) · env` (position) and
  `q_mean · Quaternion((1, vx, vy, vz)) · normalized()` (rotation); vertices get
  a scalar profile `k = (1/ω)·sin(ω·s)·env` → `k(0)=0`, `k'(0)=1` (velocity
  continuity), so the swing continues the residual vertex motion, sweeps through
  the oscillation centre (motion REVERSES), and returns to the last-frame pose
  at rest. `env` is exactly 0 at `s >= tail`, so the pose converges to the
  fitted centre and velocity → 0 — a natural settle, not a brake.
- **A stopped car stops**: if the fitted amplitude is below the epsilon there is
  no oscillation to continue and the tail is a static hold (correct physics, not
  a bug). The test cache is still swinging at the end, so the continuation is
  measurable there.

## Debris retime (`runtime/debris_retime.py`)

The car is animated *procedurally* — `frame_handler._cache_frame_for` maps the
playhead to a cache frame every frame, so changing Playback Speed / Output FPS
/ Start at Frame re-times it for free.  Debris is the opposite: `build_debris`
*bakes* each impact to an absolute TIMELINE frame (`_blender_frame_for` at
build time), freezes the rigid-body sim into F-curves, and stamps particle
emitters with absolute `frame_start` / `frame_end` / `lifetime`.  Change fps
after a build and the car re-times while every debris keyframe stays put — the
shards fire at the wrong moment.  `retime_debris` fixes that:

- **The record.** `record_build_timing(scene, ...)` stores the build-time
  `(frame_start, playback_fps, output_fps)` as `_beamng_debris_build_*` scene
  props (survives save/undo).  Called at the end of a successful debris build.
- **The retime.** On live fps/start changes `frame_handler._retime_debris`
  calls `retime_debris(scene, live...)`, which affinely rescales every debris
  key about the start frame so each key keeps the CACHE frame it was baked
  for: `f_new = start_new + (f_old - start_old) * scale` with
  `scale = (pb_old/out_old) / (pb_new/out_new)`.  Bezier handles move with
  their key (otherwise rescaling shears the curves).  Covers F-curves, particle
  windows, `_beamng_debris_launch` props, and the rigid-body world cache range.
  Incremental: the record is re-written after each call, so slider drags
  compose instead of squaring the scale; `_EPS` no-ops an unchanged mapping.
- **Old builds (no record).** Debris built before this module existed has no
  `_beamng_debris_build_*` props → `retime_debris` skips (nothing to retime
  *from*).  The debris object names carry the build-time timeline spawn frame
  (`debris_<material>_<spawn_frame>_NNN`, same suffix repeated by the
  emitters), and the impacts' cache frames are in the BVC; fitting
  `spawn = start + cache * (out/pb)` over hero suffixes recovers the build
  mapping.  Verified exact on real data (spawn = 400 + cache·3).  Manual
  recovery: `blender --background --python tests/fix_debris_resync.py -- <file.blend>`
  (records the derived mapping, retimes to the live values, asserts alignment,
  saves).
- **Particles limitation.** Emission timing re-syncs, but particle trajectories
  are solver-integrated at `scene.render.fps` — their fall speed does not slow
  with the car.  Baked rigid-body shards are the only full slow-motion.

## Tyre ground contact (`runtime/tyre_deform.py`)

BeamNG's tyre mesh is **rigid** — a loaded tyre never shows a contact patch, the
round mesh just sinks into the ground as the hub deflects. This fakes the
missing rubber at playback time, driven only by how the cached wheel geometry
sits relative to a horizontal ground plane. Four terms:

| Term | UI field | What it does |
|------|----------|--------------|
| contact patch | (implicit) | verts below ground projected onto it — patch width tracks real physics load, needs no tuning |
| static deflection | Static Deflection | extra squash of the lower carcass, weighted by depth below the axle, so a *resting* tyre also flattens |
| sidewall bulge | Sidewall Bulge | displaced rubber pushed out **horizontally** along the axle (see gotcha) |
| lift-off release | Lift-off Release | ramps every term to exactly 0 by `release` metres above ground |

**Stateless by design.** Each frame is computed from that frame's geometry
alone — nothing accumulates. That's what makes a lifted car's tyres go
*bit-exactly* round again instead of holding a flat spot from the start.

**`amount=0` short-circuits before any work** and returns the input array
identity, so the feature off is byte-identical to pre-feature playback.

### Gotchas
- **Bulge must be horizontal.** A cambered/steered axle tilts out of the ground
  plane; bulging along it shoves sidewall verts back *down through* the ground
  the patch step just lifted them onto (measured 3.5 mm of re-penetration on
  real data). `flatten_tyre` projects the axle into the ground plane first.
- **Heights in float64.** `height_offset` carries the vehicle's world position
  (can be 100s of m) while the deformation is sub-cm; float32 cancellation cost
  ~0.5 mm and visibly roughened the patch.
- **Chunked mode deforms per *member*, not per chunk.** All four tyres plus the
  rigid rims/hubs/brakes share one `wheels` mesh; a whole-chunk deform would
  smear one axle+depth across all of them.
- **Auto-ground runs after `build_scene`'s `set_frame(0)`**, and lives in
  `obj.location` (outside the vertex data). Playback folds in `obj.location @ up`
  and re-runs frame 0; the Alembic bake needs it passed as `height_bias`.
- `MAX_SQUASH_RATIO = 0.35` caps the patch depth so a wrong `ground_z` gives a
  slightly over-squashed tyre, not a pancake.

Name filter (`tire,tyre` by default) keeps rims/hubs/brakes rigid. Alembic
export bakes the same deformation into the .mdd, so renders match the viewport.

### Validation
- 74/74 tests pass (`python -m pytest -q`)
- Tyre contact verified headless against real capture data (97 objects, 4 tyres):
  flat patch spans 0.000 mm, no ground penetration, 93 non-tyre objects
  bit-identical, all 20 rigid members inside the merged `wheels` chunk
  untouched, airborne tyres restored exactly, live slider retune reaches the
  mesh. Run:
  `blender --background --python tests/blender_tyre_contact.py -- <cache.bvc>`
  (the script evicts an installed add-on's bundled `runtime`/`importer` from
  `sys.modules` — otherwise it silently tests the deployed build)
- Live "Start at Frame" verified headless (21 checks, 97 objects): range shifts
  by exactly the offset with duration unchanged, geometry at `frame_start+k` is
  bit-identical before and after the shift (slid in time, not resampled), the
  parked playhead's mesh updates without scrubbing, the offset survives an
  output-fps change as the same frame number, undo recovery keeps the live
  offset. Run:
  `blender --background --python tests/blender_start_second.py -- <cache.bvc>`
- Live "Playback Speed" / "Output FPS" verified headless (20 checks, 97 objects,
  1200-frame capture): the parked playhead's mesh refreshes without scrubbing and
  matches scrubbing to the remapped cache frame bit-identically, half speed
  doubles the duration, Output FPS moves `render.fps` without changing wall-clock
  duration, the start offset survives, a round trip restores range + geometry
  exactly, undo recovery keeps the live fps. Run:
  `blender --background --python tests/blender_playback_fps.py -- <cache.bvc>`
  Negative control: deleting the `_refresh_current_frame` call fails exactly the
  3 staleness assertions and nothing else — the other 17 pass without it, so only
  those 3 actually cover the bug.
- Live "Smooth Car Stop" verified headless (23 checks, 97 objects, 1200-frame
  capture): the timeline extends by exactly the requested frames and the value is
  persisted for recovery; the tail FITS the car's residual oscillation (period
  ~10 frames, amplitude 3.13 mm) and SWINGS through its rest centre and back
  (motion reverses — not a rigid glide); both the vertex deformation and the root
  pose lose velocity as the envelope decays and settle ON the fitted centre
  (~0.001 mm off) once past the tail; retuning the tail length while parked
  mid-tail refreshes the parked playhead's swing pose; toggling OFF clamps the
  playhead back and returns the mesh to the exact final captured pose;
  undo/reload recovery restores the LIVE tail and reproduces the swung pose
  bit-identically. Run:
  `blender --background --python tests/blender_smooth_stop.py -- <cache.bvc>`
  Negative control: disabling `_refresh_current_frame` in `update_smooth_stop`
  fails exactly the 2 parked-refresh/toggle-off assertions and nothing else —
  the other 21 pass without it.
- Deform cost ~2.2 ms/frame for 4 tyres / 1024 verts (bulge dominates; the
  `axle_axis` eigensolve is 0.36 ms of it)
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
