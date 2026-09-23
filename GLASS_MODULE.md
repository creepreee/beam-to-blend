# Glass Module — Hand-off Specification

Standalone design + implementation document for the glass/debris subsystem of
the BeamNG Cache Importer add-on. Everything an AI (or human) needs to take over
the glass work: what it is, how it works, what is done, what is not, and how to
prove a change is correct.

All file paths are relative to the repo root (wherever you cloned this
repository).

---

## 1. Purpose

BeamNG crash captures replay as vertex animation. Real cars shed **glass** when
the glass *fails*, and automotive glass does not behave like bodywork. This
module gives every glass pane in the capture one of three fates:

| Tier | Meaning | On screen |
|------|---------|-----------|
| `intact` | never hit hard enough | clean pane, full vertex animation | <----the defualt one. no touch
| `cracked` | moderate strike | pane stays in frame, a hole + crack web fades in at the impact | <------ this can be a textue file too. just add a texture file node on these type of glass and the rest of texture fle on it, i (the user manually) can add it manually
| `shattered` | violent hit, or pane strikes the road | pane empties out of the car (mesh collapses), fragments break outward or inward basd on the overall car aniamtion and fall to the ground as rigid bodies |

The boundaries between tiers are **physics-calibrated against real capture
data**, not hand-waved.

---

## 2. The three-tier model (the design that must survive) or change accordingly

Glass damage is **monotonic and irreversible**, and detection fires a pane once
per crush peak (the real windshield fires at cache frames 652, 686, 784 while
the car rolls and grinds). So the model is:

1. **One outcome per pane, never per impact.** Repeated hits collapse into the
   *worst* tier reached, recorded at the *first* frame that reached it.
2. **`ground_depth` ORs against deformation**, never blends. "The windshield hit
   the road face-on" is categorically different from "the pillar bent and
   stressed the glass"; the pane moves rigidly in the first case so deformation
   alone under-reads it. A pane whose lowest vertex goes ≥ 0.03 m below the
   ground plane shatters regardless of measured deformation.
3. **A shattered pane's intact mesh disappears** from the car (collapsed to its
   centroid at the shatter frame) so the fragments *replace* it instead of
   double-rendering.
