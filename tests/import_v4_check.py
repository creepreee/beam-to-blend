"""Import capture2_v4.bvc into Blender headless and save .blend for inspection.

Uses the real CachePlayback runtime — same code the addon uses.
"""
import os, sys

import bpy

_REPO = r'C:\Users\ubaid_i2c\Downloads\beamng-cache-importer'
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from runtime.cache_reader import CacheReader
from runtime.mesh_update import CachePlayback
from runtime import frame_handler

BVC = r'C:\Users\ubaid_i2c\AppData\Local\BeamNG\BeamNG.drive\current\captures\mycap2\capture2_v4.bvc'
OUT = r'C:\Users\ubaid_i2c\Downloads\beamng-cache-importer\test_import_v4.blend'

cr = CacheReader(BVC)
print(f"Cache: {len(cr.object_names())} objects, {cr.frame_count} frames")

playback = CachePlayback(cr)
playback.create_meshes()

# Print some diagnostic info
for obj in bpy.data.objects:
    if obj.type == 'MESH':
        mesh = obj.data
        bbox = [obj.matrix_world @ bpy.mathutils.Vector(corner) for corner in obj.bound_box]
        xs = [v.x for v in bbox]
        ys = [v.y for v in bbox]
        zs = [v.z for v in bbox]
        if 'steer' in obj.name.lower() or 'body' == obj.name.lower() or 'dash' in obj.name.lower():
            print(f"{obj.name}: verts={len(mesh.vertices)} X=[{min(xs):.3f},{max(xs):.3f}] Y=[{min(ys):.3f},{max(ys):.3f}] Z=[{min(zs):.3f},{max(zs):.3f}]")

# Set timeline to frame 1
bpy.context.scene.frame_set(1)

# Save
bpy.ops.wm.save_as_mainfile(filepath=OUT)
print(f"Saved: {OUT}")

playback.close()
cr.close()
