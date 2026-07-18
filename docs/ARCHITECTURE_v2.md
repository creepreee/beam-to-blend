# BeamNG Cache Importer — Architecture

## Design philosophy

> The capture backend must never interpret the mesh. It only copies the
> evaluated vehicle state from BeamNG into the capture format. All
> reconstruction, optimization, compression, and engine-specific processing
> belong to later stages.

This rule is non-negotiable. Every past bug in this project came from a
capture step that tried to interpret mesh data (per-primitive decomposition,
`indicesMinMax`, local vertex arrays, welding heuristics). The capture
backend reads bytes, writes bytes, and nothing else.

---

## For end users

### What this does

Captures car crash deformations from BeamNG.drive and plays them back in
Blender as smooth vertex animation. Like recording a crash in slow motion
and being able to inspect every bent panel from any angle.

### Why it's different from the old workflow

| Old way | This way |
|---|---|
| Export every frame as a separate `.glb` file | Capture once, record only positions per frame |
| Thousands of files per crash | One `.bmc` file per crash |
| Importing into Blender creates a new mesh for every frame | One mesh per part, positions animate in-place |
| Blender crashes from RAM blow-up (hundreds of thousands of datablocks) | Lightweight — a 2000-frame crash is ~200 MB of position data |
| Complex scanning and repair logic | Clean, deterministic pipeline |

### How to use it

1. **In BeamNG:** Start a capture before the crash happens. The mod records
   the vehicle mesh at render rate (~60 fps) until the crash ends.

2. **In Blender:** Open the add-on panel (N key in the 3D viewport), point
   it at the `.bmc` file, and click Import. The crash plays back on the
   timeline — scrub through any frame, orbit around the deformed car, render
   from any angle.

3. **Exporting:** Click Export Alembic to get a production-ready `.abc` file
   with the full crash animation, compatible with any DCC tool.

### What you get

- Every visible vehicle part deforming as it did in the simulation
- Materials and UVs preserved
- Smooth playback at 24–60 fps
- A single BVC file that contains the entire crash sequence
- No RAM explosion, no per-frame datablocks

### What's different under the hood

BeamNG keeps all vehicle mesh data in one shared GPU vertex pool. Vertex 0
is always the same physical point on the car — only its position changes as
the crash deforms the body. Instead of extracting each part separately and
trying to reassemble it (the old approach), we capture the entire pool at
once and sort out per-part meshes offline. This gives us perfect topology,
zero reconstruction artifacts, and much smaller capture files.

### How capture actually works

The capture mod **does not compute anything**. It's a passive reader.

Every frame, BeamNG's physics engine already deforms the vehicle mesh and
updates the vertex positions in the GPU buffer. The renderer reads this
buffer to draw the car on screen. The capture mod simply asks:

> "Can I have a copy of where every vertex currently is?"

BeamNG returns:

```
Vertex 0:  x0 y0 z0
Vertex 1:  x1 y1 z1
Vertex 2:  x2 y2 z2
...
Vertex N:  xN yN zN
```

That's the entire capture per frame. No physics computation, no deformation
solving, no rotation math — BeamNG has already done all of that. The mod is
just recording the finished result.

This is why the capture is fast and lightweight: the heavy work (simulating
the crash, deforming the mesh) happens regardless. The capture layer is a
sideshow — it reads data that already exists in memory and writes it to disk.

---

## For developers

### Vertex identity — the foundation of everything

> **Vertex identity** = The index of a vertex in the shared GPU vertex pool.
> Vertex N represents the same physical point on the vehicle throughout the
> entire capture. The positions at that index change (that is the animation),
> but index N always maps to the same point on the car body.

The entire format rests on this property. If the GPU pool ever reorders
vertices between frames, the cache is invalid.

**Evidence:**
- 2364 real crash frames from `testglt/`: byte-identical index buffers on
  every frame for 96/97 objects. Index buffers reference vertex indices — if
  the pool reordered, the indices would change to match. They don't.
  (Source: `CLAUDE.md` ground-truth research section)
- `verticesCount` never varies across frames: 533,885 for the Gavril E180
  on every frame. (Source: scanner output from `testglt/` frames 0, 168,
  745, 1055, 1152, 1406, 1761, 1872, 2267, 2362)
