# capture.lua — Complete Simplified Explanation

This document explains what the BeamNG Motion Capture (BMC) Lua mod does, how it
works, what data it produces, and where things can go wrong. Written for AI models
and humans who need to understand the capture pipeline end-to-end.

---

## Overview

The Lua mod runs **inside BeamNG.drive** (the game). It grabs the live GPU mesh
data of the player vehicle every simulation frame during a crash, and writes it to
two files on disk:

- **`capture.bin`** — binary blob: indices, UVs, per-frame vertex positions
- **`capture.meta`** — JSON: object names, material names, vertex counts, index counts

These files are then consumed by `importer/capture_reader.py` and
`importer/cache_builder.py` on the Python/Blender side to build a `.bvc` cache file.

**Why this exists:** The GLTF pipeline (`export_frame_XXXXX.glb`) was slow, bloated
(65 GB for 2364 frames), and BeamNG's GLB output shared a single vertex pool across
88 objects with material-split primitives — making extraction complex and error-prone.
The Lua mod bypasses all of that by reading directly from the GPU mesh.

---

## State Machine

The mod is a state machine with three states:

```
CAPTURE_IDLE (0) ──startCapture()──> CAPTURE_WAIT_MESH (1) ──dataIsReady──> onCaptureUpdate()
     ^                                                                               |
     └──────────────────────── finishCapture() / maxFrames reached ──────────────────┘
```

1. **IDLE**: Nothing happening. Waiting for user to call `M.startCapture(dir, frameCount)`.
2. **WAIT_MESH**: Capture started, waiting for GPU async readback to finish.
3. **WAIT_NEXT**: (Unused in current code, legacy.)

The game calls `M.onUpdate()` every frame. When state is `WAIT_MESH` and
`dataIsReady` is true, `onCaptureUpdate()` fires and processes the frame.

---

## Frame 0 — Static Data Extraction (the critical frame)

Frame 0 is special. It captures everything that does NOT change across frames:
indices, UVs, material names. This is the only frame that reads these.

### Step 1: Get the GPU mesh

```lua
currentMeshInfo = GPUMesh.bng_getGPUMesh(veh:getId())
```

This returns a `GPUMesh` object — BeamNG's API to the live GPU vertex/index buffer.
It is an **async readback**: the data may not be ready until the next frame.

Key properties available on `meshInfo`:
- `verticesCount` — total vertex count in the shared pool (e.g., 577K for E180)
- `indicesCount` — total index count (e.g., 1.7M for E180)
- `uv1Count` — UV coordinate count
- `flexmeshesCount` — number of flexmesh groups (e.g., ~97 for E180)

### Step 2: Read all indices and UVs

```lua
local indices = ffi.new('unsigned int[?]', totalIndices)
meshInfo:indicesGet(indices)

local uvs = ffi.new('float[?]', uvCount * 2)
meshInfo:uv1Get(uvs)
```

These fill flat arrays:
- `indices` — flat `uint32[]` of ALL triangle indices for the entire vehicle
- `uvs` — flat `float32[]` of UV coordinates (2 floats per vertex)

**Important:** These are the SHARED vertex pool indices. The entire vehicle's mesh
is stored in one giant vertex pool, and multiple flexmeshes/primitives index into it.

### Step 3: Get material names

```lua
local names = veh:getMaterialNames()
```

Returns a list of material name strings indexed by material ID. Used to label
each primitive with its material (e.g., "paint", "chrome", "glass").

### Step 4: Iterate all flexmeshes and all primitives

This is where the core extraction happens:

```
meshInfo
  ├── flexmeshes[0]  (e.g., "flanje_e180_body")
  │     ├── primitives[0]  (e.g., paint material)
  │     ├── primitives[1]  (e.g., leather material)
  │     └── primitives[2]  (e.g., carpet material)
  ├── flexmeshes[1]  (e.g., "flanje_e180_dash")
  │     ├── primitives[0]
  │     └── primitives[1]
  └── ... (97 flexmeshes total for E180)
```

For each primitive within each flexmesh:

```lua
local si = prim.startIndex       -- where this primitive's indices start in the shared index buffer
local ic = prim.indexCount       -- how many indices this primitive has (always multiple of 3)
local minIdx, maxIdx = meshInfo:indicesMinMax(si, si + ic)  -- exclusive upper bound!
local vc = maxIdx - minIdx + 1   -- vertex count for this primitive
```

**`indicesMinMax`** is the critical call. It scans the primitive's index range
`[si, si+ic)` (exclusive upper bound) and returns the minimum and maximum raw index
values. This tells us which slice of the shared vertex pool belongs to this primitive.

