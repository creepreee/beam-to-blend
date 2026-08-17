"""Diagnostic — what does the glass subsystem ACTUALLY do in Blender?

    blender --background --python tests/diag_glass_state.py -- [blend]

Read-only probe.  Answers, with measurements rather than claims:

  1. Does a shattered pane's INTACT mesh actually collapse during playback?
     (If not, the user sees the whole pane riding the wreck — which reads as
     "the glass follows the car" even when the fragments are fine.)
  2. Are the free fragments independent of the wreck?  Measured as the
     correlation between fragment world motion and the root empty's motion
     AFTER the fragment should have come to rest.
  3. Does the retained fringe exist at all, and does it follow the wreck?
  4. Do cracked panes ever occur on this capture?

Prints a PROBE block per question.  Never asserts — this is for finding out
what is true, not for gating.
"""

from __future__ import annotations

import os
import sys
from typing import Dict, List

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# An installed copy of the add-on shadows the repo's runtime/importer packages,
# so a probe run against it silently measures the DEPLOYED build.
for mod in [m for m in list(sys.modules)
            if m.split(".")[0] in ("runtime", "importer", "addon")]:
    del sys.modules[mod]

import bpy
from mathutils import Vector

DEFAULT_BLEND = r"C:\Users\ubaid_i2c\Downloads\vehicle materials ready made.blend"


def log(msg: str) -> None:
    print(msg, flush=True)


def register_repo_addon() -> None:
    """Register the REPO's add-on, so ``scene.beamng`` exists and points at the
    working tree rather than an installed build.

    The blend stores the cache path and the live tuning values as scene props,
    but the PropertyGroups themselves only exist once ``ui.register()`` has
    run — without it every ``scene.beamng`` access is an AttributeError.
    """
    import addon
    try:
        addon.register()
    except Exception as exc:  # pragma: no cover - already registered
        log(f"[DIAG] addon.register(): {exc}")


