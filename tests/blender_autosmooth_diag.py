"""Diagnose auto-smooth + cache animation interaction in Blender headless.

Builds cache from testglt/, imports, applies auto smooth to all objects,
then checks whether positions actually move between frames.
"""

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


def _gltf_to_blender(pos: np.ndarray) -> np.ndarray:
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

    stable = reader.stable_objects()
    dynamic = reader.dynamic_objects()
    n = reader.frame_count
    print(f"[DIAG] {n} frames, {len(stable)} stable, {len(dynamic)} dynamic")

    # --- Probe what auto-smooth properties exist -------------------------
    probe_obj = bpy.data.objects[stable[1].name]
    probe_mesh = probe_obj.data
    print(f"\n[DIAG] Probing auto-smooth API on {probe_obj.name!r}:")
    print(f"  hasattr(mesh, 'use_auto_smooth') = {hasattr(probe_mesh, 'use_auto_smooth')}")
    print(f"  hasattr(mesh, 'auto_smooth_angle') = {hasattr(probe_mesh, 'auto_smooth_angle')}")
    print(f"  hasattr(obj,  'use_auto_smooth') = {hasattr(probe_obj, 'use_auto_smooth')}")
    print(f"  hasattr(obj,  'auto_smooth_angle') = {hasattr(probe_obj, 'auto_smooth_angle')}")

    if hasattr(probe_obj, "use_auto_smooth"):
        print(f"  obj.use_auto_smooth = {probe_obj.use_auto_smooth}")
        print(f"  obj.auto_smooth_angle = {probe_obj.auto_smooth_angle}")

    # --- Apply auto smooth to ALL objects ---------------------------------
    print(f"\n[DIAG] Applying Shade Auto Smooth (30°) to all objects...")
    auto_count = 0
    for name, obj in list(playback._objects.items()) + list(playback._chunks.items()) + \
                          [(n, o) for n, o in playback._dynamic_objects.items()]:
        bpy.context.view_layer.objects.active = obj
        try:
            bpy.ops.object.shade_smooth(use_auto_smooth=True, angle=1.0472)  # 60°
            auto_count += 1
        except Exception as e:
            print(f"  [WARN] shade_smooth failed for {name}: {e}")

    print(f"[DIAG] Auto smooth applied to {auto_count} objects")

    # --- Verify positions move between frames -----------------------------
    print(f"\n[DIAG] Checking position movement (before auto-smooth fix)...")

    # Check the largest stable object
    probe = max(stable, key=lambda o: o.vertex_count).name if stable else None
    if probe is None:
        print("[FAIL] no stable objects to probe")
        sys.exit(1)

    obj = bpy.data.objects[probe]
    mesh = obj.data
    print(f"  probing: {probe!r} ({len(mesh.vertices)} verts)")

    # Read frame 0 positions
    bpy.context.scene.frame_set(1)
    a = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
    mesh.vertices.foreach_get("co", a)

    # Read frame mid positions
    mid = 1 + (n // 2)
    bpy.context.scene.frame_set(mid)
    b = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
    mesh.vertices.foreach_get("co", b)

    moved = float(np.abs(a - b).max())
    print(f"  frame 0 vs frame {n//2}: max displacement = {moved:.6f}")

    if moved < 0.00001:
        print(f"[FAIL] mesh did NOT move between frame 0 and {n//2}")
    else:
        print(f"[ok] mesh moved {moved:.6f}")

    # Check against cache reference
    expected_0 = _gltf_to_blender(reader.frame_positions(probe, 0))
    expected_mid = _gltf_to_blender(reader.frame_positions(probe, n // 2))
    ref_diff = float(np.abs(expected_0 - expected_mid).max())
    print(f"  cache reference displacement: {ref_diff:.6f}")

    # --- Check normals update between frames ------------------------------
    print(f"\n[DIAG] Checking normals...")

    bpy.context.scene.frame_set(1)
    normals_a = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
    mesh.vertices.foreach_get("normal", normals_a)

    bpy.context.scene.frame_set(mid)
    normals_b = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
    mesh.vertices.foreach_get("normal", normals_b)

    normal_diff = float(np.abs(normals_a - normals_b).max())
    print(f"  vertex normal displacement: {normal_diff:.6f}")

    if normal_diff < 0.00001 and ref_diff > 0.01:
        print(f"[WARN] positions moved {moved:.6f} but normals didn't change!")
    elif normal_diff > 0.00001:
        print(f"[ok] normals update with geometry")

    # --- Now test with toggle fix (simulate what _write_positions does) ---
    print(f"\n[DIAG] Testing auto-smooth toggle fix...")
    bpy.context.scene.frame_set(1)

    for name, obj in playback._objects.items():
        mesh = obj.data
        try:
            if hasattr(obj, "use_auto_smooth") and obj.use_auto_smooth:
                angle = obj.auto_smooth_angle if hasattr(obj, "auto_smooth_angle") else 1.0472
                obj.use_auto_smooth = False
                obj.use_auto_smooth = True
                if hasattr(obj, "auto_smooth_angle"):
                    obj.auto_smooth_angle = angle
            elif hasattr(mesh, "use_auto_smooth") and mesh.use_auto_smooth:
                angle = mesh.auto_smooth_angle if hasattr(mesh, "auto_smooth_angle") else 1.0472
                mesh.use_auto_smooth = False
                mesh.use_auto_smooth = True
                if hasattr(mesh, "auto_smooth_angle"):
                    mesh.auto_smooth_angle = angle
        except Exception as e:
            print(f"  [WARN] toggle failed for {name}: {e}")

    bpy.context.scene.frame_set(mid)
    c = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
    mesh.vertices.foreach_get("co", c)
    moved_after = float(np.abs(a - c).max())
    print(f"  after toggle fix: frame 0 vs {n//2}: max displacement = {moved_after:.6f}")

    if moved_after > 0.00001:
        print(f"[ok] positions still update after toggle fix")
    else:
        print(f"[FAIL] toggle fix broke position updates!")

    # Cleanup
    frame_handler.detach()
    reader.close()
    print(f"\n[DIAG] done")


if __name__ == "__main__":
    main()
