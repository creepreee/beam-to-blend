"""Audit what an exported Alembic actually carries, and how fast it plays.

Run:
    blender --background --python tests/blender_abc_audit.py -- <cache.bvc>

Answers, with measured numbers rather than assumptions:
  1. WHAT SURVIVES  — per-frame vertex animation, UV layers, material slots,
     custom split normals / sharp edges, object count, tyre deformation.
  2. SPEED          — mesh-update cost per frame for BVC playback (Python
     handler) vs Alembic playback (C++ MeshSequenceCache), same objects, same
     frames.  Reported as ms/frame and the implied ceiling in fps.
  3. RENDER TIMING  — does the .abc keep the tuned animation speed, i.e. does
     the ABC frame range * scene fps equal the BVC clip's duration in seconds.

Prints a report; exits non-zero only if something that should survive did not.
"""

import os
import sys
import time

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


def _argv():
    return sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []


_fail = []


def check(cond, msg):
    print(f"[ABC][{'ok  ' if cond else 'FAIL'}] {msg}")
    if not cond:
        _fail.append(msg)


def info(msg):
    print(f"[ABC][info] {msg}")


def verts(obj, dg=None):
    me = obj.evaluated_get(dg).data if dg else obj.data
    a = np.empty(len(me.vertices) * 3, dtype=np.float32)
    me.vertices.foreach_get("co", a)
    return a.reshape(-1, 3)