**⚠️ IMPORTANT: the upper bound is EXCLUSIVE.** Using `si + ic - 1` (inclusive) would
miss the last index position. For 19 of 238 primitives, the last position contained
the true max value, causing `maxIdx` to be 1 too small → one vertex missing → faces
referencing it degenerate → visible holes/seams in the mesh. This was the root cause
of the "missing faces" bug. See `docs/CAPTURE_GEOMETRY_ISSUES.md` for details.

### Step 5: Extract local indices (shift to 0-based)

```lua
local localIdx = ffi.new('unsigned int[?]', ic)
for j = 0, ic - 1 do
    localIdx[j] = indices[si + j] - minIdx
end
```

**Why shift?** The raw indices reference the shared pool (e.g., indices 50000–50200).
We subtract `minIdx` to make them 0-based relative to this primitive's own vertex
range (0–200). This makes the capture self-contained — each primitive has its own
compact index buffer.

### Step 6: Extract local UVs

```lua
local localUvs = ffi.new('float[?]', vc * 2)
for j = 0, vc - 1 do
    localUvs[j * 2]     = uvs[(minIdx + j) * 2]
    localUvs[j * 2 + 1] = uvs[(minIdx + j) * 2 + 1]
end
```

UVs are extracted for the same vertex range `[minIdx, minIdx+vc-1]`, same as
indices.

### Step 7: Record the flexmeshCache

```lua
flexmeshCache[name] = {
    minIdx = fm.minIdx,
    vertexCount = fm.vertexCount,
}
```

**This is the key state carried forward to all subsequent frames.** On every later
frame, the mod uses `minIdx` to extract vertex positions from the GPU pool. The
assumption is that **the vertex pool layout does NOT change between frames** — the
same vertex index always maps to the same physical vertex on the mesh.

### Step 8: Extract and store frame-0 reference positions (debug version)

```lua
local frame0_verts = ffi.new('float[?]', totalGPUMeshVerts * 3)
currentMeshInfo:verticesGet(frame0_verts)
for _, fm in ipairs(flexmeshes) do
    local mi = fm.minIdx
    local vc = fm.vertexCount
    local refPositions = ffi.new('float[?]', vc * 3)
    for j = 0, vc * 3 - 1 do
        refPositions[j] = frame0_verts[(mi * 3) + j]
    end
    dbg_frame0_refs[fm.name] = { positions = refPositions, ... }
end
```

Stores the exact vertex positions for every primitive at frame 0. On subsequent
frames, the mod compares current positions against these references to detect
vertex pool drift/reorganization.

### Step 9: Write static data to capture.bin

```lua
for _, sb in ipairs(staticBins) do
    binFile:write(sb.indices)    -- uint32[] of local indices
    binFile:write(sb.uvs)       -- float32[] of local UVs
end
```

The static section is written once. It contains, in order:
```
[obj0 indices] [obj0 UVs] [obj1 indices] [obj1 UVs] ...
```

Each object's section size is determined by its `indexCount * 4` bytes (indices)
+ `vertexCount * 8` bytes (UVs). These offsets are stored in `capture.meta` as
`staticByteSize`.

### Step 10: Write frame-0 positions

After the static section, frame 0's positions are appended:
```
[obj0 positions] [obj1 positions] ... [vehicle transform (7 × float32)]
```

Positions are extracted from the GPU vertex buffer using the cached `minIdx`:
```lua
local mi = flexmeshCache[name].minIdx
for j = 0, vc - 1 do
    local src = (mi + j) * 3
    vertices_at_minIdx_j = gpu_vertices[src], gpu_vertices[src+1], gpu_vertices[src+2]
end
```

The frame offset is recorded: `frameOffsets[0] = byte position of frame 0 in capture.bin`.

---

## Frames 1..N — Per-Frame Position Capture

Each subsequent frame captures ONLY the vertex positions (no indices, no UVs —
those were captured once on frame 0).

### The capture loop (called every game frame via `onUpdate`):

1. **Request GPU mesh**: `GPUMesh.bng_getGPUMesh(veh:getId())` — triggers async readback
2. **Wait for data**: `meshInfo.dataIsReady` must be true
3. **Read all vertices**: `meshInfo:verticesGet(vertices)` — fills flat `float32[]` of
   ALL vertex positions in the shared pool
4. **Per-object extraction**: For each object, copy positions at
   `vertices[minIdx*3 .. (minIdx+vc)*3-1]` into the output buffer
