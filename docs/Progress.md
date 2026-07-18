# Progress — Capture Pipeline (July 2026)

## What we built (chronological order)

### Phase 1: GLB Pipeline (done, working)
1. **GLB Reader** (`gltf_reader.py`) — pure-Python GLB parser, extracts per-object
   meshes from BeamNG's shared vertex pool via `np.unique` dedup. Cached dedup remap
   gives ~10x speedup on repeated reads.
2. **Scanner** (`scanner.py`) — classifies objects as stable (topology-identical across
   frames) vs dynamic (topology-changing). Emits `SequenceManifest`.
3. **Binary Cache Format** (`binary.py` BVC3) — header + object table + base-mesh
   blocks + UV blocks + material blocks + frame directory + frame position blocks.
4. **Cache Builder** (`cache_builder.py`) — builds BVC from scanned GLB sequence.
   Opt-in weld mode collapses coincident seam vertices at build time.
5. **Runtime** (`cache_reader.py` + `mesh_update.py` + `frame_handler.py`) — Blender-side.
   Creates each mesh once, updates vertices per frame via `position` attribute API.
   Chunked mode merges objects for better viewport FPS.
6. **Baker** (`baker.py`) — bake-to-MDD → MESH_CACHE → Alembic export pipeline.
   Proven working on real data.

### Phase 2: Capture Pipeline (done, working)
7. **Capture Reader** (`capture_reader.py`) — reads `capture.meta` + `capture.bin`
   from BeamNG's Lua capture mod. Footer-based frame offset table, per-frame vehicle
   transforms (v2).
8. **Capture→BVC Direct Path** (`cache_builder.py build_from_capture()`) — bypasses
   scanner + GLB pipeline entirely. Writes BVC directly from capture data.
9. **Lua Capture Mod** (`capture.lua`) — captures ALL material primitives per flexmesh
   from `GPUMesh.bng_getGPUMesh()`. Writes `capture.meta` with `flexmeshIndex` for
   merge grouping, `capture.bin` with per-frame positions + vehicle transforms.

### Phase 3: Geometry Fixes (done, working)
10. **Overflow clamping** — `indicesMinMax` off-by-one fix in Lua mod (exclusive upper
    bound). 19 objects had missing faces. Fixed by capturing with corrected mod + builder
    clamping indices to valid range. **Zero overflow events** after fix.
11. **Merge by flexmesh** (`_merge_capture_by_flexmesh()`) — groups 238 material
    primitives by flexmesh name prefix, concatenates verts/indices per group, keeps
    per-face material IDs as slots on one mesh. 238 → 87 objects.
12. **Weld** — `np.unique` on positions rounded to `WELD_DECIMALS=4` collapses coincident
    seam vertices across primitives. 533K → 313K verts (220K welded). BVC 4.2→2.5 GB.
13. **Frame writer source map** — compact→original mapping stores exactly which capture
    object + vertex provides each compact vertex's position. Clean scatter with no
    uninitialized garbage. See BUGS.md #9.
14. **Auto-smooth / split normals** (`_finalize_mesh` in `mesh_update.py`) — BMesh marks
    edges as sharp where face angle >60°. Fixes tyre tread/sidewall shading seam and
    panel crease rendering.

## Current state

**Capture path with merge+weld is working end-to-end:**

| Metric | Raw primitives | After merge+weld |
|---|---|---|
| Objects | 238 | **87** |
| Verts/frame | 533,937 | **313,465** |
| BVC size | 4,177 MB | **2,455 MB** |
| Overflow events | 19 | **0** |
| Build time | 27s | 55s |

**Test suite:** 24 tests passing (`python -m pytest -q`)
**Blender smoke test:** PASSED
**Addon package:** `dist/beamng_cache_importer.zip` rebuilt

## What's remaining (the last 1%)

### 1. Missing objects (~2-3 parts) — LUA MOD UPDATED, NEEDS RE-CAPTURE

The capture mod has been updated to **v4-props** (2026-07-17). It now captures rigid
props via `meshInfo:propmeshes(i)` / `propmeshesCount` — a separate list from flexmeshes
that BeamNG's own GLB exporter also uses. Props are self-contained with their own
verticesGet/indicesGet/uv1Get plus a per-frame position+rotation transform. Local verts
are baked through the transform at capture time so output positions live in the same
vehicle space as flexmesh verts.

**Lua changes:**
- `transformPropVertex()` — quaternion-rotation + translation helper
- `extractProps()` — iterates `meshInfo:propmeshes(i)`, extracts verts/indices/UVs
- `captureFrame()` — branches on `propCache[obj.name]` to read prop verts + transform
- Static extraction — appends props to metaObjects/staticBins/propCache after flexmesh loop
- `propFmBase = flexmeshesCount + 1000` — unique flexmeshIndex so builder never groups
  a prop with a flexmesh

**What needs to happen:** restart BeamNG with the updated mod, do a fresh capture. The new
capture.meta will include prop objects. No Python changes needed — the builder already
handles them as single-primitive objects.

### 2. Remaining ~17K duplicate verts across objects

After merge+weld, the user reported `Merge by Distance` in Blender still removes
~17,411 vertices. These are duplicate vertices at boundaries between DIFFERENT flexmeshes
(e.g., where the body meets the bumper, where the door meets the fender). The current
merge only groups within ONE flexmesh — it doesn't weld ACROSS flexmeshes.

**Fix options:**
- (a) Cross-flexmesh weld pass: after per-flexmesh merge, do a second weld pass across
  all objects using position rounding. Risk: some legitimately separate parts have
  coincident vertices (e.g., door seam).
- (b) Accept it — 17K out of 313K is ~5%, and Blender's merge-by-distance handles it
  interactively.
- (c) Do the weld in Blender post-import (operator or addon step).

### 3. Lua mod install path

The capture.lua mod is edited in the game source at:
`D:\danish\Games\beamng\BeamNG.drive\lua\ge\extensions\beamng\capture.lua`

And copied to the unpacked mod at:
`C:\Users\ubaid_i2c\AppData\Local\BeamNG\BeamNG.drive\current\mods\unpacked\TelekinesisController\lua\ge\extensions\beamng\capture.lua`

After editing the source, must copy to the mod path AND restart BeamNG for changes to
take effect. The `flexmeshIndex` field in capture.meta requires the updated Lua mod
to be active during capture.
