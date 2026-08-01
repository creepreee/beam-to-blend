"""Regression: does the rigid ROOT MOTION survive a save -> reopen?

The earlier reload probes only checked that vertices *deform* after a reload.
They passed while the car was still visibly broken, because deformation and
rigid motion travel through two different paths:

  * deformation -> mesh vertex positions, rewritten every frame
  * rigid motion -> the "<collection>__root" Empty's matrix_basis, driven by
    `CachePlayback._apply_transform`

`_apply_transform` returns early when `_transform_empty is None`.  That field is
module state, not saved in the .blend, so before the fix a reloaded scene left
it None: the car deformed perfectly but stayed at the origin instead of flying
through the crash.  A deformation-only assertion cannot see that.

This checks BOTH, and asserts the root actually translates a meaningful
distance, in the reopened file.

Run:
    blender --background --python tests/blender_reload_root.py -- <cache.bvc> [outdir]
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


_fail = []


def check(cond, msg):
    print(f"[ROOT][{'ok  ' if cond else 'FAIL'}] {msg}")
    sys.stdout.flush()
    if not cond:
        _fail.append(msg)


def info(m):
    print(f"[ROOT][info] {m}")
    sys.stdout.flush()


CHILD = r'''
import sys, os
sys.path.insert(0, r"{repo}")
import bpy, numpy as np
import addon
addon.register()
from runtime import frame_handler

bpy.ops.wm.open_mainfile(filepath=r"{blend}")
scene = bpy.context.scene
a = frame_handler._active
print("[C] recovered:", a is not None)
print("[C] _transform_empty:",
      None if a is None or a._transform_empty is None else a._transform_empty.name)

coll = bpy.data.collections.get("BeamNG Cache")
meshes = [o for o in (coll.objects if coll else []) if o.type == "MESH"]

def verts(o):
    dg = bpy.context.evaluated_depsgraph_get()
    me = o.evaluated_get(dg).data
    arr = np.empty(len(me.vertices) * 3, dtype=np.float32)
    me.vertices.foreach_get("co", arr)
    return arr.reshape(-1, 3)

probe = max(meshes, key=lambda o: len(o.data.vertices))
f0, f1 = scene.frame_start, (scene.frame_start + scene.frame_end) // 2

scene.frame_set(f0)
local0 = verts(probe)
world0 = np.array(probe.matrix_world.translation)
root0 = (np.array(a._transform_empty.matrix_world.translation)
         if a is not None and a._transform_empty is not None else np.zeros(3))

scene.frame_set(f1)
local1 = verts(probe)
world1 = np.array(probe.matrix_world.translation)
root1 = (np.array(a._transform_empty.matrix_world.translation)
         if a is not None and a._transform_empty is not None else np.zeros(3))

print("[C] deform_delta=%.6f" % float(np.abs(local1 - local0).max()))
print("[C] root_translation=%.6f" % float(np.linalg.norm(root1 - root0)))
print("[C] world_translation=%.6f" % float(np.linalg.norm(world1 - world0)))
print("[C] root_pos_f0=%s" % np.round(root0, 3).tolist())
print("[C] root_pos_f1=%s" % np.round(root1, 3).tolist())
'''


def measure_reference(cache_path, chunked):
    """Import fresh and record the root motion the reopened file must match."""
    bpy.ops.wm.read_factory_settings(use_empty=True)
    import addon
    # register() is not idempotent (register_class raises on a second call), and
    # this helper runs once per mode, so re-register only when needed.
    try:
        addon.register()
    except ValueError:
        pass
    reader = CacheReader(cache_path)
    chunk_map = ({k: list(v) for k, v in CHUNK_MAP_E180.items()}
                 if chunked else None)
    pb = CachePlayback(reader, chunk_map=chunk_map)
    pb.build_scene()
    frame_handler.attach(pb, playback_fps=24, output_fps=60, start_second=0.0)
    scene = bpy.context.scene
    f0, f1 = scene.frame_start, (scene.frame_start + scene.frame_end) // 2
    scene.frame_set(f0)
    r0 = np.array(pb._transform_empty.matrix_world.translation) \
        if pb._transform_empty else np.zeros(3)
    scene.frame_set(f1)
    r1 = np.array(pb._transform_empty.matrix_world.translation) \
        if pb._transform_empty else np.zeros(3)
    has_tf = bool(reader.header.get("transform_data_offset", 0))
    return reader, pb, float(np.linalg.norm(r1 - r0)), has_tf


def main():
    a = _argv()
    if not a or not os.path.exists(a[0]):
        print("[ROOT][FAIL] usage: -- <cache.bvc> [outdir]")
        return 1
    cache_path = os.path.abspath(a[0])
    outdir = os.path.abspath(a[1] if len(a) > 1 else tempfile.gettempdir())

    for chunked in (False, True):
        label = "chunked" if chunked else "per-object"
        info(f"================ {label} ================")
        reader, pb, ref_root, has_tf = measure_reference(cache_path, chunked)
        check(has_tf, f"{label}: cache carries a transform block "
                      f"(else there is no root motion to test)")
        check(pb._transform_empty is not None,
              f"{label}: fresh import created the __root empty")
        info(f"{label}: reference root motion = {ref_root:.3f} m")
        check(ref_root > 1e-3,
              f"{label}: reference root actually moves ({ref_root:.3f} m)")

        blend = os.path.join(outdir, f"_root_{label}.blend")
        bpy.ops.wm.save_as_mainfile(filepath=blend)
        reader.close()

        cp = os.path.join(outdir, f"_root_{label}.py")
        with open(cp, "w") as f:
            f.write(CHILD.format(repo=_REPO, blend=blend))
        out = subprocess.run([sys.argv[0], "--background", "--factory-startup",
                              "--python", cp],
                             capture_output=True, text=True, timeout=1800)
        vals = {}
        for line in out.stdout.splitlines():
            if line.startswith("[C]"):
                print("   " + line)
                if "=" in line:
                    k, _, v = line[4:].partition("=")
                    vals[k.strip()] = v.strip()
        if out.returncode != 0:
            for line in out.stderr.splitlines()[-15:]:
                print("     !" + line)

        deform = float(vals.get("deform_delta", 0.0))
        root = float(vals.get("root_translation", 0.0))
        check(deform > 1e-6, f"{label}: mesh still DEFORMS after reopen")
        check(root > 1e-3,
              f"{label}: root still TRANSLATES after reopen "
              f"({root:.3f} m; this is the regression)")
        if ref_root > 1e-3:
            check(abs(root - ref_root) / ref_root < 0.02,
                  f"{label}: root motion matches the fresh import "
                  f"({root:.3f} vs {ref_root:.3f} m)")

        for p in (blend, cp):
            try:
                os.remove(p)
            except OSError:
                pass

    print()
    if _fail:
        print(f"[ROOT] {len(_fail)} FAILURE(S):")
        for f in _fail:
            print("  - " + f)
        return 1
    print("[ROOT] ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    rc = main()
    sys.stdout.flush()
    sys.exit(rc)
