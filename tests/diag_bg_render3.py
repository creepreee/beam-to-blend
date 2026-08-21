"""Background-mode preflight: render 3 frames, main view layer ONLY, verify motion.

    blender -b <file.blend> -s <f> -e <f+2> -a --python tests/diag_bg_render3.py

Wait — order matters on some builds; safer form:

    blender -b <file.blend> --python tests/diag_bg_render3.py -- 1840

(the frame count/fps come from the scene; 3 consecutive frames starting at
the number given after `--`, defaulting to scene.frame_start).

Disables every non-main view layer (DEBRIS/SMOKE etc.), renders 3 frames at
low samples/res on CPU, then pixel-diffs the PNGs in-process and prints
[R3] VERDICT: MOVES / FROZEN. Also prints whether BeamNG auto-recovery ran
WITHOUT any manual workaround.
"""

import os
import sys

import bpy

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
first_frame = int(argv[0]) if len(argv) > 0 else None
count = int(argv[1]) if len(argv) > 1 else 3
samples = int(argv[2]) if len(argv) > 2 else 2

sc = bpy.context.scene


def _log(msg):
    print(f"[R3] {msg}", flush=True)


# --- 1. auto-recovery state (NO manual workaround allowed here) ---
try:
    from runtime import frame_handler as fh

    _log(f"frame_handler._active at script start = {fh._active}")
    _log("AUTO-RECOVERY OK" if fh._active is not None
         else "AUTO-RECOVERY MISSING")
except Exception as exc:
    _log(f"frame_handler import failed: {exc}")

# --- 2. main layer only ---
disabled = []
for vl in list(sc.view_layers):
    if vl.name != sc.view_layers[0].name:
        vl.use = False
        disabled.append(vl.name)
_log(f"view layers now: {[(v.name, v.use) for v in sc.view_layers]} "
     f"(disabled: {disabled})")

# --- 3. cheap render config ---
sc.cycles.samples = samples
sc.cycles.use_denoising = False
sc.cycles.device = "CPU"
sc.render.resolution_percentage = 25
sc.render.image_settings.file_format = "PNG"
out_dir = os.path.join(os.environ.get("TEMP", "/tmp"), "opencode")
os.makedirs(out_dir, exist_ok=True)
sc.render.filepath = os.path.join(out_dir, "r3_")

start = first_frame if first_frame is not None else sc.frame_start
end = min(start + count - 1, sc.frame_end)
frames = list(range(start, end + 1))
paths = [os.path.join(out_dir, f"r3_{f:04d}.png") for f in frames]
for p in paths:
    if os.path.exists(p):
        os.remove(p)

_log(f"rendering frames {frames} at {sc.render.resolution_percentage}% "
     f"samples={sc.cycles.samples}")

# --- 4. render exactly 3 stills via ANIMATION render (appends frame
# numbers to the filepath AND exercises the same frame_change/render_pre
# handler pipeline a real batch render uses) ---
sc.frame_start = frames[0]
sc.frame_end = frames[-1]
bpy.ops.render.render(animation=True)
for f, p in zip(frames, paths):
    _log(f"frame {f} -> {'SAVED' if os.path.exists(p) else 'MISSING'}")
if any(not os.path.exists(p) for p in paths):
    _log(f"files present: {os.listdir(out_dir)}")

# --- 5. pixel-diff in process ---
try:
    import numpy as np

    imgs = []
    for f, p in zip(frames, paths):
        img = bpy.data.images.load(p)
        buf = np.empty(len(img.pixels), dtype=np.float32)
        img.pixels.foreach_get(buf)
        imgs.append(buf.reshape(-1, 4))
    diffs = [float(np.abs(imgs[i] - imgs[i + 1]).mean())
             for i in range(len(imgs) - 1)]
    brightness = float(np.mean(imgs[0][:, :3]))
    for i, d in enumerate(diffs):
        _log(f"diff frame{frames[i]}->frame{frames[i + 1]}: {d:.6g}")
    _log(f"mean brightness frame0: {brightness:.4f}")
    moved = max(diffs) > 1e-5
    blank = brightness < 0.005
    _log(f"VERDICT: {'MOVES' if moved else 'FROZEN'}"
         + (" (BLANK RENDER!)" if blank else ""))
finally:
    for img in list(bpy.data.images):
        if img.name.startswith("r3_"):
            bpy.data.images.remove(img)
