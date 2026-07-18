from __future__ import annotations

"""INTELLIGENT ANIMATION SUMMARY for BVC files.

Reads a BVC cache and computes precise, concise summary statistics of the
vehicle motion. Every number comes from the data — nothing is hallucinated.

Usage:
    python tests/animation_summary.py <cache.bvc>
    python tests/animation_summary.py  # auto-detect
"""

import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import numpy as np

from runtime.cache_reader import CacheReader


def quat_multiply(q1: np.ndarray, q2: np.ndarray) -> np.ndarray:
    x1, y1, z1, w1 = q1
    x2, y2, z2, w2 = q2
    return np.array([
        w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
        w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
        w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2,
        w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2,
    ], dtype=np.float64)


def quat_conjugate(q: np.ndarray) -> np.ndarray:
    return np.array([-q[0], -q[1], -q[2], q[3]], dtype=np.float64)


def quat_to_axis_angle(q: np.ndarray) -> tuple[float, np.ndarray]:
    """(x,y,z,w) → angle (rad), axis vector (unit)."""
    half = np.arccos(np.clip(q[3], -1.0, 1.0))
    ang = 2.0 * half
    if abs(ang) < 1e-12:
        return 0.0, np.array([1.0, 0.0, 0.0])
    ax = q[:3] / np.sin(half)
    return ang, ax


def quat_log(q: np.ndarray) -> np.ndarray:
    """Quaternion log: returns the rotation vector (axis * angle/2)."""
    half, axis = quat_to_axis_angle(q)
    return axis * half  # rotation_vector * 0.5


def dq_between(q1: np.ndarray, q2: np.ndarray) -> np.ndarray:
    """Delta quaternion: q_delta such that q2 = q_delta * q1 (applies after q1)."""
    return quat_multiply(q2, quat_conjugate(q1))


def format_vec(v: np.ndarray, fmt: str = "+.3f") -> str:
    s = ", ".join(f"{x:{fmt}}" for x in v[:3])
    if len(v) == 4:
        s += f", {v[3]:{fmt}}"
    return f"({s})"


