# BeamNG Cache Importer for Blender

Turn **BeamNG.drive crash simulations** into real Blender animations.

Crash any car in BeamNG — the capture mod records the deforming vehicle mesh
and its rigid motion into a single file, and the Blender add-on replays that
file as vertex animation with materials, debris, glass shatter, tyre contact
and more.

```
BeamNG.drive                 Blender 4.x
┌──────────────────┐         ┌─────────────────────────────┐
│ crash any car    │  .bmc   │ BeamNG Cache Importer addon │
│ v5capture mod ───┼────────►│  1. Build Cache (.bvc)      │
│ (console command)│         │  2. Import                  │
└──────────────────┘         │  3. Animate / debris / bake │
                             └─────────────────────────────┘
```

> 📖 **New here?** The step-by-step artist walkthrough is in [TUTORIAL.md](TUTORIAL.md).

## What you get

- **True crash playback** — the exact deforming mesh from the physics engine,
  at 60 fps capture, retimable live in Blender (slow-mo, start offset, output
  fps) without re-importing.
- **Materials & textures** resolved from the game's own vehicle files.
- **Debris system** — impact detection spawns rigid-body shards, fine
  particle debris and detached glass fragments at the actual impact frames.
- **Glass behaviour** — laminated windshields crack (hole + radiating web
  painted in the shader), tempered glass shatters into fragments.
- **Tyre ground contact** — contact-patch flattening, sidewall bulge and
  lift-off release, computed per frame from the cached geometry.
- **Smooth car stop** — a damped continuation of the car's residual swing
  after the capture ends, so the wreck settles instead of freezing.
- **Alembic export** (experimental, script-only) — an `alembic_export` helper
  can bake the whole animation (deformation + transforms) to `.abc` for other
  DCCs; it is construction-side tooling, not a polished panel feature.

## Requirements

- **BeamNG.drive** (any recent version; the capture mod runs in the game console)
- **Blender 4.0+**
- A **capture** of a crash (see below) — anyone can share `.bmc`/`.bvc` files;
  you don't need BeamNG installed to *import* an existing capture.

## Installation

### Blender add-on

1. Download `beamng_cache_importer.zip` from the
   [Releases page](../../releases).
2. Blender → **Edit > Preferences > Add-ons > Install…** → pick the zip.
3. Enable **BeamNG Cache Importer**. The panel appears in the sidebar
   (**N key**) under **BeamNG**.

### BeamNG capture mod (only needed to record new crashes)

Copy the `tools/v5capture` folder into your BeamNG user dir:

```
<BeamNG user dir>\mods\unpacked\v5capture\
    info.json
    lua\ge\extensions\v5capture.lua
```

The BeamNG user dir is normally
`%LOCALAPPDATA%\BeamNG\BeamNG.drive\current\`. Restart BeamNG after copying.

## Capturing a crash

1. Launch BeamNG, spawn the vehicle you want to crash.
2. Open the console (**`~`**), then:

   ```lua
   extensions.load("v5capture")
   v5capture.start("captures/my_crash", 300)   -- path (relative), frames to record
   ```

3. Crash the car however you like. At 60 fps the mod records the deforming
   mesh + rigid motion per frame into **a single file**:
   `<userfolder>/captures/my_crash.bmc` (your user folder is
   `%LOCALAPPDATA%\BeamNG\BeamNG.drive\current\`). The path you pass is used
   as-is, so `"my_crash"` alone would write straight to the user folder.
4. Stop early if you want: `v5capture.stop()`

Detached parts (doors, bumpers, mirrors) are captured **origin-relative**, so
pieces that come to rest in the world do not drag along with the wreck.

## Building the cache & importing

1. In Blender, open the **BeamNG** sidebar panel.
2. **Capture Folder** → point at the folder *containing* your `.bmc`
   (e.g. `...\current\captures` where `my_crash.bmc` sits).
3. Click **1. Build Cache** — writes `my_crash.bvc` next to the capture.
   *Weld duplicate vertices* makes a smaller cache and enables smooth shading;
   leave it off if any part's edges separate mid-crash (the build verifies and
   aborts if welding would break a part).
4. Click **Import** (the next button in the panel). Chunked playback merges
   small parts for speed; the source objects stay available in a hidden
   collection.

The car animates on the timeline immediately — scrub, render, or add debris.

## Live tuning (no re-import)

These panel knobs retune the imported animation in place:

| Knob | What it does |
|------|--------------|
| **Start at Frame** | Slides the whole crash along the timeline |
| **Playback Speed / Output FPS** | Re-times playback; Output FPS changes `render.fps` only |
| **Tyre Contact** fields | Contact patch, deflection, bulge, lift-off |
| **Smooth Car Stop** / Stop Frames | Damped settle after the capture ends |

All values persist in the .blend — save, reopen, batch-render: playback
recovers automatically, including in background renders.

## Debris & glass

With the cache imported, use the **Debris** sub-panel:

- **Build Debris** — detects impacts in the cache and spawns rigid-body
  shards + fine particle debris at the impact frames.
- Glass panes crack or shatter depending on impact severity (tune the
  thresholds in the panel).
- **Clear Debris** removes everything again.

## Rendering

- Plain **F12 / Ctrl+F12** renders (Cycles, Eevee, Workbench) are fully
  supported, including background renders (`blender -b file.blend -a`).
- **View > Viewport Render Animation** is a Blender limitation with Rendered
  shading + Cycles (it re-evaluates nothing per frame — this affects keyframed
  objects too). Use Material Preview or Solid shading if you need it, or just
  render normally.
- **Export Alembic** (`beamng.export_alembic`) is available as a script-only
  operator — experimental / construction-side. Prefer rendering the cache
  directly from Blender.

## Command-line cache building

No Blender needed:

```bash
python tools/rebuild_cache.py --captures <captures-dir> --name mycrash
```

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| "No .bmc capture in the folder" | Point **Capture Folder** at the folder that directly contains your `.bmc` (e.g. `captures/` with `my_crash.bmc` inside) |
| Animation vanished after reopening the .blend | Make sure the add-on is enabled in Preferences *before* opening; recovery is automatic |
| Car deforms but sits at the origin | Recovery lost the root Empty — re-run import once; with the add-on enabled this recovers automatically |
| Parts of the car have no texture | Enable **Include Base Game Textures** and set **Game Folder** to your BeamNG install (auto-detected if left empty) |
| Debris fires at wrong times after changing speed | Re-built debris re-times automatically; very old builds need a rebuild |

## Notes on car compatibility

The importer works with **any** vehicle — the cache format is car-agnostic.
The optional **chunked playback** mode ships with a chunk map tuned for one
vehicle (the Gavril-style E180 used in development); for other cars the
importer automatically falls back to per-object playback, which is only
slightly slower. You can also pass your own chunk map programmatically
(`CachePlayback(reader, chunk_map=...)`).

## License

See [LICENSE](LICENSE).

## Credits

Built with the BeamNG.drive engine's own vehicle/part APIs; capture runs
entirely in-game via the bundled `v5capture` Lua mod (no external drivers).
