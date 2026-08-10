"""Verify 4 — a debris rebuild is deterministic: same inputs, same scene.

    blender --background --python verify/04_debris_determinism.py -- [blend]
    blender --background --python verify/04_debris_determinism.py -- [blend] --save-fp <file>
    blender --background --python verify/04_debris_determinism.py -- [blend] --check-fp <file>

Regression test for the seed fix.  The debris build used to derive its random
seeds from Python's built-in ``hash()``, which is randomized per process
(PYTHONHASHSEED): every Blender session rebuilt a *different* scene — the same
impacts produced different shard counts, emitter positions and hero pieces
every time.  ``runtime.debris_spawn`` now derives seeds from a stable crc32 of
the part/material, so a given .blend must rebuild identically.

What this script does:

  1. REBUILD — drive the add-on's own pipeline (CLEAR / DETECT / BUILD).
  2. FINGERPRINT — hash every structurally-deterministic attribute: emitter
     spawn frame, particle count and settings, emitter transform, shard
     template counts, hero/glass counts + transforms.
  3. REBUILD AGAIN in the same session (clear_debris between) and fingerprint
     again.  The two must be byte-identical.
  4. --save-fp writes the fingerprint so a LATER session can compare:
     cross-session determinism (the crc32 fix) is proven by running the script
     twice and checking the saved file matches.

Exit code: 0 = deterministic, 1 = a rebuild drifted.
"""

from __future__ import annotations

import hashlib
import os
import sys
from typing import Dict, List, Sequence

# The add-on's runtime package lives in the project root; make it importable
# even when this script is run without the add-on enabled in preferences.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import bpy

#: Default blend (PERMANENT RULE — no other blend files allowed for testing).
DEFAULT_BLEND = r"C:\Users\ubaid_i2c\Downloads\vehicle materials ready made.blend"

#: Shard / emitter objects live in the debris module's collections.
DEBRIS_COLLECTIONS = ("BeamNG Debris", "BeamNG Debris Glass")
#: The ground collider is a static helper, not part of the deterministic build.
GROUND_NAME = "BeamNG_DebrisGround"

FAILS: List[str] = []


def log(msg: str) -> None:
    print(msg, flush=True)


def debris_settings(scene) -> "object":
    """Replicate addon/operators._debris_settings from the scene UI props."""
    from runtime.debris_spawn import DebrisSettings
    props = scene.beamng_debris
    return DebrisSettings(
        density=float(getattr(props, "debris_density", 1.0)),
        scale=float(getattr(props, "debris_scale", 1.0)),
        hero_count=int(getattr(props, "debris_hero_count", 14)),
        fine_count=int(getattr(props, "debris_fine_count", 90)),
        max_hero_total=int(getattr(props, "debris_max_hero", 240)),
        speed=float(getattr(props, "debris_speed", 0.0)),
        spread=float(getattr(props, "debris_spread", 55.0)),
        bounciness=float(getattr(props, "debris_bounciness", 0.25)),
        scatter=float(getattr(props, "debris_scatter", 0.45)),
        min_severity=float(getattr(props, "debris_min_severity", 0.12)),
        min_blast_severity=float(getattr(props, "debris_min_blast_severity", 0.35)),
        variants=int(getattr(props, "debris_variants", 8)),
        settle_frames=int(getattr(props, "debris_settle_frames", 260)),
        seed=int(getattr(props, "debris_seed", 12345)),
        shatter_glass=bool(getattr(props, "debris_shatter_glass", True)),
    )


def glass_settings(scene) -> "object":
    """Replicate addon/operators._glass_settings from the scene UI props."""
    from runtime.impact_detect import GlassSettings
    props = scene.beamng_debris
    return GlassSettings(
        crack_deform=float(getattr(props, "glass_crack_deform", 0.006)),
        shatter_deform=float(getattr(props, "glass_shatter_deform", 0.022)),
        shatter_ground_depth=float(getattr(props, "glass_shatter_ground_depth", 0.03)),
        edge_retain=float(getattr(props, "glass_edge_retain", 0.05)),
    )


def has_particle_system(obj: "bpy.types.Object") -> bool:
    return any(getattr(m, "particle_system", None) is not None
               for m in obj.modifiers)


def rebuild(scene, cache_path: str, start_frame: int, playback_fps: float,
            output_fps: float, ground_shift: float) -> dict:
    """CLEAR / DETECT / BUILD via the add-on's own pipeline."""
    from runtime.cache_reader import CacheReader
    from runtime.impact_detect import detect_impacts
    from runtime.debris_spawn import (
        clear_debris,
        build_debris,
        bake_debris,
        _frozen_handlers,
    )

    with _frozen_handlers():
        clear_debris()
        reader = CacheReader(cache_path)
        try:
            events = detect_impacts(reader, ground_shift=ground_shift,
                                    playback_fps=float(playback_fps))
        finally:
            reader.close()
        if not events:
            raise RuntimeError("no impacts detected")
        summary = build_debris(
            CacheReader(cache_path), events, debris_settings(scene),
            glass_settings=glass_settings(scene),
            frame_start=int(start_frame),
            playback_fps=float(playback_fps),
            output_fps=float(output_fps),
            ground_shift=ground_shift,
        )
        bake_debris(
            summary.get("hero_objects", []),
            summary.get("bake_start", scene.frame_start),
            summary.get("bake_end", scene.frame_end),
        )
    return summary


def _fmt(v: float, nd: int = 6) -> str:
    return f"{v:.{nd}f}"


