"""Does the INSTALLED add-on recover the animation on reopen?

The repo build passes `blender_reload_probe.py`, but the user runs the copy in
`scripts/addons/beamng_cache_importer`, whose `runtime/frame_handler.py` is an
older revision.  This probe deliberately does NOT evict the installed modules
(the opposite of every other test here) so it exercises exactly the code the
interactive session runs.

Run:
    blender --background --python tests/blender_reload_installed.py -- <cache.bvc> [outdir]
"""

import os
import sys
import subprocess
import tempfile


def _argv():
    return sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []


def info(m):
    print(f"[INST] {m}")
    sys.stdout.flush()


# Stage A: enable the installed add-on, import a cache, save.
STAGE_A = r'''
import bpy, sys, os
import addon_utils
addon_utils.enable("beamng_cache_importer", default_set=True, persistent=True)
mod = sys.modules.get("beamng_cache_importer")
print("[A] installed add-on module:", getattr(mod, "__file__", "NOT LOADED"))

# Resolve runtime/importer the way the installed package does.
from beamng_cache_importer import runtime  # noqa
from runtime.cache_reader import CacheReader
from runtime.mesh_update import CachePlayback
from runtime import frame_handler
print("[A] frame_handler from:", frame_handler.__file__)

import numpy as np
reader = CacheReader(r"{cache}")
pb = CachePlayback(reader)
pb.build_scene()
frame_handler.attach(pb, playback_fps=24, output_fps=60)
scene = bpy.context.scene
coll = bpy.data.collections["BeamNG Cache"]
meshes = [o for o in coll.objects if o.type == "MESH"]
probe = max(meshes, key=lambda o: len(o.data.vertices))

def verts(o):
    dg = bpy.context.evaluated_depsgraph_get()
    me = o.evaluated_get(dg).data
    a = np.empty(len(me.vertices) * 3, dtype=np.float32)
    me.vertices.foreach_get("co", a)
    return a.reshape(-1, 3)

scene.frame_set(scene.frame_start)
p0 = verts(probe)
scene.frame_set((scene.frame_start + scene.frame_end) // 2)
p1 = verts(probe)
print("[A] animates before save:", float(np.abs(p1 - p0).max()) > 1e-6)
print("[A] scene _beamng_cache_path:", scene.get("_beamng_cache_path"))
bpy.ops.wm.save_as_mainfile(filepath=r"{blend}")
print("[A] saved")
'''

# Stage B: fresh Blender, add-on enabled from user prefs, open the file.
STAGE_B = r'''
import bpy, sys, os
import addon_utils
addon_utils.enable("beamng_cache_importer", default_set=True, persistent=True)
from runtime import frame_handler
print("[B] frame_handler from:", frame_handler.__file__)
print("[B] load_post armed:",
      [getattr(h, "__name__", repr(h)) for h in bpy.app.handlers.load_post])

bpy.ops.wm.open_mainfile(filepath=r"{blend}")
scene = bpy.context.scene
print("[B] --- opened ---")
print("[B] cache_path prop:", scene.get("_beamng_cache_path"))
print("[B] load_post after open:",
      [getattr(h, "__name__", repr(h)) for h in bpy.app.handlers.load_post])
print("[B] frame_change_pre after open:",
      [getattr(h, "__name__", repr(h)) for h in bpy.app.handlers.frame_change_pre])
print("[B] _active is None:", frame_handler._active is None)

import numpy as np
coll = bpy.data.collections.get("BeamNG Cache")
meshes = [o for o in (coll.objects if coll else []) if o.type == "MESH"]
print("[B] meshes:", len(meshes))

def verts(o):
    dg = bpy.context.evaluated_depsgraph_get()
    me = o.evaluated_get(dg).data
    a = np.empty(len(me.vertices) * 3, dtype=np.float32)
    me.vertices.foreach_get("co", a)
    return a.reshape(-1, 3)

if meshes:
    probe = max(meshes, key=lambda o: len(o.data.vertices))
    scene.frame_set(scene.frame_start)
    p0 = verts(probe)
    scene.frame_set((scene.frame_start + scene.frame_end) // 2)
    p1 = verts(probe)
    d = float(np.abs(p1 - p0).max())
    print("[B] probe=%s delta=%.6f" % (probe.name, d))
    print("[B] INSTALLED_ANIMATES_AFTER_REOPEN:", d > 1e-6)
'''


def main():
    a = _argv()
    cache = os.path.abspath(a[0])
    outdir = os.path.abspath(a[1] if len(a) > 1 else tempfile.gettempdir())
    blend = os.path.join(outdir, "_inst.blend")
    blender = sys.argv[0]

    for tag, src in (("A", STAGE_A), ("B", STAGE_B)):
        p = os.path.join(outdir, f"_inst_{tag}.py")
        with open(p, "w") as f:
            f.write(src.format(cache=cache, blend=blend))
        info(f"--- stage {tag} ---")
        out = subprocess.run([blender, "--background", "--factory-startup",
                              "--python", p],
                             capture_output=True, text=True, timeout=1800)
        for line in out.stdout.splitlines():
            if line.startswith(f"[{tag}]") or "Traceback" in line:
                print("  " + line)
        if out.returncode != 0:
            for line in out.stderr.splitlines()[-20:]:
                print("    !" + line)
        os.remove(p)

    try:
        os.remove(blend)
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
