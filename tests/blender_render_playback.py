"""Headless Blender proof that RENDERS move the car (the GUI-render bug).

Run with:
    blender --background --python tests/blender_render_playback.py

THE BUG: GUI renders (Ctrl+F12 / Viewport Render Animation) run as a WM JOB
on a BACKGROUND THREAD.  Blender fires frame-change AND render handlers on
that job thread, but the playback's handlers refused non-main threads (the
Mantaflow-bake crash guard) — so every GUI-rendered frame showed whatever
pose the viewport last had.  Scrubbing (main thread) and ``--background``
renders (also main thread) always worked, which made the bug look like
"renders ignore the animation".

This test builds a tiny animated cache (a body + two wheels translating
across 24 frames), imports it through the runtime, and performs REAL Cycles
renders — including renders executed ON A WORKER THREAD, the exact condition
that used to freeze:

  A. NEGATIVE CONTROL: with the ``render_pre`` handler removed, an animation
     render on a worker thread fires ZERO playback updates and every frame
     shows the same frozen pose (first vs last PNG identical).
  B. THE FIX: with ``render_pre`` registered, the same worker-thread
     animation render applies the mapped cache frame once per rendered
     frame, first vs last PNGs differ substantially, the result differs from
     the frozen control, and the mesh ends on the exact final cache pose.
  C. STILL RENDER (F12): parking the playhead WITHOUT scrubbing and hitting
     render applies the current cache frame.
  D. RECOVERY: after simulated undo (wiped module state) the render handler
     is re-registered by ``_try_recover`` and a worker-thread still render
     moves — no manual scrub needed.

Exits non-zero on any failure.
"""

import math
import os
import shutil
import sys
import tempfile
import threading

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TESTS = os.path.dirname(os.path.abspath(__file__))
# Repo FIRST, evicting any installed add-on's bundled runtime/importer, so we
# test the working tree rather than the deployed build.
sys.path.insert(0, _REPO)
for _mod in [m for m in sys.modules
             if m == "runtime" or m.startswith("runtime.")
             or m == "importer" or m.startswith("importer.")]:
    del sys.modules[_mod]
if _TESTS not in sys.path:
    sys.path.insert(0, _TESTS)

import bpy  # noqa: E402
import numpy as np  # noqa: E402
from mathutils import Vector  # noqa: E402

from glb_fixtures import build_glb  # noqa: E402
from importer.cache_builder import CacheBuilder  # noqa: E402
from runtime.cache_reader import CacheReader  # noqa: E402
from runtime.mesh_update import CachePlayback  # noqa: E402
from runtime import frame_handler  # noqa: E402

N_FRAMES = 24
PLAYBACK_FPS = 24.0
OUTPUT_FPS = 24.0            # 1:1 mapping: cache frame == timeline frame - 1
RES_X, RES_Y = 160, 120

_failures = []


def check(cond, msg):
    if cond:
        print(f"[RENDER][ok]   {msg}")
    else:
        print(f"[RENDER][FAIL] {msg}")
        _failures.append(msg)


def _box(cx, cy, cz, sx, sy, sz):
    """Axis-aligned box centred at (cx, cy, cz) with the given edge lengths."""
    hx, hy, hz = sx / 2.0, sy / 2.0, sz / 2.0
    pos = np.array([
        [cx - hx, cy - hy, cz - hz], [cx + hx, cy - hy, cz - hz],
        [cx + hx, cy + hy, cz - hz], [cx - hx, cy + hy, cz - hz],
        [cx - hx, cy - hy, cz + hz], [cx + hx, cy - hy, cz + hz],
        [cx + hx, cy + hy, cz + hz], [cx - hx, cy + hy, cz + hz],
    ], dtype=np.float32)
    idx = np.array([
        [0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7],
        [0, 1, 5], [0, 5, 4], [1, 2, 6], [1, 6, 5],
        [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7],
    ], dtype=np.uint32)
    return pos, idx


