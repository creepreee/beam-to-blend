# BeamNG Cache Importer — Artist Tutorial

Turn a BeamNG.drive crash into a playable, renderable Blender animation.

The workflow in one picture:

```
1. Capture   crash a car in BeamNG  →  .bmc file on disk
2. Build     Blender add-on         →  .bvc vertex cache
3. Import    Blender add-on         →  animated car + knobs
4. Render    Blender (Cycles/EEVEE) →  your shots
```

---

## Requirements

- **Blender 4.0+** (tested on 4.5)
- **BeamNG.drive** (any recent version — you only need it while capturing)
- A machine that can handle a large vertex cache. Crash captures are
  vertex-heavy: expect a **0.5–8 GB** `.bvc` file for a 30–60 s crash,
  depending on the car and capture length.

---

## Part 1 — Install the Blender add-on

1. Grab the latest `beamng_cache_importer.zip` from the
   [Releases](../../releases) page.
2. In Blender: **Edit ▸ Preferences ▸ Add-ons ▸ Install…**, pick the zip,
   then tick **BeamNG Cache Importer**.
3. A new **BeamNG** panel appears in the 3D-View sidebar (press `N` if the
   sidebar is hidden).

> The add-on must stay enabled in Preferences for saved .blend files to
> recover their animation after a restart — it hooks Blender's file-load
> event to re-attach playback.

---

## Part 2 — Install the capture mod in BeamNG

The capture side ships as a tiny unpacked mod (`v5capture`) that lives in
your BeamNG user folder.

