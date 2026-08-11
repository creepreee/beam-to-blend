"""Rebuild name.bvc from name.bmc (weld=False, matching the addon default).

Builds to a temp path, verifies the result, then atomically replaces the live
cache.  Prints a [CACHE] tag line on every line for easy log filtering.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from importer.cache_builder import CacheBuilder

CAPTURES = r"C:\Users\ubaid_i2c\AppData\Local\BeamNG\BeamNG.drive\current\captures"
BMC = os.path.join(CAPTURES, "name.bmc")
OUT_LIVE = os.path.join(CAPTURES, "name.bvc")
OUT_TMP = os.path.join(CAPTURES, "name.rebuilt.bvc")


def tag(msg):
    print(f"[CACHE] {msg}", flush=True)


def main():
    if not os.path.exists(BMC):
        tag(f"FATAL: source capture missing: {BMC}")
        return 1
    if os.path.exists(OUT_TMP):
        tag(f"removing stale temp output {OUT_TMP}")
        os.remove(OUT_TMP)

    tag(f"source = {BMC} ({os.path.getsize(BMC) / 1e9:.2f} GB)")
    tag(f"target = {OUT_LIVE}")
    t0 = time.time()
    tag(f"build start {time.strftime('%H:%M:%S')}")

    builder = CacheBuilder(CAPTURES, OUT_TMP)
    manifest = builder.build_from_capture(BMC, weld=False)

    dt = time.time() - t0
    tag(f"build done in {dt:.0f}s ({dt / 60:.1f} min) "
        f"frames={manifest.frame_count} "
        f"objects={len(manifest.stable_objects)}")
    tag(f"output = {OUT_TMP} ({os.path.getsize(OUT_TMP) / 1e9:.2f} GB)")

    # Verify header before swapping.
    from runtime.cache_reader import CacheReader
    check = CacheReader(OUT_TMP)
    tag(f"verify: frames={check.frame_count}, "
        f"objects={len(check.stable_objects())}, "
        f"version={check.header.get('version')}, "
        f"transform={'yes' if check.header.get('transform_data_offset') else 'no'}")
    if check.frame_count != manifest.frame_count:
        tag("FATAL: header frame_count mismatch")
        return 1
    check._mmap._mmap.close()
    del check

    # Swap.
    backup = OUT_LIVE + ".old"
    if os.path.exists(backup):
        os.remove(backup)
    if os.path.exists(OUT_LIVE):
        os.rename(OUT_LIVE, backup)
        tag(f"moved existing cache -> {backup}")
    os.rename(OUT_TMP, OUT_LIVE)
    tag(f"installed {OUT_LIVE} ({os.path.getsize(OUT_LIVE) / 1e9:.2f} GB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