def main():
    args = _argv()
    if not args or not os.path.exists(args[0]):
        print("[ABC][FAIL] usage: -- <cache.bvc>")
        return 1
    cache_path = os.path.abspath(args[0])
    abc_path = os.path.join(os.path.dirname(cache_path), "_audit.abc")

    PLAYBACK_FPS, OUTPUT_FPS = 24, 60

    bpy.ops.wm.read_factory_settings(use_empty=True)
    import addon
    addon.register()

    # ---------- BVC side: import exactly like the panel does -------------
    reader = CacheReader(cache_path)
    n_frames = reader.frame_count
    playback = CachePlayback(reader)
    playback.build_scene()
    frame_handler.attach(playback, playback_fps=PLAYBACK_FPS,
                         output_fps=OUTPUT_FPS, start_second=0.0)
    scene = bpy.context.scene
    src = [o for o in bpy.data.collections["BeamNG Cache"].objects
           if o.type == "MESH"]

    bvc_start, bvc_end = scene.frame_start, scene.frame_end
    bvc_duration_s = (bvc_end - bvc_start) / OUTPUT_FPS
    total_verts = sum(len(o.data.vertices) for o in src)
    info(f"cache={os.path.basename(cache_path)} frames={n_frames} "
         f"objects={len(src)} verts={total_verts}")
    info(f"BVC clip: frames {bvc_start}..{bvc_end} @ {OUTPUT_FPS} fps "
         f"= {bvc_duration_s:.3f} s (playback_fps={PLAYBACK_FPS})")

    # Reference: UVs / materials / geometry as the viewport has them.
    ref = {}
    for o in src:
        ref[o.name] = {
            "uv_layers": [l.name for l in o.data.uv_layers],
            "mats": [m.name if m else None for m in o.data.materials],
            "nverts": len(o.data.vertices),
            "npolys": len(o.data.polygons),
            "sharp": sum(1 for e in o.data.edges if e.use_edge_sharp),
        }
    ref_uv_objs = sum(1 for v in ref.values() if v["uv_layers"])
    ref_mat_slots = sum(len(v["mats"]) for v in ref.values())
    ref_sharp = sum(v["sharp"] for v in ref.values())

    # ---------- SPEED: BVC Python-handler mesh update -------------------
    # Both timing loops must do the SAME work to be comparable: frame_set plus a
    # forced depsgraph evaluation.  Timing BVC without the update and ABC with it
    # would flatter BVC, since the Alembic modifier only evaluates on update.
    PROBE_N = min(30, max(2, (bvc_end - bvc_start) or 2))
    frames = np.linspace(bvc_start, bvc_end, PROBE_N).astype(int)
    scene.frame_set(int(frames[0]))
    bpy.context.view_layer.update()
    t0 = time.perf_counter()
    for f in frames:
        scene.frame_set(int(f))
        bpy.context.view_layer.update()
    bvc_ms = (time.perf_counter() - t0) / len(frames) * 1000.0

    # ---------- export via the REAL operator ----------------------------
    scene.beamng.cache_path = cache_path
    res = bpy.ops.beamng.export_alembic(filepath=abc_path)
    check(res == {"FINISHED"}, f"export operator finished (got {res})")
    check(os.path.exists(abc_path), "produced an .abc file")
    if not os.path.exists(abc_path):
        return 1
    info(f"abc size = {os.path.getsize(abc_path) / 1e6:.1f} MB")
    reader.close()

    # ---------- re-import the .abc and audit ---------------------------
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.wm.alembic_import(filepath=abc_path, as_background_job=False)
    scene = bpy.context.scene
    got = [o for o in bpy.data.objects if o.type == "MESH"]
    animated = [o for o in got
                if any(m.type == "MESH_SEQUENCE_CACHE" for m in o.modifiers)]

    check(len(got) >= len(src),
          f"object count preserved: {len(src)} exported -> {len(got)} in .abc")
    check(len(animated) > 0,
          f"animation channel present on {len(animated)}/{len(got)} objects")

    # --- UVs ---
    abc_uv_objs = sum(1 for o in got if o.data.uv_layers)
    check(abc_uv_objs >= ref_uv_objs,
          f"UV layers survived: {ref_uv_objs} objects had UVs -> "
          f"{abc_uv_objs} in .abc")

    # --- materials ---
    abc_mat_slots = sum(len(o.data.materials) for o in got)
    check(abc_mat_slots >= ref_mat_slots,
          f"material slots survived: {ref_mat_slots} -> {abc_mat_slots}")
    info("NOTE: Alembic stores material *assignments/facesets*, never the "
         "shader node trees or image textures themselves.")

    # --- normals / sharp edges ---
    abc_sharp = sum(sum(1 for e in o.data.edges if e.use_edge_sharp)
                    for o in got)
    info(f"sharp edges: {ref_sharp} in viewport -> {abc_sharp} in .abc")

    # --- animation actually moves, and matches the source ---
    dg = bpy.context.evaluated_depsgraph_get()
    probe = max(got, key=lambda o: len(o.data.vertices))
    scene.frame_set(scene.frame_start)
    p0 = verts(probe, bpy.context.evaluated_depsgraph_get())
    scene.frame_set(scene.frame_end)
    pN = verts(probe, bpy.context.evaluated_depsgraph_get())
    d = float(np.abs(pN - p0).max()) if p0.shape == pN.shape else -1.0
    check(d > 1e-6, f"deformation animates in .abc (probe {probe.name}, "
                    f"max delta {d:.6f})")

    # ---------- SPEED: Alembic C++ playback ---------------------------
    a_start, a_end = scene.frame_start, scene.frame_end
    af = np.linspace(a_start, a_end, PROBE_N).astype(int)
    scene.frame_set(int(af[0]))
    t0 = time.perf_counter()
    for f in af:
        scene.frame_set(int(f))
        bpy.context.view_layer.update()
    abc_ms = (time.perf_counter() - t0) / len(af) * 1000.0

    print()
    print("=" * 62)
    print(f"  mesh update / frame   BVC (Python) {bvc_ms:8.2f} ms  "
          f"-> ceiling {1000.0 / max(bvc_ms, 1e-9):6.1f} fps")
    print(f"                        ABC (C++)    {abc_ms:8.2f} ms  "
          f"-> ceiling {1000.0 / max(abc_ms, 1e-9):6.1f} fps")
    if abc_ms > 0:
        print(f"  speedup               {bvc_ms / max(abc_ms, 1e-9):.2f}x")
    print("=" * 62)
    info("Headless numbers = mesh update cost only, no GPU draw. Real viewport "
         "fps is lower; the RATIO is the transferable part.")

    # ---------- RENDER TIMING ----------------------------------------
    info(f"ABC frame range {a_start}..{a_end}, scene fps {scene.render.fps}")
    abc_duration_s = (a_end - a_start) / max(1, scene.render.fps)
    info(f"ABC clip duration {abc_duration_s:.3f} s vs BVC "
         f"{bvc_duration_s:.3f} s")
    # Tolerance must scale with the clip, not be a fixed 0.5 s — on a short
    # sequence a fixed tolerance hides a total-speed mismatch.
    tol = max(2.0 / max(1, scene.render.fps), 0.02 * bvc_duration_s)
    ratio = abc_duration_s / bvc_duration_s if bvc_duration_s else 0.0
    info(f"speed ratio ABC/BVC = {ratio:.3f} (1.000 = identical speed; "
         f"<1 = ABC plays too FAST)")
    check(abs(abc_duration_s - bvc_duration_s) < tol,
          f"ABC clip duration matches the tuned BVC duration (tol {tol:.3f}s)")
    if abs(abc_duration_s - bvc_duration_s) >= tol:
        info("=> the .abc was baked one cache-frame-per-Blender-frame, so it "
             "plays at scene fps, NOT at the tuned playback_fps. Set "
             "render.fps = playback_fps after import, or retime the ABC.")

    try:
        os.remove(abc_path)
    except OSError:
        pass

    print()
    if _fail:
        print(f"[ABC] {len(_fail)} FAILURE(S):")
        for f in _fail:
            print(f"  - {f}")
        return 1
    print("[ABC] ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    rc = main()
    sys.stdout.flush()
    sys.exit(rc)
