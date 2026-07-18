"""Headless Blender smoke test for the cache runtime.

Run with:
    blender --background --python tests/blender_smoke.py -- <cache.bvc>

If no cache path is given it builds one from ``testglt/`` next to the repo.
Verifies:
  * one mesh datablock per stable+dynamic object (NO per-frame explosion),
  * mesh vertex coords equal the cache positions at several frames,
  * dynamic object topology rebuilds when vertex count changes,
  * the frame-change handler updates meshes when the timeline moves.
Exits non-zero on any failure so it can gate CI / manual runs.
"""

import os
import sys

import bpy

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import numpy as np

from importer.cache_builder import CacheBuilder
from runtime.cache_reader import CacheReader
from runtime.mesh_update import CachePlayback
from runtime import frame_handler


def _argv_after_ddash():
    return sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []


def _fail(msg):
    print(f"[SMOKE][FAIL] {msg}")
    sys.exit(1)


def _ok(msg):
    print(f"[SMOKE][ok] {msg}")


def _gltf_to_blender(pos: np.ndarray) -> np.ndarray:
    """No-op — BVC positions are already in Blender Z-up space.

    The builder converts at write time (cache_builder.py _gltf_to_blender).
    The runtime's _gltf_to_blender is also a no-op.
    """
    return pos


def _verify_stable_object(reader, playback, probe, n):
    check_frames = sorted({0, n // 2, n - 1})
    for cf in check_frames:
        bpy.context.scene.frame_set(1 + cf)
        obj = bpy.data.objects[probe]
        got = np.empty(obj.data.vertices.__len__() * 3, dtype=np.float32)
        obj.data.vertices.foreach_get("co", got)
        got = got.reshape(-1, 3)
        expected = _gltf_to_blender(reader.frame_positions(probe, cf))
        if not np.allclose(got, expected, atol=1e-5):
            maxd = float(np.abs(got - expected).max())
            _fail(f"{probe} frame {cf}: mesh != cache (max diff {maxd})")
        _ok(f"frame {cf}: {probe} vertices match cache")


def _verify_dynamic_objects(reader, n):
    for cobj in reader.dynamic_objects():
        name = cobj.name
        check_frames = sorted({0, n // 2, n - 1})
        for cf in check_frames:
            bpy.context.scene.frame_set(1 + cf)
            exp_pos, exp_idx = reader.frame_dynamic_geometry(name, cf)
            obj = bpy.data.objects.get(name)
            if obj is None:
                _fail(f"dynamic object {name!r} missing from scene")
            if len(exp_pos) == 0:
                if not obj.hide_viewport:
                    _fail(f"{name} frame {cf}: should be hidden")
                _ok(f"frame {cf}: {name} hidden (absent from this frame)")
                continue
            got = np.empty(obj.data.vertices.__len__() * 3, dtype=np.float32)
            obj.data.vertices.foreach_get("co", got)
            got = got.reshape(-1, 3)
            exp_pos_bl = _gltf_to_blender(exp_pos)
            if not np.allclose(got, exp_pos_bl, atol=1e-5):
                maxd = float(np.abs(got - exp_pos_bl).max())
                _fail(f"{name} frame {cf}: mesh != cache (max diff {maxd})")
            _ok(f"frame {cf}: {name} vertices match cache")


def main():
    args = _argv_after_ddash()
    cache_path = args[0] if args else os.path.join(_REPO, "testglt", "smoke.bvc")

    if not os.path.exists(cache_path):
        seq = os.path.join(_REPO, "testglt")
        print(f"[SMOKE] building cache from {seq} -> {cache_path}")
        CacheBuilder(seq, cache_path).build()

    bpy.ops.wm.read_factory_settings(use_empty=True)
    meshes_before = len(bpy.data.meshes)

    reader = CacheReader(cache_path)
    playback = CachePlayback(reader)
    playback.build_scene()
    frame_handler.attach(playback, frame_start=1)

    stable = reader.stable_objects()
    n_stable = len(stable)
    dynamic = reader.dynamic_objects()
    n_dynamic = len(dynamic)
    total_objects = n_stable + n_dynamic
    _ok(f"cache: {reader.frame_count} frames, {n_stable} stable, {n_dynamic} dynamic")

    # One mesh datablock per object — no per-frame explosion.
    created = len(bpy.data.meshes) - meshes_before
    if created != total_objects:
        _fail(f"expected {total_objects} mesh datablocks, got {created}")
    _ok(f"{created} mesh datablocks created ({total_objects} objects, no per-frame explosion)")

    n = reader.frame_count
    probe = max(stable, key=lambda o: o.vertex_count).name if stable else None

    if probe is not None:
        _verify_stable_object(reader, playback, probe, n)

    if n_dynamic > 0:
        _verify_dynamic_objects(reader, n)

    if probe is not None:
        obj = bpy.data.objects[probe]
        bpy.context.scene.frame_set(1)
        a = np.empty(len(obj.data.vertices) * 3, dtype=np.float32)
        obj.data.vertices.foreach_get("co", a)
        bpy.context.scene.frame_set(1 + (n - 1))
        b = np.empty(len(obj.data.vertices) * 3, dtype=np.float32)
        obj.data.vertices.foreach_get("co", b)
        moved = float(np.abs(a - b).max())
        _ok(f"{probe} moved {moved:.4f} between frame 0 and {n-1}")

    frame_handler.detach()
    reader.close()
    print("[SMOKE][PASS] all checks passed")


if __name__ == "__main__":
    main()
