"""End-to-end headless verify: the crack-shader wiring against the REAL cache.

    blender --background --python tests/blender_crack_e2e.py

Mirrors the operator's exact path (import -> detect -> build_debris ->
bake -> set_shattered_panes), then exercises the NEW crack integration that
was dead code until now:

  1. detection + resolve_glass_damage must produce cracked AND shattered panes
  2. build_debris must decorate each CRACKED pane with a "BeamNG Crack <part>"
     material assigned to the correct FACE SLICE of the merged glass chunk
     (chunked import), fade-in driver on the chunk object
  3. cracked panes must NOT appear in shattered_panes (their mesh keeps
     animating; only shattered panes collapse)
  4. BOTH crack material paths build: procedural (use_image=False) and image
     (use_image=True, node left unassigned when no file set)
  5. clear_debris must strip the crack material off the car and restore the
     pane's original slot

Exits non-zero on any failure.
"""

import os
import sys

import numpy as np

import bpy


def log(msg):
    print(msg, flush=True)


_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ZIP = os.path.join(_REPO, "dist", "beamng_cache_importer.zip")
_CACHE = os.environ.get(
    "BEAMNG_CACHE",
    r"C:\Users\ubaid_i2c\AppData\Local\BeamNG\BeamNG.drive\current\captures\name.bvc",
)
_MODULE = "beamng_cache_importer"

FAILS = []


def _check(cond, msg):
    tag = "ok" if cond else "FAIL"
    log(f"[E2E][{tag}] {msg}")
    if not cond:
        FAILS.append(msg)


def _crack_materials():
    return [m for m in bpy.data.materials
            if m.name.startswith("BeamNG Crack ")]