- The GPU vertex buffer is allocated once at vehicle load and positions are
  updated in-place by the physics engine. The D3D11 vertex buffer object is
  created with a fixed stride and vertex count; only the position data within
  it changes per frame. (Source: inferred from `bng_getGPUMesh()` lifecycle
  in `util/export.lua:247-257`)

### Pipeline overview

```
BeamNG Backend
(GPU readback today, CPU/C++ tomorrow)
        │
        │  capture.bmc  (Beam Motion Capture v1)
        ▼
Python Builder
(BMC → per-object gather → BVC)
        │
        │  BVC file
        ▼
Blender Runtime
(CacheReader + CachePlayback)
```

Each layer is independent. The format (`capture.bmc`) is the contract between
backend and builder. The BVC format is the contract between builder and
runtime. Neither side knows or cares what happens on the other side.

### Modular backend interface

Every capture backend implements three operations:

```python
class CaptureBackend:
    def begin_capture(self, vehicle_id) -> str:
        """Start capturing. Returns path to capture.bmc."""
        ...

    def capture_frame(self) -> bytes:
        """Capture one frame. Returns timestamp + positions blob."""
        ...

    def end_capture(self):
        """Finalize capture file."""
        ...
```

**Evidence that `GPUMesh.bng_getGPUMesh()` is the implementable backend:**
- BeamNG's own GLTF exporter uses it for multi-frame recording
  (`util/export.lua:256` triggers readback, `util/export.lua:1257-1261`
  polls `dataIsReady` and loops)
- The `dataIsReady` property confirms async GPU readback
  (`capture.lua:868`: `if not currentMeshInfo.dataIsReady then return end`)
- The same API is used by the in-game mesh editor
  (`editor/gen/mesh.lua:1277`) and JBeam editor
  (`editor/gen/lib/jbeam.lua:156`) — it is BeamNG's standard mesh access API

**CPU-side alternative investigated (negative result):**
- `vehicle:getFlexmesh(fid):getDebugVertexPos(i)` exists in
  `veFlexbodyDebug.lua` but is per-vertex (no bulk read), requires
  `setFlexmeshDebugMode(true)`, provides no index/UV access, and makes one
  Lua→C FFI call per vertex (550K+ calls per frame — impractical).
  (Source: full codebase search of `D:\danish\Games\beamng\BeamNG.drive\lua\ge`)
- No other CPU-side bulk mesh API was found in any of:
  - `util/export.lua` (GLTF exporter)
  - `editor/gen/` (mesh editor tools)
  - `core/vehicle/` (vehicle manager)
  - `tech/` (research platform APIs)
  - Any extension under `lua/ge/extensions/`

### Capture rate

GPU readback completes at render rate (~60 fps), not physics sub-step rate
(~200 Hz). Physics updates the GPU buffer every tick, but `bng_getGPUMesh()`
is an async staging copy that captures the buffer state only when the copy
completes — and there is only one outstanding copy at a time.

**Evidence:**
- `util/export.lua:247-257`: `_triggerExport()` calls `free()` then
  `bng_getGPUMesh()`. Only one staging resource exists at a time — the old
  one must be freed before the next is requested. No queue of physics states.
- `util/export.lua:1257-1261`: `updateGFX(dt)` polls `dataIsReady`. This
  is called once per rendered frame, not per physics tick. If physics runs
  at 200 Hz and rendering at 60 Hz, `updateGFX` is called 60 times per
  second, and captures happen at that rate.

Impact: fast sub-tick events (a wheel separating, a panel crumpling between
two capture frames) will appear as a discrete jump rather than a smooth
interpolation. For playback at 24–60 fps this is visually indistinguishable
from any game recording. For slow-motion VFX, a future C++ backend could
sample at physics rate — the format supports it via the timestamp field.

---

### Validation pipeline

Before writing any frame, the capture mod validates that the GPU pool still
matches frame 0:

```
Frame 0: hash(indices) → store reference
         hash(UVs)     → store reference
         hash(normals) → store reference

Frame N: hash(indices) → compare to frame 0
         hash(UVs)     → compare to frame 0
         hash(normals) → compare to frame 0

         ALL MATCH  → write frame
         ANY CHANGE → abort: "Topology changed. Capture invalid."
```

This catches:
- Topology-changing parts (the tierod: 115↔156 vertices at frame 168 —
  confirmed by scanning frames 0, 168, 745, 1055, 1152, 1406 across the
  full sequence)
