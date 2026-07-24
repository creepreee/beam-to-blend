from __future__ import annotations
"""End-to-end verification of the drag fix, using the ACTUAL builder map.

Reconstructs exactly what the runtime will draw:
    world = cache_builder._pool_to_blender(pool) + getPosition(verbatim)
and checks:
  (A) body nose (PCA long axis) tracks getDirectionVector  (should be ~0 deg)
  (B) the most-DETACHED part (largest pool recession) moves LESS in world than
      getPosition travels  (recession cancels => part settles, no full drag)
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
from importer.capture_reader import BmcReader
from importer.cache_builder import _pool_to_blender

bmc = BmcReader(sys.argv[1])
n = bmc.frame_count()
names = bmc.object_names()
body = max(names, key=lambda nm: bmc.object_info(nm).vertex_count)
frames = list(range(0, n, max(1, n//10)))
shared = {fi: bmc._read_shared_positions(fi).astype(np.float64) for fi in frames}
P = {fi: bmc.frame_vehicle_transform(fi)[:3].astype(np.float64) for fi in frames}
travel = max(np.linalg.norm(P[fi]-P[frames[0]]) for fi in frames)

def ang(a,b):
    a=a/np.linalg.norm(a); b=b/np.linalg.norm(b)
    return np.degrees(np.arccos(np.clip(abs(a@b),-1,1)))

# (A) nose vs heading, using the REAL builder map
ir_b = bmc.object_info(body).index_range
errs=[]
for fi in frames:
    wb = _pool_to_blender(shared[fi][ir_b])
    c = wb - wb.mean(0)
    _,_,Vt = np.linalg.svd(c, full_matrices=False)
    nose = Vt[0]
    fwd = bmc.frame_vehicle_transform(fi)[3:6].astype(np.float64)
    errs.append(ang(nose, fwd))
print(f"(A) body nose vs getDirectionVector: mean={np.mean(errs):.2f} deg "
      f"(PASS if < 5)")

# (B) detached-part settling
def recede(ir):
    cs=np.array([shared[fi][ir].mean(0) for fi in frames])
    return np.linalg.norm(cs-cs[0],axis=1).max()
def world_motion(ir):
    cs=np.array([(_pool_to_blender(shared[fi][ir])+P[fi]).mean(0) for fi in frames])
    return np.linalg.norm(cs-cs[0],axis=1).max()

rows=[(nm, recede(bmc.object_info(nm).index_range)) for nm in names]
rows.sort(key=lambda r:-r[1])
top = rows[0][0]
ir_t = bmc.object_info(top).index_range
wm = world_motion(ir_t)
print(f"(B) most-detached part: {top}")
print(f"    pool recession={rows[0][1]:.2f} m   getPosition travel={travel:.2f} m")
print(f"    reconstructed world motion={wm:.2f} m")
print(f"    (recession CANCELS translation => world motion << travel => settles)")
print(f"    verdict: {'PASS (settles)' if wm < travel*0.85 else 'still dragging'}")
bmc.close()
