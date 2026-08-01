"""Why does reload recovery fail in real use when the basic probe passes?

Two suspects the first probe did not cover:

  1. `addon/__init__.py` does::
         bpy.app.handlers.persistent(_on_load_post)
         bpy.app.handlers.load_post.append(_on_load_post)
     `bpy.app.handlers.persistent` is a DECORATOR — the return value is what
     carries the flag.  Discarding it may leave the handler NON-persistent, in
     which case Blender drops it from load_post when a file is opened and
     recovery never runs.  Probe 1 registered the add-on in the same process it
     opened the file, which can mask this.

  2. Chunked mode.  `_try_recover`'s chunk branch rebuilds `_chunks` and
     `_chunk_member_ranges` by hand; it is far more fragile than the
     per-object branch that probe 1 exercised.

Run:
    blender --background --python tests/blender_reload_probe2.py -- <cache.bvc> [outdir]
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
from runtime.mesh_update import CachePlayback, CHUNK_MAP_E180
from runtime import frame_handler


def _argv():
    return sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []


def info(m):
    print(f"[R2] {m}")
    sys.stdout.flush()


def verts(o):
    dg = bpy.context.evaluated_depsgraph_get()
    me = o.evaluated_get(dg).data
    a = np.empty(len(me.vertices) * 3, dtype=np.float32)
    me.vertices.foreach_get("co", a)
    return a.reshape(-1, 3)


# --- Test 1: is the persistent decorator actually applied? ---------------
FLAGTEST = r'''
import sys
sys.path.insert(0, r"{repo}")
import bpy
import addon
addon.register()
f = addon._on_load_post
print("[F] _bpy_persistent attr present:", hasattr(f, "_bpy_persistent"))
print("[F] _bpy_persistent value:", getattr(f, "_bpy_persistent", "<absent>"))
# What a CORRECTLY decorated function looks like, for comparison:
@bpy.app.handlers.persistent
def _reference(dummy):
    pass
print("[F] reference decorated value:", getattr(_reference, "_bpy_persistent", "<absent>"))
print("[F] SAME_AS_REFERENCE:",
      getattr(f, "_bpy_persistent", None) == getattr(_reference, "_bpy_persistent", object()))
'''

# --- Test 2: chunked-mode reopen -----------------------------------------
CHILD = r'''
import sys, os
sys.path.insert(0, r"{repo}")
import bpy, numpy as np
import addon
addon.register()
from runtime import frame_handler

bpy.ops.wm.open_mainfile(filepath=r"{blend}")
scene = bpy.context.scene
print("[C] chunked flag on scene:", scene.get("_beamng_use_chunked"))
print("[C] load_post survived open:",
      "_on_load_post" in [getattr(h, "__name__", "") for h in bpy.app.handlers.load_post])
print("[C] frame_change_pre:",
      [getattr(h, "__name__", repr(h)) for h in bpy.app.handlers.frame_change_pre])
print("[C] _active is None:", frame_handler._active is None)
a = frame_handler._active
if a is not None:
    print("[C] recovered chunks:", len(a._chunks),
          "member_range_sets:", len(a._chunk_member_ranges),
          "source objects:", len(a._objects))

coll = bpy.data.collections.get("BeamNG Cache")
meshes = [o for o in (coll.objects if coll else []) if o.type == "MESH"]
print("[C] visible playback meshes:", len(meshes))

def verts(o):
    dg = bpy.context.evaluated_depsgraph_get()
    me = o.evaluated_get(dg).data
    arr = np.empty(len(me.vertices) * 3, dtype=np.float32)
    me.vertices.foreach_get("co", arr)
    return arr.reshape(-1, 3)

if meshes:
    probe = max(meshes, key=lambda o: len(o.data.vertices))
    scene.frame_set(scene.frame_start)
    p0 = verts(probe)
    mid = (scene.frame_start + scene.frame_end) // 2
    scene.frame_set(mid)
    p1 = verts(probe)
    d = float(np.abs(p1 - p0).max())
    print("[C] probe=%s delta=%.6f" % (probe.name, d))
    print("[C] CHUNKED_ANIMATES_AFTER_REOPEN:", d > 1e-6)
else:
    print("[C] CHUNKED_ANIMATES_AFTER_REOPEN: NO_MESHES")
'''


def main():
    a = _argv()
    cache_path = os.path.abspath(a[0])
    outdir = os.path.abspath(a[1] if len(a) > 1 else tempfile.gettempdir())

    # ---------- Test 1 ----------
    info("=== TEST 1: is _on_load_post actually flagged persistent? ===")
    ft = os.path.join(outdir, "_flagtest.py")
    with open(ft, "w") as f:
        f.write(FLAGTEST.format(repo=_REPO))
    out = subprocess.run([sys.argv[0], "--background", "--factory-startup",
                          "--python", ft],
                         capture_output=True, text=True, timeout=600)
    for line in out.stdout.splitlines():
        if line.startswith("[F]"):
            print("  " + line)
    os.remove(ft)

    # ---------- Test 2 ----------
    info("=== TEST 2: chunked-mode save -> reopen ===")
    bpy.ops.wm.read_factory_settings(use_empty=True)
    import addon as _a
    _a.register()

    reader = CacheReader(cache_path)
    chunk_map = {k: list(v) for k, v in CHUNK_MAP_E180.items()}
    playback = CachePlayback(reader, chunk_map=chunk_map)
    playback.build_scene()
    frame_handler.attach(playback, playback_fps=24, output_fps=60,
                         start_second=0.0)
    scene = bpy.context.scene
    coll = bpy.data.collections["BeamNG Cache"]
    meshes = [o for o in coll.objects if o.type == "MESH"]
    probe = max(meshes, key=lambda o: len(o.data.vertices))
    scene.frame_set(scene.frame_start)
    p0 = verts(probe)
    scene.frame_set((scene.frame_start + scene.frame_end) // 2)
    p1 = verts(probe)
    info(f"chunked before save: animates="
         f"{float(np.abs(p1 - p0).max()) > 1e-6}, "
         f"{len(playback._chunks)} chunks, {len(meshes)} visible meshes")

    blend = os.path.join(outdir, "_reload2.blend")
    bpy.ops.wm.save_as_mainfile(filepath=blend)
    reader.close()

    cp = os.path.join(outdir, "_reload2_child.py")
    with open(cp, "w") as f:
        f.write(CHILD.format(repo=_REPO, blend=blend))
    out = subprocess.run([sys.argv[0], "--background", "--factory-startup",
                          "--python", cp],
                         capture_output=True, text=True, timeout=1800)
    for line in out.stdout.splitlines():
        if line.startswith("[C]") or "Traceback" in line:
            print("  " + line)
    if out.returncode != 0:
        for line in out.stderr.splitlines()[-20:]:
            print("    " + line)

    for p in (blend, cp):
        try:
            os.remove(p)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
