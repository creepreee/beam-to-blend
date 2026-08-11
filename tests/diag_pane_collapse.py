"""Diagnostic — is the intact pane mesh actually collapsing at its shatter frame?

    blender --background --python tests/diag_pane_collapse.py -- [blend]

PROBE1 in ``diag_glass_state.py`` measured every shattered pane's mesh diagonal
as byte-identical across 300 frames of a violent crash.  That has two possible
causes and they need very different fixes:

  A. the collapse map never reaches playback (a wiring bug), or
  B. the frame handler never ran during the walk, so NO vertex animation was
     applied at all and the constant diagonal is an artefact of the probe.

This isolates them: it drives ``playback.set_frame`` DIRECTLY (no handler, no
frame_set) and reports the pane diagonal before and after the shatter frame,
plus the live ``_shattered`` map and which object dict actually holds the pane.
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

DEFAULT_BLEND = r"C:\Users\ubaid_i2c\Downloads\vehicle materials ready made.blend"


def log(msg: str) -> None:
    print(msg, flush=True)


def diag(obj) -> float:
    """Local-space bounding-box diagonal of the object's mesh."""
    vs = obj.data.vertices
    if not len(vs):
        return 0.0
    xs = [v.co.x for v in vs]
    ys = [v.co.y for v in vs]
    zs = [v.co.z for v in vs]
    return ((max(xs) - min(xs)) ** 2 + (max(ys) - min(ys)) ** 2
            + (max(zs) - min(zs)) ** 2) ** 0.5


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
    log(f"[DIAG] _active={pb is not None}")
    if pb is None:
        log("[DIAG][FAIL] no live playback — recovery did not run")
        return 1

    objs = getattr(pb, "_objects", {}) or {}
    chunks = getattr(pb, "_chunks", {}) or {}
    log(f"[DIAG] playback holds {len(objs)} individual objects, "
        f"{len(chunks)} chunks")
    log(f"[DIAG] chunk names: {list(chunks)}")
    log(f"[DIAG] _shattered map (live): {getattr(pb, '_shattered', None)}")
    log(f"[DIAG] scene _beamng_shattered_panes: "
        f"{scene.get('_beamng_shattered_panes')}")

    # Which dict holds the windshield, and is the OBJECT in the scene the same
    # datablock playback writes into?
    name = "flanje_e180_windshield"
    in_objs = name in objs
    member_of = [c for c, r in (getattr(pb, "_chunk_member_ranges", {}) or {}).items()
                 if name in r]
    log(f"[DIAG] {name}: in _objects={in_objs} member_of_chunks={member_of}")
    scene_obj = bpy.data.objects.get(name)
    log(f"[DIAG] {name}: scene object exists={scene_obj is not None}")
    if in_objs and scene_obj is not None:
        log(f"[DIAG]   same datablock as playback's: "
            f"{objs[name].data is scene_obj.data}")

    # Drive playback directly at frames spanning the shatter, and report the
    # pane diagonal.  A collapsed pane has diagonal ~0.
    from runtime.impact_detect import GlassSettings

    probe_obj = objs.get(name) or scene_obj
    if probe_obj is None:
        log("[DIAG][FAIL] windshield object not found at all")
        return 1

    log(f"[DIAG] driving playback.set_frame directly (no handler):")
    for cf in (600, 640, 651, 652, 653, 700, 900, 1199):
        pb.set_frame(cf)
        log(f"[DIAG]   cache {cf:5d}: windshield diagonal = {diag(probe_obj):.4f}")

    # Now register a shatter and repeat — this is the collapse path.
    log(f"[DIAG] registering shatter at cache 652 and re-driving:")
    frame_handler.set_shattered_panes({name: 652})
    log(f"[DIAG]   _shattered now: {getattr(pb, '_shattered', None)}")
    for cf in (600, 651, 652, 653, 700, 900):
        pb.set_frame(cf)
        log(f"[DIAG]   cache {cf:5d}: windshield diagonal = {diag(probe_obj):.4f}")

    # And through the HANDLER path, which is what the viewport actually uses.
    log(f"[DIAG] via scene.frame_set (handler path):")
    start = int(scene.get("_beamng_start_frame", 400))
    pfps = float(scene.get("_beamng_playback_fps", 20.0))
    ofps = float(scene.get("_beamng_output_fps", 60.0))
    for cf in (600, 651, 652, 700, 900):
        bf = int(round(start + cf * (ofps / pfps)))
        scene.frame_set(bf)
        log(f"[DIAG]   cache {cf:5d} (blender {bf}): "
            f"diagonal = {diag(probe_obj):.4f}")

    log("[DIAG] done")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
