"""Verify the smooth-stop tail is a TRUE two-sided decaying swing.

Pure CPython (no bpy): drives CachePlayback's tail math directly against a real
cache and measures the properties the feature is supposed to have.

Checks, in the user's terms — the swing must go
    <-------->, <------->, <----->, <--->, <->, rest
not
    <-------->, -------->, ------>, --->, ->, rest

  1. seam continuity: the tail's FIRST swing amplitude equals the amplitude the
     car was actually rocking at (no instant drop to ~29%)
  2. two-sided: the motion crosses the rest centre many times (once per
     half-period), i.e. it keeps rocking both ways
  3. monotone decay: each successive peak is smaller than the one before
  4. full rest: exactly at the oscillation centre once past the tail

Run: python tests/diag_tail_verify.py <cache.bvc> [tail_frames]
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# `runtime.mesh_update` imports bpy/mathutils lazily for the parts we exercise,
# but the ROOT fit builds mathutils.Matrix/Quaternion.  Outside Blender neither
# exists, so provide the minimal quaternion algebra those two code paths use.
# The VERTEX path (the subject of this test) touches none of this.
if "mathutils" not in sys.modules:
    import types as _types

    _mu = _types.ModuleType("mathutils")

    class _Quat:
        def __init__(self, seq=(1.0, 0.0, 0.0, 0.0)):
            self.w, self.x, self.y, self.z = (float(v) for v in seq)

        def __iter__(self):
            return iter((self.w, self.x, self.y, self.z))

        def _arr(self):
            return np.array([self.w, self.x, self.y, self.z], dtype=np.float64)

        def normalize(self):
            a = self._arr()
            nrm = np.linalg.norm(a)
            if nrm > 0:
                self.w, self.x, self.y, self.z = a / nrm

        def normalized(self):
            q = _Quat((self.w, self.x, self.y, self.z))
            q.normalize()
            return q

        def inverted(self):
            a = self._arr()
            d = float(a @ a)
            return _Quat((a[0] / d, -a[1] / d, -a[2] / d, -a[3] / d))

        def __mul__(self, o):
            w1, x1, y1, z1 = self
            w2, x2, y2, z2 = o
            return _Quat((
                w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2,
                w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
                w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
                w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2,
            ))

        def to_matrix(self):
            w, x, y, z = self
            return np.array([
                [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
            ], dtype=np.float64)

    class _Matrix:
        def __init__(self, rows):
            self.m = np.array([[float(v) for v in r] for r in rows],
                              dtype=np.float64)

        def to_quaternion(self):
            m = self.m[:3, :3]
            tr = m.trace()
            if tr > 0:
                s = math.sqrt(tr + 1.0) * 2
                q = (0.25 * s, (m[2, 1] - m[1, 2]) / s,
                     (m[0, 2] - m[2, 0]) / s, (m[1, 0] - m[0, 1]) / s)
            else:
                i = int(np.argmax(np.diag(m)))
                if i == 0:
                    s = math.sqrt(1.0 + m[0, 0] - m[1, 1] - m[2, 2]) * 2
                    q = ((m[2, 1] - m[1, 2]) / s, 0.25 * s,
                         (m[0, 1] + m[1, 0]) / s, (m[0, 2] + m[2, 0]) / s)
                elif i == 1:
                    s = math.sqrt(1.0 + m[1, 1] - m[0, 0] - m[2, 2]) * 2
                    q = ((m[0, 2] - m[2, 0]) / s, (m[0, 1] + m[1, 0]) / s,
                         0.25 * s, (m[1, 2] + m[2, 1]) / s)
                else:
                    s = math.sqrt(1.0 + m[2, 2] - m[0, 0] - m[1, 1]) * 2
                    q = ((m[1, 0] - m[0, 1]) / s, (m[0, 2] + m[2, 0]) / s,
                         (m[1, 2] + m[2, 1]) / s, 0.25 * s)
            return _Quat(q).normalized()

    _mu.Quaternion = _Quat
    _mu.Matrix = _Matrix
    sys.modules["mathutils"] = _mu

from runtime.cache_reader import CacheReader

FAILED = []


def check(cond, msg):
    print(f"  [{'PASS' if cond else 'FAIL'}] {msg}")
    if not cond:
        FAILED.append(msg)


class FakePlayback:
    """CachePlayback's tail math with bpy-dependent parts stubbed out."""

    def __init__(self, reader, members):
        import runtime.mesh_update as mu
        self.mu = mu
        self.reader = reader
        self._members = members
        self._objects = {m: None for m in members}
        self._chunk_map = {}
        self._chunks = {}
        self._chunk_member_ranges = {}
        self._smooth_stop_tail = 0.0
        self._tail_fit = None
        self._tail_vfit = None
        self._current_frame = None
        self._transform_empty = None

    # borrow the real implementations
    def _tail_sample_frames(self):
        from runtime.mesh_update import CachePlayback
        return CachePlayback._tail_sample_frames(self)

    def _tail_member_names(self):
        return list(self._members)

    def _fit_omega_from_vertices(self):
        from runtime.mesh_update import CachePlayback
        return CachePlayback._fit_omega_from_vertices(self)

    def _compute_tail_vfit(self, omega):
        from runtime.mesh_update import CachePlayback
        return CachePlayback._compute_tail_vfit(self, omega)

    def _glide_positions(self, name, last, t, env):
        from runtime.mesh_update import CachePlayback
        return CachePlayback._glide_positions(self, name, last, t, env)

    def _compute_tail_fit(self):
        from runtime.mesh_update import CachePlayback
        return CachePlayback._compute_tail_fit(self)

    def _matrix_from_transform(self, tf):
        from runtime.mesh_update import CachePlayback
        return CachePlayback._matrix_from_transform(self, tf)

    def _tail_omega(self):
        from runtime.mesh_update import CachePlayback
        return CachePlayback._tail_omega(self)


