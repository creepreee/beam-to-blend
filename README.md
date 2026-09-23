# Beam to Blend

**BeamNG.drive crash simulations, imported into Blender as real vertex animation.**

Crash cars in BeamNG, the most realistic soft-body car physics engine out
there, record the exact deforming mesh + rigid motion, and this add-on
replays it in Blender as per-frame vertex animation with materials, debris,
glass shatter, tyre deformation and more.

> 📖 **New here?** The step-by-step artist walkthrough is in [TUTORIAL.md](TUTORIAL.md).
> 🐞 **Found a bug?** [Open an issue](../../issues) — seriously, that's how this gets fixed.

---

## Why this exists

Car crashes have always been a horrible job in CG. You spend ages rigging
**constraints**, manually keyframing each body panel, then days *baking* a
physics sim — and half the time the result still looks fake. Procedural
deformation simply is not a realistic way to show a crashing vehicle in a
VFX shot.

So I went looking for a simulator that gets it right, and found
**BeamNG.drive** — hands down the best and most realistic car-crash game
there is. Its engine runs real soft-body **beam** physics: the car's chassis
is a lattice of beams and nodes, so every panel genuinely deforms, crumples,
and detaches the way metal does in real impacts. If BeamNG can do the crash
for us, all the CG side has to do is *record it* and *play it back*.

## How it works, theoretically

BeamNG already owns the hard 90% — the crash. This add-on only needs to
transport that simulation into Blender without losing or faking anything.

```
BeamNG.drive                    Blender 4.x
┌────────────────────┐          ┌──────────────────────────────┐
│  any vehicle       │   .bmc   │  BeamNG Cache Importer addon  │
│  v5capture Lua mod ┼─────────►│  1 · pick the .bmc capture    │
│  (reads GPUMesh at │          │  2 · tune playback knobs      │
│  60 fps, per part) │          │  3 · Build Cache & Import     │
└────────────────────┘          │  → animate, debris, render    │
                                └──────────────────────────────┘
```

**1. Capture (in-game).** A small Lua mod (`v5capture`) runs inside
BeamNG's console. While the crash plays, it reads the vehicle's deforming
mesh straight from the render pool at 60 fps — **per part**, in world space —
plus the vehicle's rigid transform each frame. It writes everything to **one
flat file** (`captures/<name>.bmc`, a memory-map-ready binary format).

**2. Build.** The `.bmc` is a *frame dump*, not an animation. The builder
(`cache_builder.py`) welds identical duplicate vertices across frames,
indexes the topology, and bakes it into a compact vertex-cache file
(`.bvc`) — a structured directory of per-frame position streams. A 300-frame
crash recorded at 60 fps stays live and scrubbing-fast.

**3. Import & playback.** The Blender add-on memory-maps the `.bvc` and
drives each object's mesh coordinates per frame via a timeline handler —
*no keyframes are ever generated*. The rigid motion rides on a parent Empty,
so wheels/doors/detached parts stack correctly. Because each part's
per-frame positions are just bytes in the cache, playback is jitter-free and
constant-memory: the car's whole timeline streams from disk, never held in
RAM.

The simulation is captured **once**, **exactly** — and then it's just data
in Blender: retime it, slow-mo it, drop debris on it, render it.

## The old way was miserable

This project grew out of frustration with the previous approach, which
bridged BeamNG to Blender through BeamNG's **replay system** + a sequence of
thousands of GLTF exports:

- **Jittery playback** — the replay-based bridge resampled the sim and the
  result shivered. Good enough to squint at, useless for finals.
- **Disk-eating GLTF floods** — it wrote *thousands* of `.gltf` files per
  crash. A single capture chewed through gigabytes of drive space.
- **Blender choked and died** — importing that mountain of files crashed
  Blender outright and, in the worst cases, ate through **32 GB of LPDDR5
  RAM** trying to hold the whole thing at once.

**Beam to Blend does none of that.** One binary file per capture, streamed
straight off disk, no RAM pileup, no GLTF flood, and playback that matches
the simulation frame-for-frame.

## What you get

- **True crash playback** — the exact deforming soft-body mesh, 60 fps
  capture, retimable live in Blender (slow-mo, start offset, output fps)
  without re-importing.
- **Tiny footprint** — a single `.bmc` → single `.bvc`, memory-mapped.
  Playback streams from disk, so even huge captures stay smooth without
  eating your RAM.
- **Materials & textures** resolved from the game's own vehicle files.
- **Debris & particles** — impact detection spawns rigid-body shards, fine
  particle debris and detached glass fragments at the actual impact frames.
- **Glass behaviour** — laminated windshields crack (hole + radiating web
  painted in the shader); tempered glass shatters into fragments.
- **Tyre ground contact** — contact-patch flattening, sidewall bulge and
  lift-off release, computed per frame from the cached geometry.
- **Smooth car stop** — a damped continuation of the car's residual swing
  after the capture ends, so the wreck settles instead of freezing on the
  last frame.
- **Alembic export** *(experimental, script-only)* — optional bake of the
  whole animation to `.abc` for other DCCs.

