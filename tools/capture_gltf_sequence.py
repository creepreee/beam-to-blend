from __future__ import annotations

"""Drive BeamNG.drive to capture a per-frame GLB sequence via beamngpy.

Uses the **glTF Sequence Exporter** mod (patched) instead of stock util/export.
The glTF SE adds a root node with translation = veh:getPosition() - origin,
so each GLB has the vehicle positioned in world space.  Our GLB reader's
_node_world_matrix walks the parent chain and applies the root translation
automatically.

Pipeline:
    1. Connect to running BeamNG (user launches with -tcom -console)
    2. Load gltfSequenceExporter/export, configure options, set origin
    3. Pause + set deterministic (2000 physics Hz)
    4. For each frame:
        a. exportFile to relative path in BeamNG user dir (with root translation)
        b. step(N) to advance one frame
        c. move .glb from BeamNG user dir to output dir
    5. Resume sim

IMPORTANT: BeamNG's Lua io.open is sandboxed to the BeamNG user directory
(C:\\Users\\...\\AppData\\Local\\BeamNG\\BeamNG.drive\\current\\).  Absolute
paths to other drives FAIL silently.  We export to a temp subfolder there,
then Python shutil.move() copies each frame to the real output directory.

Requires patched gltfSequenceExporter/export.lua: the original guard at
line 1063 short-circuits with handler(nil) when lastMeshInfo is non-nil.
Our patch calls _ensureResourcedFreed() instead, allowing re-export.
"""

import argparse
import shutil
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
#  Defaults
# ---------------------------------------------------------------------------
BEAMNG_USER = Path(r"C:\Users\ubaid_i2c\AppData\Local\BeamNG\BeamNG.drive\current")
GAME_HOME = r"D:\danish\Games\beamng\BeamNG.drive"
OUT_DIR = r"D:\animation"
PORT = 25252

PHYSICS_HZ = 2000
TARGET_FPS = 60
STEPS_PER_FRAME = round(PHYSICS_HZ / TARGET_FPS)

# Temp subfolder inside BeamNG's user dir for staging exports
TEMP_SUBFOLDER = "_capture_tmp"

# ---------------------------------------------------------------------------
#  Lua commands
# ---------------------------------------------------------------------------

# Configure exporter: GLB binary, UVs only (no normals/tangents/colors/extras/beams)
# Uses gltfSequenceExporter/export which adds root node with world-space translation.
_LUA_SETUP = (
    'extensions.load("gltfSequenceExporter/export"); '
    "local e = extensions.gltfSequenceExporter_export; "
    'if not e then return "nil_exporter" end; '
    "e.gltfBinaryFormat = true; "
    "e.embedBuffers = true; "
    "e.exportTexCoords = true; "   # UVs for texture mapping
    "e.exportNormals = false; "    # we do shade smooth + weighted normals in Blender
    "e.exportTangents = false; "
    "e.exportColors = false; "
    "e.exportExtras = false; "
    "e.exportBeams = false; "
    'return "configured"'
)

# Set origin to current vehicle position (so vehicle starts at origin in GLB)
_LUA_SET_ORIGIN = (
    "local e = extensions.gltfSequenceExporter_export; "
    "if not e then return 'nil_exporter' end; "
    "e.setOrigin(); "
    'return "origin_set"'
)

# Export one frame.  {REL_PATH} is a relative path inside the user dir.
# The glTF SE's exportFile() computes translation = veh:getPosition() - origin,
# creates a root node with that translation, and all flexmeshes are children.
_LUA_EXPORT = (
    "local e = extensions.gltfSequenceExporter_export; "
    "if not e then return 'nil_exporter' end; "
    "local ok, err = pcall(e.exportFile, [[{REL_PATH}]]); "
    "if ok then return 'ok' else return 'err:' .. tostring(err) end"
)


