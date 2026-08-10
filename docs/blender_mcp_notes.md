# Blender MCP + Blender 4.5.9 — Working Reference for AI Agents

Goal: let future AI sessions pick up immediately without re-deriving the API quirks.

## Environment
- Blender: `C:\Users\ubaid_i2c\Downloads\blender-4.5.9-windows-x64\blender-4.5.9-windows-x64\blender.exe` (also on User PATH). 4.2.21 also exists on disk.
- MCP addon (server side, runs inside Blender): `C:\Users\ubaid_i2c\AppData\Roaming\Blender Foundation\Blender\4.5\scripts\addons\blender_mcp.py` — auto-starts a TCP socket on port **9876** when enabled (enable via `userpref.json` `"addons": {"blender_mcp": true}`).
- MCP server (client side, stdio): `uvx blender-mcp` (PyPI v1.29.0). Transport = newline-delimited JSON-RPC 2.0 over stdio. Init handshake: `initialize` with protocolVersion `2024-11-05`, then `notifications/initialized`, then `tools/list` / `tools/call`.
- opencode config: `C:\Users\ubaid_i2c\.config\opencode\opencode.jsonc` has the `mcp.blender-mcp` block (uvx, `BLENDER_HOST=localhost`, `BLENDER_PORT=9876`, `DISABLE_TELEMETRY=true`). opencode must be **restarted** to load config.
- Remote render rig: plink/pscp `danish@192.168.0.16`, queue `/home/danish/renders/remote_run_queue.sh`, watchdog `/home/danish/renders/watchdog_render.py`. Not Blender-related, separate concern.

## MCP tools available (22)
`get_scene_info`, `get_object_info`, `get_viewport_screenshot`, `execute_blender_code`,
`get_polyhaven_categories`, `search_polyhaven_assets`, `download_polyhaven_asset`, `set_texture`, `get_polyhaven_status`,
`get_hyper3d_status`, `get_sketchfab_status`, `search_sketchfab_models`, `get_sketchfab_model_preview`, `download_sketchfab_model`,
`generate_hyper3d_model_via_text`, `generate_hyper3d_model_via_images`, `poll_rodin_job_status`, `import_generated_asset`,
`get_hunyuan3d_status`, `generate_hunyuan3d_model`, `poll_hunyuan_job_status`, `import_generated_asset_hunyuan`.

- `execute_blender_code(code, user_prompt)` is the universal hammer — runs arbitrary bpy Python, returns stdout.
- `get_scene_info(user_prompt)` returns object names/type/location + material count (JSON).
- NOTE: this model (big-pickle) cannot view images — `get_viewport_screenshot` returns an image the USER sees, not the AI. Verify visually via numeric queries instead (locations, dims, colors, counts).

## Blender 4.5 API QUIRKS — commands that FAILED (do not repeat)
1. **`RigidBodyWorld.steps_per_second`** → AttributeError. Renamed in 4.5 to **`substeps_per_frame`**. `solver_iterations` still valid.
   - Confirmed RigidBodyWorld props (4.5): `bl_rna, collection, constraints, convex_sweep_test, effector_weights, enabled, point_cache, rna_type, solver_iterations, substeps_per_frame, time_scale, use_split_impulse`.
2. **`GeometryNodeDistributePointsOnFaces.distribute_method = 'POINTS'`** → `enum "POINTS" not found in ('RANDOM','POISSON')`. Use `'RANDOM'` or `'POISSON'`.
3. **`bpy.ops.object.modifier_apply(modifier="GeometryNodes")` when GN outputs instances** → `Error: Evaluated geometry from modifier does not contain a mesh`. The GN tree outputs instances (no base mesh), so Blender cannot bake a "mesh". `ev.to_mesh()` on such an object ALSO returns **0 verts/0 polys** (empty).
   - **Fix:** insert a **`GeometryNodeRealizeInstances`** node into the node group before Group Output, THEN the evaluated output is a real mesh → `to_mesh()` / `meshes.new_from_object()` / `modifier_apply` all work.
