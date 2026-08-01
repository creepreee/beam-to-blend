"""Rebuild the BVC from the BMC capture with the UV-aware weld.

Builds to a temporary path and only swaps over the existing cache once the
build succeeds and passes a UV-distortion check, so an interrupted run can
never leave the user without a usable cache.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from importer.cache_builder import CacheBuilder  # noqa: E402
from runtime.cache_reader import CacheReader  # noqa: E402

CAP = Path(
    r"C:\Users\ubaid_i2c\AppData\Local\BeamNG\BeamNG.drive\current\captures"
)
BMC = CAP / "name.bmc"
FINAL = CAP / "name.bvc"
TMP = CAP / "name.rebuild.bvc"

# Parts the user reported as stretched, plus controls that were already fine.
CHECK = [
    "tire_01a_16x7_26",
    "flanje_e180_transmission_awd",
    "flanje_e180_brace",
    "flanje_e180_subframe_R",
    "flanje_e180_strut_F",
    "flanje_e180_lowerarm_F",
    "flanje_e180_lowerarm_R",
    "flanje_e180_hood",
]


def uv_distortion(pos: np.ndarray, uv: np.ndarray, idx: np.ndarray,
                 loop_uv: np.ndarray = None) -> float:
    """99th-percentile / median texel density. 1.0 == perfectly even.
    
    When *loop_uv* is given (v5, (F,3,2)), use it directly per face corner.
    Otherwise fall back to per-vertex *uv* indexed by *idx*.
    """
    if loop_uv is not None and loop_uv.shape[0] > 0:
        t2 = loop_uv  # (F,3,2) — already per-corner
    else:
        t2 = uv[idx]  # expand per-vertex to per-corner
    t3 = pos[idx]
    a3 = 0.5 * np.linalg.norm(
        np.cross(t3[:, 1] - t3[:, 0], t3[:, 2] - t3[:, 0]), axis=1
    )
    d, e = t2[:, 1] - t2[:, 0], t2[:, 2] - t2[:, 0]
    a2 = 0.5 * np.abs(d[:, 0] * e[:, 1] - d[:, 1] * e[:, 0])
    m = a3 > 1e-12
    if m.sum() < 5:
        return float("nan")
    dens = a2[m] / a3[m]
    return float(np.percentile(dens, 99) / (np.median(dens) + 1e-30))


def main() -> int:
    if not BMC.exists():
        print(f"!! capture not found: {BMC}")
        return 1
    if TMP.exists():
        TMP.unlink()

    print(f"source : {BMC}  ({BMC.stat().st_size / 1e9:.2f} GB)")
    print(f"target : {FINAL}")
    t0 = time.time()

    CacheBuilder(BMC.parent, TMP).build(capture_bmc_path=str(BMC), weld=True)

    print(f"[build] finished in {time.time() - t0:.0f}s", flush=True)

    # --- verify before swapping ---------------------------------------
    r = CacheReader(TMP)
    names = {o.name for o in r.stable_objects()}
    print(f"[verify] {r.frame_count} frames, {len(names)} stable objects")

    worst = 0.0
    print(f"  {'part':34s} {'verts':>7s} {'uv distortion':>14s}")
    for n in CHECK:
        if n not in names:
            print(f"  {n:34s} {'MISSING':>7s}")
            continue
        o = r.get_object(n)
        dist = uv_distortion(
            r.frame_positions(n, 0).reshape(-1, 3),
            r.base_uvs(n),
            r.base_indices(n),
            loop_uv=r.base_loop_uvs(n),
        )
        flag = "" if dist < 6.0 else "   <-- STILL BAD"
        worst = max(worst, dist)
        print(f"  {n:34s} {o.vertex_count:7d} {dist:14.2f}{flag}")
    r.close()

    if worst >= 6.0:
        print(f"\n!! worst distortion {worst:.1f} — NOT swapping, temp kept at {TMP}")
        return 2

    if FINAL.exists():
        FINAL.unlink()
    os.replace(TMP, FINAL)
    print(f"\n[ok] swapped into place: {FINAL} "
          f"({FINAL.stat().st_size / 1e9:.2f} GB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
