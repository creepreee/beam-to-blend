"""Diagnose auto-smooth + cache animation — actually APPLY auto smooth."""

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
    print(f"[DIAG] {n} frames")

    stable = reader.stable_objects()
    probe_name = max(stable, key=lambda o: o.vertex_count).name
    obj = bpy.data.objects[probe_name]
    mesh = obj.data

    # --- Step 1: check what auto-smooth properties actually exist ---
    print(f"\n--- Step 1: Probing auto-smooth API on {probe_name!r} ---")
    for target, label in [(mesh, "mesh"), (obj, "obj")]:
        for prop in ["use_auto_smooth", "auto_smooth_angle"]:
            try:
                val = getattr(target, prop)
                print(f"  {label}.{prop} = {val!r}  (access OK)")
            except AttributeError as e:
                print(f"  {label}.{prop}: AttributeError")
            except Exception as e:
                print(f"  {label}.{prop}: {type(e).__name__}: {e}")

    # Check if blender_rna properties list includes these
    rna_props = {p.identifier for p in mesh.bl_rna.properties}
    for prop in ["use_auto_smooth", "auto_smooth_angle"]:
        print(f"  'mesh.{prop}' in bl_rna.properties = {prop in rna_props}")

    # --- Step 2: try to apply auto smooth through every available path ---
    print(f"\n--- Step 2: Attempting to apply auto smooth ---")

    applied = False

    # Path A: try setting mesh.use_auto_smooth
    try:
        if hasattr(mesh, "use_auto_smooth") or "use_auto_smooth" in {p.identifier for p in mesh.bl_rna.properties}:
            mesh.use_auto_smooth = True
            mesh.auto_smooth_angle = 0.5236  # 30°
            print(f"  [A] mesh.use_auto_smooth = True  -> now = {mesh.use_auto_smooth}")
            applied = True
    except Exception as e:
        print(f"  [A] failed: {e}")

    # Path B: try setting obj.use_auto_smooth (object proxy in some Blender versions)
    if not applied:
        try:
            if hasattr(obj, "use_auto_smooth"):
                obj.use_auto_smooth = True
                obj.auto_smooth_angle = 0.5236
                print(f"  [B] obj.use_auto_smooth = True  -> now = {obj.use_auto_smooth}")
                applied = True
        except Exception as e:
            print(f"  [B] failed: {e}")

    # Path C: try bpy.ops.object.shade_smooth with various args
    if not applied:
        try:
            bpy.context.view_layer.objects.active = obj
            obj.select_set(True)

            # Try different shade_smooth variants
            for args in [{}, {"use_auto_smooth": True}, {"angle": 0.5236}]:
                try:
                    bpy.ops.object.shade_smooth(**args)
                    print(f"  [C] shade_smooth({args})  succeeded")
                    applied = True
                    break
                except Exception as e:
                    print(f"  [C] shade_smooth({args})  failed: {str(e)[:80]}")

            obj.select_set(False)
        except Exception as e:
            print(f"  [C] setup failed: {e}")

    # Path D: use RNA to check/set the property
    if not applied:
        try:
            prop = mesh.bl_rna.properties.get("use_auto_smooth")
            if prop is not None:
                print(f"  [D] use_auto_smooth IN bl_rna.properties (type={prop.type})")
            else:
                print(f"  [D] use_auto_smooth NOT in bl_rna.properties")
                # Check what normals-related properties exist
                normals_props = [p.identifier for p in mesh.bl_rna.properties 
                                if 'smooth' in p.identifier.lower() or 'normal' in p.identifier.lower() or 'sharp' in p.identifier.lower() or 'auto' in p.identifier.lower()]
                print(f"  [D] related mesh bl_rna props: {normals_props}")
                obj_props = [p.identifier for p in obj.bl_rna.properties 
                            if 'smooth' in p.identifier.lower() or 'normal' in p.identifier.lower() or 'sharp' in p.identifier.lower() or 'auto' in p.identifier.lower()]
                print(f"  [D] related obj bl_rna props: {obj_props}")
        except Exception as e:
            print(f"  [D] failed: {e}")

    # Path E: check what properties the normals UI panel uses
    if not applied:
        try:
            # Maybe it's stored as an attribute on the mesh's ID properties
            id_props = dict(mesh.items()) if hasattr(mesh, "items") else {}
            print(f"  [E] mesh ID properties: {list(id_props.keys())[:10] if id_props else 'none'}")

            # Check for sharp_face / sharp_edge in attributes
            for attr_name in ["sharp_face", "sharp_edge"]:
                attr = mesh.attributes.get(attr_name)
                print(f"  [E] mesh.attributes[{attr_name!r}] = {attr}")
        except Exception as e:
            print(f"  [E] failed: {e}")

    print(f"\n  Auto smooth APPLIED = {applied}")

    # --- Step 3: if auto smooth was applied, test animation ---
    if applied:
        print(f"\n--- Step 3: Testing animation WITH auto smooth ---")

        # Check frame handler
        handler_active = any(
            getattr(h, "__name__", "") == "_on_frame_change"
            for h in bpy.app.handlers.frame_change_pre
        )
        print(f"  frame handler registered = {handler_active}")

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

        if moved < 0.00001 and expected > 0.01:
            print(f"  [FAIL] Animation stopped! Expected {expected:.6f} but got {moved:.6f}")
        elif moved > 0.00001:
            print(f"  [OK]   animation works with auto smooth")

        # Also check normals update
        bpy.context.scene.frame_set(1)
        na = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
        mesh.vertices.foreach_get("normal", na)
        bpy.context.scene.frame_set(mid)
        nb = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
        mesh.vertices.foreach_get("normal", nb)
        nd = float(np.abs(na - nb).max())
        print(f"  normals: max displacement = {nd:.6f}")
        if nd < 0.00001 and expected > 0.01:
            print(f"  [WARN] normals didn't update (stale)")

    else:
        print(f"\n--- Step 3: Could not apply auto smooth in headless mode ---")
        print(f"  Skipping animation test (auto smooth wasn't applied)")
        print(f"  The UI checkbox must use internal C code not accessible from Python.")

    frame_handler.detach()
    reader.close()
    print(f"\n[DIAG] done")


def _gltf_to_blender(pos: np.ndarray) -> np.ndarray:
    pos = pos.copy()
    y = pos[:, 1].copy()
    pos[:, 1] = -pos[:, 2]
    pos[:, 2] = y
    return pos


if __name__ == "__main__":
    main()