4. **`me.name = "x"` on mesh returned by `ev.to_mesh()`** → `bpy_struct: attribute "name" from "Mesh" is read-only`. `to_mesh()` returns a temp mesh NOT in bpy.data. Use `bpy.data.meshes.new_from_object(ev_obj)` for a real, nameable mesh ID.
5. **MCP `get_viewport_screenshot`** returns image bytes — cannot be read by image-blind models (inform the user; capture anyway for the user).
6. First stdio connection returned 10061 (connection refused) — that was Blender still booting; addon server binds only once Blender is fully up.

## Working recipes (verified)
- Create rigid body world: `bpy.ops.rigidbody.world_add()`; then `scene.rigidbody_world.substeps_per_frame = 8`, `solver_iterations = 10`.
- Add rigid body to object: select only it, make active, `bpy.ops.rigidbody.object_add(type='ACTIVE'|'PASSIVE')`, then `obj.rigid_body.collision_shape = 'BOX'|'CONVEX_HULL'|'MESH'`, `.mass`, `.friction`, `.restitution`.
  - Use `'MESH'` for a passive plane (a zero-thickness plane with BOX shape is degenerate).
- Realize GN instances → real objects: add Realize Instances node to the tree → `ev = obj.evaluated_get(depsgraph); me = bpy.data.meshes.new_from_object(ev)` → `bpy.data.objects.new("name", me)` → link to collection → `bpy.ops.mesh.separate(type='LOOSE')` in edit mode to split per-island objects.
- Build a GN tree in code: create group with `bpy.data.node_groups.new("Name","GeometryNodeTree")`, add I/O via `gtree.interface.new_socket(name="Geometry", in_out="INPUT"/"OUTPUT", socket_type="NodeSocketGeometry")`, add nodes `gtree.nodes.new("GeometryNodeMeshCube")` etc., wire with `gtree.links.new(from_sock, to_sock)`, assign `modifier.node_group = gtree`.
- Fracture a cube without the Cell Fracture addon (NOT bundled in 4.5 — moved to extensions; RBDLab exists at `...\AppData\Roaming\Blender Foundation\Blender\4.5\extensions\user_default\RBDLab` but is complex to drive): subdivide the mesh a few levels, then N random `bpy.ops.mesh.bisect(plane_co=..., plane_no=..., clear_inner=False, clear_outer=False)` cuts in edit mode, then `separate(type='LOOSE')` → shard objects. Add CONVEX_HULL rigid bodies.
- Step a rigid-body sim without baking: loop `scene.frame_set(f)` for f in range(start, end+1); rigid body world with cache mode `'SCENE'` evaluates automatically. Set `scene.frame_start`/`frame_end` first.

## Shatter-task learnings (rigid-body cube drop) — READ BEFORE FRACTURE
Approach pivoted to a plain 2m blue cube at z=12 (no GN particles) + passive ground + RB world (substeps 8, iter 10, frames 1–200). Tried to fracture by script:

**FAILED approaches (do not repeat):**
- `bpy.ops.mesh.bisect(clear_inner=False, clear_outer=False)` + `separate(type='LOOSE')` → **does NOT split**: bisect keeps both halves connected through the shared cut-loop vertices → mesh stays 1 loose island. Selection-before-bisect does not change this.
- `bmesh.ops.bisect_plane(...)` then `bmesh.ops.split(bm, geom=res['geom_cut'])` → WRONG: splits the cut FACES off as separate slivers; original surface stays ~16 huge patches. Produces 192 objects, mostly degenerate; cleaned to 16 all-2.0m parts (unfractured).
- `bmesh.ops.bisect_plane` then `bmesh.ops.split(bm, geom=bm.verts[:])` (canonical snippet) → **crashed Blender** (`Connection to Blender lost: [WinError 10054]`), likely memory blowup from repeated splits of the whole vert list. Avoid.
- GN instance realization: `modifier_apply` fails when GN outputs instances-only ("does not contain a mesh"); `to_mesh()` returns 0 verts. FIX: add `GeometryNodeRealizeInstances` node, then `meshes.new_from_object(ev_obj)`. This part WORKED (287 particles) but was abandoned for the simpler cube.
- A 9mm-particle field of 287 rigid bodies is too tiny/dusty — dropped in favor of plain cube.

