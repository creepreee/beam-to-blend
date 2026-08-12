"""Seam continuity: does the tail's first frame POP off the last captured frame?

A least-squares sine over a 24-frame window does not pass exactly through the
final sample, so the tail can open at a slightly different pose/velocity than the
capture ended on.  This measures both, per member, against the real per-frame
motion — a mismatch only matters if it is large RELATIVE to how far the car moves
in one frame anyway.

Run: python tests/diag_tail_seam.py <cache.bvc> [tail]
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runtime.cache_reader import CacheReader
import runtime.mesh_update as mu


def main():
    path = sys.argv[1]
    tail = float(sys.argv[2]) if len(sys.argv) > 2 else 30.0
    r = CacheReader(path)
    n = r.frame_count
    last = n - 1
    frames = list(range(max(0, n - 24), n))
    t_len = len(frames)
    nn = np.arange(t_len, dtype=np.float64)

    # frequency from the root (same as the addon)
    tfs = [r.frame_transform(f) for f in frames]
    p = np.array([t[0:3] for t in tfs], dtype=np.float64)
    best = None
    for period in mu._TAIL_PERIOD_GRID:
        w = 2.0 * math.pi / period
        sse = 0.0
        for k in range(3):
            c = mu._fit_sine_at(p[:, k], w)
            resid = p[:, k] - (c[0] + c[1] * np.cos(w * nn) + c[2] * np.sin(w * nn))
            sse += float(resid @ resid)
        if best is None or sse < best[0]:
            best = (sse, w)
    omega = best[1]
    basis = np.column_stack([np.ones_like(nn), np.cos(omega * nn), np.sin(omega * nn)])
    pinv = np.linalg.pinv(basis)
    t0 = float(t_len - 1)

    print(f"frames={n} tail={tail} period={2*math.pi/omega:.2f}")
    print(f"\n{'member':38s} {'realstep':>9s} {'seamjump':>9s} {'ratio':>7s} "
          f"{'fitresid':>9s} {'amp':>8s}")
    print("-" * 88)

    names = [o.name for o in r.stable_objects()]
    worst = []
    for name in names:
        try:
            stack = np.array([r.frame_positions(name, f).astype(np.float64)
                              for f in frames])
        except ValueError:
            continue
        if stack.ndim != 3 or stack.shape[1] == 0:
            continue
        coef = (pinv @ stack.reshape(t_len, -1)).reshape(3, stack.shape[1], 3)
        c, ac, as_ = coef[0], coef[1], coef[2]

        # tail pose at the seam (env=1, t=t0) vs the true final captured pose
        ct, st = math.cos(omega * t0), math.sin(omega * t0)
        seam = c + ac * ct + as_ * st
        v_last = stack[-1]
        jump = float(np.linalg.norm(seam - v_last, axis=1).mean())

        # how far the car really moves in one frame, at capture end
        realstep = float(np.linalg.norm(stack[-1] - stack[-2], axis=1).mean())

        # overall fit quality across the window
        recon = (c[None, :, :]
                 + ac[None, :, :] * np.cos(omega * nn)[:, None, None]
                 + as_[None, :, :] * np.sin(omega * nn)[:, None, None])
        resid = float(np.linalg.norm(recon - stack, axis=2).mean())
        amp = float(np.linalg.norm(np.hypot(ac, as_), axis=1).mean())

        ratio = jump / realstep if realstep > 0 else 0.0
        worst.append((ratio, name, realstep, jump, resid, amp))

    worst.sort(reverse=True)
    for ratio, name, realstep, jump, resid, amp in worst[:12]:
        print(f"{name[:38]:38s} {realstep*1000:9.4f} {jump*1000:9.4f} "
              f"{ratio:7.2f} {resid*1000:9.4f} {amp*1000:8.3f}")

    ratios = [w[0] for w in worst]
    jumps = [w[3] for w in worst]
    print(f"\nseam jump: mean={np.mean(jumps)*1000:.4f} mm  "
          f"max={np.max(jumps)*1000:.4f} mm")
    print(f"jump / one-frame-move: mean={np.mean(ratios):.2f}  max={np.max(ratios):.2f}")
    print("\n(ratio < ~1 means the seam discontinuity is smaller than the motion")
    print(" already present between two captured frames, i.e. not visible.)")


if __name__ == "__main__":
    main()
