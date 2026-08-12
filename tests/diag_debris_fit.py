from __future__ import annotations

"""Recover the frame mapping a debris build baked against, from the scene.

Phase 1 (ANALYSIS ONLY — reads, no writes).  The debris object names carry the
build-time TIMELINE spawn frame (``debris_<material>_<spawn_frame>_NNN``), and
the impacts that spawned them are in the BVC.  We fit

    spawn_frame = start_b + cache_frame * (output_b / playback_b)

over (event.cache_frame -> spawn suffix) pairs and report the candidates.

Run: blender --background --python tests/diag_debris_fit.py -- <file.blend>
"""

import re
import sys
from collections import OrderedDict
from pathlib import Path

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


SUFFIX_RE = re.compile(r"^(debris_.+?)_(\d+)(?:_\d{3})?$")


def suffix_pairs():
    """{material: [spawn_frame, ...]} in build order from object names.

    Hero chunks carry the build-time timeline spawn frame in their name
    (``debris_<material>_<spawn_frame>_NNN``); emitters repeat the same frame
    (``debris_emit_...``) so they are skipped to keep one entry per event.
    """
    out = OrderedDict()
    for obj in bpy.data.objects:
        if obj.name.startswith("debris_") and not obj.name.startswith("debris_emit_"):
            m = SUFFIX_RE.match(obj.name)
            if m:
                out.setdefault(m.group(1)[len("debris_"):], []).append(int(m.group(2)))
    for mat, vals in out.items():
        seen = []
        for v in vals:
            if not seen or seen[-1] != v:
                seen.append(v)
        out[mat] = seen
    return out


def main() -> None:
    path = sys.argv[sys.argv.index("--") + 1]
    bpy.ops.wm.open_mainfile(filepath=path)
    scene = bpy.context.scene
    cache_path = scene.get("_beamng_cache_path", "")
    ground_shift = float(scene.get("_beamng_ground_shift", 0.0))
    print(f"cache: {cache_path!r}  ground_shift={ground_shift}")

    print("\n=== debris name suffixes per material (build order) ===")
    suffixes = suffix_pairs()
    for mat, vals in suffixes.items():
        print(f"  {mat}: {vals[:30]}{'...' if len(vals) > 30 else ''} ({len(vals)})")
    if not suffixes:
        print("  (no debris objects in the scene)")
        return

    import sys as _sys
    repo = _sys.path[0]
    print(f"\nrepo on path: {repo!r}")

    from runtime.cache_reader import CacheReader
    from runtime.impact_detect import detect_impacts

    reader = CacheReader(cache_path)
    print(f"cache frames: {reader.frame_count}")
    try:
        for pb in (24.0, 27.0, 30.0, 48.0, 60.0):
            events = detect_impacts(reader, ground_shift=ground_shift,
                                    playback_fps=pb)
            print(f"\n=== events @ playback_fps={pb} ===")
            per_mat = OrderedDict()
            for e in events:
                per_mat.setdefault(e.material, []).append(e.cache_frame)
            total = 0
            for mat, frames in per_mat.items():
                print(f"  {mat}: {frames[:30]}{'...' if len(frames) > 30 else ''}"
                      f" ({len(frames)})")
                total += len(frames)
            print(f"  TOTAL events: {total}")
    finally:
        reader.close()
    print("DONE")


main()