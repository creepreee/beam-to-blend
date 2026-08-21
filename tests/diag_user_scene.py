"""RUN THIS INSIDE YOUR OPEN BLENDER, WITH YOUR CRASH SCENE LOADED.

Text Editor -> Open -> select this file -> Run Script (Alt+P? no: the
Run Script button). It will:

  1. dump the beamng handler / scene state,
  2. wrap every animation callback with a logger,
  3. run View->Viewport-Render-Animation over ~7 frames around the
     current playhead, saving PNGs,
  4. report whether the CAR moved in the saved images,
  5. restore your frame range and remove its instrumentation.

Results are written to  %TEMP%\\opencode\\user_vp_diag.log  — paste that
file's contents back. Nothing in your scene is modified permanently.
"""

import os
import tempfile
import threading

import bpy
import numpy as np

LOG_PATH = os.path.join(tempfile.gettempdir(), "opencode",
                        "user_vp_diag.log")
OUT_DIR = os.path.join(tempfile.gettempdir(), "opencode",
                       "user_vp_diag_frames")
N_TEST_FRAMES = 7

_lines = []


def log(msg):
    line = f"[USERDIAG] {msg}"
    print(line, flush=True)
    _lines.append(line)


def flush():
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "w", encoding="utf-8") as fh:
            fh.write("\n".join(_lines) + "\n")
    except OSError as exc:
        print(f"[USERDIAG] could not write log: {exc}")


# ---------------------------------------------------------------- state dump
scene = bpy.context.scene
log(f"Blender {bpy.app.version_string}, scene '{scene.name}', "
    f"engine {scene.render.engine}")
log(f"frame range {scene.frame_start}..{scene.frame_end}, "
    f"playhead {scene.frame_current}")
for prop in ("_beamng_playback_fps", "_beamng_output_fps",
             "_beamng_start_frame", "_beamng_smooth_stop_frames",
             "_beamng_smooth_stop_start", "_beamng_cache_path"):
    if prop in scene:
        log(f"scene.{prop} = {scene[prop]}")

root = bpy.data.objects.get("BeamNG_Cache__root") or next(
    (o for o in bpy.data.objects if o.name.endswith("__root")), None)
log(f"root empty: {root.name if root else 'MISSING'}")

probe_obj = None
if root:
    for child in root.children_recursive:
        if child.type == "MESH":
            probe_obj = child
            break
if probe_obj is None:
    meshes = [o for o in bpy.data.objects if o.type == "MESH"]
    probe_obj = max(meshes, key=lambda o: len(o.data.vertices)) if meshes \
        else None
log(f"probe object: {probe_obj.name if probe_obj else 'NONE'}")


def probe():
    out = {}
    if probe_obj is not None and len(probe_obj.data.vertices):
        v = probe_obj.data.vertices[0].co
        out["vtx"] = tuple(round(float(c), 3) for c in v)
    if root is not None:
        t = root.matrix_world.translation
        out["root"] = tuple(round(float(c), 3) for c in t)
    return out


before = probe()
log(f"probe BEFORE = {before}")

# ------------------------------------------------------- handler inventory
HANDLERS = ("frame_change_pre", "frame_change_post", "render_pre",
            "render_post", "depsgraph_update_post")
originals = {}
for name in HANDLERS:
    lst = getattr(bpy.app.handlers, name, [])
    names = [getattr(h, "__name__", repr(h))[:60] for h in lst]
    originals[name] = list(lst)
    log(f"{name}: {len(lst)} handler(s): {names}")

# --------------------------------------------------------------- wrap them
entries = []
wrapped = []          # (handler_list, wrapper, [originals])


def make_wrapper(name, fn):
    def wrapper(*args, **kwargs):
        sc = next((a for a in args if hasattr(a, "frame_current")), None)
        entries.append((name,
                        threading.current_thread().name,
                        getattr(sc, "frame_current", None),
                        probe()))
        return fn(*args, **kwargs)
    wrapper.__name__ = f"_userdiag_{name}_" + \
        getattr(fn, "__name__", "anon")
    return wrapper


for name in HANDLERS:
    lst = getattr(bpy.app.handlers, name, None)
    if not lst:
        continue
    for h in list(lst):
        w = make_wrapper(name, h)
        lst.remove(h)
        lst.append(w)
        wrapped.append((lst, w, h))

# --------------------------------------------------- run the ogl animation
saved_range = (scene.frame_start, scene.frame_end)
cur = scene.frame_current
scene.frame_start = cur
scene.frame_end = min(cur + N_TEST_FRAMES - 1, saved_range[1])

os.makedirs(OUT_DIR, exist_ok=True)
for f in os.listdir(OUT_DIR):
    os.remove(os.path.join(OUT_DIR, f))
scene.render.filepath = os.path.join(OUT_DIR, "u_")
scene.render.image_settings.file_format = "PNG"

win = area = region = None
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

try:
    if area is None:
        log("no VIEW_3D visible - cannot run the opengl render")
    else:
        with bpy.context.temp_override(window=win, area=area, region=region):
            result = bpy.ops.render.opengl(animation=True)
        log(f"opengl render returned {result}")
except Exception as exc:
    log(f"opengl render raised: {exc!r}")

scene.frame_start, scene.frame_end = saved_range

after = probe()
log(f"probe AFTER  = {after}  (mesh_data_moved={before != after})")

# ------------------------------------------------------------ image diff
saved = sorted(f for f in os.listdir(OUT_DIR)
               if f.lower().endswith((".png", ".jpg")))
log(f"saved files: {len(saved)}")
if len(saved) >= 2:
    def px(path):
        img = bpy.data.images.load(path)
        try:
            buf = np.empty(img.size[0] * img.size[1] * img.channels,
                           dtype=np.float32)
            img.pixels.foreach_get(buf)
            return buf.reshape(-1, img.channels)[:, :3]
        finally:
            bpy.data.images.remove(img)
    d = float(np.abs(px(os.path.join(OUT_DIR, saved[0]))
                     - px(os.path.join(OUT_DIR, saved[-1]))).mean())
    log(f"IMAGE first-vs-last mean diff = {d:.4e} "
        f"({'car MOVED in render' if d > 1e-4 else 'FROZEN RENDER'})")

kinds = {}
for name, thread, frame, pr in entries:
    kinds.setdefault((name, thread), []).append((frame, pr))
for (name, thread), evs in sorted(kinds.items()):
    frames = sorted({e[0] for e in evs})
    log(f"callback {name}: thread={thread} n={len(evs)} frames={frames[:10]}"
        f"{'...' if len(frames) > 10 else ''}")
    log(f"   sample={evs[0]}")
if not entries:
    log("NO PYTHON CALLBACKS FIRED during the opengl render")

# ---------------------------------------------------------------- cleanup
for lst, w, h in wrapped:
    try:
        lst.remove(w)
    except ValueError:
        pass
    try:
        lst.append(h)
    except ValueError:
        pass

flush()
log(f"DONE - full log at {LOG_PATH}")
