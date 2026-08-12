"""Velocity continuity at the tail seam, across every member.

Position at the seam is exact by construction (the stored seam residual).  This
measures the VELOCITY step: the fit shares one omega for the whole car, but
individual parts rock at slightly different frequencies, so the fitted sine's
slope at the last sample need not match the real one.

Reports the velocity step in mm/frame and, more usefully, as a fraction of the
PEAK speed the part reaches during its own swing (amplitude*omega) — a step that
is small next to the speeds already present in the motion is not visible.

Run: python tests/diag_tail_vel.py <cache.bvc>
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
    r = CacheReader(path)
    n = r.frame_count
    last = n - 1
    frames = list(range(max(0, n - mu._TAIL_FIT_WINDOW), n))
    t_len = len(frames)
    nn = np.arange(t_len, dtype=np.float64)

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
    ct0, st0 = math.cos(omega * t0), math.sin(omega * t0)

    rows = []
    for name in [o.name for o in r.stable_objects()]:
        try:
            stack = np.array([r.frame_positions(name, f).astype(np.float64)
                              for f in frames])
        except ValueError:
            continue
        if stack.ndim != 3 or stack.shape[1] == 0:
            continue
        coef = (pinv @ stack.reshape(t_len, -1)).reshape(3, stack.shape[1], 3)
        c, ac, as_ = coef[0], coef[1], coef[2]

        # real backward-difference velocity at capture end
        v_real = stack[-1] - stack[-2]
        # fitted sine's slope at the last sample (per frame)
        v_fit = omega * (-ac * st0 + as_ * ct0)
        step = np.linalg.norm(v_real - v_fit, axis=1)

        amp = np.linalg.norm(np.hypot(ac, as_), axis=1)
        peak_speed = amp * omega
        frac = np.where(peak_speed > 1e-12, step / np.maximum(peak_speed, 1e-12), 0.0)

        rows.append((float(frac.mean()), name,
                     float(np.linalg.norm(v_real, axis=1).mean()),
                     float(np.linalg.norm(v_fit, axis=1).mean()),
                     float(step.mean()), float(step.max()),
                     float(peak_speed.mean())))

    rows.sort(reverse=True)
    print(f"omega={omega:.4f} (period {2*math.pi/omega:.2f} frames)")
    print(f"\n{'member':36s} {'v_real':>8s} {'v_fit':>8s} {'vstep':>8s} "
          f"{'vstepmax':>9s} {'peakspd':>8s} {'step/peak':>9s}")
    print("-" * 96)
    for frac, name, vr, vf, sm, sx, ps in rows[:14]:
        print(f"{name[:36]:36s} {vr*1000:8.4f} {vf*1000:8.4f} {sm*1000:8.4f} "
              f"{sx*1000:9.4f} {ps*1000:8.4f} {frac:9.3f}")

    fr = np.array([x[0] for x in rows])
    st = np.array([x[4] for x in rows])
    sx = np.array([x[5] for x in rows])
    print(f"\nvelocity step (mm/frame): mean={st.mean()*1000:.4f} "
          f"max-of-means={st.max()*1000:.4f} worst-vertex={sx.max()*1000:.4f}")
    print(f"step / peak swing speed:  mean={fr.mean():.3f} max={fr.max():.3f}")
    print("\nInterpretation: step/peak << 1 means the seam velocity blip is small")
    print("next to the speeds the swing already sweeps through, i.e. invisible.")


if __name__ == "__main__":
    main()