- GPU driver reallocations (hypothetical — not observed in testing)
- Vehicle part detachment that alters the mesh (breakable parts)
- Any unexpected engine behaviour

**Why at capture time, not in the builder:** Corrupted frames on disk are
already written. Hashing is microseconds per frame vs milliseconds of GPU
readback latency. Abort early, save disk space, report immediately.

**Evidence that validation is a belt, not a suspender:**
- 2364 frames of GLTF exports: index buffers byte-identical every frame
  (source: `CLAUDE.md` "Full-sequence topology validation" section)
- Scanner over 10 spread-out frames (0, 168, 745, 1055, 1152, 1406, 1761,
  1872, 2267, 2362): 96 stable, 1 dynamic. The sole dynamic object
  (`flanje_e180_tierod_F`) changes vertex count (115↔156) at frame 168.
  (Source: scanner.py output logged in `CLAUDE.md`)

---

### File format: BMC v1

A single binary file (`capture.bmc`) with two sections. All integers are
little-endian.

#### Static section (written once at capture start)

| Offset | Size | Field | Description |
|---|---|---|---|---|
| 0 | 4 | magic | `"BMC1"` ASCII |
| 4 | 4 | version | `1` (uint32) |
| 8 | 4 | vertex_count | Shared pool vertex count (uint32) |
| 12 | 4 | index_count | Shared index buffer count (uint32) |
| 16 | 4 | primitive_count | Number of primitives (uint32) |
| 20 | 4 | material_count | Number of unique materials (uint32) |
| 24 | 4 | flags | Bit 0: has UVs, Bit 1: has normals, Bit 2: has transform |
| 28 | 4 | frame_size | Bytes per frame = 8 + vertex_count * 12 + (28 if has transform else 0) |
| 32 | 8 | static_size | Total bytes of static data after header (uint64) |
| 40 | | index_data | `uint32[index_count]` — shared index buffer |
| | | uv_data | `float32[vertex_count * 2]` — shared UVs (if flags bit 0 set) |
| | | normal_data | `float32[vertex_count * 3]` — shared normals (if flags bit 1 set) |
| | | primitive_table | One entry per primitive: |
| | | | `uint16 name_length` + `utf-8 name` |
| | | | `uint32 start_index` |
| | | | `uint32 index_count` |
| | | | `int32 material_id` (-1 if none) |
| | | | `int32 flexmesh_index` (-1 if independent) |
| | | material_table | One entry per material: |
| | | | `uint16 name_length` + `utf-8 name` |

#### Frame section (appended per frame)

| Offs | Size | Field |
|---|---|---|
| 0 | 8 | `float64 timestamp` — simulation time in seconds |
| 8 | vertex_count * 12 | `float32[vertex_count * 3]` — shared positions |
| + | 28 (optional) | `float32[7]` — vehicle world transform (px,py,pz, qx,qy,qz,qw) |

**Seeking:** First frame begins at `HEADER_SIZE + static_size` (HEADER_SIZE = 40).
Frame N begins at `40 + static_size + N * frame_size`. Frame count =
`(file_size - 40 - static_size) / frame_size`.

**Vehicle transform** is optional metadata, not required for playback.
Positions are already in vehicle-local space. Stored only for camera
tracking, motion blur, and debugging. If absent (`frame_size` doesn't
include the 28 bytes), the reader returns `None`.

### Python Builder: BMC → BVC

#### Old path (replaced)

```
capture.bin → per-primitive local arrays → clamp out-of-range indices
→ filter degenerate faces → merge by flexmesh → weld coincident vertices
→ scatter-gather frame_sources → BVC
```

Every step existed to repair artifacts from the per-primitive decomposition
in the old `capture.lua`. Each repair step was a potential bug.

#### New path

```
capture.bmc → group primitives by flexmesh name prefix
→ for each group: np.unique(vertex_indices_from_shared_pool) → local remap
→ for each frame: shared_positions[unique_indices] per group → write BVC
```

**Evidence this works:**
- The shared pool index buffer is byte-identical every frame (proven above).
  The same `np.unique` dedup on index references produces the same result
  for every frame — no drift, no exceptions.
- The flexmesh grouping by name prefix is the same logic used by the current
  `_merge_capture_by_flexmesh()` in `cache_builder.py:34-230`, which has
  been validated against real capture data (238 primitives → 87 objects).