1. Find your BeamNG user folder:
   - Steam default: `%LOCALAPPDATA%\BeamNG\BeamNG.drive\current\`
   - Or click **⌂ (open userfolder)** on the game's launcher.
2. Create (if missing) the folder:
   `mods\unpacked\v5capture\lua\ge\extensions\`
3. Copy the repo's `tools/v5_capture.lua` into that extensions folder and
   rename it to **`v5capture.lua`**.
4. Add a `mods\unpacked\v5capture\info.json` so the game lists the mod:

   ```json
   {
       "name": "v5capture",
       "version": "1.0",
       "title": "v5 crash capture",
       "author": "you",
       "description": "BMC crash sequence capture"
   }
   ```

5. **Restart BeamNG.** Unpacked mods are only scanned at startup.

---

## Part 3 — Capture a crash

1. Launch BeamNG, load any map, spawn any car.
2. Open the console with **`~`** (backtick). Run:

   ```lua
   extensions.load("v5capture")
   v5capture.start("captures/my_crash", 700)
   ```

   - The path is relative to your user folder — this writes
     `<userfolder>\captures\my_crash.bmc`.
   - The second number is the **max frames** (captured at 60 fps):
     700 ≈ 12 s, 1200 ≈ 20 s, 2400 ≈ 40 s. The capture also stops itself
     at the limit.
3. Drive however you like — hit things, roll it, drop it. The mod records
   the deforming mesh + rigid motion the whole time.
4. When the crash has played out, stop the capture:

   ```lua
   v5capture.stop()
   ```

5. Note the `.bmc` path. You're done in BeamNG — close it if you like.

> **Good to know**
> - The capture origin is set at `start()`: the car's spawn position. In
>   Blender your car will start near the world origin with small,
>   numerically-stable coordinates. Detached parts do **not** drift
>   relative to the car — that's handled by the capture format.
> - One `.bmc` per `start()`; start a new capture for each take.
> - Don't reload the vehicle mid-capture — the recording is bound to the
>   spawned vehicle's ID.

---

## Part 4 — Build the cache in Blender

1. Open Blender and the **BeamNG** panel (3D View ▸ N-panel ▸ BeamNG).
2. **Capture Folder** → point it at a folder containing your `.bmc`
   (e.g. copy `my_crash.bmc` into a working folder next to where you'll
   save your .blend).
3. Optional build settings:
   - **Weld duplicate vertices** — smaller cache and allows Shade Smooth /
     Weighted Normals later. Verified safe at build time (it aborts if any
     welded seam would visibly separate during playback). Leave off if you
     want a 1:1 copy of the capture.
4. Click **Build BeamNG Cache**. This writes a `.bvc` file next to the
   capture and fills in **Cache File** automatically.
5. Click **Import BeamNG Cache**. The car appears, fully posed at frame 1,
   with a parent Empty driving the rigid motion and per-frame vertex
   deformation on the meshes.

> The `.bvc` is the heavy file. Save your .blend after importing — the
> blend stores only the *path* to the cache, so keep both together (if you
> move or rename the `.bvc`, re-point **Cache File** and re-import).

---

## Part 5 — Playback knobs (all live, no re-import)

Everything in the main box retunes the **already-imported** animation in
place — drag the slider and the viewport updates, even with the playhead
parked. Nothing here requires re-importing.

| Field | What it does |
|---|---|
| **Start at Frame** | Slide the whole animation later on the timeline. Timeline frames — type `500` and playback starts at frame 500. |
| **Playback Speed** | Animation speed, in captured frames per real second. `60` = real time, `24` ≈ 2.5× slower, `15` = 4× slow-mo. |
| **Output FPS** | The scene's render FPS (smoothness of the render). Does **not** change the wall-clock duration of the crash — that's Playback Speed's job. |
| **Chunked Playback** | Merges small parts into chunks for fewer GPU uploads — faster on heavy scenes. Source objects stay available in a hidden collection. |
| **Smooth Car Stop** | Extends the timeline past the crash with a fitted damped-swing settle, so the car rocks to rest instead of freezing mid-pose. **Stop Frames** = tail length; **Stop Start Frame** (0 = off) can start the settle early, at the frame where the motion actually ends, skipping dead captured frames. |

> Tip: set **Output FPS** first, then set **Start at Frame** — a change of
> Output FPS keeps the frame *number*, so the offset stays where you put it.

---

## Part 6 — Textures (the car looks untextured!)

The add-on resolves real BeamNG materials from the vehicle's files.

1. Extract (or locate) the vehicle's game folder. For a mod car like the
   Flanje E180 this is the mod's vehicle root folder; for a vanilla car
   you can let the game zips provide it.
2. In the panel's texture section:
   - **Vehicle Folder** → the vehicle's root folder (the one containing
     `.materials.json` and its textures).
   - **Include Base Game Textures** — keep on. Mods inherit shared
     materials (tyres, brake discs, mirrors, plates) from the base game;
     without this those parts stay grey.
   - **Game Folder** — leave empty to auto-detect your BeamNG install. If
     auto-detection fails (non-standard install location), point it at the
     folder that contains `content\vehicles`.
3. Click **Assign BeamNG Textures**. Materials are built and assigned.

> If you set Game Folder manually, it's also saved in add-on Preferences
> so every future import finds it.

---

## Part 7 — Tyre contact

BeamNG's tyre mesh is rigid — a loaded tyre just sinks into the ground
instead of flattening. The **Tyre Contact** sub-panel fakes the missing
rubber at playback, driven only by how the cached wheel geometry sits
relative to the ground:

| Field | What it does |
|---|---|
| **Amount** (top slider) | Master strength — 0 = feature fully off (zero cost, byte-identical to plain playback). |
| **Static Deflection** | Extra squash so a *resting* tyre also shows a flat patch. |
| **Sidewall Bulge** | Pushes lower sidewall rubber out horizontally, like a loaded tyre. |
| **Lift-off Release** | Metres above ground over which every effect ramps back to zero — airborne tyres are bit-exactly round again. |
| **Ground Z** | Height of the ground plane. Use **auto-detect** (the button) after import; tweak only on sloped terrain. |
| **Tyre Names** | Comma-separated name filter (`tire,tyre` by default). Names matching keep their deformation; everything else in the wheel mesh (rims, hubs, brakes) stays rigid. |

The contact patch width tracks the real physics load automatically — you
only tune the cosmetic terms.

---

## Part 8 — Debris & glass

The crash captures *deform* the car, but shards and glass don't exist in
the vertex data. The **Debris** sub-panel builds them from the recorded
impacts:

1. Set the density/counts and physics feel (bounciness, scatter, speed).
2. Click **Build Impact Debris**. Rigid-body shards are baked to
   keyframes and glass panes get one of three fates: *intact*, *cracked*
   (crack web fades in), or *shattered* (pane empties, fragments fall).
3. **Clear Impact Debris** removes everything if you want a re-take.

Notes:
- Debris timing follows the car: build it at your final Playback Speed /
  Start-at-Frame and it lands on the right impacts; if you retune later,
  the debris re-times itself automatically on the next frame change.
- Glass cracking can use a custom crack image (**Use Image**) — supply
  your own texture and adjust span/scale.
- Particle-based debris falls at real render speed even in slow-mo; the
  baked rigid-body shards are the ones that slow down with the car.

---

## Part 9 — Physics extras (experimental)

The **Physics (EXPERIMENTAL)** sub-panel lets you add simple rigid-body
colliders/boundaries and preview velocity, then bake the result back to
keyframes. It's genuinely experimental — expect to re-take. The stable
path is: build debris, play, render.

---

## Part 10 — Rendering

- Real renders (F12 / Ctrl+F12) always match the viewport — the cache pose
  is applied on the render path too, including motion-blur subframes.
- **Export Alembic** bakes the entire animation (including tyre contact)
  into an `.abc` + point-cache if you want to hand the shot to another
  package or a lighter scene.
- Viewport Render Animation: if the viewport is in Rendered + Cycles,
  Blender itself freezes that mode (a known upstream limitation — even a
  plain keyframed cube freezes there). Switch the viewport to **Material
  Preview** or **Solid** first.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| "No .bmc capture in the folder" | Point **Capture Folder** at the folder that directly contains your `.bmc`. |
| Build fails: weld verification | Turn off **Weld duplicate vertices** and rebuild. |
| Animation gone after reopening the .blend | Make sure the add-on is still enabled in Preferences, and **Cache File** points at an existing `.bvc`. Recovery is automatic on load. |
| Car deforms but sits at the origin | Cache path moved/renamed — re-point **Cache File** and re-import. |
| Some parts grey/untextured | Enable **Include Base Game Textures**, and make sure **Vehicle Folder** is the vehicle's *root* folder. |
| Timeline too short / car frozen at the end | The range is derived from capture + settings; check **Start at Frame**, Playback Speed, and Smooth-stop values. |
| Renders show a statue while scrubbing works | Real renders are covered; see the Viewport Render Animation note in Part 10. |
| Cache is huge | That's normal — it's per-frame vertex data. Use **Weld duplicate vertices** at build time to shrink it. |
