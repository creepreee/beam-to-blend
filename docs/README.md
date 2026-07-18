# BeamNG Cache Importer

A Blender add-on for importing **BeamNG crash sequences** that avoids the RAM blow-up
of the naive "one GLB per frame → one mesh datablock per frame" workflow.

BeamNG exports a crash as a folder of `.glb` files, one per simulation frame (2364
frames, 97 objects, 65 GB for the test sequence). Importing each frame as a separate
mesh creates hundreds of thousands of duplicate datablocks and crashes Blender.

The key insight: across all frames, almost every object keeps **identical topology**
(vertex/edge/face counts + connectivity + vertex order). Only vertex positions move.
So we store the mesh **once** and cache only per-frame positions — the same idea as
an Alembic / geometry cache in VFX.

## Quick start

1. **Install the add-on**

   Open Blender → Edit → Preferences → Add-ons → Install...
   Pick `dist/beamng_cache_importer.zip`.

   Enable "Import-Export: BeamNG Cache Importer". A "BeamNG" tab appears in the
   File Browser sidebar (N).

2. **Import a crash**

   In the File Browser, navigate to your GLB frame folder. Three buttons:

   | Step | Button | What it does |
   |------|--------|-------------|
   | 1 | **Scan Sequence** | Reads frame headers, classifies each object as stable (cacheable) or dynamic (topology-changing). Takes seconds. |
   | 2 | **Build Cache** | Writes a `.bvc` binary vertex cache — base mesh once per stable object + per-frame position stream. Memory-light: reads one GLB at a time. |
   | 3 | **Import Cache** | Creates one mesh per stable object, wires the timeline to update vertex positions in-place via Blender's fast `position` attribute API. No per-frame datablocks. Peak viewport performance: **30 fps** in solid mode (97 objects, 550K verts). |

   Scrub the timeline — meshes move frame-to-frame from the cache.

## Pipeline

```
GLB frames  →  Scanner/classifier  →  Manifest  →  Cache builder
            →  Binary vertex cache  →  Blender runtime  →  Playback / render
```

All modules live in `importer/` (pure Python + numpy, no Blender dependency) and
`runtime/` (Blender-side, lazy bpy import). The add-on in `addon/` wires them
together.

## Status — everything real and tested

| Module | Status |
|---|---|
| **GLB reader** (`importer/gltf_reader.py`) | Reads BeamNG's shared-vertex-pool GLBs correctly. Verified on real data: 97 objects, 550K verts/frame. |
| **Scanner** (`importer/scanner.py`) | Classifies stable vs dynamic via topology-hash drift. 96 stable, 1 dynamic (`tierod_F`). |
| **Binary cache format** (`importer/binary.py`) | BVC1: header + object table + base mesh blocks + seekable frame directory + per-frame position blocks. |
| **Cache builder** (`importer/cache_builder.py`) | Streams frames one at a time. Verified on real data: 0 position/index mismatches, 39.9 MB cache vs 145 MB source (3.6×, uncompressed). |
| **Cache reader** (`runtime/cache_reader.py`) | Memmap-based, seeks to any frame/object without loading the rest. |
| **Mesh updater** (`runtime/mesh_update.py`) | Creates each mesh once via `from_pydata`, updates verts per frame via the fast `position` attribute API (`_write_positions()`; ~120× faster than legacy `foreach_set("co")`). **30 fps** in solid viewport. |
| **Frame handler** (`runtime/frame_handler.py`) | Wires timeline → cache frame via `frame_change_pre` handler. |
| **Add-on** (`addon/`) | 3 operators (scan / build / import) in the File Browser sidebar. |
| **Headless smoke test** | Passed: 96 datablocks, positions match cache, timeline scrub works. |
| **Add-on install test** | Passed end-to-end: scan → build → import → scrub in packaged add-on. |

## Verified against real data

- Test sequence: **2364 frames, 97 objects**
- **96 stable** objects (one base mesh + per-frame position cache)
- **1 dynamic** object: `flanje_e180_tierod_F` (vertex count 115 ↔ 156, changes at frame 168)
- Index buffers **byte-identical** across frames — topology provably stable
- Positions move (~0.19 max displacement) — real animation to cache
- **6.6 MB/frame** → ~15.6 GB uncompressed for the full sequence (from 65 GB of GLBs)
- Highly compressible (smooth float motion)

## Ground truth — BeamNG GLB structure

The original docs' "97 independent meshes" assumption was wrong about the file layout:

- BeamNG exports **one shared POSITION accessor** (533,885 verts) that **88 of 97
  objects** index into by material-split primitives. 9 objects have small private
  pools. Only **10 distinct POSITION accessors** total.
- The 88 objects **partition the shared pool disjointly** (no shared vertices).
- True unique vertices/frame ≈ 550,476.

The reader handles this correctly via `np.unique(return_inverse)` with local index
remap.

## How to run

- **Unit tests (no Blender):** `python -m pytest -q` (15 tests)
- **Blender smoke test:**
  `blender.exe --background --python tests/blender_smoke.py`
- **Build the add-on zip:** `python build_addon.py` → `dist/beamng_cache_importer.zip`

The `importer/` layer is plain Python — exercise it directly without Blender.

## Project layout

```
beamng_cache_importer/
├── addon/             Blender add-on (__init__, ui, operators)
├── build_addon.py     Assembles dist/beamng_cache_importer.zip
├── dist/              Built add-on zip
├── docs/              Design notes and research
├── examples/          Sample manifests
├── importer/          Core library (bpy-free)
│   ├── gltf_reader.py
│   ├── topology.py
│   ├── scanner.py
│   ├── binary.py
│   └── cache_builder.py
├── runtime/           Blender runtime
│   ├── cache_reader.py
│   ├── mesh_update.py
│   └── frame_handler.py
└── tests/             Pytest suite + Blender smoke tests
```

## Known issues

- `read_factory_settings` in Blender resets enabled add-ons — enable the add-on
  **after** any factory reset.
- Compression is future work — the cache is currently uncompressed float32.
- The dynamic object fallback (`tierod_F`) is not yet implemented — tracked in ROADMAP.