- No welding needed **for the shared-pool backend**: the shared pool already
  defines correct vertex sharing at material seams. The weld step
  (`WELD_DECIMALS=4`) in the current builder was only needed because the old
  `capture.lua` decomposed primitives into separate local arrays, creating
  artificial vertex duplication that didn't exist in the source pool.

  **Empirically verified** (`tests/verify_architecture_claims.py`, run against
  the real `testglt/` shared pool): of 117,185 groups of coincident vertices at
  frame 0, 99% drift **exactly zero** across frames; p99.9 relative drift is
  0.082 mm and the single worst pair drifts 0.27 mm while the mesh moves 2.18 m.
  Zero pairs exceed 0.5 mm. Coincident seam vertices in the shared pool are the
  same physics node and stay locked together, so dropping the weld does not crack
  seams. (Caveat: verified on a 10-frame end-of-crash window; re-run on
  high-deformation early frames before treating this as absolute.)

  > ⚠️ **SCOPE — this applies ONLY to the shared-pool (`bng_getGPUMesh` /
  > `verticesGet`) backend described here.** It does **not** license removing
  > weld from the *current* `capture.lua` path, which emits 238 **disjoint**
  > per-material primitives whose seam vertices are independent duplicates. That
  > path genuinely cracks at seams without merge+weld — this is the 2026-07-15
  > bug documented in `CLAUDE.md` / `docs/BUGS.md`. Different backend, different
  > answer: shared pool → weld optional; disjoint primitives → weld required.

#### What is removed

| Step | Why removed |
|---|---|
| `indicesMinMax` + contiguous-range copy | No per-primitive decomposition — shared pool is already correct |
| Index clamping | Shared pool indices are valid within [0, vertexCount) by construction |
| Degenerate face filtering | Indices in shared pool are well-formed (proven: 96/97 topology-stable) |
| Vertex welding | Shared pool has correct seam sharing — weld was fixing an artifact of per-primitive decomposition |
| `frame_sources` scatter-gather | Per-frame gather is a simple index into shared positions |
| `CORRUPT_EDGE_THRESHOLD` | No corruption to detect — shared pool is authoritative |

### Blender Runtime

No changes from the current implementation. The runtime reads BVC files and
does not know or care about their provenance.

