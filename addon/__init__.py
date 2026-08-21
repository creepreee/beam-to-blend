bl_info = {
    "name": "BeamNG Cache Importer",
    "author": "AI contributors",
    "version": (0, 3, 1),
    "blender": (4, 0, 0),
    "location": "File Browser > Sidebar > BeamNG",
    "description": "Cache-based importer for BeamNG GLB crash sequences",
    "category": "Import-Export",
}

import os
import sys

# The core `importer` and `runtime` packages are imported as top-level packages
# (e.g. `from importer import binary`). Make them resolvable whether this add-on
# is run from the dev repo (they sit in the parent dir) or installed as a
# packaged add-on (they are bundled inside this package's own directory).
_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
for _candidate in (_PKG_DIR, os.path.dirname(_PKG_DIR)):
    if _candidate not in sys.path:
        sys.path.append(_candidate)

# SINGLE MODULE IDENTITY (critical): when installed, the core packages live
# BOTH as `beamng_cache_importer.runtime|importer` and — via the sys.path
# entry above — as top-level `runtime|importer`.  Without aliasing, Python
# loads every submodule TWICE under the two names, each with SEPARATE
# module-level state: the add-on's handlers recover playback into one copy
# while external render scripts importing
# `beamng_cache_importer.runtime.frame_handler` see `_active is None` in the
# other and wrongly conclude recovery never ran (measured 2026-08-21: both
# copies alive in one session, each with its own `_active`).  Plant aliases
# for the packages AND every direct submodule BEFORE importing ui/operators,
# so every import path binds to exactly one module object per file.  In
# dev-repo layout the packaged submodules don't exist and only one copy ever
# exists.
import importlib as _importlib
import pkgutil as _pkgutil

for _short in ("importer", "runtime"):
    try:
        _pkg = _importlib.import_module(f"{__name__}.{_short}")
    except ImportError:
        continue
    sys.modules[_short] = _pkg
    for _modinfo in _pkgutil.iter_modules(_pkg.__path__):
        _mod = _importlib.import_module(
            f"{__name__}.{_short}.{_modinfo.name}")
        sys.modules[f"{_short}.{_modinfo.name}"] = _mod

from . import ui, operators, preferences  # noqa: E402,F401


def _on_load_post(_arg=None):
    """Auto-recover BeamNG animation after file reload.

    Blender's load clears module-level state (_active).  If the loaded file
    has BeamNG metadata stored on the scene, _try_recover will recreate the
    playback from the stored BVC path.

    NOTE: the argument is NOT the scene — in Blender 4.5 background mode
    load_post hands handlers the loaded FILEPATH STRING (measured).  Resolve
    the scene defensively instead: prefer bpy.context.scene, fall back to
    scanning bpy.data.scenes for the BeamNG metadata.
    """
    import bpy
    from runtime import frame_handler
    scene = bpy.context.scene
    if scene is None or not scene.get("_beamng_cache_path"):
        for candidate in bpy.data.scenes:
            if candidate.get("_beamng_cache_path"):
                scene = candidate
                break
    if scene is None:
        return
    # Clear stale state so _try_recover runs fresh
    frame_handler._active = None
    # Re-register frame handler (load clears handler list)
    frame_handler._ensure_handler_registered()
    # Try recovery from stored scene metadata
    if frame_handler._try_recover(scene):
        # Trigger an immediate frame update so meshes show correct positions
        try:
            frame_handler._on_frame_change(scene)
        except Exception:
            pass


def register():
    import bpy
    ui.register()
    operators.register()
    preferences.register()
    # Mark load handler as persistent (survives file reload) and register
    bpy.app.handlers.persistent(_on_load_post)
    bpy.app.handlers.load_post.append(_on_load_post)


def unregister():
    import bpy
    try:
        bpy.app.handlers.load_post.remove(_on_load_post)
    except ValueError:
        pass
    preferences.unregister()
    operators.unregister()
    ui.unregister()
