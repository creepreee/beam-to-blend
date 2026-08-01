"""Proof: enabling the add-on in saved preferences is what makes reopen work.

`blender_reload_realworld.py` found `beamng_cache_importer` is NOT enabled in
the user's saved preferences — only `beamng_texture_assign` and the glTF
sequence importer are.  So on every launch the add-on never loads, `load_post`
is never armed, and `_try_recover` never runs.  The animation is therefore not
"lost on close" — it was never re-attached on open.

This proves the causal link without touching the real preferences: it points
BLENDER_USER_CONFIG at a throwaway directory, saves prefs there WITH the add-on
enabled, then relaunches against that config and opens the file the same
double-click way that failed before.

Run:
    blender --background --python tests/blender_reload_proof.py -- <cache.bvc> [outdir]
"""

import os
import sys
import shutil
import subprocess
import tempfile


def _argv():
    return sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []


def info(m):
    print(f"[PROOF] {m}")
    sys.stdout.flush()


ENABLE_AND_SAVE = r'''
import bpy, addon_utils
addon_utils.enable("beamng_cache_importer", default_set=True, persistent=True)
bpy.ops.wm.save_userpref()
print("[E] enabled + saved prefs; check:",
      addon_utils.check("beamng_cache_importer")[1])
'''

SETUP = r'''
import bpy
from runtime.cache_reader import CacheReader
from runtime.mesh_update import CachePlayback
from runtime import frame_handler
reader = CacheReader(r"{cache}")
pb = CachePlayback(reader)
pb.build_scene()
frame_handler.attach(pb, playback_fps=24, output_fps=60)
bpy.ops.wm.save_as_mainfile(filepath=r"{blend}")
print("[S] saved")
'''

CHECK = r'''
import bpy, os, sys
import numpy as np
print("[K] add-on module loaded at startup:",
      "beamng_cache_importer" in sys.modules)
print("[K] load_post at startup:",
      [getattr(h, "__name__", repr(h)) for h in bpy.app.handlers.load_post])
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


def run(blender, script, outdir, tag, env, pre_args=()):
    p = os.path.join(outdir, f"_pf_{tag}.py")
    with open(p, "w") as f:
        f.write(script)
    out = subprocess.run([blender, "--background", *pre_args, "--python", p],
                         capture_output=True, text=True, timeout=1800, env=env)
    for line in out.stdout.splitlines():
        if line[:3] in ("[K]", "[S]", "[E]"):
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
    blend = os.path.join(outdir, "_pf.blend")

    cfg = os.path.join(outdir, "_pf_config")
    shutil.rmtree(cfg, ignore_errors=True)
    os.makedirs(cfg, exist_ok=True)
    # Point Blender at a throwaway config, but keep the real add-on directory
    # visible via BLENDER_USER_SCRIPTS so the installed package is found.
    env = dict(os.environ)
    env["BLENDER_USER_CONFIG"] = cfg
    env["BLENDER_USER_SCRIPTS"] = os.path.join(
        os.environ["APPDATA"], "Blender Foundation", "Blender", "4.5", "scripts")

    info(f"throwaway config: {cfg}")
    info("=== enable add-on in THIS config and save prefs ===")
    run(blender, ENABLE_AND_SAVE, outdir, "enable", env)

    info("=== setup: import + save ===")
    run(blender, SETUP.format(cache=cache, blend=blend), outdir, "setup", env)

    info("=== double-click path, add-on enabled in prefs ===")
    run(blender, CHECK.format(openfile=""), outdir, "a", env, pre_args=(blend,))

    info("=== File > Open path, add-on enabled in prefs ===")
    run(blender, CHECK.format(
        openfile=f'bpy.ops.wm.open_mainfile(filepath=r"{blend}")'),
        outdir, "b", env)

    shutil.rmtree(cfg, ignore_errors=True)
    try:
        os.remove(blend)
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
