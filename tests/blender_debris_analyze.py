"""Fast diagnostics on a saved debris blend.

    blender --background --python tests/blender_debris_analyze.py

Opens the blend saved by blender_debris_retention.py (SAVE_BLEND env) and
prints the root empty's translation+rotation and a probe fragment's world and
local (matrix_basis) translation at its shatter frame and at the last frame, so
we can see whether the parented fringe rides the root.
"""

import os
import sys

import bpy

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ZIP = os.path.join(_REPO, "dist", "beamng_cache_importer.zip")
_BLEND = os.environ.get(
    "ANALYZE_BLEND",
    r"C:\Users\ubaid_i2c\AppData\Local\Temp\opencode\retention.blend")


def log(msg):
    print(msg, flush=True)


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.preferences.addon_install(filepath=_ZIP, overwrite=True)
    bpy.ops.preferences.addon_enable(module="beamng_cache_importer")
    bpy.ops.wm.open_mainfile(filepath=_BLEND)

    root = None
    for o in bpy.data.objects:
        if o.type == "EMPTY" and o.name.endswith("__root"):
            root = o
    log(f"[ANA] root: {root.name if root else None}")

    parented = [o for o in bpy.data.objects
                if o.name.startswith("glassfrag_") and o.parent is not None]
    log(f"[ANA] parented fringe: {len(parented)}")

    scene = bpy.context.scene
    last = scene.frame_end
    probe = parented[0]
    spawn = int(probe.get("_beamng_debris_launch", scene.frame_start))

    def snap(label):
        scene.frame_set(scene.frame_current)
        bpy.context.view_layer.update()
        log(f"[ANA] {label}: root t {tuple(round(float(x), 3) for x in root.matrix_world.translation)} "
            f"e {tuple(round(float(x), 3) for x in root.matrix_world.to_euler())}")
        log(f"[ANA] {label}: {probe.name} t {tuple(round(float(x), 3) for x in probe.matrix_world.translation)} "
            f"basis {tuple(round(float(x), 3) for x in probe.matrix_basis.to_translation())} "
            f"parent_inv t {tuple(round(float(x), 3) for x in probe.matrix_parent_inverse.translation)}")

    scene.frame_set(spawn)
    snap("spawn")
    scene.frame_set(last)
    snap("end")

    # Does the fragment's world position match root.M(end) @ parent_inv @ basis?
    expected = (root.matrix_world @ probe.matrix_parent_inverse @ probe.matrix_basis)
    log(f"[ANA] expected(world recompute) {tuple(round(float(x), 3) for x in expected.translation)}")
    log(f"[ANA] actual(world)            {tuple(round(float(x), 3) for x in probe.matrix_world.translation)}")

    if os.path.exists(_BLEND):
        pass
    log("[ANA][DONE]")


if __name__ == "__main__":
    main()
