"""Reload recovery works in the lab — so what breaks it in real use?

Recovery is a *soft link*: the .blend stores only the BVC path, and
`_try_recover` rebuilds the playback from that file at load time.  Anything
that invalidates the path, or changes when the load handler runs, silently
produces a dead scene with no error message.  This probe exercises the
realistic ways that happens.

  1. BVC renamed / moved / deleted after the .blend was saved.
     `_try_recover` returns False on a missing path and says nothing.
  2. Blender launched WITH the .blend as a command-line argument, rather than
     opening it from an already-running session.  Different handler ordering.
  3. Add-on disabled (or a fresh machine that has the .blend but not the add-on).

Run:
    blender --background --python tests/blender_reload_failmodes.py -- <cache.bvc> [outdir]
"""

import os
import sys
import shutil
import subprocess
import tempfile


def _argv():
    return sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []


def info(m):
    print(f"[FM] {m}")
    sys.stdout.flush()


SETUP = r'''
import bpy, sys
import addon_utils
addon_utils.enable("beamng_cache_importer", default_set=True, persistent=True)
from runtime.cache_reader import CacheReader
from runtime.mesh_update import CachePlayback
from runtime import frame_handler
reader = CacheReader(r"{cache}")
pb = CachePlayback(reader)
pb.build_scene()
frame_handler.attach(pb, playback_fps=24, output_fps=60)
bpy.ops.wm.save_as_mainfile(filepath=r"{blend}")
print("[S] saved, cache_path=", bpy.context.scene.get("_beamng_cache_path"))
'''

CHECK = r'''
import bpy, sys, os
import numpy as np
{enable}
try:
    from runtime import frame_handler
except Exception as e:
    frame_handler = None
    print("[K] runtime import failed:", e)

{openfile}

scene = bpy.context.scene
coll = bpy.data.collections.get("BeamNG Cache")
meshes = [o for o in (coll.objects if coll else []) if o.type == "MESH"]
print("[K] meshes present:", len(meshes))
print("[K] stored cache_path:", scene.get("_beamng_cache_path"))
p = str(scene.get("_beamng_cache_path") or "")
print("[K] that path exists:", os.path.exists(p))
if frame_handler is not None:
    print("[K] _active is None:", frame_handler._active is None)
print("[K] frame_change_pre:",
      [getattr(h, "__name__", repr(h)) for h in bpy.app.handlers.frame_change_pre])

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
    print("[K] delta=%.6f" % d)
    print("[K] ANIMATES:", d > 1e-6)
else:
    print("[K] ANIMATES: NO_MESHES")
'''

ENABLE = ('import addon_utils\n'
          'addon_utils.enable("beamng_cache_importer", default_set=True, '
          'persistent=True)')


def run(blender, script, outdir, tag, extra_args=()):
    p = os.path.join(outdir, f"_fm_{tag}.py")
    with open(p, "w") as f:
        f.write(script)
    out = subprocess.run([blender, "--background", "--factory-startup",
                          *extra_args, "--python", p],
                         capture_output=True, text=True, timeout=1800)
    for line in out.stdout.splitlines():
        if line.startswith("[K]") or line.startswith("[S]"):
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
    blend = os.path.join(outdir, "_fm.blend")

    info("=== setup: import + save ===")
    run(blender, SETUP.format(cache=cache, blend=blend), outdir, "setup")

    openfile = f'bpy.ops.wm.open_mainfile(filepath=r"{blend}")'

    info("=== CASE 1: everything intact, opened from a running session ===")
    run(blender, CHECK.format(enable=ENABLE, openfile=openfile), outdir, "c1")

    info("=== CASE 2: Blender launched WITH the .blend on the command line ===")
    # File is loaded by Blender itself before --python runs.
    run(blender, CHECK.format(enable=ENABLE, openfile=""), outdir, "c2",
        extra_args=(blend,))

    info("=== CASE 3: add-on NOT enabled (fresh machine / disabled add-on) ===")
    run(blender, CHECK.format(enable="", openfile=openfile), outdir, "c3")

    info("=== CASE 4: BVC renamed after the .blend was saved ===")
    moved = cache + ".moved"
    os.rename(cache, moved)
    try:
        run(blender, CHECK.format(enable=ENABLE, openfile=openfile), outdir, "c4")
    finally:
        os.rename(moved, cache)
        info("(cache restored)")

    try:
        os.remove(blend)
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