def main():
    if len(sys.argv) > 1:
        bvc_path = sys.argv[1]
    else:
        candidates = [
            r"C:\Users\ubaid_i2c\Downloads\mycrash_fixed.bvc",
            r"C:\Users\ubaid_i2c\Downloads\mycrash.bvc",
        ]
        bvc_path = next((p for p in candidates if os.path.exists(p)), None)
        if bvc_path is None:
            print("Usage: python tests/animation_summary.py <cache.bvc>")
            sys.exit(1)

    if not os.path.exists(bvc_path):
        print(f"ERROR: {bvc_path} not found")
        sys.exit(1)

    reader = CacheReader(bvc_path)
    h = reader.header
    n = reader.frame_count
    tdo = h.get("transform_data_offset", 0)
    has_tf = tdo != 0

    print("=" * 88)
    print("BVC ANIMATION SUMMARY")
    print("=" * 88)
    print(f"  File:  {bvc_path}")
    print(f"  Size:  {os.path.getsize(bvc_path) / 1e9:.2f} GB")
    print(f"  Version: v{h['version']}  Frames: {n}  "
          f"Objects: {h['object_count']}")
    fps = 60.0
    dur = n / fps
    print(f"  Duration: {dur:.2f}s at {fps:.0f} fps ({n} frames)")

    # Allocate arrays for transform data
    n_skip = 1 if has_tf else 0
    positions = np.zeros((n, 3), dtype=np.float64)
    quaternions = np.zeros((n, 4), dtype=np.float64)

    for fi in range(n):
        if has_tf:
            tf = reader.frame_transform(fi)
            positions[fi] = tf[:3].astype(np.float64)
            q = tf[3:7].astype(np.float64)
            qn = np.linalg.norm(q)
            if qn > 0:
                q /= qn
            quaternions[fi] = q
        else:
            positions[fi] = 0.0
            quaternions[fi] = np.array([0.0, 0.0, 0.0, 1.0])

    reader.close()
    print(f"  Transform data: {'YES' if has_tf else 'NONE'}")

    # ---------------------------------------------------------------
    # 1. TRANSLATION
    # ---------------------------------------------------------------
    print(f"\n{'=' * 84}")
    print("  1. TRANSLATION")
    print(f"  {'=' * 84}")

    p0 = positions[0]
    pN = positions[-1]
    net_disp = np.linalg.norm(pN - p0)
    cumul_path = float(np.sum(np.linalg.norm(np.diff(positions, axis=0), axis=1)))

    print(f"  Start position:    {format_vec(p0)}")
    print(f"  End position:      {format_vec(pN)}")
    print(f"  Net displacement:  {net_disp:.4f} m")
    print(f"  Cumulative path:   {cumul_path:.4f} m")
    print(f"  Straightness:      {net_disp / cumul_path:.3f} (1=straight)")

    dt = 1.0 / fps
    vel = np.diff(positions, axis=0) / dt
    speed = np.linalg.norm(vel, axis=1)
    max_speed = float(speed.max())
    max_speed_frame = int(np.argmax(speed))
    print(f"  Peak speed:        {max_speed:.2f} m/s ({max_speed * 3.6:.1f} km/h) "
          f"at frame {max_speed_frame}")

    acc = np.diff(vel, axis=0) / dt if len(vel) > 1 else vel
    peak_acc = float(np.max(np.linalg.norm(acc, axis=1))) if len(acc) > 0 else 0.0
    print(f"  Peak acceleration: {peak_acc:.2f} m/s2 "
          f"({peak_acc / 9.81:.1f} g)")

    # ---------------------------------------------------------------
    # 2. ROTATION
    # ---------------------------------------------------------------
    print(f"\n{'=' * 84}")
    print("  2. ROTATION")
    print(f"  {'=' * 84}")

    # Axis-angle for each frame's absolute orientation
    eulers = np.zeros((n, 3), dtype=np.float64)
    for fi in range(n):
        q = quaternions[fi]
        x, y, z, w = q
        # ZYX intrinsic (yaw, pitch, roll) from quaternion
        roll = np.arctan2(2.0 * (w * x + y * z), 1.0 - 2.0 * (x * x + y * y))
        pitch = np.arcsin(np.clip(2.0 * (w * y - z * x), -1.0, 1.0))
        yaw = np.arctan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))
        eulers[fi] = np.degrees([roll, pitch, yaw])

    # Cumulative rotation via delta quaternions
    cumul_rotation = 0.0
    axis_accumulator = np.zeros(3, dtype=np.float64)
    per_frame_angle = np.zeros(n, dtype=np.float64)
    for fi in range(1, n):
        dq = dq_between(quaternions[fi - 1], quaternions[fi])
        ang, ax = quat_to_axis_angle(dq)
        per_frame_angle[fi] = abs(ang)
        cumul_rotation += abs(ang)
        axis_accumulator += ax * abs(ang)

    mean_axis = axis_accumulator / (np.linalg.norm(axis_accumulator) + 1e-30)
    assert isinstance(mean_axis, np.ndarray)
    avg_angle = np.mean(per_frame_angle[1:])
    print(f"  Cumulative rotation angle: {np.degrees(cumul_rotation):.1f} deg")
    print(f"  Average rotation per frame: {np.degrees(avg_angle):.4f} deg")
    print(f"  Dominant rotation axis: {format_vec(mean_axis, '.3f')} "
          f"(weighted by angle)")

    # Angular velocity
    ang_vel = per_frame_angle[1:] / dt
    peak_ang_vel = float(ang_vel.max())
    peak_av_frame = int(np.argmax(ang_vel)) + 1
    print(f"  Peak angular velocity: {np.degrees(peak_ang_vel):.1f} deg/s "
          f"at frame {peak_av_frame}")

    # Detect rotation sign flips (changes in direction)
    # For simplicity, check dot product of successive delta axes
    flip_count = 0
    for fi in range(2, n):
        _, ax1 = quat_to_axis_angle(dq_between(quaternions[fi - 2], quaternions[fi - 1]))
        _, ax2 = quat_to_axis_angle(dq_between(quaternions[fi - 1], quaternions[fi]))
        if np.dot(ax1, ax2) < -0.5 and per_frame_angle[fi] > 0.01:
            flip_count += 1
    print(f"  Direction reversals: {flip_count}")

    # ---------------------------------------------------------------
    # 3. MOTION PROFILE — keyframe detection
    # ---------------------------------------------------------------
    print(f"\n{'=' * 84}")
    print("  3. KEYFRAMES (significant events)")
    print(f"  {'=' * 84}")

    threshold = 0.75

    speed_thresh = max_speed * threshold
    ang_vel_thresh = peak_ang_vel * threshold
    acc_thresh = peak_acc * threshold if peak_acc > 1.0 else 0.5

    keyframes = set()

    # High-speed events
    for fi in range(1, n - 1):
        if fi < len(speed) and speed[fi] >= speed_thresh:
            # Local peak detection
            if speed[fi] >= speed[fi - 1] and speed[fi] >= speed[fi + 1]:
                keyframes.add((fi, "speed", speed[fi]))

    # High-rotation events
    for fi in range(1, n - 1):
        if fi < len(ang_vel) and ang_vel[fi] >= ang_vel_thresh:
            if ang_vel[fi] >= ang_vel[fi - 1] and ang_vel[fi] >= ang_vel[fi + 1]:
                keyframes.add((fi, "rotation", np.degrees(ang_vel[fi])))

    # High-acceleration events
    for fi in range(1, n - 1):
        if fi < len(acc) and len(acc) > 0 and np.linalg.norm(acc[fi]) >= acc_thresh:
            a_mag = np.linalg.norm(acc[fi])
            prev_a = np.linalg.norm(acc[fi - 1]) if fi > 0 else 0
            next_a = np.linalg.norm(acc[fi + 1]) if fi < len(acc) - 1 else 0
            if a_mag >= prev_a and a_mag >= next_a:
                keyframes.add((fi, "impact", a_mag))

    if len(keyframes) == 0:
        print("  No significant events detected.")
    else:
        print(f"  Detected {len(keyframes)} keyframes (>{threshold*100:.0f}% peak):")
        for frm, kind, val in sorted(keyframes, key=lambda x: x[0]):
            ts = frm / fps
            if kind == "speed":
                print(f"    F {frm:5d}  T {ts:.2f}s  SPEED PEAK  {val:.1f} m/s ({val*3.6:.0f} km/h)")
            elif kind == "rotation":
                print(f"    F {frm:5d}  T {ts:.2f}s  SPIN PEAK   {val:.1f} deg/s")
            elif kind == "impact":
                print(f"    F {frm:5d}  T {ts:.2f}s  IMPACT      {val:.1f} m/s2 ({val/9.81:.1f} g)")

    # ---------------------------------------------------------------
    # 4. DYNAMIC RANGE SUMMARY
    # ---------------------------------------------------------------
    print(f"\n{'=' * 84}")
    print("  4. DYNAMIC RANGE")
    print(f"  {'=' * 84}")

    euler_rng = np.ptp(eulers, axis=0)
    print(f"  Euler range (XYZ):   "
          f"roll={euler_rng[0]:.1f}  pitch={euler_rng[1]:.1f}  yaw={euler_rng[2]:.1f}  (deg)")
    print(f"  Euler min (XYZ):     "
          f"roll={eulers[:, 0].min():.1f}  pitch={eulers[:, 1].min():.1f}  "
          f"yaw={eulers[:, 2].min():.1f}  (deg)")
    print(f"  Euler max (XYZ):     "
          f"roll={eulers[:, 0].max():.1f}  pitch={eulers[:, 1].max():.1f}  "
          f"yaw={eulers[:, 2].max():.1f}  (deg)")

    pos_rng = np.ptp(positions, axis=0)
    print(f"  Position range (XYZ): "
          f"dx={pos_rng[0]:.2f}m  dy={pos_rng[1]:.2f}m  dz={pos_rng[2]:.2f}m")

    # ---------------------------------------------------------------
    # 5. VERTEX ANALYSIS (body object)
    # ---------------------------------------------------------------
    print(f"\n{'=' * 84}")
    print("  5. BODY DEFORMATION")
    print(f"  {'=' * 84}")

    reader = CacheReader(bvc_path)
    stable = reader.stable_objects()
    if stable:
        body = max(stable, key=lambda o: o.vertex_count)
        body_name = body.name
        body_vc = body.vertex_count

        pos_f0 = reader.frame_positions(body_name, 0).astype(np.float64)
        max_drift = 0.0
        for fi in range(0, n, max(1, n // 10)):
            pos_fi = reader.frame_positions(body_name, fi).astype(np.float64)
            drift = float(np.abs(pos_fi - pos_f0).max())
            if drift > max_drift:
                max_drift = drift
        print(f"  Body object: {body_name!r}  vertices={body_vc}")
        print(f"  Max vertex deformation (drift from frame 0): {max_drift:.4f} m")

    reader.close()

    # ---------------------------------------------------------------
    # 6. MOTION CLASSIFICATION
    # ---------------------------------------------------------------
    print(f"\n{'=' * 84}")
    print("  6. CLASSIFICATION")
    print(f"  {'=' * 84}")

    # Determine if this is a crash, tumble, roll, etc.
    total_rot = np.degrees(cumul_rotation)
    has_tumble = total_rot > 90
    has_full_roll = total_rot > 360
    has_multi_roll = total_rot > 720

    labels = []
    if has_multi_roll:
        labels.append(f"multi-roll ({total_rot / 360:.0f}+ full rotations)")
    elif has_full_roll:
        labels.append("full roll (360 deg+)")
    elif has_tumble:
        labels.append("tumble/jump (90 deg+ rotation)")

    if peak_acc > 50:
        labels.append("high-impact crash")
    elif peak_acc > 20:
        labels.append("moderate crash")
    else:
        labels.append("smooth motion")

    if cumul_path > 100:
        labels.append("long-distance travel")
    elif cumul_path > 20:
        labels.append("short travel")

    max_z = positions[:, 2].max()
    min_z = positions[:, 2].min()
    if max_z - min_z > 2.0:
        labels.append("significant vertical motion (launch/drop)")
        if min_z < -1.0:
            labels.append("car went below ground level (tunnel?)")

    if flip_count > 3:
        labels.append("multiple direction reversals")

    motion_type = " / ".join(labels)
    print(f"  Type: {motion_type}")

    print(f"\n{'=' * 88}")
    print(f"SUMMARY: {net_disp:.1f}m travel over {dur:.1f}s, "
          f"{np.degrees(cumul_rotation):.0f} deg total rotation, "
          f"peak {max_speed*3.6:.0f} km/h")
    print(f"{'=' * 88}")


if __name__ == "__main__":
    main()
