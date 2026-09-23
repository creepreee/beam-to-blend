"""Diagnose: which thread do render-driven playback updates run on, and do
mesh writes survive there?

The GUI renders as a WM JOB on a background thread; background-mode renders
run on the main thread.  This script performs BOTH from one headless session
by invoking ``bpy.ops.render.render`` normally (main thread) and from a
Python ``threading.Thread`` (job-thread stand-in), logging for every
playback update: thread name, main-thread flag, applied cache frame.

The sequence exercises BOTH motion paths:
  * body  — rigid motion via glTF node translation -> BVC transform block ->
            per-frame ``matrix_basis`` write on the ``__root`` Empty;
  * wheels — vertex deformation via ``foreach_set``.

Run with:
    blender --background --python tests/diag_render_thread.py
"""

import os
import shutil
import sys
import tempfile
import threading

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TESTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _REPO)
for _mod in [m for m in sys.modules
             if m == "runtime" or m.startswith("runtime.")
             or m == "importer" or m.startswith("importer.")]:
    del sys.modules[_mod]
if _TESTS not in sys.path:
    sys.path.insert(0, _TESTS)

import bpy  # noqa: E402
import numpy as np  # noqa: E402
from mathutils import Vector  # noqa: E402

from bmc_fixtures import moving_car_sequence  # noqa: E402
from importer.cache_builder import CacheBuilder  # noqa: E402
from runtime.cache_reader import CacheReader  # noqa: E402
from runtime.mesh_update import CachePlayback  # noqa: E402
from runtime import frame_handler  # noqa: E402

N_FRAMES = 12


def image_pixels(path):
    img = bpy.data.images.load(path)
    try:
        buf = np.empty(img.size[0] * img.size[1] * img.channels,
                       dtype=np.float32)
        img.pixels.foreach_get(buf)
        return buf.reshape(-1, img.channels)[:, :3]
    finally:
        bpy.data.images.remove(img)


