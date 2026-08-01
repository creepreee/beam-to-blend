"""Follow-up to blender_abc_bench.py: isolate WHY Alembic playback is slow,
and prove whether an .abc scene survives a .blend save/reload with no add-on.

Run:
    blender --background --python tests/blender_abc_bench2.py -- <cache.bvc> [n_frames] [outdir]

Three questions the first bench left open:
  1. Is the ~480 ms/frame an I/O cold-read artifact, or steady state?  Times a
     cold sequential pass, then a warm re-scrub of the same frames, then a
     random-access pass.
  2. Does dropping per-frame normals (6x smaller file) make it playable?
  3. Does the .abc keep animating after a .blend save + reopen in a Blender
     that has never seen this add-on?  That is the actual durability claim —
     MESH_SEQUENCE_CACHE is a native modifier + CacheFile datablock, so it
     should not need any Python handler.  Verified, not assumed.
"""

import os
import sys
import time
import tempfile
import shutil
import subprocess

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
from runtime.baker import bake_to_mdd, apply_mdd_modifiers


def _argv():
    return sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []


def info(msg):
    print(f"[B2] {msg}")
    sys.stdout.flush()


def scrub(scene, frames):
    t0 = time.perf_counter()
    for f in frames:
        scene.frame_set(int(f))
        bpy.context.view_layer.update()
    return (time.perf_counter() - t0) / len(frames) * 1000.0


CHILD = """
import sys, time, os
import bpy
scene = bpy.context.scene
got = [o for o in bpy.data.objects if o.type == 'MESH']
anim = [o for o in got if any(m.type == 'MESH_SEQUENCE_CACHE' for m in o.modifiers)]
print('[CHILD] objects=%d with_cache_modifier=%d' % (len(got), len(anim)))
print('[CHILD] range %d..%d fps %d' % (scene.frame_start, scene.frame_end, scene.render.fps))
print('[CHILD] handlers frame_change_pre=%d' % len(bpy.app.handlers.frame_change_pre))
import numpy as np
probe = max(got, key=lambda o: len(o.data.vertices))
def verts(o):
    dg = bpy.context.evaluated_depsgraph_get()
    me = o.evaluated_get(dg).data
    a = np.empty(len(me.vertices) * 3, dtype=np.float32)
    me.vertices.foreach_get('co', a)
    return a.reshape(-1, 3)
scene.frame_set(scene.frame_start)
p0 = verts(probe)
scene.frame_set(scene.frame_end)
pN = verts(probe)
d = float(np.abs(pN - p0).max()) if p0.shape == pN.shape else -1.0
print('[CHILD] probe=%s max_delta=%.6f' % (probe.name, d))
print('[CHILD] ANIMATES=%s' % (d > 1e-6))
mats = sum(len(o.data.materials) for o in got)
uvs = sum(1 for o in got if o.data.uv_layers)
print('[CHILD] material_slots=%d objects_with_uvs=%d' % (mats, uvs))
"""


def main():
    args = _argv()
    if not args or not os.path.exists(args[0]):
        print("[B2] usage: -- <cache.bvc> [n_frames] [outdir]")
        return 1
    cache_path = os.path.abspath(args[0])
    n_bench = int(args[1]) if len(args) > 1 else 120
    outdir = os.path.abspath(args[2] if len(args) > 2 else tempfile.gettempdir())

    bpy.ops.wm.read_factory_settings(use_empty=True)
    reader = CacheReader(cache_path)
    total_frames = reader.frame_count
    n_bench = min(n_bench, total_frames)
    playback = CachePlayback(reader)
    playback.build_scene()
    frame_handler.attach(playback, playback_fps=24, output_fps=60,
                         start_second=0.0)
    scene = bpy.context.scene
    info(f"cache frames={total_frames}, benching {n_bench}")

    # BVC reference: cold then warm, same protocol as ABC gets below.
    bf = [scene.frame_start + i for i in range(n_bench)]
    bvc_cold = scrub(scene, bf)
    bvc_warm = scrub(scene, bf)
    info(f"BVC cold {bvc_cold:.2f} ms/f   warm {bvc_warm:.2f} ms/f")

    frame_handler.detach()
    mdd_dir = tempfile.mkdtemp(prefix="b2_mdd_")
    names = bake_to_mdd(reader, mdd_dir, frame_start=0, frame_end=n_bench - 1)
    apply_mdd_modifiers(bpy, names, mdd_dir + os.sep, scene.frame_start)
    for o in bpy.data.objects:
        o.select_set(False)
    for n in names:
        o = bpy.data.objects.get(n)
        if o and o.type == "MESH":
            o.select_set(True)

    exported = {}
    for use_normals in (True, False):
        p = os.path.join(outdir, f"_b2_n{int(use_normals)}.abc")
        bpy.ops.wm.alembic_export(
            filepath=p, start=scene.frame_start,
            end=scene.frame_start + n_bench - 1,
            selected=True, flatten=False, face_sets=True,
            uvs=True, packuv=True, normals=use_normals)
        exported[use_normals] = p
        info(f"exported normals={use_normals}: {os.path.getsize(p)/1e6:.0f} MB")

    shutil.rmtree(mdd_dir, ignore_errors=True)
    reader.close()

    # ---- playback of each variant: cold / warm / random ----
    blend_path = None
    for use_normals, p in exported.items():
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.wm.alembic_import(filepath=p, as_background_job=False)
        sc = bpy.context.scene
        fr = list(range(sc.frame_start, sc.frame_end + 1))
        cold = scrub(sc, fr)
        warm = scrub(sc, fr)
        rng = np.random.default_rng(0)
        rand = scrub(sc, list(rng.permutation(fr)))
        info(f"ABC normals={use_normals}: {len(fr)} frames  "
             f"cold {cold:8.2f}  warm {warm:8.2f}  random {rand:8.2f} ms/f  "
             f"-> warm ceiling {1000.0/max(warm,1e-9):.1f} fps")
        if use_normals:
            blend_path = os.path.join(outdir, "_b2_reload.blend")
            bpy.ops.wm.save_as_mainfile(filepath=blend_path)

    # ---- durability: reopen the .blend in a clean Blender, no add-on ----
    info("--- reopening saved .blend in a fresh Blender with --factory-startup "
         "(no add-ons at all) ---")
    child_py = os.path.join(outdir, "_b2_child.py")
    with open(child_py, "w") as f:
        f.write(CHILD)
    out = subprocess.run(
        [sys.argv[0], "--background", "--factory-startup", blend_path,
         "--python", child_py],
        capture_output=True, text=True, timeout=900)
    for line in out.stdout.splitlines():
        if line.startswith("[CHILD]"):
            print("  " + line)
    sys.stdout.flush()

    for p in list(exported.values()) + [blend_path, child_py]:
        try:
            os.remove(p)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    rc = main()
    sys.stdout.flush()
    sys.exit(rc)
