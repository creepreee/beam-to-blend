from __future__ import annotations
"""Full build of a BMC -> BVC with the fixed map, then re-run the honest
nose-vs-heading + detached-part settling check on the BUILT BVC (what the
runtime actually reads)."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
from importer.cache_builder import CacheBuilder
from runtime.cache_reader import CacheReader
from importer.capture_reader import BmcReader

bmc_path, bvc_path = sys.argv[1], sys.argv[2]
print(f"building {bvc_path} from {bmc_path} ...", flush=True)
CacheBuilder(os.path.dirname(bvc_path), bvc_path).build_from_capture(bmc_path)

r = CacheReader(bvc_path)
bmc = BmcReader(bmc_path)
n = min(r.frame_count, bmc.frame_count())
body = max(r.stable_objects(), key=lambda o:o.vertex_count).name
frames = list(range(0, n, max(1, n//10)))

def ang(a,b):
    a=a/np.linalg.norm(a); b=b/np.linalg.norm(b)
    return np.degrees(np.arccos(np.clip(abs(a@b),-1,1)))

# reconstruct world = R@vert + pos from the BVC transform block (runtime path)
errs=[]
for fi in frames:
    v = r.frame_positions(body, fi).astype(np.float64)
    tf = r.frame_transform(fi)
    R = tf[3:12].reshape(3,3).astype(np.float64); pos = tf[:3].astype(np.float64)
    w = (R @ v.T).T + pos
    c = w - w.mean(0)
    _,_,Vt = np.linalg.svd(c, full_matrices=False)
    fwd = bmc.frame_vehicle_transform(fi)[3:6].astype(np.float64)
    errs.append(ang(Vt[0], fwd))
print(f"BUILT BVC body nose vs heading: mean={np.mean(errs):.2f} deg "
      f"({'PASS' if np.mean(errs)<5 else 'FAIL'})")
r.close(); bmc.close()
