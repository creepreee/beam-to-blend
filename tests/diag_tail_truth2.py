"""What is the car ACTUALLY doing at capture end? (translation + rotation + verts)

Looks over a long window so a slow rock can't hide behind the 24-frame fit
window, and reports the dominant period by autocorrelation/FFT rather than the
addon's 4..30-frame grid.
Run: python tests/diag_tail_truth2.py <cache.bvc>
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runtime.cache_reader import CacheReader


def mat_to_quat(tf):
    m = np.asarray(tf[3:12], dtype=np.float64).reshape(3, 3)
    # simple, robust matrix->quaternion
    t = m.trace()
    if t > 0:
        s = math.sqrt(t + 1.0) * 2
        w = 0.25 * s
        x = (m[2, 1] - m[1, 2]) / s
        y = (m[0, 2] - m[2, 0]) / s
        z = (m[1, 0] - m[0, 1]) / s
    else:
        i = int(np.argmax([m[0, 0], m[1, 1], m[2, 2]]))
        if i == 0:
            s = math.sqrt(1.0 + m[0, 0] - m[1, 1] - m[2, 2]) * 2
            w = (m[2, 1] - m[1, 2]) / s
            x = 0.25 * s
            y = (m[0, 1] + m[1, 0]) / s
            z = (m[0, 2] + m[2, 0]) / s
        elif i == 1:
            s = math.sqrt(1.0 + m[1, 1] - m[0, 0] - m[2, 2]) * 2
            w = (m[0, 2] - m[2, 0]) / s
            x = (m[0, 1] + m[1, 0]) / s
            y = 0.25 * s
            z = (m[1, 2] + m[2, 1]) / s
        else:
            s = math.sqrt(1.0 + m[2, 2] - m[0, 0] - m[1, 1]) * 2
            w = (m[1, 0] - m[0, 1]) / s
            x = (m[0, 2] + m[2, 0]) / s
            y = (m[1, 2] + m[2, 1]) / s
            z = 0.25 * s
    q = np.array([w, x, y, z], dtype=np.float64)
    return q / np.linalg.norm(q)


def dominant_period(series, lo=3.0, hi=400.0):
    """Dominant period of a 1-D series via FFT on the detrended signal."""
    v = np.asarray(series, dtype=np.float64)
    v = v - np.polyval(np.polyfit(np.arange(len(v)), v, 1), np.arange(len(v)))
    if np.ptp(v) < 1e-12:
        return None, 0.0
    win = np.hanning(len(v))
    sp = np.abs(np.fft.rfft(v * win))
    freqs = np.fft.rfftfreq(len(v), d=1.0)
    best, bestp = 0.0, None
    for i in range(1, len(sp)):
        if freqs[i] <= 0:
            continue
        p = 1.0 / freqs[i]
        if lo <= p <= hi and sp[i] > best:
            best, bestp = sp[i], p
    return bestp, best


def main():
    path = sys.argv[1]
    win = int(sys.argv[2]) if len(sys.argv) > 2 else 300
    r = CacheReader(path)
    n = r.frame_count
    last = n - 1
    start = max(0, n - win)
    frames = list(range(start, n))
    tfs = [r.frame_transform(f) for f in frames]
    if any(t is None for t in tfs):
        print("no transform block")
        return
    p = np.array([t[0:3] for t in tfs], dtype=np.float64)

    print(f"frames={n}  window={len(frames)} (f{start}..f{last})")

    print("\n=== ROOT TRANSLATION over the window ===")
    for k, ax in enumerate("xyz"):
        col = p[:, k]
        per, mag = dominant_period(col)
        print(f"  {ax}: ptp={np.ptp(col)*1000:9.4f} mm   std={col.std()*1000:8.4f} mm"
              f"   dominant period={per if per else float('nan'):8.2f} frames")

    speed = np.linalg.norm(np.diff(p, axis=0), axis=1) * 1000.0
    print(f"\n  root speed: mean={speed.mean():.4f}  max={speed.max():.4f} mm/frame")
    print(f"  speed in last 24 frames: mean={speed[-24:].mean():.4f} "
          f"max={speed[-24:].max():.4f} mm/frame")
    print(f"  speed in first 24 of window: mean={speed[:24].mean():.4f} "
          f"max={speed[:24].max():.4f} mm/frame")

    # ---- rotation ----
    qs = np.array([mat_to_quat(t) for t in tfs])
    for i in range(1, len(qs)):
        if float(qs[i] @ qs[i - 1]) < 0:
            qs[i] = -qs[i]
    qm = qs.mean(axis=0)
    qm /= np.linalg.norm(qm)

    def qmul(a, b):
        w1, x1, y1, z1 = a
        w2, x2, y2, z2 = b
        return np.array([
            w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2,
            w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
            w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
            w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2,
        ])

    qm_inv = np.array([qm[0], -qm[1], -qm[2], -qm[3]])
    offs = np.array([qmul(q, qm_inv) for q in qs])
    print("\n=== ROOT ROTATION (vector part of q*qmean^-1, ~half-angle) ===")
    for k, ax in enumerate("xyz"):
        col = offs[:, k + 1]
        per, mag = dominant_period(col)
        deg = np.ptp(col) * 2.0 * 180.0 / math.pi
        print(f"  {ax}: ptp={deg:9.5f} deg   dominant period="
              f"{per if per else float('nan'):8.2f} frames")
    ang = 2.0 * np.arcsin(np.clip(np.linalg.norm(offs[:, 1:], axis=1), 0, 1))
    print(f"  total angular deviation from mean: ptp={np.ptp(ang)*180/math.pi:.5f} deg")
    angvel = np.abs(np.diff(ang)) * 180 / math.pi
    print(f"  angular speed: mean={angvel.mean():.5f} max={angvel.max():.5f} deg/frame")

    # ---- vertices: the visible deformation ----
    print("\n=== VERTEX MOTION (largest-moving object) ===")
    names = [o.name for o in r.stable_objects()]
    scored = []
    for name in names:
        try:
            a = r.frame_positions(name, last).astype(np.float64)
            b = r.frame_positions(name, last - 1).astype(np.float64)
        except ValueError:
            continue
        scored.append((float(np.abs(a - b).max()), name))
    scored.sort(reverse=True)
    print("  top movers by last-frame step:")
    for s, nm in scored[:5]:
        print(f"    {nm[:44]:44s} {s*1000:8.4f} mm/frame")

    for _, name in scored[:2]:
        stack = np.array([r.frame_positions(name, f).astype(np.float64)
                          for f in frames])
        centre = stack.mean(axis=0)
        dev = np.linalg.norm(stack - centre, axis=2)
        vi = int(np.argmax(dev.max(axis=0)))
        trace = stack[:, vi, :]
        print(f"\n  {name}  (most-moving vertex #{vi})")
        for k, ax in enumerate("xyz"):
            per, _ = dominant_period(trace[:, k])
            print(f"    {ax}: ptp={np.ptp(trace[:,k])*1000:9.4f} mm  "
                  f"dominant period={per if per else float('nan'):8.2f} frames")
        vstep = np.linalg.norm(np.diff(trace, axis=0), axis=1) * 1000
        print(f"    step: mean={vstep.mean():.4f} max={vstep.max():.4f} mm/frame")
        print(f"    step last 24: mean={vstep[-24:].mean():.4f} "
              f"max={vstep[-24:].max():.4f}")
        print(f"    LAST 30 FRAMES of that vertex (mm, rel. to window centre):")
        rel = (trace[-30:] - centre[vi]) * 1000
        for i, f in enumerate(frames[-30:]):
            print(f"      f{f:5d}  x={rel[i,0]:9.4f} y={rel[i,1]:9.4f} z={rel[i,2]:9.4f}")


if __name__ == "__main__":
    main()
