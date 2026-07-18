# Known Bugs & Limitations

## 1. `mesh.update_tag()` instead of `mesh.update()` — rendering corruption (FIXED)

**Status:** Fixed 2026-07-11 in `dist/beamng_cache_importer.zip`

**File:** `runtime/mesh_update.py:124`

**Symptom:** After importing the cache with chunked mode, the viewport showed severely corrupted geometry.

**What it looked like:**
The vertices do not explode outward or form long spikes. They remain roughly in
their expected locations, preserving the overall silhouette of the car. However,
the surface formed by these vertices was highly irregular and unstable:

- Large flat polygonal regions became visible where the body should be smooth,
  suggesting neighboring vertices were no longer producing a continuous surface.
- Body panels appeared faceted, with abrupt shading changes across triangle
  boundaries — as if adjacent vertices were slightly misaligned or updated
  inconsistently.
- On the front bumper, left fender, and side doors, the vertices produced
  overlapping or competing surfaces. Rather than a smooth curved mesh, the
  geometry fragmented into uneven triangular patches alternating dark/light —
  as if some vertices occupied nearly identical positions or different versions
  of the same positions rendered simultaneously.
- The overall proportions remained intact (hood, roof, wheels correct) — the
  distortion was localized to specific body panels.
- The car's silhouette was correct; the *connectivity and shading* were broken.

**Other symptoms:**
- Parts disappeared/reappeared when rotating the view (stale bounding box → viewport culling)
- Parts could not be selected by clicking in the viewport (raycasting uses stale bbox)
- Ctrl+Z undid the mesh to frame 0 (clean, static) — animation stopped completely because `_current_frame` was out of sync
- Shade Smooth temporarily fixed visuals (forces one-time normal recompute) but the next frame change brought corruption back

**Root cause:** `mesh.update_tag()` only flags a viewport redraw. It does NOT
recompute normals, bounding boxes, or edge caches. The `position` attribute
API correctly writes new vertex coordinates every frame, but without
`mesh.update()` the derived data (normals, bbox, tessellation) stays frozen
at frame-0 values. The result: vertex positions move correctly, but the
*surface* rendered from them is interpolated through stale normals, producing
the patchy, faceted, z-fighting appearance described above.

**Fix:** Replaced `mesh.update_tag()` with `mesh.update()` on line 124.
Estimated cost < 1ms per chunk — CPU stays under 10ms/frame, viewport
remains GPU-bound at 30fps.

---

## 2. Chunk map rejects dynamic objects

**File:** `runtime/mesh_update.py:127-159`

**Symptom:** Import fails with `Import failed: chunk map entry 'suspension': object 'flanje_e180_tierod_F' not found in cache`. The `BeamNG Cache` collection stays empty, only `BeamNG Cache (Source)` is populated (hidden). The user sees nothing in the viewport.

**Root cause:** `validate_chunk_map()` checks chunk members against `stable_names` only. `flanje_e180_tierod_F` is a dynamic (topology-changing) object, so it's not in the stable list. The validation raises `ValueError`, aborting the entire import. Dynamic objects are created separately after chunks, but the validation doesn't account for them.

**Fix:** (Applied 2026-07-10) `validate_chunk_map()` now accepts a `dynamic_names` parameter. Dynamic objects are excluded from the "every cache object must be covered by a chunk" check. `_build_scene_chunked()` filters dynamic members out of chunk groups before building, creating them individually instead.

---

## 3. `_log_bounds` min/max on every frame (performance, fixed)

**File:** `runtime/mesh_update.py:187-189`

**Symptom:** 1-5 fps in viewport even though cache reads are 0.01ms.

**Root cause:** `_log_bounds()` computed `pos.min(axis=0)` / `pos.max(axis=0)` over 550K verts for every chunk every frame — even when logging was disabled.

**Fix:** (Applied 2026-07-10) Short-circuit at top: `if self._log_fh is None: return`.

---

## 4. Legacy `foreach_set("co")` vertex write (performance, fixed)

**File:** `runtime/mesh_update.py:111-124`

**Symptom:** 1-5 fps in viewport.

**Root cause:** `mesh.vertices.foreach_set("co", ...)` is ~120× slower than Blender 4.x's `position` attribute API (110 ms vs 0.9 ms for 550K verts).

**Fix:** (Applied 2026-07-10) `_write_positions()` helper uses the `position` attribute path on Blender 4.x, falls back to legacy on pre-4.x.

---

## 5. Merging vertices (remove doubles) breaks animation — NOT A BUG

**Type:** Inherent limitation of indexed geometry caches.

**Symptom:** Running Merge by Distance (remove doubles) on imported cache meshes
visually improves the surface (removes ~241K duplicate verts), but the per-frame
animation stops working afterward.

**Why:** The cache writes positions indexed by vertex order. Frame 0 vertex index
100 → frame N vertex index 100. If you remove doubles, the vertex count changes
and the index mapping is destroyed — positions get written to wrong vertices.

