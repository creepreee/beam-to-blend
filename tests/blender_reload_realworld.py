"""The decisive reload test: real user preferences, real launch paths.

`blender_reload_failmodes.py` showed CASE 2 (Blender launched with the .blend
as a command-line argument) fails, but it ran `--factory-startup`, where the
add-on is enabled from inside the script — i.e. *after* Blender already loaded
the file, so `load_post` could not possibly have fired.  That is not proof
about the real double-click path, where the add-on is enabled from user prefs
*before* the file loads.

This runs WITHOUT --factory-startup so the user's actual enabled-add-on set is
in play, and compares the two launch paths that matter:

  A. `blender file.blend`                 (double-click / "Open Recent")
  B. `blender` then File > Open           (already-running session)

It also reports whether the add-on is enabled in the saved preferences at all,
since that single fact decides whether recovery can ever run.

Run:
    blender --background --python tests/blender_reload_realworld.py -- <cache.bvc> [outdir]
"""

import os
import sys
import subprocess
import tempfile


def _argv():
    return sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []


def info(m):
    print(f"[RW] {m}")
    sys.stdout.flush()


PREFCHECK = r'''
import bpy, addon_utils
names = [m.__name__ for m in addon_utils.modules() if addon_utils.check(m.__name__)[1]]
print("[P] enabled add-ons:", sorted(n for n in names if "beamng" in n.lower()) or "NONE with 'beamng'")
print("[P] beamng_cache_importer enabled:",
      addon_utils.check("beamng_cache_importer")[1])
'''

SETUP = r'''
import bpy, addon_utils
addon_utils.enable("beamng_cache_importer", default_set=False, persistent=True)
from runtime.cache_reader import CacheReader
from runtime.mesh_update import CachePlayback
from runtime import frame_handler
reader = CacheReader(r"{cache}")
pb = CachePlayback(reader)
pb.build_scene()
frame_handler.attach(pb, playback_fps=24, output_fps=60)
bpy.ops.wm.save_as_mainfile(filepath=r"{blend}")
print("[S] saved with cache_path")
'''

CHECK = r'''
import bpy, os
import numpy as np
print("[K] add-on already enabled at startup:",
      "beamng_cache_importer" in [m for m in __import__("sys").modules])
{openfile}
scene = bpy.context.scene
coll = bpy.data.collections.get("BeamNG Cache")
meshes = [o for o in (coll.objects if coll else []) if o.type == "MESH"]
print("[K] meshes:", len(meshes))
print("[K] frame_change_pre:",
      [getattr(h, "__name__", repr(h)) for h in bpy.app.handlers.frame_change_pre])
try:
    from runtime import frame_handler
    print("[K] _active is None:", frame_handler._active is None)
except Exception as e:
    print("[K] runtime not importable:", e)

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
    print("[K] delta=%.6f  ANIMATES: %s" % (d, d > 1e-6))
'''


def run(blender, script, outdir, tag, pre_args=(), factory=False):
    p = os.path.join(outdir, f"_rw_{tag}.py")
    with open(p, "w") as f:
        f.write(script)
    cmd = [blender, "--background"]
    if factory:
        cmd.append("--factory-startup")
    cmd += [*pre_args, "--python", p]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    for line in out.stdout.splitlines():
        if line[:3] in ("[K]", "[S]", "[P]"):
            print("  " + line)
    if out.returncode != 0:
        for line in out.stderr.splitlines()[-8:]:
            print("    !" + line)
    os.remove(p)


def main():
    a = _argv()
    cache = os.path.abspath(a[0])
    outdir = os.path.abspath(a[1] if len(a) > 1 else tempfile.gettempdir())
    blender = sys.argv[0]
    blend = os.path.join(outdir, "_rw.blend")

    info("=== is the add-on enabled in the SAVED preferences? ===")
    run(blender, PREFCHECK, outdir, "pref")

    info("=== setup: import + save (real prefs) ===")
    run(blender, SETUP.format(cache=cache, blend=blend), outdir, "setup")

    info("=== PATH A: blender file.blend  (double-click / Open Recent) ===")
    run(blender, CHECK.format(openfile=""), outdir, "a", pre_args=(blend,))

    info("=== PATH B: running session, then File > Open ===")
    run(blender, CHECK.format(
        openfile=f'bpy.ops.wm.open_mainfile(filepath=r"{blend}")'),
        outdir, "b")

    try:
        os.remove(blend)
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