5. **Append vehicle transform**: `veh:getPosition()` + `veh:getRotation()` → 7 floats
6. **Write to capture.bin**: Append the frame data

### The extraction loop (per object, per frame):

```lua
local mi = flexmeshCache[obj.name].minIdx   -- from frame 0!
local vc = obj.vertexCount                   -- from frame 0!
for j = 0, vc - 1 do
    local src = (mi + j) * 3
    buf[w]     = vertices[src]      -- X
    buf[w + 1] = vertices[src + 1]  -- Y
    buf[w + 2] = vertices[src + 2]  -- Z
    w = w + 3
end
```

**The critical assumption:** `minIdx` from frame 0 is still valid on frame N.
If BeamNG reorganizes the vertex pool between frames (which it does for soft-body
deformation!), vertex index 50000 on frame 0 may map to a different physical vertex
on frame 50. This is the suspected root cause of the geometry corruption we see.

---

## capture.bin Binary Layout

```
┌──────────────────────────────────────────────────┐
│ STATIC SECTION                                    │
│  ┌─────────────────┬───────────────────────────┐ │
│  │ Object 0         │                           │ │
│  │   indices: ic×4  │  uvs: vc×8               │ │
│  ├─────────────────┼───────────────────────────┤ │
│  │ Object 1         │                           │ │
│  │   indices: ic×4  │  uvs: vc×8               │ │
│  ├─────────────────┼───────────────────────────┤ │
│  │ ...              │                           │ │
│  └─────────────────┴───────────────────────────┘ │
├──────────────────────────────────────────────────┤
│ FRAME DATA                                        │
│  ┌─────────────────────────────────────────────┐ │
│  │ Frame 0:                                    │ │
│  │   [obj0 positions: vc×12 bytes]             │ │
│  │   [obj1 positions: vc×12 bytes]             │ │
│  │   ...                                       │ │
│  │   [vehicle transform: 28 bytes]             │ │
│  ├─────────────────────────────────────────────┤ │
│  │ Frame 1:                                    │ │
│  │   [obj0 positions: vc×12 bytes]             │ │
│  │   [obj1 positions: vc×12 bytes]             │ │
│  │   ...                                       │ │
│  │   [vehicle transform: 28 bytes]             │ │
│  ├─────────────────────────────────────────────┤ │
│  │ ... (up to frameCount frames)               │ │
│  └─────────────────────────────────────────────┘ │
├──────────────────────────────────────────────────┤
│ FOOTER                                            │
│  frame_count:    uint64                          │
│  frame_offsets:  uint64[frame_count]             │
│  footer_offset:  uint64                          │
└──────────────────────────────────────────────────┘
```

### Position data per frame

Each frame block contains positions for ALL objects concatenated in `metaObjects`
order, followed by 7 float32s for the vehicle world transform:

```
[obj0_pos(vc×float32)] [obj1_pos(vc×float32)] ... [px,py,pz,qx,qy,qz,qw]
```

The frame directory provides O(1) seek to any frame's data.

---

## capture.meta JSON Format

```json
{
  "version": 1,
  "vehicleName": "unknown",
  "frameCount": 650,
  "objects": [
    {
      "name": "flanje_e180_body",
      "materialName": "paint",
      "vertexCount": 40785,
      "indexCount": 121581,
      "staticByteSize": 573864
    },
    ...
  ]
}
```

Each object entry records:
- `name` — unique object identifier (flexmesh name + material suffix)
- `materialName` — BeamNG material name
- `vertexCount` — number of vertices (maxIdx - minIdx + 1 from frame 0)
- `indexCount` — number of index values (always multiple of 3)
- `staticByteSize` — bytes in the static section (indices + UVs)

---

## How Indices Work (the core concept)

BeamNG stores the entire vehicle mesh in ONE shared vertex pool. Each flexmesh
(e.g., "flanje_e180_body") has multiple material primitives, each referencing a
slice of this pool via index ranges.

### Frame 0 extraction example:

```
Shared vertex pool: [v0, v1, v2, ..., v577000]

Primitive "flanje_e180_body" (paint):
  startIndex=0, indexCount=121581
  indicesMinMax → minIdx=0, maxIdx=40784
  vc = 40784 - 0 + 1 = 40785 vertices
  local indices: raw[i] - 0 = raw[i]  (no shift needed since minIdx=0)

Primitive "flanje_e180_body_flanje_e180_leather":
  startIndex=121581, indexCount=89253
  indicesMinMax → minIdx=40785, maxIdx=49776
  vc = 49776 - 40785 + 1 = 8992 vertices
  local indices: raw[i] - 40785  (shifted to 0-based)

Primitive "flanje_e180_dash":
  startIndex=210834, indexCount=56040
  indicesMinMax → minIdx=49777, maxIdx=54472
  vc = 54472 - 49777 + 1 = 4696 vertices
  local indices: raw[i] - 49777
```