def main():
    if not os.path.exists(_ZIP):
        log(f"[E2E][FAIL] add-on zip missing: {_ZIP}")
        sys.exit(1)
    if not os.path.exists(_CACHE):
        log(f"[E2E][FAIL] cache missing: {_CACHE}")
        sys.exit(1)

    bpy.ops.wm.read_factory_settings(use_empty=True)
    for _mod in [m for m in list(sys.modules)
                 if m == "beamng_cache_importer"
                 or m.startswith("beamng_cache_importer.")
                 or m == "runtime" or m.startswith("runtime.")
                 or m == "addon" or m.startswith("addon.")]:
        del sys.modules[_mod]
    bpy.ops.preferences.addon_install(filepath=_ZIP, overwrite=True)
    bpy.ops.preferences.addon_enable(module=_MODULE)
    if _MODULE not in bpy.context.preferences.addons:
        log("[E2E][FAIL] add-on did not enable")
        sys.exit(1)

    scene = bpy.context.scene
    scene.beamng.cache_path = _CACHE
    scene.beamng.playback_fps = 24
    scene.beamng.output_fps = 60
    scene.beamng.start_frame = 400
    scene.beamng.use_chunked = True   # the user's real workflow: merged chunks
    scene.frame_start = 1
    scene.frame_end = 400

    # Shrink the workload: no hero/fine debris, tiny settle.  Glass is the
    # feature under test and stays fully on.
    d = scene.beamng_debris
    d.debris_max_hero = 0
    d.debris_hero_count = 0
    d.debris_fine_count = 0
    d.debris_variants = 1
    d.debris_settle_frames = 20
    d.debris_shatter_glass = True

    log("[E2E] importing cache ...")
    res = bpy.ops.beamng.import_cache()
    if res != {"FINISHED"}:
        log(f"[E2E][FAIL] import_cache returned {res}")
        sys.exit(1)

    ground_shift = float(scene.get("_beamng_ground_shift", 0.0))
    frame_start = int(scene.get("_beamng_start_frame",
                                getattr(scene.beamng, "start_frame", 0)))
    playback_fps = float(scene.get("_beamng_playback_fps",
                                   getattr(scene.beamng, "playback_fps", 24)))
    output_fps = float(scene.get("_beamng_output_fps",
                                 getattr(scene.beamng, "output_fps", 60)))
    log(f"[E2E] ground_shift={ground_shift:.4f} start={frame_start} "
        f"fps={playback_fps}/{output_fps}")

    # Import via the TOP-LEVEL `runtime` package — the SAME module instance the
    # import operator populates (the addon imports `from runtime import ...`,
    # never `beamng_cache_importer.runtime.*`).  Importing the latter creates a
    # second module instance whose frame_handler._active stays None, which made
    # _find_pane_target fall back to the per-part objects and the chunked
    # face-slice check fail.
    from runtime.cache_reader import CacheReader
    from runtime.impact_detect import (
        GLASS_CRACKED, GLASS_SHATTERED, detect_impacts,
        resolve_glass_damage,
    )
    from runtime.debris_spawn import (
        GLASS_COLLECTION, build_debris, clear_debris,
    )

    reader = CacheReader(_CACHE)
    events = detect_impacts(reader, ground_shift=ground_shift,
                            playback_fps=float(playback_fps))
    tiers = resolve_glass_damage(events)
    cracked = [p for p, (t, _, _) in tiers.items() if t == GLASS_CRACKED]
    shattered = [p for p, (t, _, _) in tiers.items() if t == GLASS_SHATTERED]
    log(f"[E2E] glass tiers: {len(cracked)} cracked, {len(shattered)} shattered")
    for p, (t, f, e) in sorted(tiers.items()):
        log(f"[E2E]   {p}: {t} @ cache frame {f} "
            f"(ground_depth={e.ground_depth:.3f} deform={e.deform:.4f})")

    _check(len(shattered) > 0, "at least one pane shatters (real cache)")
    _check(len(cracked) > 0, "at least one pane cracks (real cache)")

    source_objects = {o.name: o for o in bpy.data.objects
                      if o.type == "MESH"}

    def _build(mode):
        log(f"[E2E] --- build_debris mode={mode} ---")
        d.glass_crack_enabled = True
        d.glass_crack_use_image = (mode == "image")
        d.glass_crack_image = ""   # node wired but left unassigned
        from beamng_cache_importer.operators import _crack_settings
        summary = build_debris(
            reader, events, None,
            glass_settings=None,
            crack_settings=_crack_settings(bpy.context),
            frame_start=int(frame_start),
            playback_fps=float(playback_fps),
            output_fps=float(output_fps),
            ground_shift=ground_shift,
            source_objects=source_objects,
        )
        return summary

    for mode in ("procedural", "image"):
        summary = _build(mode)
        cracked_infos = summary.get("cracked_panes", [])
        log(f"[E2E]   summary: glass={summary.get('glass', 0)} "
            f"retained={summary.get('retained', 0)} "
            f"shattered={len(summary.get('shattered_panes', {}))} "
            f"cracked={len(cracked_infos)}")
        _check(len(cracked_infos) > 0, f"[{mode}] cracked panes decorated")
        _check(len(summary.get("shattered_panes", {})) == len(shattered),
               f"[{mode}] shattered_panes map matches resolved panes")

        mats = _crack_materials()
        _check(len(mats) == len(cracked_infos),
               f"[{mode}] {len(mats)} crack materials for "
               f"{len(cracked_infos)} panes")

        for info in cracked_infos:
            mat = bpy.data.materials.get(info["material"])
            _check(mat is not None, f"[{mode}] {info['part']}: material exists")
            _check(info["faces"] > 0,
                   f"[{mode}] {info['part']}: {info['faces']} faces assigned")
            _check(info["chunked"],
                   f"[{mode}] {info['part']}: chunked face-slice "
                   f"(obj={info['object']})")
            _check(info["part"] not in summary.get("shattered_panes", {}),
                   f"[{mode}] {info['part']}: NOT collapsed (keeps glass)")
            if mat is not None:
                _check(mat.use_nodes and mat.node_tree is not None,
                       f"[{mode}] {info['part']}: node tree present")
            target = bpy.data.objects.get(info["object"])
            _check(target is not None
                   and target.get("_beamng_crack_amount") is not None,
                   f"[{mode}] {info['part']}: fade-in property on "
                   f"{info['object']}")
            if mode == "image":
                img_nodes = [n for n in mat.node_tree.nodes
                             if n.type == "TEX_IMAGE"] if mat else []
                _check(len(img_nodes) == 1,
                       f"[{mode}] {info['part']}: image texture node present")

        # The chunk mesh must still carry the pane's ORIGINAL glass slot at 0.
        if cracked_infos:
            obj = bpy.data.objects.get(cracked_infos[0]["object"])
            if obj is not None and obj.data is not None:
                _check(len(obj.data.materials) >= 1,
                       f"[{mode}] {obj.name}: original slot retained")

        # Bake + collapse registration, exactly like the operator.
        from runtime import frame_handler
        from runtime.debris_spawn import bake_debris
        bake = bake_debris(
            summary.get("hero_objects", []),
            summary.get("bake_start", scene.frame_start),
            summary.get("bake_end", scene.frame_end),
        )
        frame_handler.set_shattered_panes(
            summary.get("shattered_panes") or {},
            edge_retain=scene.get("_beamng_shatter_edge_retain", 0.05))
        log(f"[E2E]   baked {bake.get('baked', 0)} bodies")

        # Clear must strip the crack materials and restore the pane's glass.
        n_removed = clear_debris()
        remaining = _crack_materials()
        _check(not remaining,
               f"[{mode}] clear_debris removed all crack materials "
               f"({n_removed} removed, {len(remaining)} left)")
        glass_coll = bpy.data.collections.get(GLASS_COLLECTION)
        _check(glass_coll is None or len(glass_coll.objects) == 0,
               f"[{mode}] glass debris collection emptied")

    reader.close()

    if FAILS:
        log(f"[E2E][PASS->FAIL] {len(FAILS)} checks failed")
        sys.exit(1)
    log("[E2E][PASS] crack wiring works end-to-end on the real cache")


if __name__ == "__main__":
    main()
