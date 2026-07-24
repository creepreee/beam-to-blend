from __future__ import annotations
"""DECISIVE test: does the reconstructed body's NOSE axis align with the
physics getDirectionVector?

verticesGet() is world-ORIENTED (rotation baked) in a frame that only
translates with the vehicle origin.  The exporter's proven pipeline maps the
mesh with (vx,-vz,vy) and adds getPosition verbatim.  If the mesh map is
correct, the body's long (nose/length) axis — pool +X — must map to the SAME
world direction the vehicle actually points (getDirectionVector).  A wrong map
that is yawed 90 deg sends the nose perpendicular to the travel direction ->
detached parts' pool-recession no longer cancels getPosition -> DRAG.

We measure the body's principal (longest) axis in pool space via PCA at several
frames, map it through each candidate, and compare to getDirectionVector.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
from importer.capture_reader import BmcReader

bmc = BmcReader(sys.argv[1])
n = bmc.frame_count()
names = bmc.object_names()
body = max(names, key=lambda nm: bmc.object_info(nm).vertex_count)
ir = bmc.object_info(body).index_range

MAPS = {
    "CURRENT (vz,vx,vy)":   lambda v: np.stack([v[:,2],  v[:,0], v[:,1]], -1),
    "EXPORTER (vx,-vz,vy)": lambda v: np.stack([v[:,0], -v[:,2], v[:,1]], -1),
}

def principal_axis(pts):
    c = pts - pts.mean(0)
    _,_,Vt = np.linalg.svd(c, full_matrices=False)
    return Vt[0]  # longest axis (unit)

def ang(a,b):
    a=a/np.linalg.norm(a); b=b/np.linalg.norm(b)
    d=abs(a@b)  # axis is sign-ambiguous
    return np.degrees(np.arccos(np.clip(d,-1,1)))

frames = list(range(0, n, max(1, n//8)))
print(f"body={body}")
print(f"{'frame':>5} | {'CURRENT vs fwd':>15} | {'EXPORTER vs fwd':>16}")
print("-"*46)
cur_errs=[]; exp_errs=[]
for fi in frames:
    pool = bmc._read_shared_positions(fi).astype(np.float64)[ir]
    fwd = bmc.frame_vehicle_transform(fi)[3:6].astype(np.float64)
    ecur = ang(MAPS["CURRENT (vz,vx,vy)"](pool.reshape(-1,3))
               if False else None, fwd) if False else None
    # compute PCA in pool, then MAP the resulting axis (axis is a direction,
    # map is linear so mapping the axis == PCA of mapped points up to sign)
    ax_pool = principal_axis(pool)
    a_cur = np.array([ax_pool[2], ax_pool[0], ax_pool[1]])          # (vz,vx,vy)
    a_exp = np.array([ax_pool[0], -ax_pool[2], ax_pool[1]])         # (vx,-vz,vy)
    ec = ang(a_cur, fwd); ee = ang(a_exp, fwd)
    cur_errs.append(ec); exp_errs.append(ee)
    print(f"{fi:5d} | {ec:13.2f} deg | {ee:14.2f} deg")
print("-"*46)
print(f"mean nose-vs-heading error:  CURRENT={np.mean(cur_errs):6.2f} deg   "
      f"EXPORTER={np.mean(exp_errs):6.2f} deg")
print("(near 0 = nose points where car drives = correct; ~90 = yawed = drag)")
bmc.close()