4. **A cracked pane keeps its glass** — no fragment, no collapse. It gets a
   material that paints the damage (on which textrure node is and the user can handle this texture apllying and its orientation(see `glass_crack.py`, §6.6).
5. **Shattered panes keep an edge fringe (IMPORTANT AS REAL LIFE CAR'S WINDWOS DONT COME OUT OF THE FRAEM ENITRELY THEY LEAVE THE WINDO ESGED ONT EH AR BEHINDN IN THAT FRAME** in the frame (default 5% of the
   pane's half-extent, measured inward). A perfectly empty aperture reads as
   "the object was deleted", which is exactly what it would look like. and for this method, the basic idea is simple. only destory the central vertices of the window, leaving behind some ede=ges f the window, then do the rest of the work on those vertis only (either create the vertex group or o whatever needed)

Thresholds (`GlassSettings`, calibrated on the real capture — windshield peaks
at 0.0313 m deformation and −0.124 m ground depth at cache frame 652):

| Field | Default | Meaning |
|-------|---------|---------|
| `crack_deform` | 0.006 | m/frame of deformation to start crazing |
| `shatter_deform` | 0.022 | m/frame to detach & break up |
| `shatter_ground_depth` | 0.03 | m below ground plane → face-on road strike |
| `edge_retain` | 0.05 | fringe width as fraction of pane half-extent |

---

## 3. File map

| File | Role | Glass-relevant symbols |
|------|------|------------------------|
| `runtime/impact_detect.py` | event detection + damage tiers | `is_glass` (:77), `ImpactEvent` (:87), `DetectSettings` (:163), `kabsch_residual` (:230), `sample_delta_to_ms` (:274), `detect_impacts` (:414), `GLASS_INTACT/CRACKED/SHATTERED` (:572–574), `GlassSettings` (:577), `classify_glass_damage` (:612), `resolve_glass_damage` (:638) |
| `runtime/glass_shatter.py` | pure-numpy pane fragmentation | `pane_basis` (:77), `shatter_pane` (:262) |
| `runtime/debris_spawn.py` | spawn fragments + hero debris + rigid-body setup | `DebrisSettings` (:301 `shatter_glass`), `GLASS_COLLECTION` (:1042), `_glass_material_for` (:~1058), `_spawn_glass_pane` (:1070), glass phase in `spawn_debris` (:1372–1409) |
| `runtime/debris_shards.py` | small glass shards (fine debris) | `glass` `FractureProfile` (:77), glass tint (:322), glass shading (:353) |
| `runtime/mesh_update.py` | per-frame vertex playback + shatter collapse | `set_shattered_panes` (:716), `_collapse_shattered` (:748) |
| `runtime/glass_crack.py` | **NEW, untracked, not yet wired in** — cracked-pane shader | whole file |
| `runtime/frame_handler.py` | timeline driver | glues detection + spawn + playback |
| `addon/ui.py` | panel properties | glass fields (:431–485), glass sub-panel (:624–630) |
| `addon/operators.py` | operator glue | `_debris_settings` (:35), `_glass_settings` (:61) |
| `tools/beamng_impact_events.py` | dev/CLI glass event dump | `GLASS` list (:14) |

**Tests / verifiers** (already exist, use them):
- `tests/test_impact_detect.py` — tier classification unit tests (incl.
  glass ladder, lines 147–174)
- `tests/test_glass_crack.py` — crack placement math, pure numpy
- `tests/test_glass_shatter.py` — fragmentation unit tests
- `tests/blender_glass_crack.py` — builds the crack material in Blender
- `tests/blender_debris_retention.py` — retained-fringe behaviour in Blender
- `verify/02_rebuild_and_motion.py`, `verify/03_debris_simulation.py`,
  `verify/04_debris_determinism.py` — end-to-end headless checks
  (pass `shatter_glass`, `crack_deform`, `edge_retain` into settings)

---

## 4. End-to-end data flow

```
capture.bvc
   │
   ▼
impact_detect.detect_impacts(reader, settings, ground_shift, playback_fps)
   │  per part: kabsch_residual shape-change → peaks → one ImpactEvent per crush
   │  ImpactEvent.velocity is in REAL m/s  (single source of truth, see §5.3)
   ▼
List[ImpactEvent]   (severity 0..1, relative_floor drops the noise tail)
   │
   ▼
resolve_glass_damage(events, GlassSettings)
   │  → {part: (tier, cache_frame, event)}   ONE per pane, worst tier, first frame
   │
   ├── tier == "shattered"
   │     ▼
   │   debris_spawn.spawn_debris(...)
   │     ├─ reader.frame_positions(part, cache_frame) → world space
   │     │        (local_to_world with frame transform + ground_shift)
   │     ├─ glass_shatter.shatter_pane(verts, impact_point, count, thickness,
   │     │        edge_retain, seed)  → fragments (retained flag set per cell)
   │     ├─ _spawn_glass_pane(...)     → fragment meshes in "BeamNG Debris Glass"
   │     │     • retained fringe  → PARENTED to <collection>__root empty at the
   │     │                          shatter pose (phase 0b) — rides the wreck,
   │     │                          never launched, never a rigid body
   │     │     • free fragments   → kinematic launch keys (LAUNCH_FRAMES),
   │     │                          then registered as RIGID BODY, baked
   │     └─ mesh_update.set_shattered_panes({part: cache_frame})
   │              → intact pane mesh collapses to its centroid from that frame on
   │
   └── tier == "cracked"
         → (planned) _apply_glass_crack → build_crack_material + keyframe_crack
           on the pane's object.  NOT YET IMPLEMENTED (see §6.6, §7).
```

Timing facts: a cache frame maps to a scene frame via
`_blender_frame_for(cache_frame, frame_start, playback_fps, output_fps)`.
The bake starts `LAUNCH_FRAMES + 2` frames before the first spawn, and the
timeline is *grown* (`frame_end = last_frame + settle_frames`) so the settle
never gets truncated (truncation = "debris stuck in mid-air").

---

## 5. Detection layer (`runtime/impact_detect.py`) — DONE

### 5.1 How an impact is found
- **Deformation is the only trigger.** Kabsch-residual per-vertex
  shape-change (`kabsch_residual`, :230) removes each part's *own* rigid motion
  within the vehicle — the correction that keeps a swinging door or a spinning
  wheel from reporting 0.7 m/frame of "deformation". Velocity/ground/impulse
  signals only *annotate* a deformation event (raises severity).
- Peak picking is greedy, strongest-first, with `min_separation` suppression
  (:303), so one long crush registers once.
- Severity = `0.7·d + 0.3·s + 0.15·(kinds−1)` normalised to the run's own
  maxima (`_score_and_filter`, :536); events below
  `peak · relative_floor` (0.18) are dropped as settling noise.

### 5.2 Glass-specific fields on `ImpactEvent`
- `ground_depth` (:119) — how deep the part's **lowest vertex** went below the
  ground plane, distinct from `height` (hotspot height). Measured, not
  inferred. This is what separates "cracked" from "hit the road".
- `energy` (:123) — absolute `deform·speed`, kept so an entire mild capture
  doesn't ratchet every pane to severity 1.0.
- `velocity` (:97) — **stored in metres per second**, produced by
  `sample_delta_to_ms` (:274).

### 5.3 The velocity units contract (critical, already fixed)
`sample_delta_to_ms(delta, playback_fps, stride)` = `delta * fps / stride`.
**Consumers must NOT re-scale this field.** The historical bug: the spawn code
multiplied by 24.0 *and* ignored the stride, launching debris at roughly double
speed (55 m/s instead of 28 m/s on a stride-2 scan); an even earlier version was
reported ~30× intended. The rule encoded in the docstrings: `ImpactEvent.velocity`
is already real m/s; `debris_spawn` adds `part_vel * inherit_velocity * intensity`
and nothing else.

---

## 6. Rendering layers

### 6.1 Fragment geometry (`runtime/glass_shatter.py`) — DONE
- `pane_basis(verts)` (:77) fits an orthonormal (u, v, n) basis to the pane's
  vertex cloud — the shatter math works in the pane plane, so a raked/curved
  windshield still fractures flat.
- `shatter_pane(verts, impact_point, fragments, thickness, edge_retain, seed)`
  (:262): Voronoi partition of the pane in its own plane; each cell becomes a
  fragment (extruded to `thickness`, faces as a thin shard). **Retained is
  decided by the cell centroid's distance to the pane outline**: cells whose
  centroid sits inside `edge_retain × half-extent` of the outline stay welded
  in the frame. Impact-near cells are biased to the impact point so the
  fracture radiates from the strike.
- Calibrated: 0.05 keeps ~4–8% of the real windshield (a clean thin ring);
  0.15 keeps ~24–39% and reads as a still-glazed window.

### 6.2 Pane spawn (`runtime/debris_spawn.py`) — DONE
`_spawn_glass_pane` (:1070), only reached for `GLASS_SHATTERED`:
- Fragment count from `fragment_count_for(event.severity, True, vertex_count=...)`.
- **Retained fringe** (phase 0b): parented to the `<collection>__root` transform
  empty with `matrix_parent_inverse` resolved at `spawn_frame` — they stay stuck
  in the aperture and ride the wreck's motion (bug fix: before, 65/82 retained
  fragments followed the chassis at z=−1.5).
- **Free fragments**: per-fragment outward throw
  `vel = away · (settings.speed · glass.speed_bias · (0.3 + 1.4·intensity) + scatter) · exp(−2.4·dist) · U(0.55,1.4)`,
  with the vertical component clamped to ≤ 20% of itself ("glass falls out of a
  window, it is not lobbed upward"), plus `part_vel · inherit_velocity ·
  intensity`. With the default `speed = 0` the throw term vanishes and glass
  simply drops out of the aperture — the physically-correct default.
- **Lowest-point clamp**: launch keys are clamped on the fragment's *lowest*
  vertex (not its origin) so the hull never penetrates the ground collider at
  release; the origin-only clamp let Bullet eject thin shards downward
  (measured: 15 backlight/trunkglass fragments ended at z=−107..−118).
- Free fragments get a kinematic launch (linear-interp keyframes), then are
  registered as rigid bodies in the second phase, then baked.

### 6.3 Fine glass shards (`runtime/debris_shards.py`) — DONE
`glass` profile (:77):
`size (0.012, 0.055) m, thickness 0.004, elongation 3.6, jitter 0.45,
count_bias 2.4, speed_bias 1.35, notch 0.45, warp 0.02`. Glass sprays hard and
splinters jaggedly. Tint `(0.62, 0.72, 0.70)` at :322; special-cased glass
shading (higher specular / transmission handling) at :353.

### 6.4 Mesh collapse (`runtime/mesh_update.py`) — DONE (recently fixed)
- `set_shattered_panes(panes: Dict[str, int])` (:716): **REPLACES** the map,
  computes each pane's centroid from the cache at its shatter frame, and
  forces the next `set_frame` to redraw.
- `_collapse_shattered(name, pos)` (:748): broadcast the stored centroid over
  the member's vertices from the shatter frame onward, so the intact glass
  visually leaves the car and the fragments take over.
- **The fix that matters**: an empty `panes` map now *un-collapses* every pane
  (turning "Shatter Glass" off or clearing debris restores the intact glass).
  Before, an empty map was a silent no-op and a pane collapsed in one build
  stayed collapsed in every later one — the "shattered state never clears" bug.
  Both individual (`_set_frame_individual`, :819) and chunked
  (`_set_frame_chunked`) paths apply it.

### 6.5 Material & UI knobs (`addon/ui.py`, `addon/operators.py`) — DONE
UI fields (all persist as `_beamng_*` props, live-editable, feed straight into
the running handler):
- `debris_shatter_glass` (default True) — master switch.
- `glass_crack_deform` (0.006), `glass_shatter_deform` (0.022),
  `glass_shatter_ground_depth` (0.03), `glass_edge_retain` (0.05).
- Wired through `_glass_settings` in `operators.py:61` into `GlassSettings`.

### 6.6 Cracked-pane shader (`runtime/glass_crack.py`) — NEW, UNFINISHED, UNWIRED
**This is the only genuinely incomplete piece.** The file exists (464 lines) but
is **not tracked by git and not imported anywhere** — it is currently dead code,
with passing unit tests (`tests/test_glass_crack.py`) and a Blender smoke test
(`tests/blender_glass_crack.py`).

Design (matches the "official plan" in its module docstring):
- Real laminated glass does not shatter on a moderate centre strike: a rock
  punches a **hole** while a **web of cracks** spreads across the whole pane,
  and the pane stays in its frame. The debris system already handles the violent
  extreme (shatter); this module paints the moderate extreme.
- **The pane is still part of the vertex-cache animation**, which writes mesh
  vertices BY INDEX. Subdividing/re-topologising to give the hole real edges
  would misalign every frame of the car. So the decoration is a *material*:
  - the hole is shader alpha (transparent inside, jagged rim),
  - the crack web is procedural (polar Voronoi, spokes crowded around the
    impact, fading toward the pane edges),
  - a driver fades the whole decoration in from the pane's crack frame
    (`_beamng_crack_amount` custom property, keyframed 0→1).

API:
- Pure numpy (unit-testable without Blender): `world_to_cache_local` (:44,
  exact inverse of `impact_detect.local_to_world` incl. ground shift),
  `crack_placement(local_verts, impact_cache_local, hole_radius)` (:85,
  projects impact onto pane plane, clamps inside the outline, clamps hole to
  ≤ 0.6·pane_radius), `hole_radius_for(scale, severity)` (:112).
- Blender-only: `build_crack_material(name, placement, web_intensity, seed,
  obj)` (:199, reuses an existing material of the same name to avoid `.001`
  copies), `keyframe_crack(obj, crack_frame, ramp=2)` (:429),
  `assign_crack_material(obj, mat)` (:446, slot-0 shuffle for Blender 4.5).
- Tuning constants (:122–134): `WEB_FOCUS 0.8`, `WEB_THETA_SCALE 2.5`,
  `WEB_R_SCALE 1.0`, `LINE_WIDTH 0.06`, `FROST_BASE 0.30`, `HOLE_EDGE 0.004`,
  `JAG_AMOUNT 0.5`.

**What is missing to complete it:**
1. An `_apply_glass_crack` function — **referenced in the docstring of
   `_spawn_glass_pane` (`debris_spawn.py:1094`) but it does not exist yet.**
2. The wiring in `debris_spawn.spawn_debris`'s glass phase (:1372–1409) to call
   it for `GLASS_CRACKED` panes (and to skip fragments for them — currently
   only `GLASS_SHATTERED` is handled at all).
3. `shattered_panes` / collapse must NOT include cracked panes (correct by
   construction once 2 is done).
4. A `glass_crack_scale` UI knob (the plan's `hole_radius_for(scale, severity)`
   implies an external scale; nothing currently binds it).
5. Decide the material source: real panes carry the car's own glass shader —
   the crack material replaces it wholesale today; verify that reads correctly
   with `assign_crack_material`'s slot-0 shuffle.
6. Add `runtime/glass_crack.py` to git and to the add-on build
   (`build_addon.py`).

---

## 7. Current state summary

| Capability | State |
|-----------|-------|
| Detect impacts (deform-only trigger, severity, m/s velocity) | ✅ done, tested |
| Glass tier classification + per-pane monotonic resolution | ✅ done, tested |
| Shatter: Voronoi pane fragmentation + retained fringe | X  missing, tested but didnt see the cracks + user sees no fringe or the edge vertics stuck on the car frame window, a misleading verification script said verified which was obv wrong|
| Spawn: fragment launch, rigid bodies, bake, lowest-point clamp | partial, tested BUT there are issue .the galss foloows the car aniamtion lie if it is its part addn doesnt become indepndednt in te bledner 3d space.|
| Mesh collapse at shatter frame; **empty-map un-collapse** (clear bug) | ✅ done, tested |
| UI knobs + persistence + live retune | partial, dont know about live tune yet |
| Fine glass shards (glass fracture profile) | ✅ done |
| Cracked-pane shader (`glass_crack.py`) | ⚠️ written + unit-tested, **not wired in, untracked** |
| Crack wiring in `spawn_debris` (`_apply_glass_crack`) | ❌ missing |
| Crack UI scale knob | ❌ missing |
| Crack in git + add-on build | ❌ missing |

---

## 8. Required results / acceptance criteria

For the module to be *finished*, all of these should hold (headless-verifiable)(changng the arch is acceptable too):

first and fy bar the most importatn fix one must do: the glass pane particels afetr they got smahed and scatterred MUST NOT FOLOW THE CAR EVER. previosuly there wsa similar weero in teh bemng import addon frotn eh the game beamng, the entire mesh had root trasnformation which also was aplied on the parts already deaatched form teh car. for exmpale f the mirror is crashed and detatched from the car, the mirrir IN ITS LOCAL SPACE covred its distance for teh car 20 meters. but then on later frames teh car body moves adn that made the mirror move with respect to e global axis WHEN THE MIRROR COMPLETED ITS ANIAMTION ALREADY ON LOCAL AXIS. THE MIRROR AND ALL THE OTHER DE ATATCHED PARTS FROM THE CAR KEPT Dragigng FROM THE CAR. thiis made the whole hting really fkish since when the mirrior or deatatched obejct was suppeos to sit on its resting motion acroding to the inertia law of physics while the car be movin gin 3d space compelting its inertia motion of the rest. BUT the obejcts which de atatcehd kept dragging with teh car body. then claude fixed with this problem now the same thign happesn withthe glass shattered panes of the car too. they keep moving ithteh car eventually when pieces shattered. this si teh mst important fix we need. just like the fix happend on thse detaachebale mirrois. fix the smae thing with the glass too. 

1. **Three visible fates.** A capture with both a road-strike windshield and a
   pillar-cracked pane shows: one pane empty with fragments + edge fringe, one
   pane still in the frame with a fading crack web + hole, other panes clean.
2. **One break per pane.** `resolve_glass_damage` returns ≤ 1 entry per glass
   part; re-running debris never spawns a second pane's worth of fragments.
3. **Ground strike shatters regardless of deformation.** An event with
   `ground_depth ≥ shatter_ground_depth` but low deform → shattered
   (unit-tested in `test_impact_detect.py:155`).
4. **Cracked panes keep their glass.** No fragments, no mesh collapse; the
   pane's vertices keep animating (no subdivision) and the crack fades in
   starting at the crack frame via the driver.
5. **Shattered pane mesh collapses** from its shatter frame (collapse to
   centroid) and **restores** when "Shatter Glass" is disabled / debris is
   cleared / playback rewinds before the frame.
6. **Retained fringe follows the wreck** (parented to the transform empty at
   shatter pose), free fragments launch + bake + settle on the ground (no
   z=−100 falls, no mid-air freeze, no 90 m sideways teleport).
7. **Physically sane speeds.** With default `speed=0` the throw term vanishes
   (glass drops out of the aperture); with `inherit_velocity` the pane's
   capture velocity is added in m/s, never re-scaled by fps or stride.
8. **Determinism.** Same seed + same capture ⇒ bit-identical debris
   (`verify/04_debris_determinism.py`).
9. **All tests pass**: `python -m pytest -q` (74 currently; the crack tests are
   included once `glass_crack.py` is importable), plus the Blender smoke tests
   (`blender_glass_crack.py`, `blender_debris_retention.py`) and
   `verify/02`, `verify/03`.
10. **Packaged**: `runtime/glass_crack.py` tracked, imported, and included by
    `python build_addon.py`.

---

## 9. How to run everything

```
python -m pytest -q                                   # unit tests (no Blender)
python build_addon.py                                 # repack add-on zip
blender --background --python tests/blender_glass_crack.py
blender --background --python tests/blender_debris_retention.py
blender --background --python verify/03_debris_simulation.py -- <cache.bvc>
blender --background --python verify/04_debris_determinism.py -- <cache.bvc>
python tools/beamng_impact_events.py <cache.bvc>       # CLI glass event dump
```

Conventions to preserve: `from __future__ import annotations` at the top of
every module; `importer/` must not import `bpy`; `runtime/` and `addon/` may;
pure-math (numpy-only) must live apart from the Blender node-tree code so it
stays unit-testable outside Blender.
