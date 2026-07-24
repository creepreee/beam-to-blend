from __future__ import annotations

"""Drive BeamNG.drive to capture a per-frame GLB sequence via beamngpy.

Uses the **glTF Sequence Exporter** mod (patched) instead of stock util/export.
The glTF SE adds a root node with translation = veh:getPosition() - origin,
so each GLB has the vehicle positioned in world space.  Our GLB reader's
_node_world_matrix walks the parent chain and applies the root translation
automatically.

Two capture modes:

  deterministic (default):
    Pause + set deterministic (2000 physics Hz).  For each frame: export,
    step(N), move .glb.  Precise but changes physics behavior (pause+step
    alters the crash — car travels less distance, less rotation).

  slowmo:
    Set physics timescale to 0.01 (100x slower).  The game runs naturally
    — no pause, no step — but 100x slower so the GLTF SE can export every
    render frame at 60fps.  Physics are REAL (no pause artifacts).  The
    tradeoff: more frames for the same crash duration.

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

# --- Slowmo mode: physics timescale ---
# {SCALE} is replaced with the timescale float (0.01 = 100x slower).
# Uses simTimeAuthority extension (BeamNG's actual slow-mo system).
# setInstant() applies immediately; set() smooth-transitions (too slow for capture).
_LUA_SET_TIMESCALE = (
    "extensions.simTimeAuthority.setInstant({SCALE}); "
    "return tostring(extensions.simTimeAuthority.get())"
)
_LUA_RESET_TIMESCALE = (
    "extensions.simTimeAuthority.setInstant(1.0); "
    "return tostring(extensions.simTimeAuthority.get())"
)

# --- GE console silence: toggles _G['beamng_capture_quiet'] ---
_LUA_QUIET_ON = (
    "_G['beamng_capture_quiet'] = true; return 'quiet'"
)
_LUA_QUIET_OFF = (
    "_G['beamng_capture_quiet'] = false; return 'loud'"
)


_QUIET = False


def _log(msg: str) -> None:
    if not _QUIET:
        print(msg, flush=True)


def _lua_cmd(bng, cmd: str) -> str:
    """Send a Lua chunk and return the response string."""
    try:
        return bng.control.queue_lua_command(cmd, response=True)
    except Exception as exc:
        return f"ERROR: {exc}"


def main() -> int:
    global _QUIET, TARGET_FPS, STEPS_PER_FRAME

    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=OUT_DIR, help="output dir for .glb frames")
    ap.add_argument("--home", default=GAME_HOME, help="BeamNG game install path")
    ap.add_argument("--user-dir", default=str(BEAMNG_USER),
                    help="BeamNG user dir (where io.open writes)")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--frames", type=int, default=300,
                    help="number of frames to capture")
    ap.add_argument("--prefix", default="frame_", help="frame filename prefix")
    ap.add_argument("--launch", action="store_true",
                    help="let beamngpy launch BeamNG (else attach to running)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print Lua commands without executing")
    ap.add_argument("--quiet", "-q", action="store_true",
                    help="suppress all debug output (errors still print)")
    ap.add_argument("--mode", choices=["deterministic", "slowmo"], default="deterministic",
                    help="deterministic: pause+step at fixed physics Hz (default). "
                         "slowmo: timescale slow-mo, no pause, real physics.")
    ap.add_argument("--timescale", type=float, default=0.01,
                    help="physics timescale for slowmo mode (0.01 = 100x slower, "
                         "default: 0.01)")
    ap.add_argument("--fps", type=int, default=TARGET_FPS,
                    help=f"capture rate in frames/second (default: {TARGET_FPS}). "
                         "In deterministic mode this sets steps/frame = "
                         "physics_hz / fps. In slowmo mode it sets the export "
                         "pacing deadline.")
    args = ap.parse_args()

    _QUIET = args.quiet
    TARGET_FPS = max(1, int(args.fps))
    STEPS_PER_FRAME = max(1, round(PHYSICS_HZ / TARGET_FPS))

    user_dir = Path(args.user_dir)
    tmp_dir = user_dir / TEMP_SUBFOLDER
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Create temp staging dir
    tmp_dir.mkdir(parents=True, exist_ok=True)

    if args.mode == "slowmo":
        return _capture_slowmo(args, user_dir, tmp_dir, out_dir)
    else:
        return _capture_deterministic(args, user_dir, tmp_dir, out_dir)


def _capture_deterministic(args, user_dir: Path, tmp_dir: Path, out_dir: Path) -> int:
    """Original mode: pause + deterministic physics Hz + step(N) per frame."""
    steps_per_frame = max(1, round(PHYSICS_HZ / TARGET_FPS))

    if args.dry_run:
        print(f"[dry-run] mode=deterministic")
        print(f"[dry-run] user_dir = {user_dir}")
        print(f"[dry-run] out_dir  = {out_dir}")
        print(f"[dry-run] setup: {_LUA_SETUP}")
        print(f"[dry-run] frames={args.frames}, fps={TARGET_FPS}, "
              f"steps/frame={steps_per_frame}")
        return 0

    from beamngpy import BeamNGpy

    _log(f"[capture] connecting to BeamNG @ {args.home} :{args.port}")
    bng = BeamNGpy("localhost", args.port, home=args.home)
    bng.open(launch=args.launch)
    info = bng.system.get_info()
    _log(f"[capture] connected (tech={info.get('tech')})")

    # Configure exporter
    r = _lua_cmd(bng, _LUA_SETUP)
    _log(f"[capture] exporter setup: {r}")
    if "nil" in str(r).lower():
        print("[capture] ERROR: gltfSequenceExporter_export not loaded.  "
              "Is the vehicle spawned?  Is the mod deployed?", file=sys.stderr)
        return 1

    # Set origin to current vehicle position (vehicle starts at origin in GLB)
    r = _lua_cmd(bng, _LUA_SET_ORIGIN)
    _log(f"[capture] set origin: {r}")

    # Deterministic physics
    bng.settings.set_deterministic(steps_per_second=PHYSICS_HZ)
    _log(f"[capture] deterministic: {PHYSICS_HZ} Hz, {steps_per_frame} steps/frame "
         f"({TARGET_FPS} fps)")

    bng.control.pause()
    _log("[capture] sim paused, starting capture loop ...")

    # Silence GE console spam from GLTF SE during capture
    _lua_cmd(bng, _LUA_QUIET_ON)

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
            _log(f"[capture] WARNING frame {i}: export returned {r}")
            errors += 1

        # 2) Move file from user dir to output dir
        src = tmp_dir / tmp_name
        dst = out_dir / tmp_name
        if src.exists():
            shutil.move(str(src), str(dst))
        else:
            _log(f"[capture] WARNING frame {i}: {src} not found after export")
            errors += 1

        # 3) Advance simulation
        bng.control.step(steps_per_frame, wait=True)
        dt_total = time.time() - t0

        if i == 0 or (i + 1) % 25 == 0 or i == args.frames - 1:
            _log(f"[capture] frame {i+1}/{args.frames}  "
                 f"(export {dt_export*1000:.0f}ms, total {dt_total*1000:.0f}ms)")

    # Restore GE console logging
    _lua_cmd(bng, _LUA_QUIET_OFF)

    # Restore normal physics BEFORE resume — otherwise the sim stays locked
    # at 2000 Hz deterministic and becomes unusably slow.
    bng.settings.set_nondeterministic()
    bng.settings.remove_step_limit()
    _log("[capture] deterministic mode disabled, step limit removed")

    bng.control.resume()
    _print_stats(args.frames, t_start, out_dir, errors, tmp_dir)
    _disconnect(bng)
    return 0


def _capture_slowmo(args, user_dir: Path, tmp_dir: Path, out_dir: Path) -> int:
    """Slowmo mode: timescale slow-mo, no pause, real physics.

    Sets physicsTimescale to --timescale (default 0.01 = 100x slower).
    The game runs naturally but slowly enough that the GLTF SE can export
    every render frame at 60fps.  No pause, no step, no deterministic —
    physics are completely real.  We pace exports with sleep(1/fps) and
    let the GLTF SE capture whatever the current render state is.
    """
    sleep_sec = 1.0 / TARGET_FPS

    if args.dry_run:
        print(f"[dry-run] mode=slowmo")
        print(f"[dry-run] user_dir = {user_dir}")
        print(f"[dry-run] out_dir  = {out_dir}")
        print(f"[dry-run] setup: {_LUA_SETUP}")
        print(f"[dry-run] frames={args.frames}, fps={TARGET_FPS}, "
              f"timescale={args.timescale}")
        print(f"[dry-run] sleep between exports: {sleep_sec*1000:.1f}ms")
        return 0

    from beamngpy import BeamNGpy

    _log(f"[capture] connecting to BeamNG @ {args.home} :{args.port}")
    bng = BeamNGpy("localhost", args.port, home=args.home)
    bng.open(launch=args.launch)
    info = bng.system.get_info()
    _log(f"[capture] connected (tech={info.get('tech')})")

    # Configure exporter
    r = _lua_cmd(bng, _LUA_SETUP)
    _log(f"[capture] exporter setup: {r}")
    if "nil" in str(r).lower():
        print("[capture] ERROR: gltfSequenceExporter_export not loaded.  "
              "Is the vehicle spawned?  Is the mod deployed?", file=sys.stderr)
        return 1

    # Set origin to current vehicle position (vehicle starts at origin in GLB)
    r = _lua_cmd(bng, _LUA_SET_ORIGIN)
    _log(f"[capture] set origin: {r}")

    # Set physics timescale (no pause — game keeps running, just slow)
    scale_cmd = _LUA_SET_TIMESCALE.replace("{SCALE}", str(args.timescale))
    r = _lua_cmd(bng, scale_cmd)
    _log(f"[capture] timescale set: {r} ({args.timescale}x = "
         f"{1/args.timescale:.0f}x slower)")

    _log(f"[capture] slowmo capture: {args.frames} frames @ {TARGET_FPS} fps, "
         f"sleep={sleep_sec*1000:.1f}ms between exports")

    # Silence GE console spam from GLTF SE during capture
    _lua_cmd(bng, _LUA_QUIET_ON)

    # --- capture loop ---
    t_start = time.time()
    errors = 0

    # Drift-compensated pacing: each frame targets an ABSOLUTE deadline on a
    # monotonic clock rather than sleeping a fixed amount after each export.
    #
    #   BUG this fixes ("sudden fps gain at the crash point"): the old loop slept
    #   `sleep_sec - dt_export` per frame INDEPENDENTLY. When a frame got heavy
    #   (the crash — more geometry to serialize), dt_export exceeded sleep_sec, so
    #   that frame consumed MORE than 1/fps of wall time. Because the sim runs
    #   free (slowed, not stepped), that extra wall time = extra SIM time elapsing
    #   between exported frames → the crash looked sped-up. Then when export got
    #   cheap again the frames bunched back up → looked slowed. Uneven spacing.
    #
    #   Fix: hold a running `next_deadline`. A heavy frame that overruns its slot
    #   does NOT push the whole schedule back — the following frames simply don't
    #   sleep until the clock catches up to their (already-fixed) deadlines. Sim
    #   time per exported frame stays as even as the OS scheduler allows.
    clock = time.monotonic
    next_deadline = clock() + sleep_sec

    for i in range(args.frames):
        tmp_name = f"{args.prefix}{i:05d}.glb"
        tmp_rel = f"{TEMP_SUBFOLDER}/{tmp_name}"
        export_cmd = _LUA_EXPORT.replace("{REL_PATH}", tmp_rel)
        t0 = time.time()
        r = _lua_cmd(bng, export_cmd)
        dt_export = time.time() - t0

        if "err" in str(r).lower() or "nil" in str(r).lower():
            _log(f"[capture] WARNING frame {i}: export returned {r}")
            errors += 1

        # Move file from user dir to output dir
        src = tmp_dir / tmp_name
        dst = out_dir / tmp_name
        if src.exists():
            shutil.move(str(src), str(dst))
        else:
            _log(f"[capture] WARNING frame {i}: {src} not found after export")
            errors += 1

        # Sleep until this frame's absolute deadline (drift-compensated).
        sleep_left = next_deadline - clock()
        if sleep_left > 0:
            time.sleep(sleep_left)
        # Advance the deadline by exactly one slot.  If we overran (crash frame),
        # skip deadlines we've already blown past so we don't sleep-burst later.
        next_deadline += sleep_sec
        now = clock()
        if next_deadline < now:
            next_deadline = now + sleep_sec

        if i == 0 or (i + 1) % 25 == 0 or i == args.frames - 1:
            elapsed = time.time() - t_start
            actual_fps = (i + 1) / max(0.001, elapsed)
            _log(f"[capture] frame {i+1}/{args.frames}  "
                 f"(export {dt_export*1000:.0f}ms, "
                 f"actual {actual_fps:.1f} fps)")

    # Restore GE console logging
    _lua_cmd(bng, _LUA_QUIET_OFF)

    # Restore timescale to normal
    r = _lua_cmd(bng, _LUA_RESET_TIMESCALE)
    _log(f"[capture] timescale restored: {r}")

    _print_stats(args.frames, t_start, out_dir, errors, tmp_dir)
    _disconnect(bng)
    return 0


def _print_stats(n_frames: int, t_start: float, out_dir: Path, errors: int,
                 tmp_dir: Path | None = None) -> None:
    elapsed = time.time() - t_start
    avg = elapsed / max(1, n_frames)
    _log(f"\n[capture] done: {n_frames} frames in {elapsed:.1f}s "
         f"({avg*1000:.0f} ms/frame, {n_frames/elapsed:.1f} fps) "
         f"-> {out_dir}")
    if errors:
        print(f"[capture] WARNING: {errors} errors occurred", flush=True)
    if tmp_dir is not None:
        try:
            shutil.rmtree(str(tmp_dir), ignore_errors=True)
        except Exception:
            pass


def _disconnect(bng) -> None:
    """Disconnect socket without killing BeamNG."""
    try:
        if bng._connection and bng._connection.skt:
            bng._connection.skt.close()
    except Exception:
        pass
    _log("[capture] disconnected (BeamNG still running)")


if __name__ == "__main__":
    sys.exit(main())