**WORKING — RBDLab Cell Fracture (VERIFIED, this is the way).**
- RBDLab is enabled in the running Blender (`bl_ext.user_default.RBDLab` in prefs.addons). Underlying operator: **`bpy.ops.rbdlab.add_fracture_cell_objects`** (defined in `...\RBDLab\libs\cell_fracture\__init__.py`).
- Working call (produces ~38–40 shards, ~0.24s for 40 cells):
  ```python
  bpy.ops.rbdlab.add_fracture_cell_objects(
      source={'VERT_OWN'},          # use the object's own verts as cell centers
      source_limit=40, source_noise=0.35, recursion=0,
      margin=0.05,                  # GAPS between cells — critical for stable physics!
      use_remove_original=True, use_recenter=True, use_island_split=True,
      use_data_match=True, use_sharp_edges=True, use_sharp_edges_apply=True,
      collection_name="Shards", use_debug_redraw=False)
  ```
  - Pre-step: `subdivide(number_cuts=3)` the cube so VERT_OWN has ~98 source points.
  - `margin` < ~0.01 → cells spawn overlapping → rigid-body solver EXPLODES them (they tunnel through the ground; observed min_z -237, max_xy 44). margin=0.05 → min separation ~0.37m, clean sim.
  - `source` is an ENUM_FLAG → pass as set `{'VERT_OWN'}`.
  - Operator prints "Found N points" + "Done! N objects".
- After fracture: original cube is gone (use_remove_original). Shards named `BlueCube_cell`, `BlueCube_cell.001`… (in "Shards" collection).
- Add rigid bodies manually per shard: `bpy.ops.rigidbody.object_add(type='ACTIVE')`, `collision_shape='CONVEX_HULL'`, mass via `bmesh.calc_volume()` (NOT `mesh.calc_volume()` — does not exist in 4.5).
- Ground: passive RB. Thin flat plane + fast objects → TUNNELING. Use a thick slab (cube scaled e.g. (50,50,0.2)) with `collision_shape='BOX'`.
- RB world: `substeps_per_frame=16`, `solver_iterations=12` (4.5 names; `steps_per_second` renamed → `substeps_per_frame`).
- Step sim: loop `scene.frame_set(f)` 1..200 (cache 'SCENE' auto-evaluates; ~0.3–0.4s for 38 bodies over 200 frames). VERIFIED RESULT: cube fell 12m, shattered, scattered 38 pieces over ~5.7m; min_z 0.12 (on slab), nothing below ground.
- `Mesh.calc_volume()` does NOT exist in 4.5 → use `bmesh.new(); bm.from_mesh(me); bm.calc_volume()`.

