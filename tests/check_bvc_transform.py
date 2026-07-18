from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from runtime.cache_reader import CacheReader

for p in [
    r"C:\Users\ubaid_i2c\Downloads\beamng-cache-importer\testfinal.bvc",
    r"C:\Users\ubaid_i2c\Downloads\beamng-cache-importer\test_captures\testfinal.bvc",
]:
    try:
        r = CacheReader(p)
        h = r.header
        print(f"{p}:")
        print(f"  version={h['version']}  frames={h['frame_count']}  objects={h['object_count']}")
        tdo = h.get("transform_data_offset", 0)
        print(f"  transform_data_offset={tdo}")
        if tdo:
            tf0 = r.frame_transform(0)
            print(f"  frame 0 transform: {tf0}")
            tfn = r.frame_transform(h["frame_count"] - 1)
            print(f"  frame {h['frame_count']-1} transform: {tfn}")
        r.close()
    except Exception as e:
        print(f"{p}: ERROR {e}")
