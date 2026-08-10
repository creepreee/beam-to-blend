bl_info = {
    "name": "BeamNG Cache Importer",
    "author": "AI contributors",
    "version": (0, 2, 0),
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

from . import ui, operators, preferences  # noqa: E402,F401


def _on_load_post(_dummy):
    """Auto-recover BeamNG animation after file reload.

    Blender's load clears module-level state (_active).  If the loaded file
    has BeamNG metadata stored on the scene, _try_recover will recreate the
    playback from the stored BVC path.
    """
    import bpy
    from runtime import frame_handler
    scene = bpy.context.scene
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
