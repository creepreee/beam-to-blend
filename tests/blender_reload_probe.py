"""Does an imported BVC cache still animate after save -> reopen?

The add-on already has reload recovery (`addon._on_load_post` ->
`frame_handler._try_recover`).  The user reports the animation vanishes on
reopen anyway, so this probe finds out WHERE that chain breaks rather than
guessing.

Run:
    blender --background --python tests/blender_reload_probe.py -- <cache.bvc> [outdir]

Stage 1 (this process): import the cache the way the panel does, verify it
animates, save a .blend.
Stage 2 (child process): register the add-on FIRST (as a real enabled add-on
would be at startup), then open the .blend — so `load_post` is armed before the
file loads, exactly like the interactive case.  Then report, step by step:
  * is `_on_load_post` actually in `bpy.app.handlers.load_post`, and is it
    flagged persistent?
  * is `_on_frame_change` in `frame_change_pre` after the load?
  * did `_try_recover` rebuild `_active`, and are the scene custom props there?
  * does the mesh actually move between two frames?
"""

import os
import sys
import subprocess
import tempfile

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


def info(m):
    print(f"[RELOAD] {m}")
    sys.stdout.flush()


def verts(o):
    dg = bpy.context.evaluated_depsgraph_get()
    me = o.evaluated_get(dg).data
    a = np.empty(len(me.vertices) * 3, dtype=np.float32)
    me.vertices.foreach_get("co", a)
    return a.reshape(-1, 3)


CHILD = r'''
import sys, os
sys.path.insert(0, r"{repo}")
import bpy, numpy as np

# Register the add-on BEFORE opening the file, so load_post is armed exactly
# like it is when the add-on is enabled in preferences at startup.
import addon
addon.register()
print("[C] addon registered")

from runtime import frame_handler
lp = [getattr(h, "__name__", repr(h)) for h in bpy.app.handlers.load_post]
print("[C] load_post before open:", lp)
print("[C] _on_load_post persistent flag:",
      getattr(addon._on_load_post, "_bpy_persistent", "MISSING"))

bpy.ops.wm.open_mainfile(filepath=r"{blend}")
print("[C] --- file opened ---")

scene = bpy.context.scene
print("[C] load_post after open:",
      [getattr(h, "__name__", repr(h)) for h in bpy.app.handlers.load_post])
print("[C] frame_change_pre after open:",
      [getattr(h, "__name__", repr(h)) for h in bpy.app.handlers.frame_change_pre])
print("[C] scene _beamng_cache_path:", scene.get("_beamng_cache_path"))
print("[C] cache file still exists:",
      os.path.exists(str(scene.get("_beamng_cache_path") or "")))
print("[C] frame_handler._active is None:", frame_handler._active is None)

coll = bpy.data.collections.get("BeamNG Cache")
print("[C] collection present:", coll is not None,
      "objects:", len(coll.objects) if coll else 0)

def verts(o):
    dg = bpy.context.evaluated_depsgraph_get()
    me = o.evaluated_get(dg).data
    a = np.empty(len(me.vertices) * 3, dtype=np.float32)
    me.vertices.foreach_get("co", a)
    return a.reshape(-1, 3)

meshes = [o for o in (coll.objects if coll else []) if o.type == "MESH"]
if meshes:
    probe = max(meshes, key=lambda o: len(o.data.vertices))
    scene.frame_set(scene.frame_start)
    p0 = verts(probe)
    mid = (scene.frame_start + scene.frame_end) // 2
    scene.frame_set(mid)
    p1 = verts(probe)
    d = float(np.abs(p1 - p0).max())
    print("[C] probe=%s frames %d->%d max_delta=%.6f" % (
        probe.name, scene.frame_start, mid, d))
    print("[C] ANIMATES_AFTER_REOPEN:", d > 1e-6)
    print("[C] _active after scrub is None:", frame_handler._active is None)
else:
    print("[C] ANIMATES_AFTER_REOPEN: NO_MESHES")
'''


def main():
    a = _argv()
    if not a or not os.path.exists(a[0]):
        print("[RELOAD] usage: -- <cache.bvc> [outdir]")
        return 1
    cache_path = os.path.abspath(a[0])
    outdir = os.path.abspath(a[1] if len(a) > 1 else tempfile.gettempdir())

    bpy.ops.wm.read_factory_settings(use_empty=True)
    import addon
    addon.register()

    reader = CacheReader(cache_path)
    playback = CachePlayback(reader)
    playback.build_scene()
    frame_handler.attach(playback, playback_fps=24, output_fps=60,
                         start_second=0.0)
    scene = bpy.context.scene
    meshes = [o for o in bpy.data.collections["BeamNG Cache"].objects
              if o.type == "MESH"]
    probe = max(meshes, key=lambda o: len(o.data.vertices))
    scene.frame_set(scene.frame_start)
    p0 = verts(probe)
    scene.frame_set((scene.frame_start + scene.frame_end) // 2)
    p1 = verts(probe)
    info(f"before save: animates = {float(np.abs(p1 - p0).max()) > 1e-6} "
         f"(probe {probe.name}, {len(meshes)} meshes)")

    blend = os.path.join(outdir, "_reload_probe.blend")
    bpy.ops.wm.save_as_mainfile(filepath=blend)
    info(f"saved {blend} ({os.path.getsize(blend)/1e6:.0f} MB)")
    reader.close()

    child_py = os.path.join(outdir, "_reload_child.py")
    with open(child_py, "w") as f:
        f.write(CHILD.format(repo=_REPO, blend=blend))

    info("--- child Blender: register add-on, then open the .blend ---")
    out = subprocess.run([sys.argv[0], "--background", "--factory-startup",
                          "--python", child_py],
                         capture_output=True, text=True, timeout=1800)
    for line in out.stdout.splitlines():
        if line.startswith("[C]") or "Error" in line or "Traceback" in line:
            print("  " + line)
    if out.returncode != 0:
        print("  child stderr tail:")
        for line in out.stderr.splitlines()[-25:]:
            print("    " + line)
    sys.stdout.flush()

    for p in (blend, child_py):
        try:
            os.remove(p)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
