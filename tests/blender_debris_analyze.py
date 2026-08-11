"""Fast diagnostics on a saved debris blend.

    blender --background --python tests/blender_debris_analyze.py

Opens the blend saved by blender_debris_retention.py (SAVE_BLEND env) and
prints the root empty's translation+rotation, the glass fragment counts, the
persisted shattered-pane map, and — when live playback recovered — the rim-band
keep/interior counts per pane, so we can see the rim-band collapse held.
"""

import os
import sys

import numpy as np

import bpy

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ZIP = os.path.join(_REPO, "dist", "beamng_cache_importer.zip")
_BLEND = os.environ.get(
    "ANALYZE_BLEND",
    r"C:\Users\ubaid_i2c\AppData\Local\Temp\opencode\retention.blend")


def log(msg):
    print(msg, flush=True)


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.preferences.addon_install(filepath=_ZIP, overwrite=True)
    bpy.ops.preferences.addon_enable(module="beamng_cache_importer")
    bpy.ops.wm.open_mainfile(filepath=_BLEND)

    root = None
    for o in bpy.data.objects:
        if o.type == "EMPTY" and o.name.endswith("__root"):
            root = o
    log(f"[ANA] root: {root.name if root else None}")

    frags = [o for o in bpy.data.objects if o.name.startswith("glassfrag_")]
    parented = [o for o in frags if o.parent is not None]
    log(f"[ANA] glass fragments: {len(frags)} total, "
        f"{len(parented)} parented (fringe objects removed by design)")

    scene = bpy.context.scene
    log(f"[ANA] scene shattered panes: "
        f"{scene.get('_beamng_shattered_panes')}")
    log(f"[ANA] scene rim-band width: "
        f"{scene.get('_beamng_shatter_edge_retain')}")

    try:
        from runtime import frame_handler
        pb = frame_handler._active
    except Exception:
        pb = None
    if pb is not None:
        panes = getattr(pb, "_shattered", {}) or {}
        log(f"[ANA] live _shattered: {panes}")
        for name in sorted(panes):
            keep = (pb._shattered_keeps or {}).get(name)
            n = len(keep) if keep is not None else 0
            rim = int(keep.sum()) if keep is not None else 0
            log(f"[ANA]   {name:40s} verts={n:5d} rim={rim:5d} "
                f"interior={n - rim:5d}")

    log("[ANA][DONE]")


if __name__ == "__main__":
    main()
