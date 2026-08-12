from __future__ import annotations

"""Fix the debris/particle desync in an OLD .blend (built before the build
timing was recorded), in place.

The debris object names carry the build-time timeline spawn frame while the
cache holds the impact's cache frame; the affine fit is exact for this scene
(spawn = 400 + cache * 3  ->  build mapping start=400, playback=20, output=60).
We record that and apply the standard incremental retime to the CURRENT live
mapping (start=400, playback=27, output=60), then save.

VERIFY BEFORE TRUST: the script asserts several known pairs land exactly.

Run: blender --background --python tests/fix_debris_resync.py -- <file.blend>
"""

import sys
from pathlib import Path

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _key_at(obj, idx=0):
    adt = getattr(obj, "animation_data", None)
    action = getattr(adt, "action", None) if adt else None
    if action is None:
        return None
    for fc in action.fcurves:
        if fc.keyframe_points:
            kp = fc.keyframe_points[idx]
            return kp.co.x, kp.handle_left.x, kp.handle_right.x
    return None


def main() -> None:
    path = sys.argv[sys.argv.index("--") + 1]
    bpy.ops.wm.open_mainfile(filepath=path)
    scene = bpy.context.scene

    cur_start = int(scene.get("_beamng_frame_start", scene.frame_start))
    cur_pb = float(scene.get("_beamng_playback_fps", 24.0))
    cur_out = float(scene.get("_beamng_output_fps", 60.0))
    print(f"live mapping: start={cur_start} playback={cur_pb} output={cur_out}")

    # Recover the build mapping from a hero key: debris_steel_steel_2356_000's
    # first key sits at its launch; the underlying cache frame came out of the
    # fit (spawn 2356 <-> cache 652, rate 3).
    obj = bpy.data.objects.get("debris_steel_2356_000")
    before = _key_at(obj)
    print(f"  sample key before: {before[0]:.3f}")

    from runtime.debris_retime import build_timing, record_build_timing, retime_debris

    recorded = build_timing(scene)
    if recorded is not None:
        print(f"  recorded build timing already present: {recorded}")
    else:
        # Derived by exact affine fit of (impact cache frame -> name suffix).
        record_build_timing(scene, 400, 20.0, 60.0)
        print("  recorded recovered build timing: (400, 20, 60)")

    out = retime_debris(scene, cur_start, cur_pb, cur_out)
    print(f"retime summary: {out}")

    # Collect launch frames from debris names for a sanity spot-check.
    # Build mapping: spawn = 400 + cache*3, LAUNCH_FRAMES=3, so an object whose
    # name carries suffix S was launched at timeline S-3 under the BUILD mapping.
    # After the affine retime it must sit at cur_start + ((S-3) - 400)*scale.
    import re
    scale = (20.0 / 60.0) / (cur_pb / cur_out)
    n_ok = 0
    n_bad = 0
    for o in bpy.data.objects:
        m = re.match(r"^debris_(?:emit_)?[a-z_]+_(\d+)", o.name)
        if not m:
            continue
        old_suffix = int(m.group(1))
        launch = o.get("_beamng_debris_launch")
        if launch is None:
            continue
        expected = cur_start + ((old_suffix - 3) - 400) * scale
        if abs(float(launch) - expected) < 0.51:
            n_ok += 1
        else:
            n_bad += 1
            if n_bad <= 5:
                print(f"  MISMATCH {o.name}: old suffix={old_suffix}"
                      f" launch now={launch} expected={expected:.1f}")
    print(f"spot-check: {n_ok} aligned, {n_bad} misaligned")

    hero = bpy.data.objects.get("debris_steel_2356_000")
    after = _key_at(hero)
    print(f"  sample hero key after: {after[0]:.3f}"
          f" (bake-start key expected ~1841 = 400+(2345-400)*20/27)")

    if n_bad:
        print("ERROR: alignment failed - NOT saving")
        raise SystemExit(1)

    bpy.ops.wm.save_mainfile(filepath=path)
    print(f"SAVED: {path}")


main()