## Requirements

- **BeamNG.drive** — any recent version (only needed to *record* crashes)
- **Blender 4.0+**
- A **capture** — anyone can share `.bmc`/`.bvc` files; you don't need
  BeamNG installed to *import* an existing capture.

### Tested on

This add-on was developed and tested on an **Acer Swift Go 14** — Intel
**Core Ultra 7 155H** (16 cores), **32 GB LPDDR5 RAM**, Intel **Arc** iGPU —
running Blender 4.x and current BeamNG.drive. It runs fine on modest
hardware; heavy captures are I/O-bound, not RAM-bound, so the laptop stays
usable while Blender renders.

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

The BeamNG user dir is normally `%LOCALAPPDATA%\BeamNG\BeamNG.drive\current\`.
Restart BeamNG after copying.

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
2. **1 · Pick your capture** → point at the `.bmc` file itself (e.g.
   `...\current\captures\my_crash.bmc`). You can also copy the `.bmc` next
   to your .blend and point there.
3. **2 · Playback** → set **Playback Speed** and **Output FPS** however you
   like (details in the TUTORIAL; you can retune them after importing too).
4. Optional build settings (**Chunked Playback**, **Weld duplicate
   vertices**) if you want them.
5. **3 · Build Cache & Import** — one click. It writes `my_crash.bvc` next
   to your `.bmc` and imports the car: deforming meshes + a parent Empty
   driving the rigid motion.

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

## Debris, particles & glass

With the cache imported, use the **Debris** sub-panel:

- **Build Debris** — detects impacts in the cache and spawns rigid-body
  shards + fine particle debris at the impact frames.
- **Glass panes** crack or shatter depending on impact severity (tune the
  thresholds in the panel).
- **Prepare Fluid Effector** *(optional)* — for water/paint interactions:
  bakes the proxy mesh to `.mdd` + `MESH_CACHE` and moves the Mantaflow FLUID
  effector onto the proxy so the bake reads depsgraph data on its own thread
  without crashing.
- **Clear Debris** removes everything again.

## Rendering

- Plain **F12 / Ctrl+F12** renders (Cycles, Eevee, Workbench) are fully
  supported, including background renders (`blender -b file.blend -a`).
- **View > Viewport Render Animation** is a Blender limitation with Rendered
  shading + Cycles (it re-evaluates nothing per frame — this affects keyframed
  objects too). Use Material Preview or Solid shading if you need it, or just
  render normally.

## Command-line cache building

No Blender needed:

```bash
python tools/rebuild_cache.py --captures <captures-dir> --name mycrash
```

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| "Pick a .bmc capture file first" | Use **1 · Pick your capture** to point at the `.bmc` file |
| Animation vanished after reopening the .blend | Make sure the add-on is enabled in Preferences *before* opening; recovery is automatic |
| Car deforms but sits at the origin | Recovery lost the root Empty — re-run import once; with the add-on enabled this recovers automatically |
| Parts of the car have no texture | Enable **Include Base Game Textures** and set **Vehicle Folder** to the vehicle's root folder (falls back to auto-detected game files) |
| Debris fires at wrong times after changing speed | Re-built debris re-times automatically; very old builds need a rebuild |

## Notes on car compatibility

The importer works with **any** vehicle — the cache format is car-agnostic
(the captured `.bmc` is just vertex data in game world space; every car
imports automatically). The optional **chunked playback** mode ships with a
chunk map tuned for the development vehicle; other cars fall back to
per-object playback, which is only slightly slower. You can also pass your
own chunk map programmatically (`CachePlayback(reader, chunk_map=...)`).

### Development vehicle

The [captures](#capturing-a-crash) used to build and test this project were
recorded with a **Toyota Corolla E180** vehicle mod by **Flanje** in
BeamNG.drive — a beautifully detailed, bone-stock-quality model that deforms
and detaches convincingly, which made it perfect for testing the crash
pipeline:

- https://www.modland.net/beamng.drive-mods/cars/toyota-corolla-e180-2.html
- https://www.modland.net/beamng.drive-mods/cars/toyota-corolla-e180-6.html

Big thanks to **Flanje** for such a clean, high-quality asset — the E180 was
a joy to crash-test this add-on against. If you want to reproduce the exact
development captures/renders, open an issue and the vehicle + sample
captures can be shared.

## Project status & roadmap

This is a working, actively maintained project — but debris physics and
particle counts are still being polished. See [TODO.md](TODO.md) for the
roadmap and known limitations.

## Licensing & credits

This project is licensed under the **GPL-3.0** license — see
[LICENSE](LICENSE). Third-party additions (vehicle models, BeamNG.drive)
belong to their respective owners.

Built against the BeamNG.drive engine's own vehicle/part APIs; the capture
runs entirely in-game via the bundled `v5capture` Lua mod (no external
drivers, no modded pipeline dependencies).

### Credits

- **Vehicle mod** — [Toyota Corolla E180](#development-vehicle) by
  [**Flanje**](https://www.modland.net) — the development test vehicle, and
  a seriously good model.