Current implementation verified working (source: `CLAUDE.md` "Current state
of the build" table):
- `runtime/cache_reader.py` — memmap-based BVC reader with per-frame seek
- `runtime/mesh_update.py` — `CachePlayback` creates each mesh once via
  `from_pydata`, updates positions per frame via the fast `position`
  attribute API (~120× faster than legacy `foreach_set("co")`)
- `runtime/frame_handler.py` — `frame_change_pre` handler mapping Blender
  timeline → cache frame index
- `runtime/baker.py` — MDD writer + Alembic export via MESH_CACHE modifiers

### Evidence summary table

| Claim | Evidence | Source |
|---|---|---|
| Vertex identity is stable | 2364 frames, byte-identical index buffers, 96/97 objects stable | `testglt/` research (CLAUDE.md) |
| GPU readback is the only API | `export.lua:256`; no CPU bulk API found | Full codebase search of `lua/ge/` |
| GPU readback works per-frame | `export.lua:1109-1110` loop: request→read→request | `export.lua` |
| `dataIsReady` confirms async path | `capture.lua:868` | `capture.lua` |
| No CPU equivalent | `getDebugVertexPos(i)` is per-vertex, no indices/UVs | `veFlexbodyDebug.lua` |
| Only one staging copy at a time | `free()` then `bng_getGPUMesh()` in `export.lua:247-257` | `export.lua` |
| Capture rate = render rate | `updateGFX(dt)` drives capture loop at `export.lua:1257-1261` | `export.lua` |
| Weld was fixing an artifact | 44.1% reduction was seam duplication from per-primitive decomposition | `cache_builder.py` |
| Chunked playback improves FPS | 198ms→~15ms per frame (14 chunks vs 97 objects) | CLAUDE.md |
| Tierod is the sole topology change | 115↔156 vertices at frame 168, all other 96 objects stable | Scanner output over 10 spread frames |

### Validated claims

Each claim below was verified against source code or test data before freezing
the architecture. These are not assumptions — they are measured facts.

| # | Claim | Evidence | Verdict |
|---|---|---|---|
| 1 | `verticesGet()` returns the full shared pool, not per-primitive data | `export.lua:864-868`: allocates `verticesCount * 3` buffer, fills it with one call, writes one GLTF accessor | Confirmed |
| 2 | `indicesGet()` is globally stable — safe to write once | `export.lua:858-861`: index buffer written once (`if not gltfRoot then`). 2364 frames: byte-identical index buffers (`CLAUDE.md:25-28`) | Confirmed |
| 3 | Capture backend is a passive read with no reconstruction | `export.lua:856`: `processExport()` reads → writes. No minIdx, no per-primitive decomposition, no local arrays | Confirmed |
| 4 | `updateGFX(dt)` is the correct capture callback | `export.lua:1257-1261`: fires once per rendered frame, polls `dataIsReady`, calls `processExport()` | Confirmed |
| 5 | v1 captures at render rate (~60 fps) | `export.lua:1257-1261`: `updateGFX` is render-loop callback. `export.lua:247-257`: one outstanding staging copy at a time, no queue | Confirmed |
| 6 | Vehicle transform is optional metadata | Positions are vehicle-local (proven: max pool drift 0.10m over 93m tumble, `CLAUDE.md`). Transform not needed for playback | Confirmed |
| 7 | BMC v1 format layout is locked | 40-byte header + static_size + fixed-size frames + optional transform. Spec at `docs/architecture.md:237-248` | Locked |
| 8 | GPU readback is sufficient for v1 | Same API as BeamNG's own GLTF exporter. Proven multi-frame workflow (`export.lua:1109-1110`) | Confirmed |

### Questions and answers

**Q: Can `verticesGet()` be called every frame for thousands of frames?**
A: Yes. `export.lua:1109-1110` proves the loop. No limit on sequential
readbacks.

**Q: When exactly should `verticesGet()` be called?**
A: In `updateGFX(dt)` — the render loop callback
(`export.lua:1257-1261`). Mesh state corresponds to the last rendered frame,
with a consistent 1-2 frame delay.

**Q: Does `verticesGet()` preserve vertex identity?**
A: Yes. Byte-identical index buffers across 2364 frames prove the pool never
reorders. `verticesCount` is invariant. The GPU buffer is allocated once at
vehicle load.

**Q: Does every physics state get captured?**
A: No. Render-rate capture (~60 Hz) means intermediate physics states (~200 Hz)
between captured frames are lost. This is acceptable for 24-60 fps playback.
For slow-motion VFX, a future C++ backend could capture at physics rate.

**Q: Why not use `vehicle:getFlexmesh().getDebugVertexPos()`?**
A: Debug-only per-vertex API. Requires `setFlexmeshDebugMode(true)`. One FFI
call per vertex (550K+ calls per frame). No index buffer access, no UV access.
Not a production capture path.

**Q: How does the builder group primitives into objects?**
A: By flexmesh name prefix. The first primitive of a flexmesh has the base
name (e.g., `flanje_e180_body`). Subsequent primitives share a prefix plus
material suffix. `np.unique` on the combined index ranges from the shared
pool gives the vertex set for each object.

**Q: Can the format be reused with a different backend?**
A: Yes. The `.bmc` format is backend-agnostic. A C++ plugin, CPU mesh API,
or network stream all produce the same format. The builder and runtime never
change.

### File map

| File | Purpose |
|---|---|
| `capture.lua` | GPU readback backend → writes `capture.bmc` |
| `importer/capture_format.py` | BMC v1 binary format (pack/unpack) |
| `importer/capture_reader.py` | Reads `capture.bmc` for the builder |
| `importer/cache_builder.py` | BMC → BVC (simplified: no weld, no repair) |
| `importer/binary.py` | BVC format (unchanged) |
| `runtime/cache_reader.py` | Blender-side BVC reader (unchanged) |
| `runtime/mesh_update.py` | Blender mesh playback (unchanged) |
| `runtime/frame_handler.py` | Timeline handler (unchanged) |
| `runtime/baker.py` | MDD + Alembic export (unchanged) |
| `addon/` | Blender add-on UI (unchanged) |
| `tests/` | Test suite |

### Status

| Component | Status |
|---|---|
| Backend (GPU readback) | Architecture frozen, implementation pending |
| BMC format spec | Frozen |
| Python builder | Architecture frozen, implementation pending |
| Blender runtime | Complete and working (no changes needed) |