def _lua_cmd(bng, cmd: str) -> str:
    """Send a Lua chunk and return the response string."""
    try:
        return bng.control.queue_lua_command(cmd, response=True)
    except Exception as exc:
        return f"ERROR: {exc}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=OUT_DIR, help="output dir for .glb frames")
    ap.add_argument("--home", default=GAME_HOME, help="BeamNG game install path")
    ap.add_argument("--user-dir", default=str(BEAMNG_USER),
                    help="BeamNG user dir (where io.open writes)")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--frames", type=int, default=300,
                    help="number of frames to capture")
    ap.add_argument("--fps", type=int, default=TARGET_FPS,
                    help="target capture fps (steps/frame = 2000/fps)")
    ap.add_argument("--prefix", default="frame_", help="frame filename prefix")
    ap.add_argument("--launch", action="store_true",
                    help="let beamngpy launch BeamNG (else attach to running)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print Lua commands without executing")
    args = ap.parse_args()

    user_dir = Path(args.user_dir)
    tmp_dir = user_dir / TEMP_SUBFOLDER
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    steps_per_frame = max(1, round(PHYSICS_HZ / args.fps))

    # Create temp staging dir
    tmp_dir.mkdir(parents=True, exist_ok=True)

    if args.dry_run:
        print(f"[dry-run] user_dir = {user_dir}")
        print(f"[dry-run] out_dir  = {out_dir}")
        print(f"[dry-run] setup: {_LUA_SETUP}")
        print(f"[dry-run] frames={args.frames}, fps={args.fps}, "
              f"steps/frame={steps_per_frame}")
        return 0

    from beamngpy import BeamNGpy

    print(f"[capture] connecting to BeamNG @ {args.home} :{args.port}", flush=True)
    bng = BeamNGpy("localhost", args.port, home=args.home)
    bng.open(launch=args.launch)
    info = bng.system.get_info()
    print(f"[capture] connected (tech={info.get('tech')})", flush=True)

    # Configure exporter
    r = _lua_cmd(bng, _LUA_SETUP)
    print(f"[capture] exporter setup: {r}", flush=True)
    if "nil" in str(r).lower():
        print("[capture] ERROR: gltfSequenceExporter_export not loaded.  "
              "Is the vehicle spawned?  Is the mod deployed?", file=sys.stderr)
        return 1

    # Set origin to current vehicle position (vehicle starts at origin in GLB)
    r = _lua_cmd(bng, _LUA_SET_ORIGIN)
    print(f"[capture] set origin: {r}", flush=True)

    # Deterministic physics
    bng.settings.set_deterministic(steps_per_second=PHYSICS_HZ)
    print(f"[capture] deterministic: {PHYSICS_HZ} Hz, {steps_per_frame} steps/frame "
          f"({args.fps} fps)", flush=True)

    bng.control.pause()
    print("[capture] sim paused, starting capture loop ...", flush=True)

    # --- capture loop ---
    t_start = time.time()
    errors = 0

    for i in range(args.frames):
        # 1) Export CURRENT state to temp dir (relative path)
        tmp_name = f"{args.prefix}{i:05d}.glb"
        tmp_rel = f"{TEMP_SUBFOLDER}/{tmp_name}"
        export_cmd = _LUA_EXPORT.replace("{REL_PATH}", tmp_rel)
        t0 = time.time()
        r = _lua_cmd(bng, export_cmd)
        dt_export = time.time() - t0

        if "err" in str(r).lower() or "nil" in str(r).lower():
            print(f"[capture] WARNING frame {i}: export returned {r}", flush=True)
            errors += 1

        # 2) Move file from user dir to output dir
        src = tmp_dir / tmp_name
        dst = out_dir / tmp_name
        if src.exists():
            shutil.move(str(src), str(dst))
        else:
            print(f"[capture] WARNING frame {i}: {src} not found after export",
                  flush=True)
            errors += 1

        # 3) Advance simulation
        bng.control.step(steps_per_frame, wait=True)
        dt_total = time.time() - t0

        if i == 0 or (i + 1) % 25 == 0 or i == args.frames - 1:
            print(f"[capture] frame {i+1}/{args.frames}  "
                  f"(export {dt_export*1000:.0f}ms, total {dt_total*1000:.0f}ms)",
                  flush=True)

    # Restore normal physics BEFORE resume — otherwise the sim stays locked
    # at 2000 Hz deterministic and becomes unusably slow.
    bng.settings.set_nondeterministic()
    bng.settings.remove_step_limit()
    print("[capture] deterministic mode disabled, step limit removed", flush=True)

    bng.control.resume()
    elapsed = time.time() - t_start
    avg = elapsed / max(1, args.frames)
    print(f"\n[capture] done: {args.frames} frames in {elapsed:.1f}s "
          f"({avg*1000:.0f} ms/frame, {args.frames/elapsed:.1f} fps) "
          f"-> {out_dir}", flush=True)
    if errors:
        print(f"[capture] WARNING: {errors} errors occurred", flush=True)

    # Cleanup temp dir
    try:
        shutil.rmtree(str(tmp_dir), ignore_errors=True)
    except Exception:
        pass

    # Just disconnect the socket — do NOT call bng.close() which would
    # quit the BeamNG process.  We want the user's game to keep running.
    try:
        if bng._connection and bng._connection.skt:
            bng._connection.skt.close()
    except Exception:
        pass
    print("[capture] disconnected (BeamNG still running)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