def _write_sequence(seq_dir):
    """24 GLB frames: a body sliding +X, wheels riding along with a bob."""
    os.makedirs(seq_dir, exist_ok=True)
    for f in range(N_FRAMES):
        bx = -1.0 + 0.08 * f
        bob = 0.02 * math.sin(2.0 * math.pi * f / 12.0)
        body_pos, body_idx = _box(bx, 0.0, 0.6, 1.0, 0.5, 0.35)
        wfl_pos, wfl_idx = _box(bx - 0.55, -0.32, 0.15 + bob, 0.28, 0.28, 0.28)
        wfr_pos, wfr_idx = _box(bx - 0.55, 0.32, 0.15 - bob, 0.28, 0.28, 0.28)
        glb = build_glb([
            ("body", body_pos, body_idx),
            ("wheel_fl", wfl_pos, wfl_idx),
            ("wheel_fr", wfr_pos, wfl_idx),
        ])
        with open(os.path.join(seq_dir, f"frame_{f:05d}.glb"), "wb") as fh:
            fh.write(glb)


def read_verts(obj):
    buf = np.empty(len(obj.data.vertices) * 3, dtype=np.float32)
    obj.data.vertices.foreach_get("co", buf)
    return buf


def image_pixels(path):
    img = bpy.data.images.load(path)
    try:
        buf = np.empty(img.size[0] * img.size[1] * img.channels, dtype=np.float32)
        img.pixels.foreach_get(buf)
        return buf.reshape(-1, img.channels)[:, :3]   # drop alpha
    finally:
        bpy.data.images.remove(img)


def mean_diff(pa, pb):
    return float(np.abs(pa - pb).mean())


def setup_render_env(scene, center, radius):
    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = 8
    scene.cycles.use_adaptive_sampling = False
    scene.cycles.use_denoising = False
    scene.cycles.max_bounces = 2
    scene.render.resolution_x = RES_X
    scene.render.resolution_y = RES_Y
    scene.render.image_settings.file_format = "PNG"
    scene.view_settings.view_transform = "Standard"

    world = bpy.data.worlds.new("World")
    world.use_nodes = True
    world.node_tree.nodes["Background"].inputs[0].default_value = \
        (0.65, 0.65, 0.65, 1.0)
    scene.world = world

    sun_data = bpy.data.lights.new("Sun", "SUN")
    sun_data.energy = 4.0
    sun = bpy.data.objects.new("Sun", sun_data)
    sun.rotation_euler = (math.radians(55), 0.0, math.radians(25))
    scene.collection.objects.link(sun)

    cam_data = bpy.data.cameras.new("Cam")
    cam_data.clip_end = max(100.0, radius * 20.0)
    cam = bpy.data.objects.new("Cam", cam_data)
    scene.collection.objects.link(cam)
    direction = Vector((0.35, -1.0, 0.55)).normalized()
    cam.location = center + direction * (radius * 3.2)
    aim = Vector(center) - cam.location
    cam.rotation_euler = aim.to_track_quat("-Z", "Y").to_euler()
    scene.camera = cam


def render_anim_on_worker(scene, out_dir, tag, box):
    """Run an animation render on a NON-main thread.

    This reproduces the GUI condition: Blender executes the whole render
    pipeline — including every Python render/frame handler — on the WM job
    thread when rendering from the UI.  Errors are captured into ``box``;
    a hard crash would kill the process instead (which the CI run shows).
    """

    def worker():
        try:
            scene.render.filepath = os.path.join(out_dir, tag + "_")
            bpy.ops.render.render(animation=True)
            box["ok"] = True
        except BaseException as exc:   # noqa: BLE001 - reported verbatim
            box["err"] = repr(exc)

    th = threading.Thread(target=worker, name="render-job-standin")
    th.start()
    th.join(timeout=600)
    return th


def render_still_on_worker(scene, out_dir, tag, box):
    """Run a STILL render of ``scene.frame_current`` on a NON-main thread."""

    def worker():
        try:
            scene.render.filepath = os.path.join(out_dir, tag + "_")
            bpy.ops.render.render(write_still=True)
            box["ok"] = True
        except BaseException as exc:   # noqa: BLE001 - reported verbatim
            box["err"] = repr(exc)

    th = threading.Thread(target=worker, name="render-job-standin")
    th.start()
    th.join(timeout=600)
    return th


