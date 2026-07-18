"""Import capture-built BVC into Blender and verify positions."""
from __future__ import annotations
import os, sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

bvc_path = os.path.join(_REPO, "capture_latest", "mycap_final.bvc")
assert os.path.exists(bvc_path), f"BVC not found: {bvc_path}"

import bpy
import numpy as np
from runtime.cache_reader import CacheReader
from runtime.mesh_update import CachePlayback
from runtime import frame_handler

def _gltf_to_blender(pos: np.ndarray) -> np.ndarray:
    """glTF Y-up to Blender Z-up: (X, Y, Z) -> (X, -Z, Y)"""
    pos = pos.copy()
    y = pos[:, 1].copy()
    pos[:, 1] = -pos[:, 2]
    pos[:, 2] = y
    return pos

bpy.ops.wm.read_factory_settings()

cr = CacheReader(bvc_path)
stable_list = cr.stable_objects()
dynamic_list = cr.dynamic_objects()
all_objects = stable_list + dynamic_list
object_count = len(all_objects)
frame_count = cr.frame_count
total_verts = sum(o.vertex_count for o in all_objects)
print(f"Objects: {object_count} ({len(stable_list)} stable, {len(dynamic_list)} dynamic), Frames: {frame_count}")
print(f"Total verts: {total_verts}")

playback = CachePlayback(cr)
playback.build_scene()
bpy.context.view_layer.update()

datablocks = len(bpy.data.meshes)
objects = [o for o in bpy.data.objects if o.type == 'MESH']
print(f"Mesh datablocks: {datablocks}, Mesh objects: {len(objects)}")

total_blender_verts = sum(len(o.data.vertices) for o in objects)
print(f"Cache verts: {total_verts}, Blender verts: {total_blender_verts}")
assert abs(total_blender_verts - total_verts) / max(total_verts, 1) < 0.05, \
    f"Vertex count mismatch: cache={total_verts} blender={total_blender_verts}"

test_frames = [0, 325, 649]
max_diffs = {}

frame_handler.attach(playback, frame_start=0)

for frame in test_frames:
    playback.set_frame(frame)
    bpy.context.scene.frame_set(frame)
    bpy.context.view_layer.update()

    worst_diff = 0.0
    worst_name = ""
    tested = 0
    for obj in objects:
        name = obj.name
        try:
            raw = cr.frame_positions(name, frame)
            cached = _gltf_to_blender(raw)
        except (KeyError, ValueError):
            continue
        me = obj.data
        co = np.empty(len(me.vertices) * 3, dtype=np.float32)
        me.vertices.foreach_get("co", co)
        co = co.reshape(-1, 3)
        n = min(len(cached), len(co))
        if n == 0:
            continue
        diff = float(np.abs(co[:n] - cached[:n]).max())
        if diff > worst_diff:
            worst_diff = diff
            worst_name = name
        tested += 1

    max_diffs[frame] = (worst_diff, worst_name, tested)
    print(f"Frame {frame}: max diff={worst_diff:.8f} ({worst_name}), tested {tested} objects")

assert max_diffs[0][0] < 0.001, f"Frame 0 diff too large: {max_diffs[0][0]}"
assert max_diffs[325][0] < 0.001, f"Frame 325 diff too large: {max_diffs[325][0]}"
assert max_diffs[649][0] < 0.001, f"Frame 649 diff too large: {max_diffs[649][0]}"

# Verify animation exists by comparing raw cache positions at frame 0 vs 649
obj_for_anim = objects[0].name
try:
    p0 = cr.frame_positions(obj_for_anim, 0)
    pN = cr.frame_positions(obj_for_anim, 649)
    anim_max = float(np.abs(pN - p0).max())
    print(f"Animation check ({obj_for_anim}): frame 0 vs 649 max delta = {anim_max:.6f}")
    assert anim_max > 0.0, f"No animation detected in cache (frame 0 vs 649 = 0.0)"
except (KeyError, ValueError):
    print("(skipping animation check for first object)")

print(f"\nAll PASSED. Datablocks: {datablocks}")
bpy.ops.wm.save_mainfile(filepath=bvc_path.replace('.bvc', '_blend_capture.blend'))
print(f"Saved .blend")

frame_handler.detach()
cr.close()
