"""Third Alembic probe: is the retime real, and is the slow playback real?

Run with --factory-startup so no third-party add-on can pollute the timings:
    blender --background --factory-startup --python tests/blender_abc_bench3.py -- <abc> <n_exported> <export_fps>

bench2 exported 120 frames at 60 fps and got back a 0..48 range.  Two candidate
explanations, and they have opposite consequences:
  A) the .abc only has 49 samples  -> data was LOST, export is broken
  B) the .abc has all 120 samples at 1/60 s spacing, and a 24 fps scene simply
     resamples them -> nothing lost, but the clip plays 2.5x too fast
This imports the same file into a 24 fps scene and a 60 fps scene and compares
the resulting frame range; only (B) predicts the range scaling with scene fps.

Also re-times playback under --factory-startup to confirm the ms/frame figure.
"""

import os
import sys
import time

import bpy
import numpy as np


def _argv():
    return sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []


def info(msg):
    print(f"[B3] {msg}")
    sys.stdout.flush()


def load(abc, fps):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.context.scene.render.fps = fps
    bpy.ops.wm.alembic_import(filepath=abc, as_background_job=False)
    return bpy.context.scene


def main():
    args = _argv()
    abc = os.path.abspath(args[0])
    n_exported = int(args[1])
    export_fps = float(args[2])
    info(f"file={abc} ({os.path.getsize(abc)/1e6:.0f} MB), "
         f"exported {n_exported} frames at {export_fps} fps "
         f"= {n_exported/export_fps:.3f} s of animation")

    ranges = {}
    for fps in (24, 30, 60):
        sc = load(abc, fps)
        n = sc.frame_end - sc.frame_start + 1
        ranges[fps] = n
        info(f"imported into a {fps} fps scene -> range "
             f"{sc.frame_start}..{sc.frame_end} = {n} frames "
             f"= {n/fps:.3f} s")

    ok = abs(ranges[60] / max(ranges[24], 1) - 60 / 24) < 0.15
    info(f"range scales with scene fps: {ok}  "
         f"(60fps/24fps range ratio = {ranges[60]/max(ranges[24],1):.2f}, "
         f"expected 2.50)")
    info("=> " + ("CONFIRMED (B): all samples are in the file; the frame range "
                  "is just seconds*scene_fps. Set scene fps = the export fps "
                  "and the clip runs at the authored speed."
                  if ok else
                  "NOT explained by resampling — samples may be missing."))

    # ---- playback timing at the matching fps, clean Blender ----
    sc = load(abc, int(export_fps))
    fr = list(range(sc.frame_start, min(sc.frame_end, sc.frame_start + 60) + 1))
    for f in fr:                       # warm
        sc.frame_set(f)
        bpy.context.view_layer.update()
    t0 = time.perf_counter()
    for f in fr:
        sc.frame_set(f)
        bpy.context.view_layer.update()
    ms = (time.perf_counter() - t0) / len(fr) * 1000.0
    got = [o for o in bpy.data.objects if o.type == "MESH"]
    nv = sum(len(o.data.vertices) for o in got)
    info(f"factory-startup playback: {ms:.2f} ms/frame over {len(fr)} frames, "
         f"{len(got)} objects, {nv} verts -> {1000.0/max(ms,1e-9):.1f} fps ceiling")

    # ---- how much of that is per-object overhead vs vertex count? ----
    # Delete all but 10 objects and re-time: if cost scales with object count
    # the bottleneck is per-mesh rebuild, not raw vertex throughput.
    keep = sorted(got, key=lambda o: -len(o.data.vertices))[:10]
    for o in got:
        if o not in keep:
            bpy.data.objects.remove(o, do_unlink=True)
    kv = sum(len(o.data.vertices) for o in keep)
    for f in fr:
        sc.frame_set(f)
        bpy.context.view_layer.update()
    t0 = time.perf_counter()
    for f in fr:
        sc.frame_set(f)
        bpy.context.view_layer.update()
    ms10 = (time.perf_counter() - t0) / len(fr) * 1000.0
    info(f"same file, 10 heaviest objects only ({kv} verts, "
         f"{100.0*kv/max(nv,1):.0f}% of the verts): {ms10:.2f} ms/frame "
         f"-> {1000.0/max(ms10,1e-9):.1f} fps ceiling")
    info(f"cost per object ~{(ms-ms10)/max(len(got)-10,1):.2f} ms; "
         f"object count, not vertex count, dominates"
         if ms10 < ms * (kv / max(nv, 1)) * 2 else
         "cost tracks vertex count")
    return 0


if __name__ == "__main__":
    rc = main()
    sys.stdout.flush()
    sys.exit(rc)
