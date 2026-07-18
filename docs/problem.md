# problem.md — The "stretched / shredded geometry" bug (capture path)

Written 2026-07-15 for any AI or human picking this up. This documents a bug that
took two full sessions to localise because the symptom (bad geometry in Blender)
was several layers away from the cause (a vertex-gather bug in the BeamNG Lua
capture mod). Read this before touching the capture path.

---

## Symptom (what the user saw)

Importing the capture-path BVC into Blender produced:

- A car whose outer silhouette was roughly right, but
- lots of **vertices stretched across the whole car**, especially at the front and
  *inside* the body,
- a **dense shredded / overlapping interior** of sliver triangles,
- persistent **gaps / cavities / missing faces**.

Crucially: this was visible **at frame 0**, on a car still sitting on the scene —
before any crash deformation. So it was not a physics/animation problem.

## What it was NOT (ruled out with evidence)

1. **Not the weld.** Deleting the weld, re-offsetting, and component-splitting the
   merged mesh all failed to fix it.
2. **Not the merge-by-part-name** in the baker. Even a single raw primitive,
   imported alone with no merge and no weld, already contained car-spanning
   triangles.
3. **Not the crash physics.** Present at frame 0. Raw per-primitive edge lengths
   barely changed across the crash (max edge 4.274m → 4.272m).
4. **Not a double-transform.** Captured positions are vehicle-LOCAL (Z-up, body
   ±2.5m around origin); the per-frame 7-float transform carries world motion.
   Applying it is correct.

## The decisive comparison (ground truth)

We have a known-good reference: the GLTF/`testglt/` export path, which imports
cleanly. Comparing the **same object** across the two exporters was the smoking
gun:

| Export | object `taillight_trunk` | verts | max edge | giant triangles |
|---|---|---|---|---|
| GLB (good) | whole object | 31,983 | 0.071 m | 0 |
| Capture (broken) | primitive `..._chrome` | 26,573 | **4.274 m** | **8,784** |

Same total vertices, same bounds — but the capture split one flexmesh into
material primitives, and one primitive (`chrome`) had triangles spanning the
entire car (bbox X: -1.9 → 2.4 m). The **positions were fine; the vertex set that
each primitive gathered was wrong.**

## Root cause (the actual bug, in the Lua mod)

File: `TelekinesisController/lua/ge/extensions/beamng/capture.lua`
(installed under `BeamNG.drive/current/mods/unpacked/...`).

BeamNG exports one vehicle as a **shared GPU vertex pool** plus a set of
**flexmeshes**, each split into **material primitives** (`gpuPrimitive_t` with
`startIndex`, `indexCount`, `materialId`). A primitive is a slice of the shared
**index** buffer; its indices point into the shared **vertex** pool.

The mod extracted each primitive like this (old, broken):

```lua
local minIdx, maxIdx = meshInfo:indicesMinMax(si, si + ic - 1)
local vc = maxIdx - minIdx + 1                 -- assume a CONTIGUOUS block
for j = 0, ic - 1 do
    localIdx[j] = indices[si + j] - minIdx     -- remap against minIdx
end
-- per frame, gather vc vertices starting at minIdx:
for j = 0, vc - 1 do  gather vertices[(minIdx + j)*3]  end
```

This assumes a primitive's indices **densely fill** `[minIdx, maxIdx]`. That is
only true for the **base** primitive (`p == 0`). A **material-split** primitive
(e.g. all the "chrome" trim, or all the "paint") references a **sparse, scattered**
subset of the pool — chrome vertices from the front bumper *and* the rear
taillights. For such a primitive:

- `minIdx..maxIdx` spans a huge range (26,573 for chrome),
- gathering that whole contiguous block pulls in **thousands of foreign
  vertices** belonging to other primitives,
- `localIdx = index - minIdx` then maps the triangles onto that oversized block,
  wiring **unrelated front/rear vertices together** → the 4.27 m car-spanning
  triangles, the stretch, the shred.

**Second, compounding bug:** the per-frame gather cache was **keyed by object
name** (`flexmeshCache[obj.name]`). Material-split primitives can share a material
→ duplicate names → the second one **overwrites** the first in the table, so the
first object's per-frame positions were gathered from the wrong `minIdx`. 7 of the
13 worst-broken objects had duplicate names.

The GLB path never hit this because it reads the shared pool and uses
`np.unique((pool, vertex))` identity, which naturally separates disjoint surfaces
regardless of material batching.

## The fix (in the mod — v4)

Do exactly what the GLB reader does, in Lua. For each primitive:

1. Walk its `ic` indices and collect the **unique** global vertex indices it
   actually references (`seen`/`usedList`).
2. `table.sort(usedList)` → the ordered global-index list `usedFfi`.
3. Build `remap[globalIdx] = denseLocalIdx` and rewrite the index buffer against
   it (the Lua equivalent of `np.unique(return_inverse)`).
4. Gather UVs for exactly those used vertices, in the same order.
5. Store `usedFfi` per object in an **ordered** list `frameGather` (parallel to
   `metaObjects`, **not** keyed by name). Per-frame gather then pulls exactly
   those global vertices in that order — collision-proof and sparse-safe.

After this, each primitive is a self-contained mesh with a compact, correct vertex
set. The baker's merge-by-part-name + weld then stitches material seams as
designed.

## Why the baker fix alone couldn't work

The corruption is **baked into `capture.bin`** — the wrong vertices were written
to disk at capture time. No baker-side dedup, re-offset, or component split can
recover the correct surface from a position stream that gathered the wrong
vertices. **The fix had to be in the mod, followed by a re-capture.**

## How to verify a new capture is clean

Per-primitive at **frame 0**, before any baker processing:
- No triangle edge should span a large fraction of the vehicle (e.g. > ~1.5 m for
  a passenger car). A `chrome`/`glass` primitive with a 4 m edge = still broken.
- A trim primitive (chrome, glass) should have a **small** vertex count and tight
  bounds, not tens of thousands of verts spanning the whole car.
- Object names in `capture.meta` may still repeat (baker de-dups), but per-frame
  positions must be gathered by index order, so duplicates no longer corrupt.

Then run the baker with `weld=True` and confirm in Blender: coherent silhouette,
no interior shred, seams closed under deformation.

## Files touched by the fix

- `capture.lua` (the mod) — `extractFlexmeshes` (sparse-unique extraction) and
  `captureFrame` (ordered `frameGather` instead of name-keyed `flexmeshCache`).
  Bumped to **version 4**.
- Baker (`importer/cache_builder.py`) merge-by-part-name + weld is still correct
  and still required for the capture path — it stitches material seams. It just
  couldn't fix corrupt source data on its own.

## One-line summary for the next AI

The stretched/shredded mesh was **not** a baker bug — the BeamNG capture mod
gathered a contiguous `minIdx..maxIdx` vertex span for each material primitive,
but split primitives reference a *sparse scattered* subset of the shared pool, so
it pulled in foreign vertices and wired car-spanning triangles (made worse by a
name-keyed per-frame cache colliding on duplicate material names). Fix = gather
only each primitive's unique referenced vertices with a dense remap
(np.unique-style) and key per-frame gather by object position, then re-capture.