def main():
    tmp = tempfile.mkdtemp(prefix="beamng_diag_thread_")
    results = {}
    try:
        bmc_path = os.path.join(tmp, "capture.bmc")
        cache_path = os.path.join(tmp, "diag.bvc")
        out_dir = os.path.join(tmp, "frames")
        os.makedirs(out_dir, exist_ok=True)

        moving_car_sequence(bmc_path, n_frames=N_FRAMES)
        CacheBuilder(tmp, cache_path).build(bmc_path)

        bpy.ops.wm.read_factory_settings(use_empty=True)
        reader = CacheReader(cache_path)
        print(f"[DIAG] transform block present: "
              f"{bool(reader.header.get('transform_data_offset', 0))}")
        playback = CachePlayback(reader)
        playback.build_scene()
        frame_handler.attach(playback, frame_start=1,
                             playback_fps=24.0, output_fps=24.0)
        scene = bpy.context.scene
        print(f"[DIAG] root empty bound: {playback._transform_empty is not None}")

        # Minimal render env.
        scene.render.engine = "CYCLES"
        scene.cycles.device = "CPU"
        scene.cycles.samples = 4
        scene.cycles.use_adaptive_sampling = False
        scene.cycles.use_denoising = False
        scene.render.resolution_x = 128
        scene.render.resolution_y = 96
        scene.view_settings.view_transform = "Standard"
        world = bpy.data.worlds.new("World")
        world.use_nodes = True
        world.node_tree.nodes["Background"].inputs[0].default_value = \
            (0.7, 0.7, 0.7, 1.0)
        scene.world = world
        cam_data = bpy.data.cameras.new("Cam")
        cam = bpy.data.objects.new("Cam", cam_data)
        scene.collection.objects.link(cam)
        cam.location = Vector((1.2, -3.4, 1.4))
        d = Vector((0.0, 0.0, 0.5)) - cam.location
        cam.rotation_euler = d.to_track_quat("-Z", "Y").to_euler()
        scene.camera = cam

        log = []
        orig = playback.set_frame

        def spy(pos):
            t = threading.current_thread()
            log.append((t.name, t is threading.main_thread(), float(pos)))
            orig(pos)

        playback.set_frame = spy

        # Log RAW handler invocations (before any thread guard inside them)
        # so we can tell "handler never fired" apart from "fired but skipped".
        # Blender holds direct references to the registered functions, so we
        # swap the REGISTERED objects for wrappers carrying the same names.
        entries = []
        orig_fc = frame_handler._on_frame_change
        orig_rp = frame_handler._on_render_pre

        def spy_fc(scene, depsgraph=None):
            t = threading.current_thread()
            entries.append(("frame_change", t.name, t is threading.main_thread(),
                            int(scene.frame_current)))
            return orig_fc(scene, depsgraph)

        def spy_rp(scene=None, depsgraph=None):
            t = threading.current_thread()
            entries.append(("render_pre", t.name, t is threading.main_thread(),
                            None if scene is None else int(scene.frame_current)))
            return orig_rp(scene, depsgraph)

        spy_fc.__name__ = "_on_frame_change"
        spy_rp.__name__ = "_on_render_pre"
        for h in list(bpy.app.handlers.frame_change_pre):
            if getattr(h, "__name__", "") == "_on_frame_change":
                bpy.app.handlers.frame_change_pre.remove(h)
        for h in list(bpy.app.handlers.render_pre):
            if getattr(h, "__name__", "") == "_on_render_pre":
                bpy.app.handlers.render_pre.remove(h)
        bpy.app.handlers.frame_change_pre.append(spy_fc)
        bpy.app.handlers.render_pre.append(spy_rp)

        def render_anim(tag):
            scene.render.filepath = os.path.join(out_dir, tag + "_")
            bpy.ops.render.render(animation=True)

        def png(tag, frame):
            return image_pixels(
                os.path.join(out_dir, f"{tag}_{frame:04d}.png"))

        def report(tag):
            mains = [c for c in log if c[1]]
            others = [c for c in log if not c[1]]
            print(f"[DIAG] {tag}: {len(log)} updates "
                  f"(main={len(mains)} other={len(others)})")
            for name, is_main, pos in log[:40]:
                print(f"[DIAG]   thread={name!r} main={is_main} pos={pos}")
            if len(log) > 40:
                print(f"[DIAG]   ... {len(log) - 40} more")
            kinds = {}
            for kind, tname, is_main, _f in entries:
                key = (kind, tname, is_main)
                kinds[key] = kinds.get(key, 0) + 1
            print(f"[DIAG] {tag}: raw handler invocations: {kinds}")
            entries.clear()

        # --- render 1: MAIN thread (background default) ------------------
        log.clear()
        err = None
        try:
            render_anim("main")
        except Exception as exc:      # pragma: no cover
            err = exc
            print(f"[DIAG] main-thread render raised: {exc!r}")
        report("MAIN-THREAD RENDER")
        n_main_calls = len(log)
        d_main = None
        try:
            d_main = image_mean_diff(png("main", 1), png("main", N_FRAMES))
            print(f"[DIAG] main render first-vs-last mean diff: {d_main:.3e}")
        except Exception as exc:
            print(f"[DIAG] main render images unavailable: {exc!r}")
        results["main"] = (n_main_calls, d_main)

        # --- render 2: WORKER thread (GUI job-thread stand-in) -----------
        log.clear()
        box = {}

        def worker():
            try:
                render_anim("worker")
                box["ok"] = True
            except BaseException as exc:   # noqa: BLE001 - diagnostics
                box["err"] = repr(exc)

        th = threading.Thread(target=worker, name="render-job-standin")
        th.start()
        th.join(timeout=300)
        print(f"[DIAG] worker thread finished={not th.is_alive()} "
              f"ok={box.get('ok')} err={box.get('err')}")
        report("WORKER-THREAD RENDER")
        n_worker_calls = len(log)
        d_worker = None
        try:
            d_worker = image_mean_diff(png("worker", 1),
                                       png("worker", N_FRAMES))
            print(f"[DIAG] worker render first-vs-last mean diff: "
                  f"{d_worker:.3e}")
        except Exception as exc:
            print(f"[DIAG] worker render images unavailable: {exc!r}")
        results["worker"] = (n_worker_calls, d_worker)

        # Root empty pose after everything.
        root = bpy.data.objects.get("BeamNG Cache__root")
        if root is not None:
            loc = tuple(round(v, 4) for v in root.matrix_basis.to_translation())
            print(f"[DIAG] root empty final translation: {loc}")

        frame_handler.detach_handler()
        reader.close()
        print("[DIAG] summary:", results)
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def image_mean_diff(a, b):
    return float(np.abs(a - b).mean())


if __name__ == "__main__":
    sys.exit(main())
