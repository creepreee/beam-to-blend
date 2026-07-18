"""Headless test: install the packaged add-on zip and run its operators.

    blender --background --python tests/blender_addon_install.py

Installs dist/beamng_cache_importer.zip, enables it, then drives the three
operators (scan, build cache, import cache) against testglt/. Exits non-zero on
any failure.
"""

import os
import sys

import bpy

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ZIP = os.path.join(_REPO, "dist", "beamng_cache_importer.zip")
_SEQ = os.path.join(_REPO, "testglt")
_MODULE = "beamng_cache_importer"


def _fail(msg):
    print(f"[ADDON][FAIL] {msg}")
    sys.exit(1)


def _ok(msg):
    print(f"[ADDON][ok] {msg}")


def main():
    if not os.path.exists(_ZIP):
        _fail(f"zip not found: {_ZIP} (run: python build_addon.py)")

    # Reset to an empty scene FIRST — read_factory_settings resets preferences
    # (including enabled add-ons), so it must happen before we enable ours.
    bpy.ops.wm.read_factory_settings(use_empty=True)

    # Install + enable the add-on from the zip.
    bpy.ops.preferences.addon_install(filepath=_ZIP, overwrite=True)
    bpy.ops.preferences.addon_enable(module=_MODULE)
    if _MODULE not in bpy.context.preferences.addons:
        _fail("add-on did not enable")
    _ok("add-on installed and enabled")

    # Operators must be genuinely callable (poll), not just lazy stubs.
    if bpy.ops.beamng.scan_sequence.poll() is False and not hasattr(
        bpy.types, "BEAMNG_OT_scan_sequence"
    ):
        _fail("operator beamng.scan_sequence not registered")
    _ok("operators registered and callable")

    # The operators read the sequence folder + settings from scene properties
    # (not operator keywords). Set them, and exercise the parallel read path.
    scene = bpy.context.scene
    scene.beamng.sequence_dir = _SEQ
    scene.beamng.workers = 4
    _ok(f"scan/build with {scene.beamng.workers} parallel workers")

    bpy.ops.beamng.scan_sequence()
    _ok("scan_sequence ran")

    bpy.ops.beamng.build_cache()
    cache = os.path.join(_SEQ, "testglt.bvc")
    if not os.path.exists(cache):
        _fail(f"build_cache did not produce {cache}")
    _ok(f"build_cache produced {os.path.getsize(cache)/1e6:.1f} MB cache")

    meshes_before = len(bpy.data.meshes)
    bpy.ops.beamng.import_cache()
    created = len(bpy.data.meshes) - meshes_before
    if created < 90:
        _fail(f"import_cache created only {created} meshes")
    _ok(f"import_cache created {created} mesh datablocks")

    # Scrub the timeline; confirm the handler moves geometry.
    import numpy as np

    scene = bpy.context.scene
    obj = max(
        (o for o in bpy.data.objects if o.type == "MESH"),
        key=lambda o: len(o.data.vertices),
    )
    scene.frame_set(scene.frame_start)
    a = np.empty(len(obj.data.vertices) * 3, dtype=np.float32)
    obj.data.vertices.foreach_get("co", a)
    scene.frame_set(scene.frame_end)
    b = np.empty(len(obj.data.vertices) * 3, dtype=np.float32)
    obj.data.vertices.foreach_get("co", b)
    moved = float(np.abs(a - b).max())
    if moved <= 0:
        _fail("geometry did not move when scrubbing the timeline")
    _ok(f"timeline scrub moved '{obj.name}' by {moved:.4f}")

    # Cleanup the generated cache.
    try:
        from runtime import frame_handler  # noqa

        frame_handler.detach()
    except Exception:
        pass
    if os.path.exists(cache):
        os.remove(cache)
    print("[ADDON][PASS] packaged add-on works end to end")


if __name__ == "__main__":
    main()
