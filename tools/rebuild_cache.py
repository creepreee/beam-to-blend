"""Rebuild a .bvc cache from a .bmc capture (weld=False, matching the addon default).

Builds to a temp path, verifies the result, then atomically replaces the live
cache.  Prints a [CACHE] tag line on every line for easy log filtering.

Usage:
    python tools/rebuild_cache.py --captures <dir> [--name name] [--no-swap]
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from importer.cache_builder import CacheBuilder


def tag(msg):
    print(f"[CACHE] {msg}", flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--captures", required=True,
                    help="BeamNG captures directory (containing <name>.bmc)")
    ap.add_argument("--name", default="name",
                    help="capture base name, default: name")
    ap.add_argument("--no-swap", action="store_true",
                    help="verify only; do not replace the live .bvc")
    args = ap.parse_args()

    captures = os.path.abspath(args.captures)
    bmc = os.path.join(captures, f"{args.name}.bmc")
    out_live = os.path.join(captures, f"{args.name}.bvc")
    out_tmp = os.path.join(captures, f"{args.name}.rebuilt.bvc")

    if not os.path.exists(bmc):
        tag(f"FATAL: source capture missing: {bmc}")
        return 1
    if os.path.exists(out_tmp):
        tag(f"removing stale temp output {out_tmp}")
        os.remove(out_tmp)

    tag(f"source = {bmc} ({os.path.getsize(bmc) / 1e9:.2f} GB)")
    tag(f"target = {out_live}")
    t0 = time.time()
    tag(f"build start {time.strftime('%H:%M:%S')}")

    builder = CacheBuilder(captures, out_tmp)
    manifest = builder.build_from_capture(bmc, weld=False)

    dt = time.time() - t0
    tag(f"build done in {dt:.0f}s ({dt / 60:.1f} min) "
        f"frames={manifest.frame_count} "
        f"objects={len(manifest.stable_objects)}")
    tag(f"output = {out_tmp} ({os.path.getsize(out_tmp) / 1e9:.2f} GB)")

    # Verify header before swapping.
    from runtime.cache_reader import CacheReader
    check = CacheReader(out_tmp)
    tag(f"verify: frames={check.frame_count}, "
        f"objects={len(check.stable_objects())}, "
        f"version={check.header.get('version')}, "
        f"transform={'yes' if check.header.get('transform_data_offset') else 'no'}")
    if check.frame_count != manifest.frame_count:
        tag("FATAL: header frame_count mismatch")
        return 1
    check._mmap._mmap.close()
    del check

    if args.no_swap:
        tag("--no-swap: leaving rebuilt cache at " + out_tmp)
        return 0

    # Swap.
    backup = out_live + ".old"
    if os.path.exists(backup):
        os.remove(backup)
    if os.path.exists(out_live):
        os.rename(out_live, backup)
        tag(f"moved existing cache -> {backup}")
    os.rename(out_tmp, out_live)
    tag(f"installed {out_live} ({os.path.getsize(out_live) / 1e9:.2f} GB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
