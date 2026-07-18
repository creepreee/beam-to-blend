bl_info = {
    "name": "BeamNG Cache Importer",
    "author": "AI contributors",
    "version": (0, 1, 0),
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


def register():
    ui.register()
    operators.register()
    preferences.register()


def unregister():
    preferences.unregister()
    operators.unregister()
    ui.unregister()
