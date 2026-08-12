from __future__ import annotations

"""Inspect a .blend for the debris/particle fps-desync state.

Opens the file, dumps the live frame-handler props, the recorded debris build
timing, debris collection contents, keyframe ranges, particle windows and the
rigid body world — everything needed to see why debris is not resynced to the
car after a live fps change.

Run: blender --background --python tests/diag_debris_blend.py -- <file.blend>
"""

import sys

import bpy


def main() -> None:
    path = sys.argv[sys.argv.index("--") + 1]
    bpy.ops.wm.open_mainfile(filepath=path)

    scene = bpy.context.scene

    print("=== LIVE frame handler props ===")
    for key in ("_beamng_cache_path", "_beamng_frame_start",
                "_beamng_start_frame", "_beamng_playback_fps",
                "_beamng_output_fps", "_beamng_smooth_stop_frames"):
        print(f"  {key} = {scene.get(key)!r}")

    print("=== Recorded debris build timing ===")
    for key in ("_beamng_debris_build_start",
                "_beamng_debris_build_playback_fps",
                "_beamng_debris_build_output_fps"):
        print(f"  {key} = {scene.get(key)!r}")

    print("=== Timeline ===")
    print(f"  frame_start={scene.frame_start}  frame_end={scene.frame_end}"
          f"  render.fps={scene.render.fps}")

    print("=== Debris collections ===")
    from collections import Counter
    count = Counter()
    emitters = 0
    for coll_name in ("BeamNG Debris", "BeamNG Debris Shards",
                      "BeamNG Debris Glass"):
        coll = bpy.data.collections.get(coll_name)
        if coll is None:
            print(f"  {coll_name}: (missing)")
            continue
        parts = [o for o in coll.all_objects
                 if any(getattr(m, "particle_system", None)
                        for m in getattr(o, "modifiers", ()) or ())]
        count[coll_name] = len(coll.all_objects)
        emitters += len(parts)
        print(f"  {coll_name}: {len(coll.all_objects)} objects"
              f" ({len(parts)} emitters)")

    print(f"  TOTAL debris objects: {sum(count.values())}, emitters: {emitters}")

    print("=== Keyframe ranges per debris object (first 8) ===")
    seen = set()
    shown = 0
    for coll_name in ("BeamNG Debris", "BeamNG Debris Shards",
                      "BeamNG Debris Glass"):
        coll = bpy.data.collections.get(coll_name)
        if coll is None:
            continue
        for obj in coll.all_objects:
            adt = getattr(obj, "animation_data", None)
            action = getattr(adt, "action", None) if adt else None
            if action is None or action.name in seen:
                continue
            seen.add(action.name)
            lo, hi = 1e18, -1e18
            for fc in action.fcurves:
                for kp in fc.keyframe_points:
                    lo = min(lo, kp.co.x)
                    hi = max(hi, kp.co.x)
            launch = obj.get("_beamng_debris_launch")
            print(f"  {obj.name}: action={action.name}"
                  f" keys {lo:.0f}..{hi:.0f} launch={launch}")
            shown += 1
            if shown >= 8:
                break
        if shown >= 8:
            break

    print("=== Particle windows (first 8 emitters) ===")
    shown = 0
    for obj in bpy.data.objects:
        for mod in getattr(obj, "modifiers", ()) or ():
            psys = getattr(mod, "particle_system", None)
            if psys is None:
                continue
            st = psys.settings
            print(f"  {obj.name}: start={st.frame_start:.0f}"
                  f" end={st.frame_end:.0f} life={st.lifetime}")
            shown += 1
            if shown >= 8:
                break
        if shown >= 8:
            break

    print("=== Rigid body world ===")
    rbw = getattr(scene, "rigidbody_world", None)
    if rbw is None:
        print("  (none)")
    else:
        pc = rbw.point_cache
        print(f"  present, cache {pc.frame_start}..{pc.frame_end}")

    print("=== Installed add-on version ===")
    import addon_utils
    for mod in addon_utils.modules():
        name = getattr(mod, "bl_info", {}).get("name", "")
        if "BeamNG" in name or "beamng" in getattr(mod, "__name__", ""):
            print(f"  {getattr(mod, '__name__', '?')}: {getattr(mod, 'bl_info', {})}")
    print("DONE")


main()