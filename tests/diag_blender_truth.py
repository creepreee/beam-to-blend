from __future__ import annotations

"""Blender-side honest heading check (runs the REAL runtime path).

Loads the BVC through CachePlayback + frame_handler (exactly like the addon),
reads the body's WORLD vertices straight out of Blender's evaluated scene, and
compares the body heading (Kabsch, frame0->frameN) against the INDEPENDENT
getDirectionVector() recorded in the .bmc.

Writes the result to a text file (Blender stdout is unreliable in --background).

Usage:
    blender --background --python tests/diag_blender_truth.py -- <cache.bvc> <capture.bmc> <out.txt>
"""

import sys
import os

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import numpy as np
import bpy

from runtime.cache_reader import CacheReader
from runtime.mesh_update import CachePlayback
from runtime import frame_handler
from importer.capture_reader import BmcReader


def kabsch(A, B):
    H = A.T @ B
    U, _, Vt = np.linalg.svd(H)
    d = np.sign(np.linalg.det(Vt.T @ U.T))
    return Vt.T @ np.diag([1.0, 1.0, d]) @ U.T


def rot_angle_deg(R):
    return float(np.degrees(np.arccos(np.clip((np.trace(R) - 1.0) / 2.0, -1.0, 1.0))))


def angle_between(a, b):
    a = a / (np.linalg.norm(a) + 1e-12)
    b = b / (np.linalg.norm(b) + 1e-12)
    return float(np.degrees(np.arccos(np.clip(np.dot(a, b), -1.0, 1.0))))


def main():
    args = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    bvc_path, bmc_path, out_path = args[0], args[1], args[2]

    reader = CacheReader(bvc_path)
    bmc = BmcReader(bmc_path)
    n = min(reader.frame_count, bmc.frame_count())

    bpy.ops.wm.read_factory_settings(use_empty=True)
    pb = CachePlayback(reader, log_path=None, chunk_map=None)
    pb.build_scene()
    frame_handler.attach(pb, frame_start=1)

    body = max((o for o in bpy.data.objects if o.type == "MESH"),
               key=lambda o: len(o.data.vertices))
    empty = bpy.data.objects.get("BeamNG Cache__root")

    def world_body(fi):
        bpy.context.scene.frame_set(1 + fi)
        bpy.context.view_layer.update()
        M = np.array(empty.matrix_world, dtype=np.float64)
        R, p = M[:3, :3], M[:3, 3]
        v = np.empty(len(body.data.vertices) * 3, dtype=np.float32)
        body.data.vertices.foreach_get("co", v)
        v = v.reshape(-1, 3).astype(np.float64)
        return (R @ v.T).T + p

    def truth_fwd(fi):
        f = bmc.frame_vehicle_transform(fi)[3:6].astype(np.float64)
        return f / (np.linalg.norm(f) + 1e-12)

    w0 = world_body(0)
    w0c = w0 - w0.mean(0)
    truth0 = truth_fwd(0)

    lines = []
    lines.append("REAL Blender runtime path — body heading vs physics getDirectionVector")
    lines.append(f"{'frame':>5} | {'recon rot':>10} | {'physics':>8} | {'err':>7} | excess")
    lines.append("-" * 52)
    worst = 0.0
    for fi in range(0, n, max(1, n // 15)):
        wN = world_body(fi)
        wNc = wN - wN.mean(0)
        R = kabsch(w0c, wNc)
        recon = rot_angle_deg(R)
        phys = angle_between(truth0, truth_fwd(fi))
        err = angle_between(R @ truth0, truth_fwd(fi))
        worst = max(worst, err)
        exc = f"{recon / phys:5.2f}x" if phys > 3.0 else "  -  "
        lines.append(f"{fi:5d} | {recon:8.2f}d | {phys:6.2f}d | {err:5.2f}d | {exc}")
    lines.append("-" * 52)
    lines.append(f"worst heading error: {worst:.2f} deg")
    lines.append("VERDICT: " + ("PASS" if worst < 8.0 else "FAIL"))

    with open(out_path, "w") as f:
        f.write("\n".join(lines) + "\n")

    reader.close()
    bmc.close()


if __name__ == "__main__":
    main()