**This is inherent to all indexed geometry cache systems** (Alembic, MDD, USD,
Point Cache, etc.). You cannot change mesh topology and keep indexed position
animation intact.

**Solution (implemented 2026-07-11):** Option A — weld duplicates during
cache build (`importer/cache_builder.py`), which remaps both base indices and all
per-frame position blocks so the cache itself has welded topology. Available as an
opt-in checkbox "Weld duplicate vertices" in the build panel (default off).
**Verification:** 44.1% vertex reduction on real data (550K→307K), max reconstruction
error 0.00021 (well within the 0.001 safety guard). A per-frame guard aborts the
build if any welded group ever separates. BVC2 format unchanged (only stored
counts shrink); runtime fully transparent. See `tests/test_weld.py` for the spec.

**⚠️ Degenerate face crash — Weighted Normal modifier (fixed 2026-07-11):**
Welding creates zero-area triangles (2+ corners collapse to the same vertex), and
the raw GLB data has degenerate tris like `[1,1,1]` in the tierod. Blender's
`normals_corner_custom_set_from_verts()` segfaults on these. The builder now filters
out degenerate faces for **both** welded stable objects and dynamic per-frame data.
Verified: 97/97 Weighted Normal modifiers apply without crash on animation data.

---

## 6. Custom split normals on raw GLB — NOT CACHE-RELATED

**Type:** BeamNG source data artifact.

**Symptom:** Individual GLB imports show dark/black triangles on some surfaces.
Clearing custom split normals fixes it.

**Why it doesn't apply to our cache:** The cache stores only positions + indices.
Normals (custom or otherwise) are never written, so clearing them has no effect
on the cached animation. The issue only appears when importing raw GLB files
directly.

---

## 7. Static Alembic export in chunked mode (fixed)

**File:** `addon/operators.py:28-92`

**Symptom:** Alembic `.abc` exports with no animation — single static frame when re-imported.

**Root cause:** In chunked mode, the 97 source objects (which carry MESH_CACHE modifiers) live in a collection with `hide_viewport=True` / `hide_render=True`. A hidden collection is excluded from the view-layer depsgraph, so MESH_CACHE never evaluates per-frame. `obj.select_set(True)` silently no-ops in a disabled collection → 0 objects exported. This was not caught by the earlier test which used individual mode (no hidden collection).

**Fix:** (Applied 2026-07-10) `_reveal_for_export()` / `_restore_after_export()` helpers temporarily un-hide collections + objects before export and restore state afterward.

---

## 8. Objects disappear on camera rotate — stale object bounding box (FIXED)

**Status:** Fixed 2026-07-11 in `dist/beamng_cache_importer.zip`.

**File:** `runtime/mesh_update.py` `_write_positions()`

**Symptom:** While navigating the viewport (rotating the camera), body parts vanish
one by one until the whole car is gone except the front tierod. Toggling into Edit mode
and back (doing nothing) brings everything back. Recurs on the next rotate.

**Root cause — the performance fix introduced it.** Bug #4's fix switched vertex writes
from the legacy `mesh.vertices.foreach_set("co", ...)` channel to the fast Blender 4.x
`mesh.attributes["position"].data.foreach_set("vector", ...)` channel (~120x faster).
But the two channels differ in a hidden way: the **legacy `vertices.co` write dirties the
mesh's bounding box; the `position` attribute write does NOT** — even followed by
`mesh.update()`. So the object keeps its **frame-0 bounding box** forever. As the car
deforms/flies away over the crash, each chunk's real geometry leaves its stale bbox.
Blender's viewport **frustum culling tests the stale bbox**, so on rotate it decides the
object is off-screen and hides it. The front tierod survived because it's the *dynamic*
object — rebuilt from scratch each frame (`from_pydata`), so its bbox is always fresh.
Edit-mode toggle forces a full bbox recompute — hence the manual workaround.

**Proven empirically (not guessed):** headless test moved a real chunk mesh's verts +500
in X via each write path and read `obj.bound_box`:
- legacy `vertices.foreach_set("co")` → bbox FOLLOWS (76.8 → 576.8)
- `position` attr + `mesh.update()` + `update_tag()` → bbox **STALE** (76.8 → 76.8) ← bug
- `position` attr + `mesh.transform(Identity)` + `update()` → bbox FOLLOWS ✓

Note: `mesh.update()`, `mesh.update_tag()`, `obj.update_tag()`, `view_layer.update()`,
and `depsgraph.update()` do **NOT** refresh the object bbox after a position-attr write
(all tested). This is why BUGS.md #1's `update()` change and Progress.md attempts 1–3
never fixed it — they addressed shading, not the bbox dirty-flag.

**Fix:** After the `position`-attribute write, call `mesh.transform(_IDENTITY_4X4)` (a
cached identity matrix). It re-flags the bounding box dirty at C speed (~2 ms for 550K
verts) without changing any coordinate. `set_frame` stays ~15 ms/frame chunked (~66 fps
CPU-only) — well above the 15 fps target.