def fingerprint() -> str:
    """Deterministic structural fingerprint of the current debris scene.

    Particle simulation is live and its RNG is owned by Blender, not by the
    build — so the fingerprint deliberately covers BUILD output only (counts,
    settings, transforms), which is what the crc32 seed fix made stable.
    """
    import mathutils

    lines: List[str] = []
    scene = bpy.context.scene
    targets = []
    for coll_name in DEBRIS_COLLECTIONS:
        coll = bpy.data.collections.get(coll_name)
        if coll is not None:
            targets.extend(o for o in coll.objects if o not in targets)
    shard_names = sorted(bpy.data.meshes, key=lambda m: m.name)
    shard_names = [m.name for m in shard_names if m.name.startswith("shard_")]
    lines.append(f"shard_meshes={len(shard_names)}")
    lines.append(f"shard_names={'|'.join(shard_names)}")

    for o in sorted(targets, key=lambda o: o.name):
        loc = o.location
        rot = getattr(o, "rotation_euler", None)
        rot = tuple(rot) if rot is not None else tuple(o.rotation_quaternion)
        scl = o.scale
        line = [o.name, _fmt(loc.x), _fmt(loc.y), _fmt(loc.z),
                "|".join(_fmt(x) for x in rot), "|".join(_fmt(x) for x in scl)]
        mod = next((m for m in o.modifiers if m.type == "PARTICLE_SYSTEM"), None)
        if mod is not None:
            st = mod.particle_system.settings
            line += ["ps", str(int(st.frame_start)),
                     str(int(st.count)), str(int(st.frame_end)),
                     _fmt(st.normal_factor), _fmt(st.factor_random),
                     _fmt(st.particle_size), _fmt(st.size_random),
                     _fmt(st.mass), _fmt(st.damping), str(int(st.subframes)),
                     "|".join(_fmt(x) for x in st.object_align_factor)]
        lines.append(" | ".join(line))
    blob = "\n".join(lines).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def main(argv: Sequence[str]) -> int:
    argv_blend = [a for a in argv if a.lower().endswith(".blend")]
    blend = argv_blend[0] if argv_blend else DEFAULT_BLEND
    save_fp = None
    check_fp = None
    for i, a in enumerate(argv):
        if a == "--save-fp" and i + 1 < len(argv):
            save_fp = argv[i + 1]
        if a == "--check-fp" and i + 1 < len(argv):
            check_fp = argv[i + 1]
    if not os.path.exists(blend):
        log(f"[VERIFY4][FAIL] blend not found: {blend}")
        return 1

    bpy.ops.wm.open_mainfile(filepath=blend)
    scene = bpy.context.scene
    cache_path = scene.get("_beamng_cache_path") or \
        getattr(scene.beamng, "cache_path", "")
    if not cache_path or not os.path.exists(cache_path):
        log(f"[VERIFY4][FAIL] cache not found: {cache_path}")
        return 1
    start_frame = int(getattr(scene.beamng, "start_frame", 0))
    playback_fps = float(getattr(scene.beamng, "playback_fps", 24))
    output_fps = float(getattr(scene.beamng, "output_fps", 60))
    ground_shift = float(scene.get("_beamng_ground_shift", 0.0))
    log(f"[VERIFY4] opening {blend}")

    first = rebuild(scene, cache_path, start_frame, playback_fps, output_fps,
                    ground_shift)
    fp1 = fingerprint()
    log(f"[VERIFY4] 1st rebuild: {first.get('hero')} hero, "
        f"{first.get('emitters')} emitters, {first.get('shards')} shards, "
        f"{first.get('glass')} glass -> fp={fp1[:16]}...")

    second = rebuild(scene, cache_path, start_frame, playback_fps, output_fps,
                     ground_shift)
    fp2 = fingerprint()
    log(f"[VERIFY4] 2nd rebuild: {second.get('hero')} hero, "
        f"{second.get('emitters')} emitters, {second.get('shards')} shards, "
        f"{second.get('glass')} glass -> fp={fp2[:16]}...")

    ok = True
    if fp1 != fp2:
        log(f"[VERIFY4][FAIL] in-session rebuild drifted:\n  {fp1}\n  {fp2}")
        FAILS.append("second rebuild in the same session differs")
        ok = False
    else:
        log(f"[VERIFY4][PASS] in-session rebuild byte-identical (fp={fp1[:16]}...)")

    if save_fp:
        with open(save_fp, "w", encoding="utf-8") as fh:
            fh.write(fp1 + "\n")
        log(f"[VERIFY4] saved fingerprint -> {save_fp}")
    if check_fp:
        if os.path.exists(check_fp):
            with open(check_fp, "r", encoding="utf-8") as fh:
                saved = fh.read().strip()
            if saved == fp1:
                log(f"[VERIFY4][PASS] rebuild matches saved fingerprint "
                    f"(fp={fp1[:16]}...) — deterministic across sessions")
            else:
                log(f"[VERIFY4][FAIL] rebuild differs from saved fingerprint "
                    f"(saved {saved[:16]}..., now {fp1[:16]}...)")
                FAILS.append("rebuild differs from the saved fingerprint")
                ok = False
        else:
            log(f"[VERIFY4][FAIL] --check-fp file not found: {check_fp}")
            FAILS.append("check-fp file missing")
            ok = False

    if not ok:
        log(f"[VERIFY4][FAIL] " + "; ".join(FAILS))
        return 1
    log(f"[VERIFY4][PASS] debris rebuild is deterministic")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
