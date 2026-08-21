"""Probe: does the add-on auto-recover when a .blend opens in BACKGROUND mode?

Run EXACTLY like a batch render would:
    blender -b <file.blend> --python tests/diag_bg_loadpost.py

Answers, in order:
  1. is the add-on ENABLED in this machine's Blender prefs?
  2. did its handlers get registered (load_post / frame_change / render_pre)?
  3. did load_post fire and populate frame_handler._active?
  4. are the _beamng_* scene props present in this file?

If (1) is False nothing else can work: a disabled add-on registers nothing,
so no handler exists to recover playback — this is failure mode #1 in
CLAUDE.md.  Works on any OS; run the same one-liner on the render box.
"""

import importlib
import sys

import bpy

print("[BGPROBE] === background load_post probe ===")
print(f"[BGPROBE] Blender {bpy.app.version_string}, "
      f"background={bpy.app.background}")

scene = bpy.context.scene
print(f"[BGPROBE] scene={scene.name!r} "
      f"range {scene.frame_start}..{scene.frame_end}")
for prop in ("_beamng_cache_path", "_beamng_playback_fps",
             "_beamng_output_fps", "_beamng_start_frame"):
    print(f"[BGPROBE] scene.{prop} = {scene.get(prop, '<absent>')}")

enabled = "beamng_cache_importer" in bpy.context.preferences.addons
print(f"[BGPROBE] add-on enabled in prefs: {enabled}")


def named(handlers):
    return [getattr(h, "__name__", repr(h))[:50] for h in handlers]


for name in ("load_post", "frame_change_pre", "render_pre"):
    lst = named(getattr(bpy.app.handlers, name))
    beamng = [n for n in lst if n.startswith("_on_")]
    print(f"[BGPROBE] {name}: total={len(lst)} beamng={beamng}")

if enabled:
    mod = importlib.import_module("beamng_cache_importer")
    fh = importlib.import_module("beamng_cache_importer.runtime."
                                 "frame_handler")
    top = importlib.import_module("runtime.frame_handler") \
        if "runtime" in sys.modules else None
    if top is not None:
        same = fh is top
        print(f"[BGPROBE] module identity: packaged IS top-level "
              f"runtime.frame_handler -> {same}")
    active = getattr(fh, "_active", "NO-MODULE")
    print(f"[BGPROBE] frame_handler._active = {active}")
    if active in (None, "NO-MODULE"):
        ok = False
        if active is None:
            ok = fh._try_recover(scene)
            print(f"[BGPROBE] manual _try_recover -> {ok}, "
                  f"_active now {fh._active}")
            if fh._active is not None:
                r = fh._active._reader if hasattr(fh._active, "_reader") \
                    else None
                n = getattr(r, "frame_count", "?") if r else "?"
                print(f"[BGPROBE] recovered playback frames={n}")
    else:
        print("[BGPROBE] VERDICT: load_post auto-recovery WORKED "
              "(playback live before the script ran)")
else:
    print("[BGPROBE] VERDICT: add-on NOT ENABLED on this machine — "
          "no handler could ever fire. Install the zip and tick the "
          "checkbox (Edit > Preferences > Add-ons), then re-run.")