## BeamNG crash-scene task (scene2.blend) — HARD CONSTRAINTS
- File: `D:\stranger things\scene3\scene2.blend` (open in Blender). Car crash captured from BeamNG; animated at runtime by the `beamng_cache_importer` addon (`C:\Users\ubaid_i2c\AppData\Roaming\Blender Foundation\Blender\4.5\scripts\addons\beamng_cache_importer`).
- **NEVER modify car geometry.** A `frame_change_pre` handler (`runtime.frame_handler._on_frame_change` → `CachePlayback.set_frame`) rewrites every car part's vertex positions from `name.bvc` each frame. Any mesh edit is overwritten / can break the animation. Particles/emitters/ground must be SEPARATE objects.
- **NEVER press Ctrl+Z in this .blend** — undo destroys the car mesh animation (known Blender+addon bug). Also avoid undo-generating ops in scripts.
- Cache: `C:\Users\ubaid_i2c\AppData\Local\BeamNG\BeamNG.drive\current\captures\name.bvc` (4.5 GB, 1200 frames, 97 objects, v5, per-frame transform). Mapping: `cache_frame = round((blender_frame - 400) * 24/60)` (start frame 400, playback 24 / output 60).
- Crash: car jumps (cache ~0-590), tumbles, nose-dives into ground cache ~600-700 (Blender ~1900-2150), crushes/settles by cache ~1000. Body min-z reaches -2.08m at impact; settles buried ~-0.98m.
- Scene has NO ground object; no large meshes. Car parts parented to `BeamNG Cache__root`. Playback chunk meshes: body, hood, trunk, door_FL/FR/RL/RR, wheels, glass, lights, fenders_bumpers, suspension, interior, engine_bay. 97 source meshes `flanje_e180_*`. Body material includes `CarPaint_White` (car is white).
- Task: per-OBJECT impact detection (each part's min-z / velocity / vertex-compression → impact events) → spawn matching particles ONLY when that part hits/deforms. Windshield cracking = user's addon-side job (not us). Car-matched materials (white paint metal shards, glass BSDF shards), visible ground plane for particle collision.
- RBD Lab particles module = Blender-native PSys wrappers (`utils/particles.py`, presets `DefaultDebris/Dust/Smoke/RealDust`) — no impact detection built in.

## Current scene task state
- **DONE**: cube falls & shatters via RBDLab → 38 shards scattered on ground slab (frame 200). Scene: Ground slab + 38 `BlueCube_cell` rigid bodies.
- Cell Fracture addon (`object_fracture_cell`) NOT bundled in 4.5; RBDLab is the installed tool.

## CRITICAL CORRECTION (read before diagnosing "RB won't move")
- **`obj.location` is NEVER updated by the rigid-body sim.** RB overrides the transform at depsgraph-evaluation time only; the object's base `location`/`rotation` stay at rest values. This caused a false "rigid body solver is frozen" alarm across an entire session.
- **To read simulated transforms:** `depsgraph = bpy.context.evaluated_depsgraph_get()` then `ev = obj.evaluated_get(depsgraph); ev.matrix_world.translation`. Call `depsgraph.update()` after `frame_set()` first. Bake cache `is_baked=True` + `point_cache` reports are fine — trust evaluated matrices, not `location`.
- Verified: scripted `scene.frame_set(f)` loop DOES advance the RB solver normally; no special playback needed.

## Bottom-only fracture (part-scatter) — VERIFIED approach
Use the RBDLab **Prepare → Proxy** workflow to fracture ONLY a sub-region; the parent object stays whole.
1. `bpy.ops.rbdlab.prepare_proxy_add_modifier()` → duplicates object to `<name>_proxy` (original auto-hidden, becomes `current_proxy_ob`).
2. Boolean-DIFFERENCE on the proxy against a cover box → proxy becomes the desired region only (bottom 40%, z 11→11.8). Apply the boolean.
3. `bpy.ops.rbdlab.prepare_accept_proxy()` → proxy becomes a real mesh. Subdivide it (`number_cuts=2`) to seed fracture points.
4. `bpy.ops.rbdlab.add_fracture_cell_objects(source={'VERT_OWN'}, source_limit=30, source_noise=0.35, margin=0.05)` on the proxy only → 27 cells in `BottomShards` collection; delete the proxy slab.
5. Carve the original: boolean-DIFFERENCE with a bottom-cover box → top 60% chunk (z 11.8→13), 0.8m overlap removed → no overlap with shards.
6. Physics: top chunk ACTIVE CONVEX_HULL (mass via bmesh volume), shards ACTIVE CONVEX_HULL, thick passive BOX slab; `substeps_per_frame=16`, `solver_iterations=12`.
7. VERIFIED RESULT (evaluated matrices): top chunk fell and landed tilted as ONE piece at z≈0.77; 27 bottom shards settled on slab, z 0.055–0.711, x spread 8.5m, y spread 14.2m. Only the bottom layer breaks apart — top stays solid. (Scatter this wide comes from `source_noise=0.35` + margin 0.05; reduce noise to tighten.)
