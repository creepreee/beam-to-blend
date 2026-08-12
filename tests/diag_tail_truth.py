"""Diagnose the smooth-stop tail against a real cache (no bpy needed).

Measures what the car is ACTUALLY doing at capture end (root oscillation and
vertex oscillation), then replays the CURRENT tail math to see what it produces.
Run: python tests/diag_tail_truth.py <cache.bvc>
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runtime.cache_reader import CacheReader

TAIL_FIT_WINDOW = 24
PERIOD_GRID = np.linspace(4.0, 30.0, 40)
LAMBDA = 1.5


def fit_sine_at(values, omega, eps=None):
    values = np.asarray(values, dtype=np.float64)
    if values.ndim != 1 or len(values) < 8:
        return None
    if eps is not None and np.ptp(values) < eps:
        return None
    n = np.arange(len(values), dtype=np.float64)
    x = np.column_stack([np.ones_like(n), np.cos(omega * n), np.sin(omega * n)])
    coef, *_ = np.linalg.lstsq(x, values, rcond=None)
    return coef


def main():
    path = sys.argv[1]
    r = CacheReader(path)
    n = r.frame_count
    last = n - 1
    print(f"cache: {path}")
    print(f"frames={n}  objects={len(r.object_names())}")

    # ---- root translation: what is the car actually doing at the end? ----
    start = max(0, n - TAIL_FIT_WINDOW)
    frames = list(range(start, n))
    tfs = [r.frame_transform(f) for f in frames]
    if any(t is None for t in tfs):
        print("NO TRANSFORM BLOCK in cache")
        return
    p = np.array([t[0:3] for t in tfs], dtype=np.float64)
    print("\n=== ROOT TRANSLATION, last 24 frames (mm, relative to frame -24) ===")
    rel = (p - p[0]) * 1000.0
    for i, f in enumerate(frames):
        print(f"  f{f:5d}  x={rel[i,0]:9.3f}  y={rel[i,1]:9.3f}  z={rel[i,2]:9.3f}")

    # per-frame velocity of the root
    vel = np.diff(p, axis=0) * 1000.0  # mm/frame
    speed = np.linalg.norm(vel, axis=1)
    print("\n=== ROOT SPEED (mm/frame) ===")
    print("  " + "  ".join(f"{s:.3f}" for s in speed))
    print(f"  speed at capture end = {speed[-1]:.4f} mm/frame")
    print(f"  mean speed over window = {speed.mean():.4f} mm/frame")
    print(f"  max  speed over window = {speed.max():.4f} mm/frame")

    # ---- the fit the addon computes ----
    best = None
    for period in PERIOD_GRID:
        w = 2.0 * math.pi / period
        sse = 0.0
        for k in range(3):
            c = fit_sine_at(p[:, k], w)
            nn = np.arange(len(frames), dtype=np.float64)
            resid = p[:, k] - (c[0] + c[1] * np.cos(w * nn) + c[2] * np.sin(w * nn))
            sse += float(resid @ resid)
        if best is None or sse < best[0]:
            best = (sse, w, period)
    sse, omega, period = best
    pos_fit = {k: fit_sine_at(p[:, k], omega) for k in range(3)}
    print(f"\n=== FITTED OSCILLATION ===")
    print(f"  period = {period:.3f} cache frames   omega = {omega:.4f} rad/frame")
    print(f"  joint SSE = {sse:.3e}")
    for k, ax in enumerate("xyz"):
        c, ac, as_ = pos_fit[k]
        amp = math.hypot(ac, as_)
        print(f"  {ax}: centre={c:12.6f}  amp={amp*1000:8.4f} mm")

    # phase at capture end: where in the swing does the capture stop?
    t0 = last - start
    print(f"\n=== PHASE AT CAPTURE END (t0={t0}) ===")
    for k, ax in enumerate("xyz"):
        c, ac, as_ = pos_fit[k]
        amp = math.hypot(ac, as_)
        val = ac * math.cos(omega * t0) + as_ * math.sin(omega * t0)
        dval = omega * (-ac * math.sin(omega * t0) + as_ * math.cos(omega * t0))
        frac = (val / amp) if amp > 1e-12 else 0.0
        print(f"  {ax}: offset-from-centre={val*1000:8.4f} mm "
              f"({frac:+.3f} of amp)   d/ds={dval*1000:8.4f} mm/frame")

    # ---- VERTEX motion: the thing the user actually sees deform ----
    print("\n=== VERTEX OSCILLATION vs the (v_last - v_prev) the tail uses ===")
    names = [o.name for o in r.stable_objects()][:6]
    for name in names:
        try:
            v_last = r.frame_positions(name, last).astype(np.float64)
            v_prev = r.frame_positions(name, last - 1).astype(np.float64)
        except ValueError:
            continue
        step = np.linalg.norm(v_last - v_prev, axis=1)
        # the real oscillation amplitude of the vertices over the window
        stack = np.array([r.frame_positions(name, f).astype(np.float64)
                          for f in frames])
        centre = stack.mean(axis=0)
        dev = np.linalg.norm(stack - centre, axis=2)  # (T, V)
        real_amp = dev.max(axis=0)
        offset_now = np.linalg.norm(v_last - centre, axis=1)
        # what amplitude does the current tail give? |v_last-v_prev| / omega
        tail_amp = step / omega
        print(f"  {name[:38]:38s}")
        print(f"     mean |v_last - v_prev|      = {step.mean()*1000:9.4f} mm/frame")
        print(f"     mean REAL osc amplitude     = {real_amp.mean()*1000:9.4f} mm")
        print(f"     mean |v_last - centre|      = {offset_now.mean()*1000:9.4f} mm")
        print(f"     mean TAIL amplitude (v/w)   = {tail_amp.mean()*1000:9.4f} mm")
        ratio = tail_amp.mean() / real_amp.mean() if real_amp.mean() > 1e-12 else 0
        print(f"     tail/real amplitude ratio   = {ratio:9.4f}   <-- want ~1.0")

    # ---- replay the CURRENT tail: root path over a 30-frame tail ----
    tail = 30.0
    print(f"\n=== CURRENT TAIL ROOT PATH (tail={tail} cache frames) ===")
    print("  s      env     x-offset   y-offset   z-offset   speed(mm/frame)")
    prevp = None
    for i in range(0, 33):
        s = float(i)
        u = 1.0 if s >= tail else s / tail
        env = (1.0 - u) * math.exp(-LAMBDA * u)
        t = t0 + s
        ct, st = math.cos(omega * t), math.sin(omega * t)
        cur = np.empty(3)
        for k in range(3):
            c, ac, as_ = pos_fit[k]
            cur[k] = (ac * ct + as_ * st) * env
        sp = np.linalg.norm(cur - prevp) * 1000.0 if prevp is not None else float("nan")
        prevp = cur.copy()
        print(f"  {s:5.1f}  {env:6.4f}  {cur[0]*1000:9.4f}  {cur[1]*1000:9.4f}  "
              f"{cur[2]*1000:9.4f}   {sp:8.4f}")

    # ---- sign changes = does it actually swing both ways? ----
    print("\n=== DOES THE TAIL SWING BOTH WAYS? (sign of z-offset) ===")
    zs = []
    for i in range(0, int(tail) + 1):
        s = float(i)
        u = 1.0 if s >= tail else s / tail
        env = (1.0 - u) * math.exp(-LAMBDA * u)
        t = t0 + s
        c, ac, as_ = pos_fit[2]
        zs.append((ac * math.cos(omega * t) + as_ * math.sin(omega * t)) * env)
    signs = [1 if z > 0 else (-1 if z < 0 else 0) for z in zs]
    flips = sum(1 for a, b in zip(signs, signs[1:]) if a * b < 0)
    print(f"  z crossings of centre during tail: {flips} "
          f"(expect ~{tail/(period/2):.1f} for a real decaying swing)")

    # vertex swing profile k
    print("\n=== VERTEX PROFILE k(s) = (1/w)sin(w s) env ===")
    print("  s        k        (k>0 = moving along +v, k<0 = reversed)")
    for i in range(0, int(tail) + 1, 2):
        s = float(i)
        u = 1.0 if s >= tail else s / tail
        env = (1.0 - u) * math.exp(-LAMBDA * u)
        k = (1.0 / omega) * math.sin(omega * s) * env
        print(f"  {s:5.1f}  {k:9.5f}")


if __name__ == "__main__":
    main()
