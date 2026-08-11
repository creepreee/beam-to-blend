"""Diagnostic — WHICH glass actually follows the car, and where does it sit?

    blender --background --python tests/diag_glass_follow.py -- [blend]

Replicates the OPERATOR's path exactly.  This matters: ``verify/03`` wraps
``build_debris`` in ``_frozen_handlers()``, but ``BEAMNG_OT_build_debris`` does
NOT — and phase 0b of ``_spawn_glass_pane`` calls ``scene.frame_set(spawn_frame)``
to resolve the root empty's shatter pose.  With handlers frozen that frame_set
does not drive the root, so the fringe's ``matrix_parent_inverse`` is resolved
against a stale pose.  A verify that freezes handlers is therefore testing a
DIFFERENT build than the user gets, which is exactly how a "verified" fringe can
be invisible in the real add-on.

Measures, per pane:
  * fringe count / free count, and the fringe's distance from the pane's own
    aperture centroid at the shatter frame (a fringe welded in the seal sits ON
    the aperture; a broken parent_inverse puts it metres away or under the car)
  * whether each free fragment's motion tracks the root DURING FLIGHT, not only
    after it has settled (a fragment that has come to rest trivially "does not
    track" — the late-interval test in diag_glass_state cannot see drag)
  * visibility state at a frame after the shatter
"""

from __future__ import annotations

import os
import sys
from typing import Dict, List

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

for mod in [m for m in list(sys.modules)
            if m.split(".")[0] in ("runtime", "importer", "addon")]:
    del sys.modules[mod]

import bpy
import numpy as np
from mathutils import Vector

DEFAULT_BLEND = r"C:\Users\ubaid_i2c\Downloads\vehicle materials ready made.blend"