### Per-frame position extraction:

On every frame, the mod reads the ENTIRE vertex pool (all 577K vertices), then
for each primitive copies only the slice `[minIdx, minIdx+vc-1]`:

```
Frame 50:
  GPU pool: [v0', v1', v2', ..., v577000']  (all deformed positions)

  "flanje_e180_body" (paint):
    minIdx=0, vc=40785
    copies: pool[0..40784] → 40785 vertex positions

  "flanje_e180_body_flanje_e180_leather":
    minIdx=40785, vc=8992
    copies: pool[40785..54776] → 8992 vertex positions
```

---

## The Suspected Bug: Vertex Pool Reorganization

The entire capture pipeline assumes **vertex index stability**: vertex index N on
frame 0 maps to the same physical vertex as index N on frame 500.

**If BeamNG reorganizes the GPU vertex pool between frames** (which soft-body
solvers commonly do for performance), then:
- Frame 0: index 50000 → fender vertex at position (1.0, 2.0, 3.0)
- Frame 50: index 50000 → dash vertex at position (5.0, 0.5, -1.0)

The indices captured on frame 0 would now reference completely wrong vertices,
producing the "stretched geometry" / "weird vertices" we see.

### What the debug version checks:

1. **Vertex pool size stability**: Logs `meshInfo.verticesCount` every frame.
   If it changes, the pool was reorganized.

2. **Index pool size stability**: Logs `meshInfo.indicesCount` every frame.
   If it changes, primitives were added/removed.

3. **Flexmesh count stability**: Logs `meshInfo.flexmeshesCount` every frame.

4. **Index overflow**: Every frame, checks if `minIdx + vc > totalVerts` for
   every primitive. If so, the index range is out of bounds.

5. **Cross-frame position delta**: Stores frame-0 reference positions for every
   primitive. On subsequent frames, computes the maximum position change for each
   primitive. If this delta is abnormally large for a small frame-to-frame step,
   the vertex pool was likely shuffled.

6. **Raw index samples**: Logs the first 5 and last 5 raw index values for every
   primitive on frame 0. Useful for manual verification.

7. **Unique index count**: Counts unique indices per primitive and compares with
   `vertexCount`. If unique indices > vertexCount, the index buffer references
   vertices outside the `minIdx..maxIdx` range — an impossible state that indicates
   corruption.

8. **Local index bounds**: Every local index is checked against `[0, vc-1]`.

---

## Debug Output Files

When the debug version runs, it produces:

### `capture_diag.log`

Written to the same directory as `capture.bin`. Contains:

- **Frame 0 reference data**: For every primitive: minIdx, maxIdx, vc, first/last
  positions, raw index samples
- **Vertex pool stability**: Total vertices, indices, flexmesh counts per frame
- **Position deltas**: Maximum position change from frame 0, per object, per sampled frame
- **Index overflow events**: Every frame where any primitive's index range exceeds
  the vertex pool size

### BeamNG console log

All `log('I', logTag, ...)` calls go to BeamNG's log file (typically
`game.log` or accessible via the in-game console). Search for `beamngCapture`
to filter.

---

## Key API Calls Used

| API Call | Returns | When Used |
|---|---|---|
| `GPUMesh.bng_getGPUMesh(vehId)` | GPUMesh object (async readback) | Every frame |
| `meshInfo.dataIsReady` | bool | Poll until true |
| `meshInfo.verticesCount` | int (total shared vertices) | Every frame |
| `meshInfo.indicesCount` | int (total shared indices) | Frame 0 + every frame (debug) |
| `meshInfo.flexmeshesCount` | int (number of flexmesh groups) | Frame 0 + every frame (debug) |
| `meshInfo:verticesGet(buf)` | fills float[] with all positions | Every frame |
| `meshInfo:indicesGet(buf)` | fills uint32[] with all indices | Frame 0 only |
| `meshInfo:uv1Get(buf)` | fills float[] with UVs | Frame 0 only |
| `meshInfo:flexmeshes(i)` | flexmesh object | Frame 0 only |
| `flexmesh.meshName` | string (e.g., "flanje_e180_body") | Frame 0 only |
| `flexmesh.primitivesCount` | int | Frame 0 only |
| `flexmesh:primitivesGet(buf)` | fills gpuPrimitive_t[] | Frame 0 only |
| `prim.startIndex` | int (offset into shared index buffer) | Frame 0 only |
| `prim.indexCount` | int (number of indices) | Frame 0 only |
| `prim.materialId` | int (index into material names) | Frame 0 only |
| `meshInfo:indicesMinMax(lo, hi)` | minIdx, maxIdx | Frame 0 only |
| `veh:getPosition()` | vec3 (world position) | Every frame |
| `veh:getRotation()` | quat (world rotation) | Every frame |
| `veh:getMaterialNames()` | string[] | Frame 0 only |
| `meshInfo:free()` | releases GPU readback buffer | Between frames |

