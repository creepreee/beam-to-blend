"""Diagnose View -> Viewport Render Animation (bpy.ops.render.opengl).

THE REPORT: during Viewport Render Animation the scene moves (native
animation: debris rigid bodies, particles) but the handler-driven car stays
frozen — while scrubbing works.

This script reproduces THAT exact operation — ``bpy.ops.render.opengl(
animation=True)`` from a real 3D viewport, windowed (OpenGL needs a GPU
context, so this cannot run with --background) — and logs EVERY candidate
callback with its thread, the playhead frame, and the live probe-vertex
position:

  frame_change_pre/post, render_init/pre/write/post/complete/cancel,
  depsgraph_update_post

so we can see which of them fire per frame on this path, on which thread,
and whether mesh data actually changes between frames.

Run WINDOWED (a Blender window opens briefly and closes itself):
    blender --python tests/diag_viewport_render.py
Output also goes to %TEMP%/opencode/vp_render_diag.log.
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

from bmc_fixtures import moving_car_sequence  # noqa: E402
from importer.cache_builder import CacheBuilder  # noqa: E402
from runtime.cache_reader import CacheReader  # noqa: E402
from runtime.mesh_update import CachePlayback  # noqa: E402
from runtime import frame_handler  # noqa: E402

N_FRAMES = 12
LOG_PATH = os.path.join(tempfile.gettempdir(), "opencode",
                        "vp_render_diag.log")

_log_lines = []


def log(msg):
    line = f"[VP] {msg}"
    print(line, flush=True)
    _log_lines.append(line)


def flush_log():
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "w", encoding="utf-8") as fh:
            fh.write("\n".join(_log_lines) + "\n")
    except OSError:
        pass


_state = {"entries": [], "stubs": [], "wrapped": [], "probe": None,
          "before": None}


def probe_pos():
    """First vertex of 'body' from the ORIGINAL mesh (what our writes touch)."""
    obj = bpy.data.objects.get("body")
    if obj is None:
        return None
    return tuple(round(float(v), 4) for v in obj.data.vertices[0].co)


def _img_pixels(path):
    img = bpy.data.images.load(path)
    try:
        buf = np.empty(img.size[0] * img.size[1] * img.channels,
                       dtype=np.float32)
        img.pixels.foreach_get(buf)
        return buf.reshape(-1, img.channels)[:, :3]
    finally:
        bpy.data.images.remove(img)


def _remove_all_instrumentation():
    for item in _state["wrapped"]:
        lst, fn = item
        if lst is None:
            continue
        try:
            lst.remove(fn)
        except ValueError:
            pass
    for lst, fn in _state["stubs"]:
        try:
            lst.remove(fn)
        except ValueError:
            pass
    orig_ap = _state.get("orig_apply_playhead")
    if orig_ap is not None:
        frame_handler._apply_playhead = orig_ap


def _finish():
    entries = _state["entries"]
    after = probe_pos()
    log(f"probe vertex after = {after}  "
        f"(moved={_state['before'] != after})")

    # Diff the SAVED opengl-rendered PNGs: did the IMAGE move, or only the
    # underlying mesh data?
    out_dir = _state.get("out_dir")
    if out_dir:
        saved = sorted(f for f in os.listdir(out_dir)
                       if f.lower().endswith((".png", ".jpg")))
        log(f"saved render files: {len(saved)} "
            f"({saved[:3]}{'...' if len(saved) > 3 else ''})")
        if len(saved) >= 2:
            first = _img_pixels(os.path.join(out_dir, saved[0]))
            last = _img_pixels(os.path.join(out_dir, saved[-1]))
            d = float(np.abs(first - last).mean())
            log(f"IMAGE first-vs-last mean diff = {d:.4e} "
                f"({'car MOVED in the render' if d > 1e-4 else 'FROZEN RENDER'})")
            log(f"brightness first={float(first.mean()):.4f} "
                f"last={float(last.mean()):.4f} "
                f"(~0 means BLANK renders - diff inconclusive)")

    kinds = {}
    for name, tname, frame, _p in entries:
        kinds.setdefault((name, tname), []).append(frame)
    for (name, tname), frames in sorted(kinds.items()):
        uniq = sorted({f for f in frames if f is not None})
        log(f"callback {name:22s} thread={tname:20s} "
            f"n={len(frames):3d} frames={uniq[:14]}"
            f"{'...' if len(uniq) > 14 else ''}")
    if not entries:
        log("NO PYTHON CALLBACKS FIRED AT ALL during the opengl render")

    _remove_all_instrumentation()
    try:
        frame_handler.detach_handler()
    except Exception:
        pass
    if _state["probe"] is not None:
        _state["probe"].close()
    flush_log()
    bpy.app.timers.register(lambda: (bpy.ops.wm.quit_blender(), None)[1],
                            first_interval=0.3)
    return None


def _watcher(scene):
    """Wait for the modal OGL animation to walk the playhead to the end."""
    if scene.frame_current >= scene.frame_end:
        if _state.get("grace") is None:
            _state["grace"] = 2          # let the last frame settle
            return 0.5
        if _state["grace"] > 0:
            _state["grace"] -= 1
            return 0.5
        _finish()
        return None
    if _state.get("waited", 0) > 120:    # hard cap ~120 s
        log("watcher timed out waiting for the animation")
        _finish()
        return None
    _state["waited"] = _state.get("waited", 0) + 1
    return 0.5


def _start_test():
    try:
        tmp = tempfile.mkdtemp(prefix="beamng_vp_diag_")
        bmc_path = os.path.join(tmp, "capture.bmc")
        cache_path = os.path.join(tmp, "diag.bvc")
        moving_car_sequence(bmc_path, n_frames=N_FRAMES)
        CacheBuilder(tmp, cache_path).build(bmc_path)

        # Factory startup KEEPS the default 3D viewport (required for the
        # opengl render operator); clear its default cube/lamp/camera.
        bpy.ops.wm.read_factory_settings()
        for obj in list(bpy.data.objects):
            bpy.data.objects.remove(obj, do_unlink=True)

        scene = bpy.context.scene
        keyframed_only = os.environ.get("VP_KEYFRAMED") == "1"
        if keyframed_only:
            # Native-animation control: a plain keyframed cube.  If THIS
            # also freezes in the ogl render, the whole Rendered-shading
            # ogl path ignores per-frame evaluation (Blender-level issue),
            # not something specific to handler-driven mesh writes.
            pos, idx = _box(0.0, 0.0, 0.5, 1.0)
            mesh = bpy.data.meshes.new("kf_cube_mesh")
            mesh.from_pydata(
                [tuple(float(c) for c in v) for v in pos], [],
                [tuple(int(i) for i in t) for t in idx])
            cube = bpy.data.objects.new("kf_cube", mesh)
            scene.collection.objects.link(cube)
            cube.location.z = 0.0
            cube.keyframe_insert("location", frame=1)
            cube.location.z = 2.0
            cube.keyframe_insert("location", frame=N_FRAMES)
            light_data = bpy.data.lights.new("kf_sun", type="SUN")
            light_data.energy = 3.0
            light = bpy.data.objects.new("kf_sun", light_data)
            light.location = (4, -4, 6)
            scene.collection.objects.link(light)
            scene.frame_end = N_FRAMES
            reader = None
        else:
            reader = CacheReader(cache_path)
            playback = CachePlayback(reader)
            playback.build_scene()
            frame_handler.attach(playback, frame_start=1,
                                 playback_fps=24.0, output_fps=24.0)
            _state["probe"] = reader

        shading = os.environ.get("VP_SHADING", "SOLID")
        engine = os.environ.get("VP_ENGINE", "BLENDER_WORKBENCH")
        scene.render.engine = engine
        win0 = next(iter(bpy.context.window_manager.windows), None)
        screen = win0.screen if win0 else None
        v3d = next((s for a in (screen.areas if screen else [])
                    if a.type == "VIEW_3D"
                    for s in a.spaces if s.type == "VIEW_3D"), None)
        if v3d is not None:
            v3d.shading.type = shading

        out_dir = os.path.join(tmp, "frames")
        os.makedirs(out_dir, exist_ok=True)
        scene.render.filepath = os.path.join(out_dir, "vp_")
        scene.render.image_settings.file_format = "PNG"
        _state["out_dir"] = out_dir

        log(f"VARIANT shading={shading} engine={engine} "
            f"transforms={os.environ.get('VP_TRANSFORMS', '0')} "
            f"keyframed_only={keyframed_only} | "
            f"cache {reader.frame_count if reader else '-'} frames, "
            f"timeline {scene.frame_start}..{scene.frame_end}")

        # ---- instrument EVERYTHING --------------------------------------
        def cb(name, fn):
            def wrapper(*args):
                sc = next((a for a in args
                           if hasattr(a, "frame_current")), None)
                frame = getattr(sc, "frame_current", None)
                _state["entries"].append(
                    (name, threading.current_thread().name, frame,
                     probe_pos()))
                return fn(*args)

            wrapper.__name__ = f"_vpdiag_{name}"
            return wrapper

        orig_fc = frame_handler._on_frame_change
        orig_rp = frame_handler._on_render_pre
        wrapped_fc = cb("frame_change_pre", orig_fc)
        wrapped_rp = cb("render_pre", orig_rp)

        # Evaluated-copy probe: after each applied frame, is our write
        # visible in the EVALUATED mesh (what Cycles draws)?
        if os.environ.get("VP_PROBE_EVAL") == "1":
            base_ap = frame_handler._apply_playhead
            _state["orig_apply_playhead"] = base_ap

            def ap_with_eval(scene):
                base_ap(scene)
                try:
                    obj = bpy.data.objects.get("body")
                    dg = bpy.context.evaluated_depsgraph_get()
                    ev = obj.evaluated_get(dg)
                    co_ev = tuple(round(float(c), 4)
                                  for c in ev.data.vertices[0].co)
                    co_orig = tuple(round(float(c), 4)
                                    for c in obj.data.vertices[0].co)
                    log(f"playhead@{scene.frame_current}: "
                        f"orig={co_orig} evaluated={co_ev} "
                        f"match={co_orig == co_ev}")
                except Exception as exc:
                    log(f"eval probe failed: {exc!r}")

            ap_with_eval.__name__ = "_vpdiag_ap_eval"
            frame_handler._apply_playhead = ap_with_eval
            _state["wrapped"].append((None, None))   # marker only

        for h in list(bpy.app.handlers.frame_change_pre):
            if getattr(h, "__name__", "") == "_on_frame_change":
                bpy.app.handlers.frame_change_pre.remove(h)
        for h in list(bpy.app.handlers.render_pre):
            if getattr(h, "__name__", "") == "_on_render_pre":
                bpy.app.handlers.render_pre.remove(h)
        bpy.app.handlers.frame_change_pre.append(wrapped_fc)
        bpy.app.handlers.render_pre.append(wrapped_rp)
        _state["wrapped"] = [
            (bpy.app.handlers.frame_change_pre, wrapped_fc),
            (bpy.app.handlers.render_pre, wrapped_rp),
        ]

        def mk_stub(name, lst):
            def stub(scene=None, depsgraph=None):
                _state["entries"].append(
                    (name, threading.current_thread().name,
                     getattr(scene, "frame_current", None), probe_pos()))
            stub.__name__ = f"_vpdiag_{name}"
            lst.append(stub)
            _state["stubs"].append((lst, stub))

        mk_stub("frame_change_post", bpy.app.handlers.frame_change_post)
        mk_stub("render_init", bpy.app.handlers.render_init)
        mk_stub("render_pre_extra", bpy.app.handlers.render_pre)
        mk_stub("render_write", bpy.app.handlers.render_write)
        mk_stub("render_post", bpy.app.handlers.render_post)
        mk_stub("render_complete", bpy.app.handlers.render_complete)
        mk_stub("render_cancel", bpy.app.handlers.render_cancel)
        mk_stub("depsgraph_update_post",
                bpy.app.handlers.depsgraph_update_post)

        # ---- run View -> Viewport Render Animation -----------------------
        scene.frame_set(scene.frame_start)
        _state["before"] = probe_pos()
        log(f"parked playhead; probe vertex before = {_state['before']}")

        win, area, region = None, None, None
        for w in bpy.context.window_manager.windows:
            for a in w.screen.areas:
                if a.type == "VIEW_3D":
                    win, area = w, a
                    for r in a.regions:
                        if r.type == "WINDOW":
                            region = r
                    break
            if area:
                break
        if area is None or region is None:
            log("no VIEW_3D found — cannot run the opengl render")
            _finish()
            return

        err = None
        try:
            with bpy.context.temp_override(window=win, area=area,
                                           region=region):
                result = bpy.ops.render.opengl(animation=True)
            log(f"opengl invoke returned {result} (modal ops return "
                f"'RUNNING_MODAL' and finish via timer)")
        except Exception as exc:   # pragma: no cover
            err = repr(exc)
            log(f"opengl render raised: {err}")
            _finish()
            return

        bpy.app.timers.register(lambda: _watcher(scene), first_interval=1.0)
    except Exception as exc:   # pragma: no cover
        import traceback
        traceback.print_exc()
        log(f"test setup failed: {exc!r}")
        flush_log()
        bpy.app.timers.register(lambda: (bpy.ops.wm.quit_blender(), None)[1],
                                first_interval=0.3)


if __name__ == "__main__":
    # Defer so the window/GPU context is fully up before touching GL.
    bpy.app.timers.register(_start_test, first_interval=1.5)
