"""Diag — is the imported scene actually chunked, and does _find_pane_target
see the chunk member faces?

    blender --background --python tests/diag_chunk_state.py
"""
import os
import sys

import bpy

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ZIP = os.path.join(_REPO, "dist", "beamng_cache_importer.zip")
_CACHE = os.environ.get(
    "BEAMNG_CACHE",
    r"C:\Users\ubaid_i2c\AppData\Local\BeamNG\BeamNG.drive\current\captures\name.bvc",
)
_MODULE = "beamng_cache_importer"


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    for _mod in [m for m in list(sys.modules)
                 if m == "beamng_cache_importer"
                 or m.startswith("beamng_cache_importer.")]:
        del sys.modules[_mod]
    bpy.ops.preferences.addon_install(filepath=_ZIP, overwrite=True)
    bpy.ops.preferences.addon_enable(module=_MODULE)

    scene = bpy.context.scene
    scene.beamng.cache_path = _CACHE
    scene.beamng.playback_fps = 24
    scene.beamng.output_fps = 60
    scene.beamng.start_frame = 400
    scene.beamng.use_chunked = True
    scene.frame_start = 1
    scene.frame_end = 400

    print("[DIAG] importing ...", flush=True)
    res = bpy.ops.beamng.import_cache()
    print(f"[DIAG] import_cache -> {res}", flush=True)

    from runtime import frame_handler
    pb = frame_handler._active
    print(f"[DIAG] frame_handler module = {frame_handler.__file__}", flush=True)
    print(f"[DIAG] frame_handler._active = {pb}", flush=True)
    print(f"[DIAG] scene._beamng_use_chunked = "
          f"{scene.get('_beamng_use_chunked')}", flush=True)
    print(f"[DIAG] scene._beamng_cache_path = "
          f"{scene.get('_beamng_cache_path')}", flush=True)
    print(f"[DIAG] chunk objects in scene: "
          f"{[o.name for o in bpy.data.objects if o.name in ('glass', 'metal', 'wheels', 'trim')]}",
          flush=True)
    if pb is None:
        sys.exit(1)

    cmap = getattr(pb, "_chunk_map", None)
    chunks = getattr(pb, "_chunks", {})
    faces = getattr(pb, "_chunk_member_faces", {})
    ranges = getattr(pb, "_chunk_member_ranges", {})
    objs = getattr(pb, "_objects", {})
    print(f"[DIAG] _chunk_map = {cmap}", flush=True)
    print(f"[DIAG] _chunks keys = {sorted(chunks.keys())}", flush=True)
    print(f"[DIAG] _chunk_member_faces keys = {sorted(faces.keys())}", flush=True)
    print(f"[DIAG] _chunk_member_ranges keys = {sorted(ranges.keys())}", flush=True)
    print(f"[DIAG] _objects (sample) = {sorted(objs.keys())[:8]} ...", flush=True)
    if "glass" in faces:
        glass_members = sorted(faces["glass"].keys())
        print(f"[DIAG] glass chunk members ({len(glass_members)}): "
              f"{glass_members}", flush=True)
        for p in ("flanje_e180_doorglass_RL",
                  "flanje_e180_headlightglass_L",
                  "flanje_e180_headlightglass_R",
                  "flanje_e180_windshield"):
            print(f"[DIAG]   face slice of {p}: "
                  f"{faces['glass'].get(p)}", flush=True)
    else:
        print("[DIAG] NO 'glass' entry in _chunk_member_faces", flush=True)
    print("[DIAG] scene._beamng_use_chunked = "
          f"{scene.get('_beamng_use_chunked')}", flush=True)


if __name__ == "__main__":
    main()