---

## What "Primitives" Means

BeamNG uses **flexmeshes** — deformable meshes that change shape during simulation.
Each flexmesh (e.g., "flanje_e180_body") can have multiple **primitives**, each
representing a different material on that mesh part.

Example for "flanje_e180_body":
```
flexmesh: flanje_e180_body
  primitive[0]: paint material      → 40,785 vertices
  primitive[1]: leather material    →  8,992 vertices
  primitive[2]: carpet material     →  5,082 vertices
  primitive[3]: interior glass      →    314 vertices
  primitive[4]: airbag material     →      6 vertices
```

All primitives within a flexmesh share the SAME vertex pool (the flexmesh's GPU
vertex buffer). Each primitive gets its own index range within that pool.

The mod iterates ALL primitives (not just `primitives[0]`) — this was a fix from
an earlier version that only captured the first primitive per flexmesh, losing all
multi-material parts.

---

## Name Generation

Each captured object gets a unique name:
- First primitive of a flexmesh: keeps the flexmesh name (e.g., "flanje_e180_body")
- Additional primitives: appends material name (e.g., "flanje_e180_body_flanje_e180_paint")
- Fallback: appends "pN" if material name is empty

Names are deduplicated: if "flanje_e180_body_flanje_e180_paint" already exists,
the next one becomes "flanje_e180_body_flanje_e180_paint_2", etc.

---

## The Position Extraction Problem (why we have corruption)

Here's the fundamental problem, step by step:

### What SHOULD happen (stable vertex pool):

```
Frame 0: GPU pool = [head_vertex, door_vertex, fender_vertex, ...]
         Index 100 → head_vertex at (0, 0, 1.5)
         Primitive stores minIdx=100, vc=50

Frame 50: GPU pool = [head_vertex', door_vertex', fender_vertex', ...]
          Index 100 → head_vertex' at (0, 0.1, 1.4)  ← SAME vertex, just deformed
          Extraction: pool[100..149] → correct deformed positions ✓
```

### What MIGHT be happening (unstable vertex pool):

```
Frame 0: GPU pool = [head_vertex, door_vertex, fender_vertex, ...]
         Index 100 → head_vertex at (0, 0, 1.5)
         Primitive stores minIdx=100, vc=50

Frame 50: GPU pool = [fender_vertex', wheel_vertex', door_vertex', head_vertex', ...]
          Index 100 → fender_vertex' at (2.0, 0.5, 0.3)  ← WRONG vertex!
          Extraction: pool[100..149] → garbage positions from wrong mesh parts ✗
```

### How to detect it:

The debug version captures frame-0 reference positions. On frame 50, if the
positions at `[minIdx..minIdx+vc-1]` have changed dramatically for a part that
should only move slightly, the vertex pool was likely reorganized.

The `capture_diag.log` file will show:
- If vertex pool size changed between frames
- If any primitive has position deltas that are unreasonably large
- If index ranges overflow the vertex pool
- Exact raw index values for manual inspection

---

## Version History

| Version | Changes |
|---|---|
| v1 | Initial version, single primitive per flexmesh |
| v2 | Added vehicle world transform (7 float32 per frame) |
| v3 | Fixed to iterate ALL primitives per flexmesh |
| v3-diag | Adds comprehensive debug logging + `indicesMinMax` off-by-one fix (`si+ic-1` → `si+ic`) |

---

## File Locations

- **Installed mod**: `D:\danish\Games\beamng\BeamNG.drive\lua\ge\extensions\beamng\capture.lua`
- **Capture output**: `<BeamNG user folder>/captures/<name>/capture.bin` + `.meta` + `_diag.log`
- **Python reader**: `importer/capture_reader.py`
- **Python builder**: `importer/cache_builder.py` (`build_from_capture()`)
- **This document**: `docs/capture_lua_simplified.md`