def log(msg: str) -> None:
    print(msg, flush=True)


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

    from runtime.cache_reader import CacheReader
    from runtime.impact_detect import (
        GlassSettings, detect_impacts, local_to_world, resolve_glass_damage,
    )
    from runtime.debris_spawn import (
        DebrisSettings, GLASS_COLLECTION, bake_debris, build_debris,
        clear_debris,
    )
    from runtime import frame_handler

    start = int(scene.get("_beamng_start_frame", 400))
    pfps = float(scene.get("_beamng_playback_fps", 20.0))
    ofps = float(scene.get("_beamng_output_fps", 60.0))
    gshift = float(scene.get("_beamng_ground_shift", 0.0))
    cache_path = scene.get("_beamng_cache_path")
    log(f"[DIAG] start={start} pfps={pfps} ofps={ofps} gshift={gshift:.4f}")
    log(f"[DIAG] handler live during build: "
        f"{len(bpy.app.handlers.frame_change_pre)} pre-handlers")

    root = next((o for o in bpy.data.objects
                 if o.type == "EMPTY" and o.name.endswith("__root")), None)

    reader = CacheReader(cache_path)
    events = detect_impacts(reader, ground_shift=gshift, playback_fps=pfps)
    tiers = resolve_glass_damage(events, GlassSettings())

    # ---- build EXACTLY as the operator does: handlers LIVE ----------------
    clear_debris()
    summary = build_debris(
        CacheReader(cache_path), events, DebrisSettings(),
        glass_settings=GlassSettings(), frame_start=start,
        playback_fps=pfps, output_fps=ofps, ground_shift=gshift)
    bake_debris(summary.get("hero_objects", []),
                summary.get("bake_start", scene.frame_start),
                summary.get("bake_end", scene.frame_end))
    panes = summary.get("shattered_panes") or {}
    frame_handler.set_shattered_panes(panes)
    log(f"[DIAG] glass={summary.get('glass')} retained={summary.get('retained')}")

    glass_coll = bpy.data.collections.get(GLASS_COLLECTION)
    frags = list(glass_coll.objects) if glass_coll else []

    # Group fragments by pane.
    by_pane: Dict[str, Dict[str, List]] = {}
    for o in frags:
        for pane in panes:
            if o.name.startswith(f"glassfrag_{pane}_"):
                d = by_pane.setdefault(pane, {"fringe": [], "free": []})
                d["fringe" if o.parent is not None else "free"].append(o)
                break

    # ---- where does the fringe sit relative to its own aperture? ----------
    log(f"[DIAG] fringe placement vs. the pane's aperture at the shatter frame")
    for pane in sorted(by_pane):
        cf = panes[pane]
        bf = int(round(start + cf * (ofps / pfps)))
        scene.frame_set(bf)
        bpy.context.view_layer.update()
        loc = reader.frame_positions(pane, cf)
        world = local_to_world(loc, reader.frame_transform(cf),
                              ground_shift=gshift)
        ap_centre = Vector(tuple(world.mean(axis=0)))
        ap_extent = float(np.linalg.norm(world.max(axis=0) - world.min(axis=0)))
        fr = by_pane[pane]["fringe"]
        fe = by_pane[pane]["free"]
        if fr:
            ds = [(o.matrix_world.translation - ap_centre).length for o in fr]
            log(f"[DIAG]   {pane:40s} fringe={len(fr):3d} free={len(fe):3d} "
                f"aperture_diag={ap_extent:.3f} "
                f"fringe_dist min={min(ds):.3f} med={sorted(ds)[len(ds)//2]:.3f} "
                f"max={max(ds):.3f}")
        else:
            log(f"[DIAG]   {pane:40s} fringe=  0 free={len(fe):3d} "
                f"aperture_diag={ap_extent:.3f}  <== NO FRINGE AT ALL")

    # ---- do free fragments track the root DURING FLIGHT? -----------------
    first_cf = min(panes.values())
    first_bf = int(round(start + first_cf * (ofps / pfps)))
    walk = [first_bf + k for k in (1, 4, 8, 16, 32, 64, 128, 256)]
    walk = [f for f in walk if f <= scene.frame_end]
    pos: Dict[int, Dict[str, Vector]] = {}
    rootp: Dict[int, Vector] = {}
    for f in walk:
        scene.frame_set(f)
        bpy.context.view_layer.update()
        rootp[f] = root.matrix_world.translation.copy() if root else Vector()
        pos[f] = {o.name: o.matrix_world.translation.copy() for o in frags}

    log(f"[DIAG] root-tracking DURING FLIGHT (per interval, free fragments)")
    log(f"[DIAG]   interval        root_dx   n_free  tracking(>0.5 root_frac)"
        f"   mean_frac")
    for a, b in zip(walk, walk[1:]):
        droot = rootp[b] - rootp[a]
        if droot.length < 1e-6:
            continue
        fracs = []
        for pane in by_pane:
            for o in by_pane[pane]["free"]:
                d = pos[b][o.name] - pos[a][o.name]
                fracs.append(d.dot(droot) / droot.length_squared)
        if not fracs:
            continue
        n_track = sum(1 for x in fracs if x > 0.5)
        log(f"[DIAG]   f{a}->f{b}  {droot.length:8.3f}  {len(fracs):6d}   "
            f"{n_track:6d} ({n_track/len(fracs):5.1%})        "
            f"{sum(fracs)/len(fracs):+.3f}")

    # ---- fringe tracking + visibility -----------------------------------
    log(f"[DIAG] fringe tracking (expected ~1.000 by design) + visibility")
    a, b = walk[-2], walk[-1]
    droot = rootp[b] - rootp[a]
    for pane in sorted(by_pane):
        fr = by_pane[pane]["fringe"]
        if not fr:
            continue
        fracs = [((pos[b][o.name] - pos[a][o.name]).dot(droot)
                  / droot.length_squared) if droot.length > 1e-9 else 0.0
                 for o in fr]
        vis = sum(1 for o in fr if not o.hide_viewport)
        rvis = sum(1 for o in fr if not o.hide_render)
        log(f"[DIAG]   {pane:40s} n={len(fr):3d} "
            f"mean_root_frac={sum(fracs)/len(fracs):+.3f} "
            f"visible={vis}/{len(fr)} render_visible={rvis}/{len(fr)}")

    log(f"[DIAG] free-fragment visibility at f{b}:")
    tot = vis = 0
    for pane in by_pane:
        for o in by_pane[pane]["free"]:
            tot += 1
            vis += 0 if o.hide_viewport else 1
    log(f"[DIAG]   {vis}/{tot} free fragments visible")
    log("[DIAG] done")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
