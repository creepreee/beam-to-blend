# Capture Geometry Issues

## Status

**Root cause found and fixed (2026-07-16).** Diagnostic logging added to both Lua mod
and Python builder. Remaining ~6% geometry issues are from material-primitive splitting.

## Root Cause: `indicesMinMax` off-by-one in Lua mod

The Lua mod called `meshInfo:indicesMinMax(si, si + ic - 1)` with an **inclusive** upper
bound, but BeamNG's API uses **exclusive** upper bounds. This missed scanning the last
index position, causing `maxIdx` to be 1 less than the true maximum.

**Effect:** For 19 of 238 primitives, one vertex was missing from the extracted mesh.
Any face referencing that vertex became degenerate (clamped to vc-1) and was filtered
out by the builder's degenerate face removal. This caused:
- 1 missing face on each tyre (visible grey/white line at tyre seam)
- Missing face on doorglass_FR near side mirror
- Visible seam on body paint (paint vs paint_2)
- Dashboard/steering wheel geometry gaps

**Fix:** Changed to `meshInfo:indicesMinMax(si, si + ic)` (exclusive upper bound).
The Lua mod now also cross-checks both ranges and logs when they differ.

### Confirmed data (2026-07-16 capture)

Diagnostic capture of 650 frames, 238 objects:
- **Vertex pool: rock stable** — 533,885 verts, 1,658,349 indices, 88 flexmeshes
  on ALL 650 frames. No GPU reorganization.
- **No index overflow on Lua side** (after the fix)
- **No position corruption** — cross-frame deltas smooth (0→19.26m over 650 frames)
- **BVC positions match capture exactly** — 100/100 spot checks passed
- **19 objects had off-by-one** (each had exactly 1 index at value `vc`)

## Secondary issue: material-primitive splitting

The capture mod produces 238 separate objects (one per material primitive). When a
flexmesh has multiple material primitives (e.g., body has `paint` and `paint_2`),
the boundary between them shows a visible seam because:

1. Vertices at the boundary are duplicated in both objects (different local indices)
2. No shared vertices across object boundaries
3. The "line of faces on paint_2 that should be on paint" is a material assignment
   boundary — both are legitimate paint materials, but the split creates a visual gap

**Mitigation:** Build with `weld=True` to merge primitives by flexmesh name. This
combines siblings (e.g., `paint` + `paint_2`) into one mesh with material slots,
eliminating the seam. Currently: 238 raw primitives → 84 merged parts.

## Bugs fixed

### 1. `indicesMinMax` off-by-one (2026-07-16)

**Lua mod line:** `meshInfo:indicesMinMax(si, si + ic - 1)` → `si + ic`

BeamNG's `indicesMinMax` uses exclusive upper bounds. Using `si + ic - 1` missed
the last index position. For 19 objects where the last index was the true maximum,
`maxIdx` was 1 too small → `vc = maxIdx - minIdx + 1` was 1 too small → one vertex
missing → faces referencing it degenerate.

Objects affected: all 4 tyres, doorglass_FR, mirror_R, bumper_F_mirror, bumper_R,
body_paint, dash_silver, headlight_R_bake, hub_F, door carpet/shuts, seats (4).

### 2. frame_block_size missing `* 3` (2026-07-15)

The capture path computed `frame_block_size = max_stable_vertex_total * 4` but the
correct formula is `max_stable_vertex_total * 3 * 4` (3 float32 coords × 4 bytes
each). This meant the frame directory pointed to overlapping frame blocks, causing
frame N>0 to read the wrong data.

### 3. index_count double-multiply (2026-07-15)

The capture path stored `index_count = face_counts_d[name] * 3` (total flat indices)
but the GLB path stores `index_count = face_count` (number of faces, i.e., shape[0]).
The `CacheReader.base_indices()` method multiplies by 3 internally, so the capture
path's values caused 3× too many indices to be read.

## Debug instrumentation added

### Lua mod (`capture.lua` v3-diag)

- Logs EVERY primitive (not just first) with si, ic, minIdx, maxIdx, vc, matId
- **Cross-checks old vs new `indicesMinMax` range** and logs when they differ
- Logs raw index values (first 5, last 5) for every primitive
- Counts unique indices per primitive, warns if unique > vc
- Checks local index bounds [0, vc-1] on frame 0
- Logs vertex pool size, index pool size, flexmesh count EVERY frame
- Stores frame-0 reference positions, computes cross-frame deltas
- Checks minIdx + vc > totalVerts overflow on every frame for every object
- Writes `capture_diag.log` with full diagnostic dump

### Python builder (`cache_builder.py`)

- Prints per-object overflow detail when clamping (which faces, which corners)
- Prints per-object degenerate face count when filtering
- All overflow/filtering logged to console during build

## Debug output files

- `capture_diag.log` — written to capture directory by the Lua mod
- Console log — search `beamngCapture` in BeamNG's log

## Remaining issues

### 1. Steering wheel not captured

The steering wheel is NOT a flexmesh — it's a rigid prop not exposed by
`GPUMesh.bng_getGPUMesh()`. The capture API only returns deformable flexmeshes.
The 88 flexmeshes in the E180 capture do not include the steering wheel.

**Workaround:** Import the vehicle mesh as a static GLB (single frame) for the
steering wheel, or add a separate capture method for rigid props.

### 2. Tyre shading (fixed)

Each tyre had a grey/white line at the tread/sidewall boundary. Root cause:
smooth shading averaged normals across faces at >60° angles. Fix in
`runtime/mesh_update.py` `_finalize_mesh`: BMesh marks edges as sharp where
the dihedral angle exceeds 60°, splitting normals at hard creases (tyre
tread/sidewall, panel seams). Done once at import, not per frame.

## Verified results (mycap2, 650 frames, weld=True)

| Metric | Before merge | After merge |
|---|---|---|
| Objects | 238 | **87** |
| Verts/frame | 533,937 | **313,465** |
| Verts welded | 0 | **220,472** |
| BVC size | 4,177 MB | **2,455 MB** |
| Build time | 27s | 55s |
| Overflow | 19 clamps | **0** |
| Degenerates | 4 faces lost | **0** (in merge) |

## Frame writer corruption bug (FIXED 2026-07-16)

The first merged BVC build showed the car "sliced like a cake" — only the front-left
quarter visible. Root cause: the frame writer used `np.maximum.at()` with uninitialized
scatter indices for unused vertices, corrupting position data across all frames.

**Fix:** Replaced with a "compact→original" source map. For each compact vertex, store
exactly which capture object + original vertex provides its position. Frame writer uses
direct `merged_pos[valid] = pos_all[source_map[valid]]` — no scatter, no garbage entries.

See `docs/BUGS.md` item 9 for full details.
