"""Compare our read_glb() with Blender's native glTF importer on the same GLB.

Run: blender --background --python tests/compare_gltf_import.py
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))

import bpy
import numpy as np

GLB_FILE = REPO / "testglt" / "export_frame_01816.glb"


def _read_our(path: Path):
    from importer.gltf_reader import read_glb
    doc = read_glb(path)
    return {o.name: o.positions.copy() for o in doc.objects}


def _bounds(arr, label=""):
    if len(arr) == 0:
        return f"{label:10s}  (empty)"
    mn = arr.min(axis=0)
    mx = arr.max(axis=0)
    return (f"{label:10s}  "
            f"x=[{mn[0]:10.4f},{mx[0]:10.4f}]  "
            f"y=[{mn[1]:10.4f},{mx[1]:10.4f}]  "
            f"z=[{mn[2]:10.4f},{mx[2]:10.4f}]")


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)

    # --- Import with Blender's native glTF importer ---
    bpy.ops.import_scene.gltf(filepath=str(GLB_FILE))
    print(f"\n=== Blender native import: {GLB_FILE.name} ===")

    # Collect Blender objects (only those with mesh data)
    blender_objs = {}
    for ob in bpy.data.objects:
        if ob.type == 'MESH':
            mesh = ob.data
            # Local-space vertices (as stored in the mesh)
            local_verts = np.empty((len(mesh.vertices), 3), dtype=np.float32)
            mesh.vertices.foreach_get("co", local_verts.ravel())

            # World-space vertices: local_verts transformed by object matrix
            mat = np.array(ob.matrix_world, dtype=np.float64)
            xyz1 = np.ones((local_verts.shape[0], 4), dtype=np.float64)
            xyz1[:, :3] = local_verts
            world_verts = (mat @ xyz1.T).T[:, :3].astype(np.float32)

            blender_objs[ob.name] = {
                "location": np.array(ob.location),
                "local": local_verts,
                "world": world_verts,
            }
            print(f"\n  Object: {ob.name}")
            print(f"    location: {tuple(ob.location)}")
            print(f"    local:    {_bounds(local_verts, 'local')}")
            print(f"    world:    {_bounds(world_verts, 'world')}")

    # --- Read with our importer ---
    our = _read_our(GLB_FILE)
    print(f"\n=== Our read_glb() ===")
    for name, pos in sorted(our.items()):
        print(f"\n  Object: {name}")
        print(f"    our:     {_bounds(pos, 'our')}")
        if name in blender_objs:
            w = blender_objs[name]["world"]
            l = blender_objs[name]["local"]
            # Compare our positions (baked world) with Blender's world
            if pos.shape == w.shape:
                diff = float(np.abs(pos - w).max())
                print(f"    diff_vs_blender_world: {diff:.6f}")
            # Also compare our positions vs Blender's local (they may differ)
            if pos.shape == l.shape:
                diff_local = float(np.abs(pos - l).max())
                print(f"    diff_vs_blender_local: {diff_local:.6f}")
        else:
            print(f"    (not found in Blender)")

    # Also report objects in Blender not found in ours
    for name in blender_objs:
        if name not in our:
            print(f"\n  Object in Blender only: {name}")

    print("\n=== Done ===")


if __name__ == "__main__":
    main()
