"""Export one .abc from a cache and leave it on disk for other probes.

Run:
    blender --background --python tests/blender_abc_make.py -- <cache.bvc> <n_frames> <out.abc> [normals0|normals1]
"""

import os
import sys
import tempfile
import shutil

import bpy

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)
for _mod in [m for m in sys.modules
             if m == "runtime" or m.startswith("runtime.")
             or m == "importer" or m.startswith("importer.")]:
    del sys.modules[_mod]

from runtime.cache_reader import CacheReader
from runtime.mesh_update import CachePlayback
from runtime.baker import bake_to_mdd, apply_mdd_modifiers


def _argv():
    return sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []


def main():
    a = _argv()
    cache_path, n, out = os.path.abspath(a[0]), int(a[1]), os.path.abspath(a[2])
    normals = (len(a) < 4) or a[3] != "normals0"

    bpy.ops.wm.read_factory_settings(use_empty=True)
    reader = CacheReader(cache_path)
    n = min(n, reader.frame_count)
    playback = CachePlayback(reader)
    playback.build_scene()
    scene = bpy.context.scene
    scene.frame_start = 0

    mdd_dir = tempfile.mkdtemp(prefix="mk_mdd_")
    names = bake_to_mdd(reader, mdd_dir, frame_start=0, frame_end=n - 1)
    apply_mdd_modifiers(bpy, names, mdd_dir + os.sep, 0)
    for o in bpy.data.objects:
        o.select_set(False)
    for nm in names:
        o = bpy.data.objects.get(nm)
        if o and o.type == "MESH":
            o.select_set(True)
    bpy.ops.wm.alembic_export(
        filepath=out, start=0, end=n - 1, selected=True, flatten=False,
        face_sets=True, uvs=True, packuv=True, normals=normals)
    shutil.rmtree(mdd_dir, ignore_errors=True)
    reader.close()
    print(f"[MAKE] wrote {out} ({os.path.getsize(out)/1e6:.0f} MB), "
          f"{n} frames, normals={normals}, scene fps={scene.render.fps}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
