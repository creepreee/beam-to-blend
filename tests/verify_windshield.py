from __future__ import annotations
"""Prove the weld did NOT create windshield spikes.

A spike = a welded vertex whose triangles get stretched far beyond the
original (unwelded) mesh's max edge length in some frame.  We compare, per
frame, the max triangle edge length of the WELDED windshield against the
UNWELDED one.  If welding fused verts that separate, the welded max edge
explodes relative to the unwelded max edge.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
from runtime.cache_reader import CacheReader

welded_bvc, unwelded_bvc = sys.argv[1], sys.argv[2]
target = sys.argv[3] if len(sys.argv) > 3 else "flanje_e180_windshield"

rw = CacheReader(welded_bvc)
ru = CacheReader(unwelded_bvc)
iw = rw.base_indices(target)
iu = ru.base_indices(target)
n = min(rw.frame_count, ru.frame_count)

def max_edge(pos, idx):
    tri = pos[idx]
    e = np.concatenate([
        np.linalg.norm(tri[:,0]-tri[:,1],axis=1),
        np.linalg.norm(tri[:,1]-tri[:,2],axis=1),
        np.linalg.norm(tri[:,2]-tri[:,0],axis=1),
    ])
    return float(e.max()) if len(e) else 0.0

print(f"target={target}")
print(f"  welded:   {rw.get_object(target).vertex_count} verts, {iw.shape[0]} faces")
print(f"  unwelded: {ru.get_object(target).vertex_count} verts, {iu.shape[0]} faces")
print(f"\n{'frame':>5} | {'welded maxEdge':>14} | {'unwelded maxEdge':>16} | {'ratio':>7}")
print("-"*55)
worst = 0.0
for fi in range(0, n, max(1, n//12)):
    pw = rw.frame_positions(target, fi)
    pu = ru.frame_positions(target, fi)
    mw = max_edge(pw, iw); mu = max_edge(pu, iu)
    ratio = mw/mu if mu>1e-9 else 1.0
    worst = max(worst, ratio)
    print(f"{fi:5d} | {mw:12.4f} m | {mu:14.4f} m | {ratio:6.2f}x")
print("-"*55)
print(f"worst welded/unwelded max-edge ratio: {worst:.3f}x")
if worst < 1.05:
    print("PASS: welded windshield edges match unwelded (NO spikes).")
else:
    print("FAIL: welding stretched edges (spikes present).")
rw.close(); ru.close()