**Regression test:** `tests/blender_bbox_tracking.py` — asserts the chunk object's bbox
tracks its vertices after a large shift, and that positions are not corrupted.
Run: `blender --background --python tests/blender_bbox_tracking.py`.

---

## 9. Animation freezes after "Shade Auto Smooth" — object not tagged dirty (FIXED)

**Status:** Fixed 2026-07-13 in `dist/beamng_cache_importer.zip`.

**File:** `runtime/mesh_update.py` `_write_positions()`

**Symptom:** The user right-clicks the car in the viewport and picks **Shade Auto
Smooth**. From that moment the animation stops — the car freezes at its current
position and never moves again on frame change, even though the timeline advances.

**Root cause:** In Blender 4.1+ "Shade Auto Smooth" is **no longer** the old
`mesh.use_auto_smooth` flag — it adds a **"Smooth by Angle" Geometry Nodes
modifier** (operator `bpy.ops.object.shade_auto_smooth`, node group appended from
the essentials asset library). Once a modifier is present, the object's *displayed*
geometry is the **modifier's evaluated output**, which the interactive depsgraph
caches **per object**. `_write_positions()` wrote new coordinates to the base mesh
and tagged only the **mesh** datablock (`mesh.update()` / `mesh.update_tag()`).
That is enough when the object has no modifiers (displayed geometry = the mesh),
but it does **not** invalidate the object-level modifier evaluation. So the
modifier kept emitting its frame-0 result forever → frozen car.

**Two things were wrong:**
1. The old code had a dead `use_auto_smooth` toggle block (lines ~140–154) that
   checked for `obj.use_auto_smooth` / `mesh.use_auto_smooth`. **Neither attribute
   exists in Blender 4.5**, so the block never executed — it targeted the pre-4.1
   API. Removed.
2. `_write_positions()` never called `obj.update_tag()`. Added it after the mesh
   update so every playback object is marked dirty each frame; the depsgraph then
   re-evaluates its modifier stack (Smooth by Angle, Weighted Normal, etc.) against
   the freshly-written base mesh.

**Fix:** After `mesh.update()` + `mesh.update_tag()`, call `obj.update_tag()` when
an object is supplied. Removed the dead `use_auto_smooth` block and the now-unused
`import math`.

**⚠️ Verification caveat (important for the next model):** this is an
**interactive-viewport-only** bug and could **not** be reproduced headless. A
scripted `bpy.context.evaluated_depsgraph_get()` forces a **full** scene
re-evaluation every call, so the evaluated (post-modifier) geometry moves between
frames **even with the fix disabled** — proven by toggling the `obj.update_tag()`
line off and re-running. The freeze only manifests with Blender's incremental
viewport depsgraph, which honours fine-grained dirty tags. `obj.update_tag()` is
the canonical, documented way to force per-object re-evaluation and is the same
mechanism `from_pydata` rebuilds (dynamic objects) already trigger implicitly —
which is why the dynamic tierod never froze. The regression guard
(`tests/blender_autosmooth_anim.py`) therefore asserts the *code contract* (the
real `set_frame` path runs cleanly with the real "Smooth by Angle" modifier on all
97 objects and keeps advancing the base mesh), not the pixels — see its docstring.

**Regression test:** `tests/blender_autosmooth_anim.py`.
Run: `blender --background --factory-startup --python tests/blender_autosmooth_anim.py`.

## 9. Frame writer merge_map corruption — sliced car (FIXED)

**Status:** Fixed 2026-07-16

**File:** `importer/cache_builder.py` — `_merge_capture_by_flexmesh()` + frame writer

**Symptom:** After building a merged BVC (weld=True) from capture data, importing in
Blender showed the car "sliced like a cake" — only the front-left quarter was visible,
the rest was scrambled or missing.

**Root cause:** The frame writer used `np.maximum.at(merged_pos, vert_remap, pos_all)`
where `vert_remap` was an "original→compact" mapping built with `np.empty(vc)`. The
`np.empty` left **uninitialized garbage** for vertices not referenced by any face (the
"unused" vertices). When these garbage indices were used as scatter targets, positions
from unrelated vertices were written into random compact slots — corrupting the entire
frame position block.

For multi-primitive merged objects, the problem was compounded: `weld_remap` was built
only from `np.unique(welded_idx)` (IDs that appear in the index buffer), so vertices
NOT referenced by any face could index outside the `weld_remap` array → IndexError or
garbage.

**Fix:** Replaced the "original→compact" scatter approach with a **"compact→original"
source map**. For each compact vertex, the source map stores exactly which capture object
and which original vertex index provides its position. The frame writer now does:
```python
valid = source_map >= 0  # skip vertices this member doesn't contribute
merged_pos[valid] = pos_all[source_map[valid]]
```
No uninitialized entries, no `np.maximum.at`, no scatter-gather corruption. Each compact
vertex gets its position from exactly one source vertex — deterministic and correct.

**Regression:** `python -m pytest -q` passes. Real-data build: 238→87 objects, 533K→313K
verts, 4.2→2.5 GB. Frame positions verified correct via CacheReader spot checks.
