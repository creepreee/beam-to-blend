from __future__ import annotations

"""Blender diagnostic — imports a BVC and reports EXACT vertex positions
vs what the cache reader says they should be.

Usage:
    blender --background --python tests/blender_diagnose.py -- <cache.bvc>

Output:
    - Per-object vertex comparison at frame 0 and last frame
    - Overall pass/fail per object
    - Saves diagnostic.blend in the same directory as the cache
"""

import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import bpy
import numpy as np

from runtime.cache_reader import CacheReader
from runtime.mesh_update import CachePlayback
from runtime import frame_handler


def _gltf_to_blender(pos: np.ndarray) -> np.ndarray:
    """Convert Y-up glTF coords to Blender Z-up (same as mesh_update.py)."""
    pos = pos.copy()
    y = pos[:, 1].copy()
    pos[:, 1] = -pos[:, 2]
    pos[:, 2] = y
    return pos


def main():
    args = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    if not args:
        print("Usage: blender --background --python tests/blender_diagnose.py -- <cache.bvc>")
        sys.exit(1)

    cache_path = os.path.abspath(args[0])
    if not os.path.exists(cache_path):
        print(f"ERROR: {cache_path} not found")
        sys.exit(1)

    diag_dir = os.path.dirname(cache_path)
    log_path = os.path.join(diag_dir, "blender_diagnostic.log")
    blend_path = os.path.join(diag_dir, "diagnostic.blend")

    print("=" * 72)
    print(f"BLENDER DIAGNOSTIC")
    print(f"Cache: {cache_path}")
    print(f"Log:   {log_path}")
    print("=" * 72)

    # Setup Blender
    bpy.ops.wm.read_factory_settings(use_empty=True)
    meshes_before = len(bpy.data.meshes)

    # Load cache
    reader = CacheReader(cache_path)
    n_frames = reader.frame_count
    stable = reader.stable_objects()
    dynamic = reader.dynamic_objects()

    print(f"\nCache: {n_frames} frames, {len(stable)} stable, {len(dynamic)} dynamic")

    # Build scene
    playback = CachePlayback(reader, log_path=log_path)
    playback.build_scene()
    frame_handler.attach(playback, frame_start=1)

    created = len(bpy.data.meshes) - meshes_before
    print(f"Created {created} mesh datablocks")
    print()

    all_pass = True

    # --- VERIFY FRAME 0 ---
    print("=" * 72)
    print("FRAME 0 — CREATION VERIFICATION")
    print("=" * 72)

    for cobj in stable:
        name = cobj.name
        obj = bpy.data.objects.get(name)
        if obj is None:
            print(f"[FAIL] {name}: object not found in scene")
            all_pass = False
            continue

        mesh = obj.data
        n_blender = len(mesh.vertices)
        if n_blender != cobj.vertex_count:
            print(f"[FAIL] {name}: expected {cobj.vertex_count} verts, Blender has {n_blender}")
            all_pass = False
            continue

        # Get Blender vertices
        got = np.empty(n_blender * 3, dtype=np.float32)
        mesh.vertices.foreach_get("co", got)
        got = got.reshape(-1, 3)

        # Get expected from cache (via _gltf_to_blender)
        expected = _gltf_to_blender(reader.frame_positions(name, 0))

        # Compare
        diff = np.abs(got - expected)
        max_diff = diff.max()
        mean_diff = diff.mean()
        n_mismatch = int((diff > 1e-5).sum())

        if max_diff > 1e-5:
            print(f"[FAIL] {name}: max_diff={max_diff:.6f}  mean_diff={mean_diff:.6f}  mismatched_verts={n_mismatch}/{n_blender}")
            # Show sample of mismatched vertices
            bad = np.where(diff.max(axis=1) > 1e-5)[0][:5]
            for bi in bad:
                print(f"       vert[{bi}]: got=({got[bi,0]:.4f},{got[bi,1]:.4f},{got[bi,2]:.4f})  exp=({expected[bi,0]:.4f},{expected[bi,1]:.4f},{expected[bi,2]:.4f})  diff={diff[bi]}")
            all_pass = False
        else:
            print(f"[PASS] {name}: {n_blender} verts match cache (max_diff={max_diff:.8f})")

    # --- VERIFY LAST FRAME ---
    last = n_frames - 1
    print()
    print("=" * 72)
    print(f"FRAME {last} — POSITION VERIFICATION")
    print("=" * 72)

    bpy.context.scene.frame_set(1 + last)

    for cobj in stable:
        name = cobj.name
        obj = bpy.data.objects.get(name)
        if obj is None:
            continue

        mesh = obj.data
        n_blender = len(mesh.vertices)
        if n_blender == 0:
            continue

        got = np.empty(n_blender * 3, dtype=np.float32)
        mesh.vertices.foreach_get("co", got)
        got = got.reshape(-1, 3)

        expected = _gltf_to_blender(reader.frame_positions(name, last))

        diff = np.abs(got - expected)
        max_diff = diff.max()

        if max_diff > 1e-5:
            print(f"[FAIL] {name}: frame {last} max_diff={max_diff:.6f}")
            all_pass = False
        else:
            print(f"[PASS] {name}: frame {last} matches (max_diff={max_diff:.8f})")

    # --- BOUNDING BOX CHECK ---
    print()
    print("=" * 72)
    print("BOUNDING BOX CHECK (frame 0)")
    print("=" * 72)

    all_mins = []
    all_maxs = []
    for cobj in stable:
        name = cobj.name
        obj = bpy.data.objects.get(name)
        if obj is None or len(obj.data.vertices) == 0:
            continue
        got = np.empty(len(obj.data.vertices) * 3, dtype=np.float32)
        obj.data.vertices.foreach_get("co", got)
        got = got.reshape(-1, 3)
        mn = got.min(axis=0)
        mx = got.max(axis=0)
        all_mins.append(mn)
        all_maxs.append(mx)
        print(f"  {name:42s}  x=[{mn[0]:8.3f},{mx[0]:8.3f}]  y=[{mn[1]:8.3f},{mx[1]:8.3f}]  z=[{mn[2]:8.3f},{mx[2]:8.3f}]")

    if all_mins:
        overall_min = np.min(all_mins, axis=0)
        overall_max = np.max(all_maxs, axis=0)
        print()
        print(f"  ** OVERALL BOUNDS **")
        print(f"     x=[{overall_min[0]:.3f}, {overall_max[0]:.3f}]  width={overall_max[0]-overall_min[0]:.3f}")
        print(f"     y=[{overall_min[1]:.3f}, {overall_max[1]:.3f}]  depth={overall_max[1]-overall_min[1]:.3f}")
        print(f"     z=[{overall_min[2]:.3f}, {overall_max[2]:.3f}]  height={overall_max[2]-overall_min[2]:.3f}")
        print(f"     Size: {overall_max[0]-overall_min[0]:.1f} x {overall_max[1]-overall_min[1]:.1f} x {overall_max[2]-overall_min[2]:.1f}")

    # --- FRAME HANDLER TEST ---
    print()
    print("=" * 72)
    print("FRAME HANDLER TEST")
    print("=" * 72)

    # Pick largest object to test
    largest = max(stable, key=lambda o: o.vertex_count)
    name = largest.name
    obj = bpy.data.objects.get(name)

    bpy.context.scene.frame_set(1)
    got0 = np.empty(len(obj.data.vertices) * 3, dtype=np.float32)
    obj.data.vertices.foreach_get("co", got0)

    bpy.context.scene.frame_set(1 + last)
    gotN = np.empty(len(obj.data.vertices) * 3, dtype=np.float32)
    obj.data.vertices.foreach_get("co", gotN)

    displacement = float(np.abs(gotN - got0).max())
    print(f"  {name}: max displacement frame 0 → {last} = {displacement:.6f}")
    if displacement < 0.001:
        print(f"  *** WARNING: {name} barely moved between frames 0 and {last}!")
        print(f"      The animation may be stuck.")
    else:
        print(f"  Animation appears active (displacement {displacement:.4f})")

    # Save diagnostic blend
    print()
    print(f"Saving diagnostic blend to: {blend_path}")
    bpy.ops.wm.save_as_mainfile(filepath=blend_path)

    reader.close()

    print()
    print("=" * 72)
    if all_pass:
        print("DIAGNOSTIC: ALL CHECKS PASSED")
    else:
        print("DIAGNOSTIC: SOME CHECKS FAILED — see above for details")
    print("=" * 72)

    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    main()
