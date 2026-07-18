"""Diagnose: after shade_smooth(), then manually enable sharp_edge/sharp_face."""

import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import bpy
import numpy as np

from importer.cache_builder import CacheBuilder
from runtime.cache_reader import CacheReader
from runtime.mesh_update import CachePlayback
from runtime import frame_handler


def _gltf_to_blender(pos):
    pos = pos.copy()
    y = pos[:, 1].copy()
    pos[:, 1] = -pos[:, 2]
    pos[:, 2] = y
    return pos


def main():
    cache_path = os.path.join(_REPO, "testglt", "smoke.bvc")
    if not os.path.exists(cache_path):
        seq = os.path.join(_REPO, "testglt")
        print(f"[DIAG] building cache from {seq} -> {cache_path}")
        CacheBuilder(seq, cache_path).build()

    bpy.ops.wm.read_factory_settings(use_empty=True)
    reader = CacheReader(cache_path)
    playback = CachePlayback(reader)
    playback.build_scene()
    frame_handler.attach(playback, frame_start=1)

    n = reader.frame_count
    stable = reader.stable_objects()
    probe_name = max(stable, key=lambda o: o.vertex_count).name
    obj = bpy.data.objects[probe_name]
    mesh = obj.data

    print(f"[DIAG] probing {probe_name!r}, {len(mesh.vertices)} verts, {n} frames")

    # Step 1: list all mesh attributes that exist
    print("\n--- Mesh attributes ---")
    for a in mesh.attributes:
        print(f"  {a.name:30s} domain={a.domain}  type={a.data_type}")

    # Step 2: try setting sharp_face and sharp_edge manually
    print("\n--- Trying to enable sharp edges (simulating auto smooth) ---")
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)

    # Apply shade smooth first (clears any flat shading)
    bpy.ops.object.shade_smooth()

    # Now manually add sharp_edge attribute (this is what auto smooth does in 4.5)
    # First check if sharp_edge exists
    sharp_edge = mesh.attributes.get("sharp_edge")
    print(f"  sharp_edge attr exists before: {sharp_edge is not None}")

    if sharp_edge is None:
        # Create it - all False means no sharp edges (= smooth everywhere, no auto smooth effect)
        sharp_edge = mesh.attributes.new("sharp_edge", 'BOOLEAN', 'EDGE')
        print(f"  created sharp_edge attribute: {sharp_edge}")

    # Set some edges as sharp to simulate auto smooth with angle threshold
    # Use half the edges as sharp
    n_edges = len(mesh.edges)
    sharp_data = np.zeros(n_edges, dtype=bool)
    sharp_data[:n_edges // 3] = True  # mark 1/3 of edges as sharp
    sharp_edge.data.foreach_set("value", sharp_data)
    mesh.update()
    print(f"  set {sharp_data.sum()} / {n_edges} edges as sharp")

    # Also check if auto smooth flag was set by the operation
    # (it wasn't since we used Python API, not UI)
    
    # Step 3: test animation with sharp edges
    print("\n--- Testing animation WITH sharp edges (auto smooth simulation) ---")

    # Read frame 0
    bpy.context.scene.frame_set(1)
    a = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
    mesh.vertices.foreach_get("co", a)

    # Read frame mid
    mid = 1 + (n // 2)
    bpy.context.scene.frame_set(mid)
    b = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
    mesh.vertices.foreach_get("co", b)

    moved = float(np.abs(a - b).max())
    expected = float(np.abs(
        _gltf_to_blender(reader.frame_positions(probe_name, 0)) -
        _gltf_to_blender(reader.frame_positions(probe_name, n // 2))
    ).max())
    print(f"  frame 0 vs {n//2}: moved = {moved:.6f}  (expected ~{expected:.6f})")

    if moved > 0.00001:
        print(f"  [OK] animation works with sharp_edge attribute")
    else:
        print(f"  [FAIL] animation stopped!")

    # Step 4: check normals
    bpy.context.scene.frame_set(1)
    na = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
    mesh.vertices.foreach_get("normal", na)
    bpy.context.scene.frame_set(mid)
    nb = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
    mesh.vertices.foreach_get("normal", nb)
    nd = float(np.abs(na - nb).max())
    print(f"  normals: max displacement = {nd:.6f}")
    if nd > 0.00001:
        print(f"  [OK] normals update with sharp_edge")
    else:
        print(f"  [FAIL] normals didn't update")

    # Step 5: now try directly setting auto smooth via operator 
    # and check what it does
    print("\n--- Testing shade_smooth with angle parameter ---")
    bpy.ops.object.shade_smooth()
    
    # Try to create CD_CUSTOMLOOPNORMAL through operator
    # In 4.5.9, there's no auto smooth operator, but maybe there's
    # a way to create custom normals
    try:
        # Try using the mesh's custom split normals operator
        bpy.ops.mesh.customdata_custom_splitnormals_add()
        print("  [INFO] customdata_custom_splitnormals_add succeeded")
    except Exception as e:
        print(f"  [INFO] customdata_custom_splitnormals_add: {e}")

    # Check if CD_CUSTOMLOOPNORMAL appears via a new attribute
    print("\n  Attributes after custom split normals add:")
    for a in mesh.attributes:
        print(f"    {a.name:30s} domain={a.domain}  type={a.data_type}")

    # Test animation again
    bpy.context.scene.frame_set(1)
    a2 = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
    mesh.vertices.foreach_get("co", a2)
    bpy.context.scene.frame_set(mid)
    b2 = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
    mesh.vertices.foreach_get("co", b2)
    moved2 = float(np.abs(a2 - b2).max())
    print(f"\n  After customdata: moved = {moved2:.6f}")
    if moved2 > 0.00001:
        print(f"  [OK] animation still works")
    else:
        print(f"  [FAIL] animation stopped after customdata operation")

    frame_handler.detach()
    reader.close()
    print(f"\n[DIAG] done")


if __name__ == "__main__":
    main()
