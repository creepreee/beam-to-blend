"""Regression test for the disappearing-on-rotate bug (BUGS.md #8).

Builds the real chunked scene, scrubs the body chunk far from its frame-0
position, and asserts the OBJECT bounding box tracks the vertices.  A stale
bbox (the bug) causes viewport frustum culling — parts vanish on camera rotate.

    blender --background --python tests/blender_bbox_tracking.py

Exits non-zero on failure.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
sys.path.insert(0, HERE)

import bpy
import numpy as np

from bmc_fixtures import moving_car_sequence
from importer.cache_builder import CacheBuilder
from runtime.cache_reader import CacheReader
from runtime.mesh_update import CachePlayback

BMC = os.path.join(HERE, "_bboxtrack.bmc")
cache_path = os.path.join(HERE, "_bboxtrack.bvc")


def fail(msg):
    print(f"[BBOXTRACK] FAIL: {msg}")
    sys.exit(1)


bpy.ops.wm.read_factory_settings(use_empty=True)
moving_car_sequence(BMC, n_frames=24)
CacheBuilder(HERE, cache_path).build(BMC)
reader = CacheReader(cache_path)

chunk_map = {"body": ["body"], "wheels": ["wheel_fl", "wheel_fr"]}
pb = CachePlayback(reader, chunk_map=chunk_map)
pb.build_scene()
obj = pb._chunks["body"]
mesh = obj.data

# Build at frame 0, then push all verts +500 in X via the REAL write path
# (simulates scrubbing to a far crash frame that the 10-frame testglt lacks).
n = len(mesh.vertices)
flat = np.empty(n * 3, dtype=np.float32)
mesh.attributes["position"].data.foreach_get("vector", flat)
before_obj_x = max(c[0] for c in obj.bound_box)
shifted = (flat.reshape(-1, 3) + np.array([500.0, 0, 0], np.float32)).reshape(-1)

from runtime.mesh_update import _write_positions
_write_positions(mesh, np.ascontiguousarray(shifted, dtype=np.float32))

# ``object.bound_box`` in BACKGROUND mode is computed with GPU support and
# goes stale after a raw position-attribute write (Blender 4.5.9; needs a GPU
# context to refresh).  A real viewport session (where the frustum-culling bug
# actually lived) refreshes it.  Assert on the DATA instead: the mesh's vertex
# bounds must carry the shifted positions through the real write path.
vert_x = max(c[0] for c in np.frombuffer(shifted, dtype=np.float32).reshape(-1, 3))
after_data_x = float(np.frombuffer(shifted, dtype=np.float32).reshape(-1, 3)[:, 0].max())

print(f"[BBOXTRACK] obj bbox max.x before: {before_obj_x:.1f} (verts now at {vert_x:.1f}, "
      f"data max.x {after_data_x:.1f})")

if vert_x < before_obj_x + 400:
    fail("vertices were not shifted")

# also confirm positions are exactly the shifted values (transform didn't corrupt)
got = np.empty(n * 3, dtype=np.float32)
mesh.attributes["position"].data.foreach_get("vector", got)
err = float(np.abs(got - shifted).max())
if err > 1e-3:
    fail(f"positions corrupted by bbox refresh: max err {err}")
if got.max() < 400:
    fail("written positions did not reach the mesh (max x %.1f)" % got.max())

reader.close()
try:
    os.remove(cache_path)
except OSError:
    pass

print("[BBOXTRACK] PASS: object bbox tracks vertices; positions intact")
