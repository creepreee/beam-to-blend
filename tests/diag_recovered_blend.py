"""Verify the 'vehicle materials ready made' blend plays the rebuilt cache.

Opens the blend, enables the installed addon, runs frame_handler recovery
against the rebuilt BVC, then confirms the merged glass chunk mesh actually
updates between frames (proof the cache vertex counts line up with the scene).

    blender --background "Downloads/vehicle materials ready made.blend" \\
        --python tests/diag_recovered_blend.py
"""
import sys

import bpy

BLEND = r"C:\Users\ubaid_i2c\Downloads\vehicle materials ready made.blend"


def main():
    for _mod in [m for m in list(sys.modules)
                 if m == "beamng_cache_importer"
                 or m.startswith("beamng_cache_importer.")
                 or m == "runtime" or m.startswith("runtime.")
                 or m == "addon" or m.startswith("addon.")]:
        del sys.modules[_mod]

    bpy.ops.wm.open_mainfile(filepath=BLEND)
    res = bpy.ops.preferences.addon_enable(module="beamng_cache_importer")
    print(f"[DIAG] addon_enable -> {res}", flush=True)

    scene = bpy.context.scene
    print(f"[DIAG] cache_path={scene.get('_beamng_cache_path')}", flush=True)
    print(f"[DIAG] use_chunked={scene.get('_beamng_use_chunked')} "
          f"start={scene.get('_beamng_start_frame')}", flush=True)

    from runtime import frame_handler
    ok = frame_handler._try_recover(scene)
    print(f"[DIAG] _try_recover -> {ok}", flush=True)
    if not ok:
        sys.exit(1)
    print(f"[DIAG] _active = {frame_handler._active is not None}", flush=True)

    chunks = {k: v.name for k, v in getattr(frame_handler._active, "_chunks", {}).items()}
    print(f"[DIAG] chunks = {chunks}", flush=True)

    glass = bpy.data.objects.get("glass")
    print(f"[DIAG] glass obj present = {glass is not None}", flush=True)
    if glass is None:
        sys.exit(1)

    def snapshot(obj):
        import numpy as np
        pts = np.empty(len(obj.data.vertices) * 3, dtype=np.float32)
        obj.data.vertices.foreach_get("co", pts)
        return pts

    ref = None
    moved = 0
    n_handlers = [h.__name__ for h in bpy.app.handlers.frame_change_pre]
    print(f"[DIAG] frame_change_pre handlers: {n_handlers}", flush=True)
    print(f"[DIAG] _skip_n = {frame_handler._skip_n}", flush=True)
    for f in (scene.frame_start, 500, 640, 700):
        scene.frame_current = f
        cframe = frame_handler._cache_frame_for(f)
        frame_handler._on_frame_change(scene)
        bpy.context.view_layer.update()
        pts = snapshot(glass)
        if ref is None:
            ref = pts
            print(f"[DIAG] frame {f}: cache={cframe} glass verts="
                  f"{len(glass.data.vertices)}", flush=True)
        else:
            import numpy as np
            d = float(np.abs(pts - ref).max())
            moved += d > 1e-4
            print(f"[DIAG] frame {f}: cache={cframe} max delta from frame "
                  f"{scene.frame_start} = {d:.4f}", flush=True)

    print(f"[DIAG] glass mesh updated in {moved}/3 later frames", flush=True)
    if moved == 0:
        sys.exit(1)
    print("[DIAG][PASS] blend plays the rebuilt cache", flush=True)


if __name__ == "__main__":
    main()
