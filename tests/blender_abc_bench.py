"""Steady-state Alembic vs BVC benchmark on a real capture.

``blender_abc_audit.py`` answers *what survives*; it times only a couple of
frames, so its speed figure is dominated by the Alembic cold open.  This script
answers *how fast it actually plays back* once warm, and *how big the file
gets*, using a bounded frame range so a 1200-frame cache does not have to be
written in full.

Run:
    blender --background --python tests/blender_abc_bench.py -- <cache.bvc> [n_frames] [outdir]

Measures, on the same objects and the same frames:
  * ABC export wall time and bytes/frame (-> extrapolated full-clip size)
  * ABC playback ms/frame, warmed up, scrubbing sequentially like the timeline
  * BVC playback ms/frame under identical conditions
  * ABC size with normals on vs off (per-frame normals are the size driver)
"""

import os
import sys
import time
import tempfile
import shutil

import bpy

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)
for _mod in [m for m in sys.modules
             if m == "runtime" or m.startswith("runtime.")
             or m == "importer" or m.startswith("importer.")]:
    del sys.modules[_mod]

import numpy as np

from runtime.cache_reader import CacheReader
from runtime.mesh_update import CachePlayback
from runtime import frame_handler
from runtime.baker import bake_to_mdd, apply_mdd_modifiers


def _argv():
    return sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []


def info(msg):
    print(f"[BENCH] {msg}")
    sys.stdout.flush()


def time_scrub(scene, frames, warmup=5):
    """Sequential scrub with warmup; returns ms/frame, excluding warmup."""
    for f in frames[:warmup]:
        scene.frame_set(int(f))
        bpy.context.view_layer.update()
    t0 = time.perf_counter()
    for f in frames:
        scene.frame_set(int(f))
        bpy.context.view_layer.update()
    return (time.perf_counter() - t0) / len(frames) * 1000.0


def main():
    args = _argv()
    if not args or not os.path.exists(args[0]):
        print("[BENCH] usage: -- <cache.bvc> [n_frames] [outdir]")
        return 1
    cache_path = os.path.abspath(args[0])
    n_bench = int(args[1]) if len(args) > 1 else 120
    outdir = args[2] if len(args) > 2 else tempfile.gettempdir()

    bpy.ops.wm.read_factory_settings(use_empty=True)

    reader = CacheReader(cache_path)
    total_frames = reader.frame_count
    n_bench = min(n_bench, total_frames)
    playback = CachePlayback(reader)
    playback.build_scene()
    frame_handler.attach(playback, playback_fps=24, output_fps=60,
                         start_second=0.0)
    scene = bpy.context.scene
    src = [o for o in bpy.data.collections["BeamNG Cache"].objects
           if o.type == "MESH"]
    total_verts = sum(len(o.data.vertices) for o in src)
    info(f"cache={os.path.basename(cache_path)} total_frames={total_frames} "
         f"objects={len(src)} verts={total_verts}")
    info(f"benchmarking {n_bench} frames")

    # ---------- BVC steady-state playback ----------
    bvc_frames = [scene.frame_start + i for i in range(n_bench)]
    bvc_ms = time_scrub(scene, bvc_frames)
    info(f"BVC  playback: {bvc_ms:.2f} ms/frame "
         f"-> {1000.0 / max(bvc_ms, 1e-9):.1f} fps ceiling")

    # ---------- bake + export a bounded range ----------
    frame_handler.detach()
    mdd_dir = tempfile.mkdtemp(prefix="bench_mdd_")
    t0 = time.perf_counter()
    names = bake_to_mdd(reader, mdd_dir, frame_start=0, frame_end=n_bench - 1)
    bake_s = time.perf_counter() - t0
    mdd_bytes = sum(os.path.getsize(os.path.join(mdd_dir, f))
                    for f in os.listdir(mdd_dir))
    info(f"bake: {bake_s:.1f} s for {len(names)} objects, "
         f"{mdd_bytes / 1e6:.0f} MB of .mdd")

    apply_mdd_modifiers(bpy, names, mdd_dir + os.sep, scene.frame_start)
    for o in bpy.data.objects:
        o.select_set(False)
    for n in names:
        o = bpy.data.objects.get(n)
        if o and o.type == "MESH":
            o.select_set(True)

    results = {}
    for label, use_normals in (("normals=True", True), ("normals=False", False)):
        abc_path = os.path.join(outdir, f"_bench_{int(use_normals)}.abc")
        t0 = time.perf_counter()
        bpy.ops.wm.alembic_export(
            filepath=abc_path,
            start=scene.frame_start, end=scene.frame_start + n_bench - 1,
            selected=True, flatten=False, face_sets=True,
            uvs=True, packuv=True, normals=use_normals,
        )
        export_s = time.perf_counter() - t0
        size = os.path.getsize(abc_path)
        results[label] = (abc_path, size, export_s)
        info(f"export {label}: {export_s:.1f} s, {size / 1e6:.0f} MB "
             f"({size / n_bench / 1e6:.2f} MB/frame) "
             f"-> full {total_frames}f would be "
             f"{size / n_bench * total_frames / 1e9:.1f} GB")

    for n in names:
        o = bpy.data.objects.get(n)
        if o:
            for m in list(o.modifiers):
                if m.type == "MESH_CACHE":
                    o.modifiers.remove(m)
    shutil.rmtree(mdd_dir, ignore_errors=True)
    reader.close()

    # ---------- ABC steady-state playback ----------
    abc_path = results["normals=True"][0]
    bpy.ops.wm.read_factory_settings(use_empty=True)
    t0 = time.perf_counter()
    bpy.ops.wm.alembic_import(filepath=abc_path, as_background_job=False)
    info(f"abc import (cold open): {time.perf_counter() - t0:.1f} s")
    scene = bpy.context.scene
    got = [o for o in bpy.data.objects if o.type == "MESH"]
    animated = [o for o in got
                if any(m.type == "MESH_SEQUENCE_CACHE" for m in o.modifiers)]
    info(f"abc: {len(got)} objects, {len(animated)} with a cache modifier, "
         f"frames {scene.frame_start}..{scene.frame_end}, fps {scene.render.fps}")

    abc_frames = [scene.frame_start + i
                  for i in range(min(n_bench, scene.frame_end - scene.frame_start + 1))]
    abc_ms = time_scrub(scene, abc_frames)
    info(f"ABC  playback: {abc_ms:.2f} ms/frame "
         f"-> {1000.0 / max(abc_ms, 1e-9):.1f} fps ceiling")

    print()
    print("=" * 66)
    print(f"  BVC (Python handler) {bvc_ms:8.2f} ms/frame  "
          f"{1000.0 / max(bvc_ms, 1e-9):6.1f} fps ceiling")
    print(f"  ABC (C++ seq cache)  {abc_ms:8.2f} ms/frame  "
          f"{1000.0 / max(abc_ms, 1e-9):6.1f} fps ceiling")
    print(f"  ABC / BVC speed      {bvc_ms / max(abc_ms, 1e-9):.2f}x")
    print("=" * 66)
    info("headless = mesh update only, no GPU draw; the RATIO transfers, the "
         "absolute fps does not")

    for path, _, _ in results.values():
        try:
            os.remove(path)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    rc = main()
    sys.stdout.flush()
    sys.exit(rc)
