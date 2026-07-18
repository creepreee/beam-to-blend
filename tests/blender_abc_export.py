"""End-to-end test of the REAL export operator in CHUNKED mode.

This exercises addon/operators.py BEAMNG_OT_export_alembic exactly as the user
does (chunked import → export), then re-imports the .abc and asserts real
per-frame animation is present.

    blender --background --python tests/blender_abc_export.py

Exits non-zero on failure so it can gate CI.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

import bpy
import numpy as np

from importer.scanner import SequenceScanner
from importer.cache_builder import CacheBuilder
from runtime.cache_reader import CacheReader
from runtime.mesh_update import CachePlayback, CHUNK_MAP_E180
from runtime import frame_handler

CHUNKED = "--individual" not in sys.argv

SEQ = os.path.join(REPO, "testglt")
cache_path = os.path.join(HERE, "_e2e.bvc")
abc_path = os.path.join(HERE, "_e2e.abc")


def fail(msg):
    print(f"[E2E] FAIL: {msg}")
    sys.exit(1)


bpy.ops.wm.read_factory_settings(use_empty=True)

# register the real add-on operators/props
import addon
addon.register()

# --- build cache ---
manifest = SequenceScanner(SEQ).scan()
CacheBuilder(SEQ, cache_path).build(manifest=manifest)

reader = CacheReader(cache_path)
n_frames = reader.frame_count
print(f"[E2E] frames={n_frames}")

# --- import (chunked = real workflow; individual = regression check) ---
print(f"[E2E] mode={'CHUNKED' if CHUNKED else 'INDIVIDUAL'}")
playback = CachePlayback(reader, chunk_map=CHUNK_MAP_E180 if CHUNKED else None)
playback.build_scene()
frame_handler.attach(playback, frame_start=1)
bpy.context.scene.frame_start = 1
bpy.context.scene.frame_end = n_frames
bpy.context.scene.frame_set(5)  # simulate user scrubbing before export
bpy.context.scene.beamng.cache_path = cache_path

# --- run the REAL operator (execute path, bypassing file dialog) ---
res = bpy.ops.beamng.export_alembic(filepath=abc_path)
print(f"[E2E] operator result: {res}")
if res != {"FINISHED"}:
    fail(f"operator did not finish: {res}")
reader.close()

if not os.path.exists(abc_path):
    fail("no .abc produced")

# --- re-import and verify animation ---
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.wm.alembic_import(filepath=abc_path, as_background_job=False)
scene = bpy.context.scene
mesh_objs = [o for o in bpy.data.objects if o.type == "MESH"]
with_cache = [o for o in mesh_objs
              if any(m.type == "MESH_SEQUENCE_CACHE" for m in o.modifiers)]
print(f"[E2E] imported meshes={len(mesh_objs)} with MeshSequenceCache={len(with_cache)}")

if len(mesh_objs) == 0:
    fail("ABC re-imported 0 meshes")
if len(with_cache) == 0:
    fail("no MeshSequenceCache modifiers — ABC has no animation channel")


def sample(obj, frame):
    scene.frame_set(frame)
    dg = bpy.context.evaluated_depsgraph_get()
    me = obj.evaluated_get(dg).data
    arr = np.empty(len(me.vertices) * 3, dtype=np.float32)
    me.vertices.foreach_get("co", arr)
    return arr.reshape(-1, 3)


probe = max(mesh_objs, key=lambda o: len(o.data.vertices))
p0 = sample(probe, scene.frame_start)
pN = sample(probe, scene.frame_end)
if p0.shape != pN.shape:
    fail(f"probe vertex count changed {p0.shape}->{pN.shape}")
diff = float(np.abs(pN - p0).max())
print(f"[E2E] probe={probe.name} frame {scene.frame_start} vs {scene.frame_end} max|Δ|={diff:.5f}")

if diff <= 1e-3:
    fail("ABC is static — no per-frame animation")

# cleanup
for p in (cache_path, abc_path):
    try:
        os.remove(p)
    except OSError:
        pass

print("[E2E] PASS: real operator produced an animated Alembic in chunked mode")
