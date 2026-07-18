"""Quick check: import animation cache and print body centroid."""
from __future__ import annotations
import sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))

import bpy
import numpy as np

CACHE = REPO / "animation" / "animation.bvc"
bpy.ops.wm.read_factory_settings(use_empty=True)

from runtime.mesh_update import CachePlayback
from runtime.cache_reader import CacheReader
from runtime.frame_handler import detach

reader = CacheReader(CACHE)
playback = CachePlayback(reader)
playback.build_scene()

objs = [ob for ob in bpy.data.objects if ob.type == 'MESH']
print(f"[TEST] {len(objs)} mesh objects, {len(bpy.data.meshes)} datablocks")

for fi in [0, 5, 9]:
    bpy.context.scene.frame_set(fi)
    playback.set_frame(fi)
    ob = bpy.data.objects.get("flanje_e180_body")
    if ob:
        verts = np.empty((len(ob.data.vertices), 3), dtype=np.float32)
        ob.data.vertices.foreach_get("co", verts.ravel())
        mn, mx = verts.min(axis=0), verts.max(axis=0)
        centroid = verts.mean(axis=0)
        print(f"  frame {fi}: body centroid=({centroid[0]:.2f},{centroid[1]:.2f},{centroid[2]:.2f})"
              f"  Z limits=[{mn[2]:.2f},{mx[2]:.2f}]")

detach()
reader.close()
print("[TEST][PASS]")
