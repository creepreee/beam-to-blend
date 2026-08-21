"""Round 2: persistent markers + wrapped _on_load_post.

    blender -b --python tests/diag_bg_openfile.py

Non-persistent load_post handlers are REMOVED by the file load before they
can fire, so round 1's marker proved nothing.  This one marks everything
persistent, swaps a logging wrapper in for the add-on's own _on_load_post,
then opens the .blend via wm.open_mainfile.
"""

import importlib
import traceback

import bpy

BLEND = r"C:\Users\ubaid_i2c\Downloads\vehicle materials ready made.blend"

fired = {"marker": 0}


@bpy.app.handlers.persistent
def marker(_scene):
    fired["marker"] += 1
    print(f"[OPENPROBE2] persistent marker fired #{fired['marker']} "
          f"scene={bpy.context.scene.name!r}")


bpy.app.handlers.load_post.append(marker)

addon = importlib.import_module("beamng_cache_importer")
orig = addon._on_load_post


@bpy.app.handlers.persistent
def wrapped_load_post(arg):
    import bpy as _bpy
    print(f"[OPENPROBE2] >>> _on_load_post INVOKED, arg type="
          f"{type(arg).__name__} value={str(arg)[:60]!r}")
    ctx_scene = _bpy.context.scene
    print(f"[OPENPROBE2]     context.scene={ctx_scene!r}")
    fh2 = importlib.import_module(
        "beamng_cache_importer.runtime.frame_handler")
    if ctx_scene is not None:
        print(f"[OPENPROBE2]     ctx props="
              f"{ctx_scene.get('_beamng_cache_path', '<absent>')}")
        fh2._active = None
        print(f"[OPENPROBE2]     try_recover(ctx.scene) -> "
              f"{fh2._try_recover(ctx_scene)}")
        fh2._active = None
    else:
        found = [sc.name for sc in _bpy.data.scenes
                 if sc.get("_beamng_cache_path")]
        print(f"[OPENPROBE2]     context.scene is None; "
              f"scenes with props={found}")
        for sc in _bpy.data.scenes:
            if sc.get("_beamng_cache_path"):
                print(f"[OPENPROBE2]     try_recover({sc.name!r}) -> "
                      f"{fh2._try_recover(sc)}")
                fh2._active = None
                break
    try:
        orig(arg)
        print("[OPENPROBE2] <<< orig returned without exception")
    except Exception:
        traceback.print_exc()
        print("[OPENPROBE2] <<< orig RAISED")


wrapped_load_post.__name__ = "_on_load_post_wrapped"
lp = bpy.app.handlers.load_post
for i, h in enumerate(lp):
    if getattr(h, "__name__", "") == "_on_load_post":
        lp[i] = wrapped_load_post
        break

print(f"[OPENPROBE2] armed: {len(lp)} load_post handler(s)")
fh = importlib.import_module("beamng_cache_importer.runtime.frame_handler")

bpy.ops.wm.open_mainfile(filepath=BLEND)

print(f"[OPENPROBE2] after open: marker={fired['marker']}, "
      f"_active={getattr(fh, '_active', None)}")
if fh._active is None:
    ok = fh._try_recover(bpy.context.scene)
    print(f"[OPENPROBE2] manual _try_recover -> {ok}")
