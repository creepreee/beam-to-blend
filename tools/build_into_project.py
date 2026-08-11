"""Build the crack/debris check into the user's project blend and save it.

Faithful to the real UI clicks:
  addon enable -> open file (load_post auto-recovers the playback)
  -> Clear Debris (strips the old, pre-crack debris)
  -> Build Debris (user's saved settings: cracks ON, shatter OFF, hero 14/fine 90)
  -> verify cracks landed on the glass chunk
  -> save the file

Exits non-zero on failure WITHOUT saving, so the project file is never
half-built.
"""
import os
import sys

import bpy

BLEND = r"C:\Users\ubaid_i2c\Downloads\vehicle materials ready made.blend"


def main():
    print("[BUILD] enabling addon ...", flush=True)
    res = bpy.ops.preferences.addon_enable(module="beamng_cache_importer")
    print(f"[BUILD] addon_enable -> {res}", flush=True)

    print(f"[BUILD] opening {os.path.basename(BLEND)} ...", flush=True)
    bpy.ops.wm.open_mainfile(filepath=BLEND)
    scene = bpy.context.scene
    print(f"[BUILD] scene={scene.name} objs={len(bpy.data.objects)} "
          f"frame_range={scene.frame_start}..{scene.frame_end}", flush=True)

    # load_post should have auto-recovered the playback (addon was registered
    # BEFORE the open).  Confirm the live playback exists so crack face-slicing
    # can address the merged glass chunk.
    from runtime import frame_handler
    live = frame_handler._active is not None
    print(f"[BUILD] playback live after recovery: {live}", flush=True)
    if not live:
        print("[BUILD] playback NOT live - running manual recovery ...", flush=True)
        if not frame_handler._try_recover(scene):
            print("[BUILD][FAIL] recovery failed - aborting without save", flush=True)
            sys.exit(1)

    print("[BUILD] clearing old debris ...", flush=True)
    res = bpy.ops.beamng.clear_debris()
    print(f"[BUILD] clear_debris -> {res}", flush=True)

    # The crack painter only runs when glass shatter is on (debris_spawn gates
    # BOTH the crack and shatter branches behind settings.shatter_glass, and the
    # UI greys the crack panel out unless it is ticked).  The saved file had
    # debris_shatter_glass=False with glass_crack_enabled=True, so cracks were
    # silently a no-op.  Enable shatter so the cracks actually appear.
    if not scene.beamng_debris.debris_shatter_glass:
        print("[BUILD] enabling debris_shatter_glass (required for cracks)",
              flush=True)
        scene.beamng_debris.debris_shatter_glass = True

    print("[BUILD] building debris (detect -> spawn -> bake) ...", flush=True)
    res = bpy.ops.beamng.build_debris()
    print(f"[BUILD] build_debris -> {res}", flush=True)
    if res != {"FINISHED"}:
        print("[BUILD][FAIL] build_debris did not finish - aborting without save",
              flush=True)
        sys.exit(1)

    # --- verification ------------------------------------------------------
    mats = [m.name for m in bpy.data.materials
            if m.name.startswith("BeamNG Crack ")]
    print(f"[BUILD] crack materials: {len(mats)}", flush=True)
    for m in sorted(mats):
        print(f"[BUILD]   {m}", flush=True)

    glass = bpy.data.objects.get("glass")
    crack_slots = []
    if glass is not None and glass.data is not None:
        for i, mat in enumerate(glass.data.materials):
            if mat is not None and mat.name.startswith("BeamNG Crack "):
                crack_slots.append(i)
        print(f"[BUILD] glass chunk has {len(glass.data.materials)} slots, "
              f"crack slots: {crack_slots}", flush=True)
    if not mats or not crack_slots:
        print("[BUILD][FAIL] cracks not found on glass - aborting without save",
              flush=True)
        sys.exit(1)

    # Set playhead to just after the first crack impact (cache ~652 ->
    # blender (652 - 400) * 60/20 + ... via the live mapping) so the crack is
    # visible the moment the file is opened.
    try:
        from runtime import frame_handler as fh
        target = None
        for f in range(scene.frame_start, scene.frame_end + 1):
            if fh._cache_frame_for(f) >= 652:
                target = f
                break
        if target:
            scene.frame_current = target
            fh._on_frame_change(scene)
            print(f"[BUILD] playhead parked at frame {target} "
                  f"(cache {fh._cache_frame_for(target)})", flush=True)
    except Exception as exc:
        print(f"[BUILD] (playhead park skipped: {exc})", flush=True)

    print("[BUILD] saving file ...", flush=True)
    bpy.ops.wm.save_mainfile(filepath=BLEND)
    print("[BUILD][PASS] saved", flush=True)


if __name__ == "__main__":
    main()