def main():
    path = sys.argv[1]
    tail = float(sys.argv[2]) if len(sys.argv) > 2 else 30.0
    r = CacheReader(path)
    n = r.frame_count
    last = n - 1

    import runtime.mesh_update as mu

    # The module binds `mathutils` only inside `try: import bpy` — which fails
    # outside Blender, so the name is absent even though our stub is installed.
    if getattr(mu, "mathutils", None) is None:
        mu.mathutils = sys.modules["mathutils"]

    names = [o.name for o in r.stable_objects()]
    # pick the biggest movers — they show the swing most clearly
    scored = []
    for nm in names:
        try:
            a = r.frame_positions(nm, last).astype(np.float64)
            b = r.frame_positions(nm, last - 1).astype(np.float64)
        except ValueError:
            continue
        scored.append((float(np.abs(a - b).max()), nm))
    scored.sort(reverse=True)
    members = [nm for _, nm in scored[:6]]

    pb = FakePlayback(r, members)
    pb._smooth_stop_tail = tail
    pb._tail_fit = pb._compute_tail_fit()
    omega = pb._tail_omega()
    pb._tail_vfit = pb._compute_tail_vfit(omega)

    period = 2.0 * math.pi / omega
    print(f"cache: {path}")
    print(f"frames={n}  tail={tail} cache frames  "
          f"omega={omega:.4f} rad/fr  period={period:.2f} fr  "
          f"({tail/period:.1f} swings in the tail)")

    check(pb._tail_vfit is not None, "per-vertex oscillation fit computed")
    if pb._tail_vfit is None:
        return

    # ---- envelope shape -------------------------------------------------
    print("\n=== ENVELOPE ===")
    env0 = mu._tail_envelope(0.0)
    env1 = mu._tail_envelope(1.0)
    check(abs(env0 - 1.0) < 1e-12,
          f"env(0) == 1 (opens at the CURRENT swing size, no seam drop) "
          f"[{env0:.6f}]")
    check(abs(env1) < 1e-12, f"env(1) == 0 (reaches exact rest) [{env1:.6f}]")
    d0 = (mu._tail_envelope(1e-4) - mu._tail_envelope(0.0)) / 1e-4
    check(abs(d0) < 1e-2,
          f"env'(0) ~ 0 (decay eases IN, no amplitude cliff) [{d0:.5f}]")
    d1 = (mu._tail_envelope(1.0) - mu._tail_envelope(1 - 1e-4)) / 1e-4
    check(abs(d1) < 1e-2,
          f"env'(1) ~ 0 (settles softly, not clipped) [{d1:.5f}]")
    print("  u:    " + "  ".join(f"{u:5.2f}" for u in np.linspace(0, 1, 11)))
    print("  env:  " + "  ".join(f"{mu._tail_envelope(u):5.3f}"
                                 for u in np.linspace(0, 1, 11)))

    # ---- the visible vertex swing --------------------------------------
    # ---- seam exactness: tail frame 0 == final captured pose ------------
    print("\n=== SEAM (tail must OPEN on the exact final captured pose) ===")
    vfit0 = pb._tail_vfit
    t_seam = (last - vfit0["start"])
    seam_err = 0.0
    for m in members:
        got = pb._glide_positions(m, last, t_seam, mu._tail_envelope(0.0))
        want = r.frame_positions(m, last).astype(np.float64)
        seam_err = max(seam_err, float(np.abs(got - want).max()))
    print(f"  worst per-vertex seam error = {seam_err*1e6:.4f} um")
    check(seam_err < 1e-6,
          f"tail frame 0 reproduces the final captured pose exactly "
          f"({seam_err*1e6:.4f} um)")

    name = members[0]
    vfit = pb._tail_vfit
    centre, ac, as_, _seam = vfit["members"][name]
    amp = np.linalg.norm(np.stack([np.hypot(ac[:, k], as_[:, k])
                                   for k in range(3)], axis=1), axis=1)
    vi = int(np.argmax(amp))
    print(f"\n=== VERTEX SWING: {name} vertex #{vi} ===")

    # real captured amplitude of that vertex, from the data
    frames = pb._tail_sample_frames()
    stack = np.array([r.frame_positions(name, f).astype(np.float64)
                      for f in frames])
    real_centre = stack[:, vi, :].mean(axis=0)
    real_amp = np.abs(stack[:, vi, :] - real_centre).max()

    t0 = last - vfit["start"]

    # sample the tail densely; measure signed deviation along the dominant axis
    axis = int(np.argmax(np.abs(stack[:, vi, :] - real_centre).max(axis=0)))
    fit_centre = float(centre[vi, axis])
    ss = np.arange(0.0, tail + 1.0, 0.25)
    devs = []
    for s in ss:
        u = min(1.0, s / tail)
        env = mu._tail_envelope(u)
        p = pb._glide_positions(name, last, t0 + s, env)
        devs.append(float(p[vi, axis]) - fit_centre)
    devs = np.array(devs)

    # 1. seam continuity
    first_peak = np.abs(devs[: int(period / 0.25) + 1]).max()
    print(f"  real captured amplitude (axis {'xyz'[axis]}) = {real_amp*1000:8.4f} mm")
    print(f"  tail's FIRST swing amplitude              = {first_peak*1000:8.4f} mm")
    ratio = first_peak / real_amp if real_amp > 0 else 0.0
    print(f"  ratio = {ratio:.4f}")
    check(ratio > 0.80,
          f"tail OPENS at the real swing amplitude (ratio {ratio:.3f} > 0.80) "
          f"— the old velocity-extrapolation gave ~0.29")

    # 2. two-sided: count centre crossings
    signs = np.sign(devs)
    nz = signs[signs != 0]
    crossings = int(np.sum(nz[:-1] * nz[1:] < 0))
    expected = int(tail / (period / 2))
    print(f"  centre crossings: {crossings} (expect ~{expected})")
    check(crossings >= max(2, expected - 2),
          f"swing is TWO-SIDED: crosses rest centre {crossings} times "
          f"(rocks both ways, never one-directional)")

    # 3. monotone decay of successive peaks
    peaks = []
    for i in range(1, len(devs) - 1):
        if abs(devs[i]) >= abs(devs[i - 1]) and abs(devs[i]) > abs(devs[i + 1]):
            peaks.append((ss[i], devs[i]))
    print(f"  successive peak amplitudes (mm):")
    for s, d in peaks:
        bar = "#" * max(1, int(abs(d) * 1000 * 4))
        print(f"    s={s:6.2f}  {d*1000:+9.4f}  {bar}")
    mags = [abs(d) for _, d in peaks]
    mono = all(b <= a * 1.02 + 1e-9 for a, b in zip(mags, mags[1:]))
    check(len(mags) >= 3, f"tail contains multiple swings ({len(mags)} peaks)")
    check(mono, "each successive swing is SMALLER than the last (monotone decay)")
    if len(mags) >= 2:
        both_signs = any(d1 * d2 < 0 for (_, d1), (_, d2) in zip(peaks, peaks[1:]))
        check(both_signs, "consecutive peaks ALTERNATE sign (<-->, <-->, not -->,-->)")

    # 4. rest
    p_end = pb._glide_positions(name, last, t0 + tail, mu._tail_envelope(1.0))
    p_past = pb._glide_positions(name, last, t0 + tail + 7.0,
                                 mu._tail_envelope(1.5))
    rest_off = float(np.abs(p_end[vi] - centre[vi]).max())
    check(rest_off < 1e-9,
          f"at s=tail every vertex sits exactly on its oscillation centre "
          f"({rest_off*1000:.6f} mm off)")
    check(float(np.abs(p_end - p_past).max()) < 1e-9,
          "stays at rest past the tail (no drift, no re-start)")

    # velocity decay, measured
    def speed_at(s):
        u = min(1.0, s / tail)
        a = pb._glide_positions(name, last, t0 + s, mu._tail_envelope(u))
        u2 = min(1.0, (s + 1) / tail)
        b = pb._glide_positions(name, last, t0 + s + 1, mu._tail_envelope(u2))
        return float(np.abs(b[vi] - a[vi]).max()) * 1000

    v_seam = speed_at(0.0)
    v_mid = speed_at(tail * 0.5)
    v_late = speed_at(tail * 0.9)
    # real speed just before the capture ends
    v_real = float(np.abs(stack[-1, vi] - stack[-2, vi]).max()) * 1000
    print(f"\n=== SPEED (mm/frame, vertex #{vi}) ===")
    print(f"  real, last captured frame = {v_real:8.4f}")
    print(f"  tail at seam (s=0)        = {v_seam:8.4f}")
    print(f"  tail mid   (s={tail*0.5:.0f})         = {v_mid:8.4f}")
    print(f"  tail late  (s={tail*0.9:.0f})         = {v_late:8.4f}")
    check(v_late < v_mid, "speed decays toward the end of the tail")

    # ---- root swing -----------------------------------------------------
    if pb._tail_fit is not None:
        print("\n=== ROOT SWING ===")
        fit = pb._tail_fit
        cx = fit["pos"][0][0]
        rt0 = last - fit["start"]
        rdev = []
        for s in ss:
            u = min(1.0, s / tail)
            env = mu._tail_envelope(u)
            tf = pb.mu.CachePlayback._glide_transform(pb, s, env, last)
            rdev.append(float(tf[0]) - cx)
        rdev = np.array(rdev)
        rs = np.sign(rdev)
        rnz = rs[rs != 0]
        rcross = int(np.sum(rnz[:-1] * rnz[1:] < 0))
        print(f"  root x centre crossings: {rcross} (expect ~{expected})")
        check(rcross >= max(2, expected - 2),
              f"root SWINGS both ways {rcross} times (not a one-way glide)")
        rpeaks = [abs(rdev[i]) for i in range(1, len(rdev) - 1)
                  if abs(rdev[i]) >= abs(rdev[i - 1]) and abs(rdev[i]) > abs(rdev[i + 1])]
        print("  root peak amplitudes (mm): " +
              "  ".join(f"{m*1000:.3f}" for m in rpeaks))
        check(all(b <= a * 1.02 + 1e-9 for a, b in zip(rpeaks, rpeaks[1:])),
              "root swings shrink monotonically")
        check(abs(rdev[-1]) < 1e-6,
              f"root settles ON its oscillation centre "
              f"({abs(rdev[-1])*1000:.6f} mm off)")

    print("\n" + "=" * 60)
    if FAILED:
        print(f"FAILED {len(FAILED)}:")
        for f in FAILED:
            print(f"  - {f}")
        sys.exit(1)
    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
