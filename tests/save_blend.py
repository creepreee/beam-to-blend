import os, sys

import bpy

_REPO = r'C:\Users\ubaid_i2c\Downloads\beamng-cache-importer'
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from runtime.cache_reader import CacheReader
from runtime.mesh_update import CachePlayback
from runtime import frame_handler

BVC = r'C:\Users\ubaid_i2c\AppData\Local\BeamNG\BeamNG.drive\current\captures\mycap2\capture2_v4.bvc'
OUT = r'C:\Users\ubaid_i2c\Downloads\beamng-cache-importer\test_import_v4.blend'

reader = CacheReader(BVC)
playback = CachePlayback(reader)
playback.build_scene()
frame_handler.attach(playback, frame_start=1)

bpy.context.scene.frame_set(1)

bpy.ops.wm.save_as_mainfile(filepath=OUT)
print(f"Saved: {OUT}")

playback.close()
reader.close()
