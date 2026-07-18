from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
from importer.capture_reader import CaptureReader

BIN = r"C:/Users/ubaid_i2c/AppData/Local/BeamNG/BeamNG.drive/current/captures/mycap2/capture.bin"

def quat_to_matrix(q):
    x, y, z, w = q
    return np.array([
        [1-2*(y*y+z*z),   2*(x*y-w*z),   2*(x*z+w*y)],
        [  2*(x*y+w*z), 1-2*(x*x+z*z),   2*(y*z-w*x)],
        [  2*(x*z-w*y),   2*(y*z+w*x), 1-2*(x*x+y*y)],
    ], dtype=np.float64)

with CaptureReader(BIN) as r:
    meta = r.meta
    nf = meta.frame_count
    nobj = len(meta.objects)
    print(f"version={meta.version} frames={nf} objects={nobj}")

    # Pick the body object (largest vertex count, likely the shell)
    body_i = max(range(nobj), key=lambda i: meta.objects[i].vertex_count)
    print(f"body object: [{body_i}] {meta.objects[body_i].name} vc={meta.objects[body_i].vertex_count}")

    frames_to_check = [0, 50, 100, 200, 325, 500, min(649, nf-1)]
    frames_to_check = [f for f in frames_to_check if f < nf]

    print("\n=== VEHICLE TRANSFORM PER FRAME ===")
    for fi in frames_to_check:
        vtx = r.frame_vehicle_transform(fi)
        pos = vtx[:3]; q = vtx[3:7]
        M = quat_to_matrix(q)
        # Where does each pool axis go?
        print(f"frame {fi:4d}: pos=({pos[0]:+.2f},{pos[1]:+.2f},{pos[2]:+.2f}) "
              f"quat=({q[0]:+.3f},{q[1]:+.3f},{q[2]:+.3f},{q[3]:+.3f}) "
              f"|q|={np.linalg.norm(q):.3f}")

    print("\n=== POOL (LOCAL) POSITION STATS for body, per frame ===")
    print("If LOCAL: bbox center stays near constant, spans constant.")
    print("If WORLD: bbox center moves with vehicle position.")
    for fi in frames_to_check:
        p = r.frame_positions(fi, body_i)
        c = p.mean(axis=0)
        span = p.max(axis=0) - p.min(axis=0)
        print(f"frame {fi:4d}: center=({c[0]:+.2f},{c[1]:+.2f},{c[2]:+.2f}) "
              f"span=({span[0]:.2f},{span[1]:.2f},{span[2]:.2f})")

    # Rigidity check: for the body, compute the orientation directly from pool
    # positions via Kabsch vs frame 0. If pool is LOCAL and rigid rotation is
    # baked out, orientation should be ~identity across frames.
    print("\n=== KABSCH: rotation of body pool vs frame 0 ===")
    p0 = r.frame_positions(0, body_i).astype(np.float64)
    c0 = p0.mean(axis=0)
    p0c = p0 - c0
    for fi in frames_to_check:
        pf = r.frame_positions(fi, body_i).astype(np.float64)
        cf = pf.mean(axis=0)
        pfc = pf - cf
        H = p0c.T @ pfc
        U, S, Vt = np.linalg.svd(H)
        d = np.sign(np.linalg.det(Vt.T @ U.T))
        D = np.diag([1, 1, d])
        R = Vt.T @ D @ U.T
        # angle of rotation
        angle = np.degrees(np.arccos(np.clip((np.trace(R)-1)/2, -1, 1)))
        # residual rigidity
        resid = np.sqrt(((pfc @ R.T - p0c)**2).sum(axis=1)).mean()
        print(f"frame {fi:4d}: rotation_angle={angle:7.2f}deg  mean_resid={resid:.4f}m")