def main():
    tmp = tempfile.mkdtemp(prefix="beamng_render_test_")
    try:
        seq_dir = os.path.join(tmp, "seq")
        cache_path = os.path.join(tmp, "render_test.bvc")
        out_dir = os.path.join(tmp, "frames")
        os.makedirs(out_dir, exist_ok=True)

        _write_sequence(seq_dir)
        CacheBuilder(seq_dir, cache_path).build()

        bpy.ops.wm.read_factory_settings(use_empty=True)
        reader = CacheReader(cache_path)
        playback = CachePlayback(reader)
        playback.build_scene()
        frame_handler.attach(playback, frame_start=1,
                             playback_fps=PLAYBACK_FPS,
                             output_fps=OUTPUT_FPS)

        scene = bpy.context.scene
        n_src = reader.frame_count
        print(f"[RENDER] cache: {n_src} frames, "
              f"timeline {scene.frame_start}..{scene.frame_end}")

        # Camera framing from the true cache bounds across ALL frames.
        lo = np.full(3, np.inf, dtype=np.float64)
        hi = np.full(3, -np.inf, dtype=np.float64)
        for name in reader.object_names():
            for f in range(n_src):
                p = reader.frame_positions(name, f).astype(np.float64)
                lo = np.minimum(lo, p.min(axis=0))
                hi = np.maximum(hi, p.max(axis=0))
        center = (lo + hi) / 2.0
        radius = float(np.linalg.norm(hi - lo) / 2.0)
        setup_render_env(scene, center, radius)

        # Spy on every set_frame so we can see what the render applied.
        spy_calls = []
        orig_set_frame = playback.set_frame

        def spy(pos):
            spy_calls.append(float(pos))
            orig_set_frame(pos)

        playback.set_frame = spy

        rendered_frames = []

        def rec_write(*args):
            sc = args[0]
            rendered_frames.append(int(sc.frame_current))

        bpy.app.handlers.render_write.append(rec_write)

        probe = bpy.data.objects["body"]
        expected_last = reader.frame_positions("body", N_FRAMES - 1)

        # --- sanity: the VIEWPORT path still works -----------------------
        spy_calls.clear()
        scene.frame_set(11)
        check(spy_calls == [10.0],
              f"viewport scrub maps frame 11 -> cache 10 (got {spy_calls})")

        def png(tag, frame):
            return image_pixels(
                os.path.join(out_dir, f"{tag}_{frame:04d}.png"))

        # --- A. negative control: the OLD build (no render handler) ------
        # Renders ran on the job thread even then; with no render_pre there
        # was NOTHING left to apply updates (frame_change refuses job
        # threads) -> statue.
        for h in list(bpy.app.handlers.render_pre):
            if getattr(h, "__name__", "") == "_on_render_pre":
                bpy.app.handlers.render_pre.remove(h)
        scene.frame_set(scene.frame_start)      # park on the first pose
        spy_calls.clear()
        rendered_frames.clear()
        box = {}
        th = render_anim_on_worker(scene, out_dir, "ctrl", box)
        check(not th.is_alive() and "err" not in box,
              f"control: worker-thread render completed ({box})")
        check(spy_calls == [],
              "CONTROL: zero playback updates during the job-thread render "
              "(this is why the old build froze)")
        check(rendered_frames == list(range(scene.frame_start,
                                            scene.frame_end + 1)),
              f"control rendered every frame "
              f"({len(rendered_frames)} frames written)")
        d_ctrl = mean_diff(png("ctrl", scene.frame_start),
                           png("ctrl", scene.frame_end))
        check(d_ctrl < 1e-5,
              f"CONTROL: first vs last render identical -> frozen car "
              f"(mean diff {d_ctrl:.2e})")

        # --- B. the fix: render handler registered ------------------------
        frame_handler._ensure_handler_registered()
        check(any(getattr(h, "__name__", "") == "_on_render_pre"
                  for h in bpy.app.handlers.render_pre),
              "fix: render_pre handler registered")
        spy_calls.clear()
        rendered_frames.clear()
        box = {}
        th = render_anim_on_worker(scene, out_dir, "fix", box)
        check(not th.is_alive() and "err" not in box,
              f"fix: worker-thread render completed without crashing "
              f"({box})")

        check(len(spy_calls) == len(rendered_frames) > 0,
              f"fix: one playback update per rendered frame "
              f"({len(spy_calls)} updates for {len(rendered_frames)} frames)")
        if len(spy_calls) == len(rendered_frames):
            pairs_ok = all(abs(c - (f - scene.frame_start)) <= 0.01
                           for c, f in zip(spy_calls, rendered_frames))
            check(pairs_ok,
                  "fix: every rendered frame got its own mapped cache frame "
                  "(1:1 mapping)")
        check(spy_calls and abs(spy_calls[-1] - (N_FRAMES - 1)) <= 0.01,
              f"fix: last rendered frame reached cache frame "
              f"{N_FRAMES - 1} (got {spy_calls[-1] if spy_calls else None})")

        d_fix_span = mean_diff(png("fix", scene.frame_start),
                               png("fix", scene.frame_end))
        check(d_fix_span > 1e-4,
              f"FIX: first vs last render differ -> the car moves "
              f"(mean diff {d_fix_span:.2e})")
        d_vs_ctrl = mean_diff(png("fix", scene.frame_end),
                              png("ctrl", scene.frame_end))
        check(d_vs_ctrl > 1e-4,
              f"FIX: final render differs from the frozen control "
              f"(mean diff {d_vs_ctrl:.2e})")

        got = read_verts(probe)
        err = float(np.abs(got - expected_last.ravel()).max())
        check(err <= 1e-4,
              f"fix: mesh ends on the exact final cache pose "
              f"(max err {err:.2e})")

        # --- C. still render (F12, main thread like the GUI op) ----------
        scene.frame_current = 13               # direct assignment: no handler
        spy_calls.clear()
        scene.render.filepath = os.path.join(out_dir, "still_")
        bpy.ops.render.render(write_still=True)
        check(set(spy_calls) == {12.0},
              f"still: F12 applied cache frame 12 without scrubbing "
              f"(got {sorted(set(spy_calls))})")
        exp12 = reader.frame_positions("body", 12)
        err = float(np.abs(read_verts(probe) - exp12.ravel()).max())
        check(err <= 1e-4,
              f"still: mesh matches cache frame 12 (max err {err:.2e})")

        # --- D. undo/reload recovery ------------------------------------
        for h in list(bpy.app.handlers.render_pre):
            if getattr(h, "__name__", "") == "_on_render_pre":
                bpy.app.handlers.render_pre.remove(h)
        frame_handler._active = None           # simulated undo wipe
        check(frame_handler._try_recover(scene),
              "recovery: playback rebuilt from scene props")
        names = [getattr(h, "__name__", "")
                 for h in bpy.app.handlers.render_pre]
        check(names.count("_on_render_pre") == 1,
              f"recovery: render handler re-registered exactly once "
              f"(got {names})")
        frame_handler._ensure_handler_registered()
        names = [getattr(h, "__name__", "")
                 for h in bpy.app.handlers.render_pre]
        check(names.count("_on_render_pre") == 1,
              "recovery: registration stays idempotent")

        spy_calls.clear()
        scene.frame_current = 20
        # Recovery built a NEW playback object — point the spy at it too.
        recovered = frame_handler._active
        orig_recovered = recovered.set_frame

        def spy_recovered(pos):
            spy_calls.append(float(pos))
            orig_recovered(pos)

        recovered.set_frame = spy_recovered
        box = {}
        th = render_still_on_worker(scene, out_dir, "recov", box)
        check(not th.is_alive() and "err" not in box,
              f"recovery: worker still render completed ({box})")
        check(set(spy_calls) == {19.0},
              f"recovery: background render after undo applied cache frame "
              f"19 (got {sorted(set(spy_calls))})")

        frame_handler.detach_handler()
        check(not bpy.app.handlers.render_pre
              and not bpy.app.handlers.frame_change_pre,
              "detach removes both handlers")

        reader.close()

        print(f"[RENDER] {len(_failures)} failure(s)")
        if _failures:
            for f in _failures:
                print(f"  - {f}")
            return 1
        print("[RENDER][PASS] renders apply the cache per frame — including "
              "job-thread renders, stills, and post-undo recovery")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
