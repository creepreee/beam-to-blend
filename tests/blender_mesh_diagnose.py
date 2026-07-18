from __future__ import annotations

"""Bare-bones mesh diagnostic — geometry only, no UVs/materials.

Usage:
    blender --background --python tests/blender_mesh_diagnose.py -- <cache.bvc> [max_frames]

Checks per object:
  - Vertex count matches
  - Face count matches
  - All face indices in bounds
  - No degenerate faces (duplicate vertex indices)
  - No collinear triangles (area2 < 1e-12)
  - mesh.validate() passes
  - Vertex positions match cache (round-trip)
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
    pos = pos.copy()
    y = pos[:, 1].copy()
    pos[:, 1] = -pos[:, 2]
    pos[:, 2] = y
    return pos


def main():
    args = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    if not args:
        print("Usage: blender --background --python tests/blender_mesh_diagnose.py -- <cache.bvc> [max_frames]")
        sys.exit(1)

    cache_path = os.path.abspath(args[0])
    max_frames = int(args[1]) if len(args) > 1 else 100

    if not os.path.exists(cache_path):
        print(f"ERROR: {cache_path} not found")
        sys.exit(1)

    diag_dir = os.path.dirname(cache_path)
    log_path = os.path.join(diag_dir, "mesh_diagnostic.log")
    blend_path = os.path.join(diag_dir, "mesh_diagnostic.blend")

    print("=" * 60)
    print("BARE-BONES MESH DIAGNOSTIC")
    print(f"Cache: {cache_path}")
    print(f"Max frames: {max_frames}")
    print("=" * 60)

    bpy.ops.wm.read_factory_settings(use_empty=True)

    reader = CacheReader(cache_path)
    n_frames = min(reader.frame_count, max_frames)
    stable = reader.stable_objects()
    dynamic = reader.dynamic_objects()

    print(f"\n{n_frames} frames, {len(stable)} stable, {len(dynamic)} dynamic\n")

    playback = CachePlayback(reader, log_path=log_path)
    playback.build_scene()
    frame_handler.attach(playback, frame_start=1)

    overall_pass = True

    for cobj in stable:
        name = cobj.name
        obj = bpy.data.objects.get(name)
        if obj is None:
            print(f"[SKIP] {name}: not found")
            continue

        mesh = obj.data
        passed = True

        # Vertex count
        v = len(mesh.vertices)
        if v != cobj.vertex_count:
            print(f"[FAIL] {name}: verts {v}/{cobj.vertex_count}")
            passed = False

        # Face count
        f = len(mesh.polygons)
        if f != cobj.face_count:
            print(f"[FAIL] {name}: faces {f}/{cobj.face_count}")
            passed = False

        # Index bounds
        bad_idx = 0
        for p in mesh.polygons:
            for vi in p.vertices:
                if vi < 0 or vi >= v:
                    bad_idx += 1
        if bad_idx:
            print(f"[FAIL] {name}: {bad_idx} OOB indices")
            passed = False

        # Degenerate faces
        n_degen = 0
        for p in mesh.polygons:
            if len(set(p.vertices)) < 3:
                n_degen += 1
        if n_degen:
            print(f"[FAIL] {name}: {n_degen} degenerate faces")
            passed = False

        # Collinear triangles in Blender
        co = np.empty(v * 3, dtype=np.float32)
        mesh.vertices.foreach_get("co", co)
        co = co.reshape(-1, 3)
        n_collin = 0
        for p in mesh.polygons:
            vi = list(p.vertices[:3])
            v0, v1, v2 = co[vi]
            area2 = np.dot(np.cross(v1 - v0, v2 - v0), np.cross(v1 - v0, v2 - v0))
            if area2 < 1e-12:
                n_collin += 1
        if n_collin:
            print(f"[FAIL] {name}: {n_collin} collinear tris")
            passed = False

        # validate()
        if mesh.validate(verbose=False):
            print(f"[FAIL] {name}: validate() returned errors")
            passed = False

        # Position round-trip
        expected = _gltf_to_blender(reader.frame_positions(name, 0))
        diff = np.abs(co - expected).max()
        if diff > 1e-5:
            print(f"[FAIL] {name}: positions mismatch max_diff={diff:.8f}")
            passed = False

        if passed:
            print(f"[PASS] {name}: v={v} f={f}")

    # One animation check
    print()
    if stable:
        largest = max(stable, key=lambda o: o.vertex_count)
        obj = bpy.data.objects.get(largest.name)
        last = n_frames - 1
        bpy.context.scene.frame_set(1 + last)
        co0 = np.empty(len(obj.data.vertices) * 3, dtype=np.float32)
        obj.data.vertices.foreach_get("co", co0)
        expected = _gltf_to_blender(reader.frame_positions(largest.name, last))
        diff = np.abs(co0.reshape(-1, 3) - expected).max()
        print(f"Animation {largest.name} f0->f{last}: max_diff={diff:.8f}")
        if diff < 0.001:
            print("  *** WARNING: barely moved — animation may be stuck")

    print(f"\nSaving: {blend_path}")
    bpy.ops.wm.save_as_mainfile(filepath=blend_path)
    reader.close()
    sys.exit(0 if overall_pass else 1)


if __name__ == "__main__":
    main()
