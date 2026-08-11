"""Diagnostic — measure the pane INSIDE its merged chunk mesh.

    blender --background --python tests/diag_chunk_collapse.py -- [blend]

``diag_pane_collapse.py`` measured a standalone ``flanje_e180_windshield``
object and saw a frozen diagonal.  That object is a LEFTOVER: this blend is a
chunked import, so playback writes the merged ``glass`` chunk mesh and the
per-part object is never updated.  Measuring it says nothing about the collapse.

This probe reads the pane's real vertices — the chunk mesh's slice
``_chunk_member_ranges['glass'][pane]`` — so it measures what is actually drawn.
"""

from __future__ import annotations

import os
import sys
from typing import List

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

for mod in [m for m in list(sys.modules)
            if m.split(".")[0] in ("runtime", "importer", "addon")]:
    del sys.modules[mod]

import bpy
import numpy as np

DEFAULT_BLEND = r"C:\Users\ubaid_i2c\Downloads\vehicle materials ready made.blend"


def log(msg: str) -> None:
    print(msg, flush=True)


def member_diag(obj, start: int, end: int) -> float:
    """Bounding-box diagonal of vertices [start:end) of a chunk mesh."""
    n = len(obj.data.vertices)
    co = np.empty(n * 3, dtype=np.float64)
    obj.data.vertices.foreach_get("co", co)
    sl = co.reshape(-1, 3)[start:end]
    if not len(sl):
        return 0.0
    return float(np.linalg.norm(sl.max(axis=0) - sl.min(axis=0)))


def main(argv: List[str]) -> int:
    argv = [a for a in argv if a.lower().endswith(".blend")]
    blend = argv[0] if argv else DEFAULT_BLEND

    import addon
    try:
        addon.register()
    except Exception as exc:
        log(f"[DIAG] addon.register(): {exc}")

    bpy.ops.wm.open_mainfile(filepath=blend)
    scene = bpy.context.scene
    from runtime import frame_handler

    pb = frame_handler._active
    if pb is None:
        log("[DIAG][FAIL] no live playback")
        return 1

    ranges = getattr(pb, "_chunk_member_ranges", {}) or {}
    chunks = getattr(pb, "_chunks", {}) or {}
    log(f"[DIAG] chunk_map set: {bool(getattr(pb, '_chunk_map', None))}")

    # Locate every glass pane's chunk + slice.
    panes = dict(getattr(pb, "_shattered", {}) or {})
    log(f"[DIAG] live _shattered: {panes}")
    located = {}
    for pane in panes:
        for cname, members in ranges.items():
            if pane in members:
                located[pane] = (cname, members[pane])
                break
    for pane, (cname, (s, e)) in sorted(located.items()):
        log(f"[DIAG] {pane:42s} -> chunk '{cname}' verts [{s}:{e}) "
            f"({e - s} verts)")

    if not located:
        log("[DIAG][FAIL] no shattered pane located in any chunk")
        return 1

    log(f"[DIAG] pane diagonals INSIDE the chunk mesh, "
        f"driving playback.set_frame:")
    header = "  ".join(f"{p.replace('flanje_e180_','')[:14]:>14s}"
                       for p in sorted(located))
    log(f"[DIAG]   cache_frame  {header}")
    for cf in (600, 640, 651, 652, 653, 660, 690, 700, 900, 1199):
        pb.set_frame(cf)
        row = []
        for pane in sorted(located):
            cname, (s, e) = located[pane]
            row.append(f"{member_diag(chunks[cname], s, e):14.4f}")
        log(f"[DIAG]   {cf:11d}  " + "  ".join(row))

    log(f"[DIAG] shatter cache frames: "
        f"{ {p: panes[p] for p in sorted(located)} }")

    # Un-collapse check: an empty map must restore the glass.
    frame_handler.set_shattered_panes({})
    pb.set_frame(900)
    log(f"[DIAG] after set_shattered_panes({{}}) at cache 900:")
    for pane in sorted(located):
        cname, (s, e) = located[pane]
        log(f"[DIAG]   {pane:42s} diagonal={member_diag(chunks[cname], s, e):.4f}")

    log("[DIAG] done")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
