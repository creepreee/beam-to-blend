from __future__ import annotations

"""HONEST end-to-end rotation diagnostic (non-circular).

Every prior diagnostic compared the runtime's applied matrix against the
*stored* matrix — trivially equal, so they always "passed" while the car
looked wrong on screen.  This script does NOT do that.

It reconstructs the FINAL WORLD GEOMETRY of the car body exactly as Blender
would draw it:

    world_vert = R_stored @ mesh_vert + pos_stored

then measures the body's actual rigid heading via Kabsch (frame 0 -> frame N)
and compares it against a fully INDEPENDENT ground-truth signal: the raw
getDirectionVector() recorded in the .bmc, which never feeds the mesh geometry.

If the reconstructed car body heading matches getDirectionVector within a few
degrees, the on-screen rotation is correct (single rotation).  If it is ~2x
(e.g. a 90 deg turn shows as ~180 deg), the rotation is being applied twice.

Usage:
    python tests/diag_truth.py <cache.bvc> <capture.bmc>

Pure CPython (numpy only) — no Blender, no bpy, no GPU.  Nothing here can be
fooled by a self-consistent stored matrix.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from runtime.cache_reader import CacheReader
from importer.capture_reader import BmcReader


def kabsch(A: np.ndarray, B: np.ndarray) -> np.ndarray:
    """Rigid rotation R mapping centered point-set A onto centered B."""
    H = A.T @ B
    U, _, Vt = np.linalg.svd(H)
    d = np.sign(np.linalg.det(Vt.T @ U.T))
    return Vt.T @ np.diag([1.0, 1.0, d]) @ U.T


def rot_angle_deg(R: np.ndarray) -> float:
    return float(np.degrees(np.arccos(np.clip((np.trace(R) - 1.0) / 2.0, -1.0, 1.0))))


def angle_between(a: np.ndarray, b: np.ndarray) -> float:
    a = a / (np.linalg.norm(a) + 1e-12)
    b = b / (np.linalg.norm(b) + 1e-12)
    return float(np.degrees(np.arccos(np.clip(np.dot(a, b), -1.0, 1.0))))


def main() -> None:
    if len(sys.argv) < 3:
        print("usage: python tests/diag_truth.py <cache.bvc> <capture.bmc>")
        sys.exit(1)
    bvc_path, bmc_path = sys.argv[1], sys.argv[2]

    reader = CacheReader(bvc_path)
    bmc = BmcReader(bmc_path)

    n = min(reader.frame_count, bmc.frame_count())
    body = max(reader.stable_objects(), key=lambda o: o.vertex_count)
    print(f"BVC frames={reader.frame_count}  BMC frames={bmc.frame_count()}")
    print(f"body object: {body.name}  verts={body.vertex_count}")
    print()

    # --- reconstruct WORLD geometry of the body at each sampled frame ---
    def world_body(fi: int) -> np.ndarray:
        v = reader.frame_positions(body.name, fi).astype(np.float64)  # already Blender-space mesh
        tf = reader.frame_transform(fi)
        pos = tf[:3].astype(np.float64)
        R = tf[3:12].reshape(3, 3).astype(np.float64)
        return (R @ v.T).T + pos

    # independent ground-truth heading (physics getDirectionVector, never touches mesh)
    def truth_fwd(fi: int) -> np.ndarray:
        f = bmc.frame_vehicle_transform(fi)[3:6].astype(np.float64)
        return f / (np.linalg.norm(f) + 1e-12)

    w0 = world_body(0)
    c0 = w0.mean(0)
    w0c = w0 - c0
    truth0 = truth_fwd(0)

    print("Reconstructed car body heading (what Blender draws) vs INDEPENDENT")
    print("getDirectionVector() from physics. 'excess' near 1.0x = correct;")
    print("near 2.0x = rotation applied twice (the reported bug).")
    print()
    print(f"{'frame':>5} | {'recon body rot':>14} | {'physics turn':>12} | "
          f"{'heading err':>11} | {'excess':>7}")
    print("-" * 66)

    worst = 0.0
    for fi in range(0, n, max(1, n // 15)):
        wN = world_body(fi)
        wNc = wN - wN.mean(0)
        R = kabsch(w0c, wNc)
        recon_turn = rot_angle_deg(R)                    # how much the drawn body rotated
        physics_turn = angle_between(truth0, truth_fwd(fi))  # how much it should have
        # where the reconstructed body actually points now:
        recon_fwd = R @ truth0
        heading_err = angle_between(recon_fwd, truth_fwd(fi))
        worst = max(worst, heading_err)
        excess = (recon_turn / physics_turn) if physics_turn > 3.0 else float("nan")
        exc_s = f"{excess:6.2f}x" if excess == excess else "   -  "
        print(f"{fi:5d} | {recon_turn:12.2f}° | {physics_turn:10.2f}° | "
              f"{heading_err:9.2f}° | {exc_s:>7}")

    print("-" * 66)
    print(f"worst heading error vs physics ground truth: {worst:.2f}°")
    if worst < 8.0:
        print("VERDICT: PASS — drawn car points where physics says (single rotation).")
    else:
        print("VERDICT: FAIL — drawn heading diverges from physics (double/mis-rotation).")

    reader.close()
    bmc.close()


if __name__ == "__main__":
    main()