def main(argv: List[str]) -> int:
    argv = [a for a in argv if a.lower().endswith(".blend")]
    blend = argv[0] if argv else DEFAULT_BLEND
    if not os.path.exists(blend):
        log(f"[DIAG][FAIL] blend not found: {blend}")
        return 1

    from runtime.cache_reader import CacheReader
    from runtime.impact_detect import (
        GLASS_CRACKED, GLASS_SHATTERED, GlassSettings, detect_impacts,
        resolve_glass_damage,
    )
    from runtime.debris_spawn import (
        DEBRIS_COLLECTION, GLASS_COLLECTION, GROUND_NAME,
        build_debris, clear_debris,
    )
    from runtime.debris_bake import bake_debris
    from runtime.debris_physics import _frozen_handlers
    from runtime import frame_handler

    register_repo_addon()
    bpy.ops.wm.open_mainfile(filepath=blend)
    scene = bpy.context.scene
    log(f"[DIAG] opened {blend}")

    beamng = getattr(scene, "beamng", None)
    cache_path = scene.get("_beamng_cache_path") or \
        (getattr(beamng, "cache_path", "") if beamng else "")
    if not cache_path or not os.path.exists(cache_path):
        log(f"[DIAG][FAIL] cache not found: {cache_path}")
        return 1
    # Prefer the persisted LIVE values (`_beamng_*`) over the UI props: a live
    # retune writes those and they are what playback is actually using.
    start_frame = int(scene.get("_beamng_start_frame",
                                getattr(beamng, "start_frame", 0) if beamng else 0))
    playback_fps = float(scene.get("_beamng_playback_fps",
                                   getattr(beamng, "playback_fps", 24) if beamng else 24))
    output_fps = float(scene.get("_beamng_output_fps",
                                 getattr(beamng, "output_fps", 60) if beamng else 60))
    ground_shift = float(scene.get("_beamng_ground_shift", 0.0))
    log(f"[DIAG] start={start_frame} playback_fps={playback_fps} "
        f"output_fps={output_fps} ground_shift={ground_shift:.4f}")
    log(f"[DIAG] handler live: _active={frame_handler._active is not None}")

    root = next((o for o in bpy.data.objects
                 if o.type == "EMPTY" and o.name.endswith("__root")), None)
    log(f"[DIAG] transform root: {root.name if root else None}")

    # ---- probe 4: tiers on real data ------------------------------------
    reader = CacheReader(cache_path)
    events = detect_impacts(reader, ground_shift=ground_shift,
                            playback_fps=playback_fps)
    tiers = resolve_glass_damage(events, GlassSettings())
    n_sh = sum(1 for t, _, _ in tiers.values() if t == GLASS_SHATTERED)
    n_cr = sum(1 for t, _, _ in tiers.values() if t == GLASS_CRACKED)
    log(f"[PROBE4] {len(events)} events; panes: {n_sh} shattered, {n_cr} cracked")
    for part, (tier, cf, ev) in sorted(tiers.items()):
        log(f"[PROBE4]   {part:42s} {tier:10s} cache={cf} sev={ev.severity:.3f} "
            f"gd={ev.ground_depth:+.4f}")

    # ---- build --------------------------------------------------------
    with _frozen_handlers():
        clear_debris()
        settings_mod = __import__("runtime.debris_spawn", fromlist=["DebrisSettings"])
        settings = settings_mod.DebrisSettings()
        summary = build_debris(
            CacheReader(cache_path), events, settings,
            glass_settings=GlassSettings(),
            frame_start=start_frame, playback_fps=playback_fps,
            output_fps=output_fps, ground_shift=ground_shift)
        bake_debris(summary.get("hero_objects", []),
                    summary.get("bake_start", scene.frame_start),
                    summary.get("bake_end", scene.frame_end))
    panes = summary.get("shattered_panes") or {}
    log(f"[DIAG] built: glass={summary.get('glass')} "
        f"retained={summary.get('retained')} panes={len(panes)}")
    frame_handler.set_shattered_panes(panes)

    glass_coll = bpy.data.collections.get(GLASS_COLLECTION)
    frags = sorted(glass_coll.objects, key=lambda o: o.name) if glass_coll else []
    parented = [o for o in frags if o.parent is not None]
    free = [o for o in frags if o.parent is None]
    log(f"[PROBE3] {len(frags)} fragment objects: {len(parented)} parented "
        f"(fringe), {len(free)} unparented (free)")
    if parented:
        log(f"[PROBE3]   fringe parents: "
            f"{sorted({o.parent.name for o in parented})}")

    # ---- walk the timeline ---------------------------------------------
    # Pane objects: which real car object holds each shattered pane?
    pane_objs: Dict[str, "bpy.types.Object"] = {}
    for part in panes:
        o = bpy.data.objects.get(part)
        if o is not None:
            pane_objs[part] = o
    log(f"[DIAG] pane objects found individually: {len(pane_objs)}/{len(panes)}")
    if len(pane_objs) < len(panes):
        # chunked import: panes live inside a merged chunk mesh
        log(f"[DIAG]   (chunked import — panes are members of a merged mesh)")
        pb = frame_handler._active
        if pb is not None:
            log(f"[DIAG]   chunks={list(getattr(pb, '_chunks', {}) or {})}")

    sample = free[: min(24, len(free))]
    fringe_sample = parented[: min(12, len(parented))]
    shatter_blender = {}
    for part, cf in panes.items():
        shatter_blender[part] = int(round(start_frame + cf * (output_fps / playback_fps)))
    first_shatter = min(shatter_blender.values()) if shatter_blender else scene.frame_start
    log(f"[DIAG] shatter blender frames: {sorted(set(shatter_blender.values()))}")

    # Sample well after the shatter so everything has had time to settle.
    walk = [first_shatter - 5, first_shatter + 2, first_shatter + 30,
            first_shatter + 120, first_shatter + 300]
    walk = [f for f in walk if scene.frame_start <= f <= scene.frame_end]
    root_pos: Dict[int, Vector] = {}
    frag_pos: Dict[int, Dict[str, Vector]] = {}
    fringe_pos: Dict[int, Dict[str, Vector]] = {}
    pane_bounds: Dict[int, Dict[str, tuple]] = {}

    for f in walk:
        scene.frame_set(f)
        bpy.context.view_layer.update()
        if root is not None:
            root_pos[f] = root.matrix_world.translation.copy()
        frag_pos[f] = {o.name: o.matrix_world.translation.copy() for o in sample}
        fringe_pos[f] = {o.name: o.matrix_world.translation.copy()
                         for o in fringe_sample}
        b = {}
        for part, o in pane_objs.items():
            mat = o.matrix_world
            pts = [mat @ v.co for v in o.data.vertices]
            if pts:
                xs = [p.x for p in pts]; ys = [p.y for p in pts]; zs = [p.z for p in pts]
                diag = ((max(xs) - min(xs)) ** 2 + (max(ys) - min(ys)) ** 2
                        + (max(zs) - min(zs)) ** 2) ** 0.5
                b[part] = (diag, Vector((sum(xs) / len(xs), sum(ys) / len(ys),
                                         sum(zs) / len(zs))))
        pane_bounds[f] = b

    log(f"[DIAG] walked frames {walk}")
    for f in walk:
        rp = root_pos.get(f)
        log(f"[DIAG] frame {f}: root={tuple(round(c,3) for c in rp) if rp else None}")

    # ---- probe 1: does the intact pane collapse? ------------------------
    log(f"[PROBE1] pane mesh diagonal (collapse => ~0)")
    for part in sorted(pane_objs):
        row = " ".join(
            f"f{f}={pane_bounds[f].get(part, (float('nan'),))[0]:7.3f}" for f in walk)
        sb = shatter_blender.get(part)
        log(f"[PROBE1]   {part:42s} shatter@{sb} {row}")

    # ---- probe 2: are free fragments independent of the wreck? ----------
    # Compare each fragment's displacement against the root's displacement over
    # the SAME interval, late in the settle when the fragment must be at rest.
    if len(walk) >= 2 and root is not None:
        a, b = walk[-2], walk[-1]
        droot = (root_pos[b] - root_pos[a])
        log(f"[PROBE2] interval f{a}->f{b}: root moved {droot.length:.3f} m "
            f"{tuple(round(c,3) for c in droot)}")
        log(f"[PROBE2] free fragments (should be ~0 if settled & independent):")
        n_follow = 0
        for name in sorted(frag_pos[a]):
            d = frag_pos[b][name] - frag_pos[a][name]
            # How much of the fragment's motion is explained by the root's?
            proj = (d.dot(droot) / droot.length_squared) if droot.length > 1e-9 else 0.0
            flag = ""
            if droot.length > 0.05 and proj > 0.5:
                flag = "  <== TRACKS ROOT"
                n_follow += 1
            log(f"[PROBE2]   {name:44s} moved={d.length:7.3f} "
                f"root_frac={proj:+7.3f}{flag}")
        log(f"[PROBE2] {n_follow}/{len(frag_pos[a])} sampled free fragments "
            f"track the root")
        log(f"[PROBE2] fringe fragments (SHOULD track the root by design):")
        for name in sorted(fringe_pos[a]):
            d = fringe_pos[b][name] - fringe_pos[a][name]
            proj = (d.dot(droot) / droot.length_squared) if droot.length > 1e-9 else 0.0
            log(f"[PROBE2]   {name:44s} moved={d.length:7.3f} root_frac={proj:+7.3f}")

    # ---- keyframe census ------------------------------------------------
    log(f"[DIAG] keyframe census by family:")
    fams: Dict[str, List[int]] = {}
    for coll_name in (DEBRIS_COLLECTION, GLASS_COLLECTION):
        c = bpy.data.collections.get(coll_name)
        if c is None:
            continue
        for o in c.objects:
            if o.name == GROUND_NAME:
                continue
            fam = o.name.split("_")[0] if "_" not in o.name else "_".join(
                o.name.split("_")[:2])
            ad = o.animation_data
            n = 0
            if ad and ad.action:
                for fc in ad.action.fcurves:
                    if fc.data_path == "location":
                        n = max(n, len(fc.keyframe_points))
            fams.setdefault(fam, []).append(n)
    for fam, counts in sorted(fams.items()):
        counts.sort()
        log(f"[DIAG]   {fam:28s} n={len(counts):4d} loc_keys "
            f"min={counts[0]} med={counts[len(counts)//2]} max={counts[-1]}")

    log("[DIAG] done")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
