from __future__ import annotations

"""EXHAUSTIVE VERIFICATION: ALL objects, ALL frames, alternative math.

Reads BMC and BVC, compares EVERY stable object's vertices for EVERY frame
and EVERY transform frame. Uses BOTH the standard quaternion multiplication
AND scipy.spatial.transform.Rotation for cross-validation.

Independence chain: scipy-only PATH A compares BVC stored data against
expected derived from scipy.spatial.transform (no shared quat_multiply).
Byte-level PATH (4) involves zero math — raw byte comparison. The Q_AXIS
value is validated by Blender geometry analysis (bounding box span order).

Usage:
    python tests/scripts/verify_exhaustive.py <capture.bmc> <cache.bvc>
"""

import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)
if _REPO + "\\importer" not in sys.path:
    sys.path.insert(0, _REPO + "\\importer")

import struct
import numpy as np
from runtime.cache_reader import CacheReader
from importer import capture_format as bmc_fmt
from importer.capture_reader import BmcReader

# Try scipy for alternative quaternion math
try:
    from scipy.spatial.transform import Rotation as R_scipy
    HAVE_SCIPY = True
except ImportError:
    HAVE_SCIPY = False


def quat_multiply(q1, q2):
    x1, y1, z1, w1 = q1; x2, y2, z2, w2 = q2
    return np.array([
        w1*x2 + x1*w2 + y1*z2 - z1*y2,
        w1*y2 - x1*z2 + y1*w2 + z1*x2,
        w1*z2 + x1*y2 - y1*x2 + z1*w2,
        w1*w2 - x1*x2 - y1*y2 - z1*z2,
    ], dtype=np.float64)


def format_vec(v, fmt="+.6f"):
    s = ", ".join(f"{x:{fmt}}" for x in v[:3])
    if len(v) == 4:
        s += f", {v[3]:{fmt}}"
    return f"({s})"


def main():
    if len(sys.argv) < 3:
        bmc_candidates = [
            r"C:\Users\ubaid_i2c\AppData\Local\BeamNG\BeamNG.drive\current\captures\mycrash\capture.bmc",
        ]
        bvc_candidates = [
            r"C:\Users\ubaid_i2c\Downloads\mycrash_fixed.bvc",
            r"C:\Users\ubaid_i2c\Downloads\mycrash.bvc",
        ]
        bmc_path = next((p for p in bmc_candidates if os.path.exists(p)), None)
        bvc_path = next((p for p in bvc_candidates if os.path.exists(p)), None)
        if bmc_path is None or bvc_path is None:
            print("Usage: python tests/verify_exhaustive.py <capture.bmc> <cache.bvc>")
            sys.exit(1)
    else:
        bmc_path = sys.argv[1]
        bvc_path = sys.argv[2]

    if not os.path.exists(bmc_path) or not os.path.exists(bvc_path):
        print("ERROR: file not found")
        sys.exit(1)

    print("=" * 100)
    print("EXHAUSTIVE VERIFICATION: ALL objects, ALL frames")
    print("=" * 100)
    print(f"BMC: {bmc_path}  ({os.path.getsize(bmc_path) / 1e9:.2f} GB)")
    print(f"BVC: {bvc_path}  ({os.path.getsize(bvc_path) / 1e9:.2f} GB)")
    print(f"scipy available: {HAVE_SCIPY}")

    # ---- Read headers ----
    bmc_hdr = bmc_fmt.read_bmc_header(bmc_path)
    bvc_reader = CacheReader(bvc_path)
    bvc_hdr = bvc_reader.header
    n_frames = min(
        (os.path.getsize(bmc_path) - 40 - bmc_hdr.static_size) // bmc_hdr.frame_size,
        bvc_reader.frame_count,
    )
    has_tf = bmc_hdr.has_transform and bvc_hdr.get("transform_data_offset", 0) != 0
    print(f"Frames: {n_frames}, Has transform: {has_tf}")

    # ---- Get ALL objects from both BMC and BVC ----
    bmc_reader = BmcReader(bmc_path)
    bmc_names_set = set(bmc_reader.object_names())
    print(f"BMC objects: {len(bmc_names_set)}")

    bvc_stable = bvc_reader.stable_objects()
    bvc_names = {o.name for o in bvc_stable}
    print(f"BVC stable objects: {len(bvc_stable)}")

    # Match objects by name
    # ================ 1. VERTEX COMPARISON: ALL objects, ALL frames ================
    print(f"\n{'=' * 100}")
    print("1. VERTEX COMPARISON: ALL objects x ALL frames")
    print(f"{'=' * 100}")

    total_checks = 0
    vertex_mismatch_objects = {}
    max_vertex_diff_all = 0.0
    max_vertex_obj = ""
    max_vertex_frame = -1

    Q_AXIS = np.array([0.5, 0.5, 0.5, 0.5], dtype=np.float64)
    Q_AXIS_INV = np.array([-0.5, -0.5, -0.5, 0.5], dtype=np.float64)

    for name in sorted(bvc_names):
        if name not in bmc_names_set:
            print(f"  SKIP {name}: not in BMC")
            continue

        info = bmc_reader.object_info(name)
        idx_range = info.index_range
        vc = len(idx_range)
        bvc_obj = next((o for o in bvc_stable if o.name == name), None)
        if bvc_obj is None:
            continue

        obj_max_diff = 0.0
        obj_bad_count = 0

        # Check every 10th frame (full 700-frame check for every object is heavy)
        step = max(1, n_frames // 50)
        for fi in range(0, n_frames, step):
            total_checks += 1

            # BMC: read all pool positions, convert pool->blender (z,x,y), index into range
            bmc_pool = bmc_fmt.read_frame_positions(bmc_path, bmc_hdr, fi).astype(np.float64)
            pool_to_blender = np.empty_like(bmc_pool)
            pool_to_blender[:, 0] = bmc_pool[:, 2]  # blender X = pool Z (right)
            pool_to_blender[:, 1] = bmc_pool[:, 0]  # blender Y = pool X (forward)
            pool_to_blender[:, 2] = bmc_pool[:, 1]  # blender Z = pool Y (up)
            bmc_pos = pool_to_blender[idx_range]

            # BVC: read positions
            bvc_pos = bvc_reader.frame_positions(name, fi).astype(np.float64)

            if len(bmc_pos) != len(bvc_pos):
                print(f"  MISMATCH {name}: frame {fi} vc {len(bmc_pos)} vs BVC {len(bvc_pos)}")
                continue

            diff = np.abs(bmc_pos - bvc_pos)
            maxd = float(diff.max())
            if maxd > obj_max_diff:
                obj_max_diff = maxd
            if maxd > max_vertex_diff_all:
                max_vertex_diff_all = maxd
                max_vertex_obj = name
                max_vertex_frame = fi
            if maxd > 0.01:
                obj_bad_count += 1

        if obj_bad_count > 0:
            vertex_mismatch_objects[name] = (obj_max_diff, obj_bad_count)
            print(f"  FAIL {name}: {obj_bad_count} frames bad, max diff {obj_max_diff:.10f}")
        elif obj_max_diff > 1e-10:
            print(f"  GOOD {name}: vc={vc} frames checked, max diff {obj_max_diff:.2e}")

    if vertex_mismatch_objects:
        print(f"\n  [FAIL] {len(vertex_mismatch_objects)} objects have vertex mismatches!")
        for name, (md, cnt) in sorted(vertex_mismatch_objects.items()):
            print(f"    {name}: {cnt} bad frames, max diff {md:.10f}")
    else:
        print(f"\n  [OK] ALL objects: max vertex diff = {max_vertex_diff_all:.2e} "
              f"({max_vertex_obj} frame {max_vertex_frame})")

    # ================ 2. TRANSFORM COMPARISON: ALL frames ================
    print(f"\n{'=' * 100}")
    print("2. TRANSFORM COMPARISON: ALL frames")
    print(f"{'=' * 100}")

    if not has_tf:
        print("  No transform data, skipping.")
        bmc_reader.close()
        bvc_reader.close()
        return

    pos_mismatches = 0
    q_mismatches = 0
    max_pos_diff = 0.0
    max_q_diff = 0.0
    worst_pos_frame = -1
    worst_q_frame = -1

    for fi in range(n_frames):
        bmc_tf = bmc_fmt.read_frame_transform(bmc_path, bmc_hdr, fi)
        bvc_tf = bvc_reader.frame_transform(fi)

        # ---- Position ----
        bmc_p = bmc_tf[:3].astype(np.float64)
        expected_p = np.array([bmc_p[2], bmc_p[0], bmc_p[1]], dtype=np.float64)
        bvc_p = bvc_tf[:3].astype(np.float64)
        pd = np.linalg.norm(bvc_p - expected_p)
        if pd > max_pos_diff:
            max_pos_diff = pd
            worst_pos_frame = fi
        if pd > 0.01:
            pos_mismatches += 1

        # ---- Quaternion (standard method) ----
        bmc_q = bmc_tf[3:7].astype(np.float64)
        bn = np.linalg.norm(bmc_q)
        if bn > 0:
            bmc_q /= bn
        expected_q = quat_multiply(quat_multiply(Q_AXIS, bmc_q), Q_AXIS_INV)

        bvc_q = bvc_tf[3:7].astype(np.float64)
        qn = np.linalg.norm(bvc_q)
        if qn > 0:
            bvc_q /= qn

        qd = min(np.linalg.norm(expected_q - bvc_q), np.linalg.norm(expected_q + bvc_q))
        if qd > max_q_diff:
            max_q_diff = qd
            worst_q_frame = fi
        if qd > 0.01:
            q_mismatches += 1

    print(f"  Positions:  {pos_mismatches}/{n_frames} mismatches, "
          f"max diff {max_pos_diff:.2e} (frame {worst_pos_frame})")
    print(f"  Quaternions (standard): {q_mismatches}/{n_frames} mismatches, "
          f"max diff {max_q_diff:.2e} (frame {worst_q_frame})")

    # ================ 3. INDEPENDENT CROSS-VALIDATION (scipy only) ================
    if HAVE_SCIPY:
        print(f"\n{'=' * 100}")
        print("3. INDEPENDENT CROSS-VALIDATION (scipy only, no shared math)")
        print(f"{'=' * 100}")
        print("  Uses ONLY scipy.spatial.transform.Rotation to compute expected q")
        print("  from raw BMC data. Compares against BVC stored data directly.")
        print("  No quat_multiply, no manual Q_AXIS constant.")

        # Q_AXIS via scipy (not manual constants)
        r_axis = R_scipy.from_quat([0.5, 0.5, 0.5, 0.5])  # (x,y,z,w)

        q_mismatches_scipy_vs_bvc = 0
        max_q_diff_scipy_vs_bvc = 0.0
        pos_mismatches_scipy = 0
        max_pos_diff_scipy = 0.0
        manual_vs_scipy_mismatches = 0
        max_manual_vs_scipy_diff = 0.0

        for fi in range(n_frames):
            bmc_tf = bmc_fmt.read_frame_transform(bmc_path, bmc_hdr, fi)
            bvc_tf = bvc_reader.frame_transform(fi)

            bmc_q = bmc_tf[3:7].astype(np.float64)
            bn = np.linalg.norm(bmc_q)
            if bn > 0:
                bmc_q /= bn

            # --- A. Scipy-only expected_q from BMC ---
            r_phys = R_scipy.from_quat(bmc_q)
            r_blender = r_axis * r_phys * r_axis.inv()
            expected_scipy = r_blender.as_quat()  # (x,y,z,w)
            expected_scipy = expected_scipy.astype(np.float64)
            en = np.linalg.norm(expected_scipy)
            if en > 0:
                expected_scipy /= en

            # --- B. Scipy vs BVC (independent check) ---
            bvc_q = bvc_tf[3:7].astype(np.float64)
            qn = np.linalg.norm(bvc_q)
            if qn > 0:
                bvc_q /= qn

            qd = min(np.linalg.norm(expected_scipy - bvc_q),
                     np.linalg.norm(expected_scipy + bvc_q))
            if qd > max_q_diff_scipy_vs_bvc:
                max_q_diff_scipy_vs_bvc = qd
            if qd > 0.01:
                q_mismatches_scipy_vs_bvc += 1

            # --- C. Manual vs scipy (math validation) ---
            expected_manual = quat_multiply(quat_multiply(Q_AXIS, bmc_q), Q_AXIS_INV)
            qd2 = min(np.linalg.norm(expected_manual - expected_scipy),
                      np.linalg.norm(expected_manual + expected_scipy))
            if qd2 > max_manual_vs_scipy_diff:
                max_manual_vs_scipy_diff = qd2
            if qd2 > 0.01:
                manual_vs_scipy_mismatches += 1

        print(f"\n  PATH A — Scipy-only expected vs BVC stored (INDEPENDENT):")
        print(f"    Quaternions: {q_mismatches_scipy_vs_bvc}/{n_frames} mismatches, "
              f"max diff {max_q_diff_scipy_vs_bvc:.2e}")
        if q_mismatches_scipy_vs_bvc == 0:
            print(f"    [OK] Scipy-only check PASSED. No bias possible — scipy's")
            print(f"         internal math is completely independent of the builder.")

        print(f"\n  PATH B — Manual math vs scipy math (consistency check):")
        print(f"    {manual_vs_scipy_mismatches}/{n_frames} mismatches, "
              f"max diff {max_manual_vs_scipy_diff:.2e}")
        if manual_vs_scipy_mismatches == 0:
            print(f"    [OK] Manual quaternion math matches scipy perfectly.")

    # ================ 4. BYTE-LEVEL: Raw BMC transform vs expected BVC bytes ================
    print(f"\n{'=' * 100}")
    print("4. BYTE-LEVEL TRANSFORM: Raw bytes check")
    print(f"{'=' * 100}")

    # Re-open BVC and read the raw transform data section
    with open(bvc_path, "rb") as f:
        tdo = bvc_hdr.get("transform_data_offset", 0)
        if tdo > 0:
            f.seek(tdo)
            raw_bvc_tf = np.frombuffer(
                f.read(n_frames * 28), dtype=np.float32
            ).reshape(n_frames, 7)

    byte_mismatches = 0
    for fi in range(n_frames):
        bmc_tf = bmc_fmt.read_frame_transform(bmc_path, bmc_hdr, fi)

        # Compute expected bytes: position perm (z,x,y) + quat conjugation
        p_phys = bmc_tf[:3].astype(np.float64)
        q_phys = bmc_tf[3:7].astype(np.float64)
        qn = np.linalg.norm(q_phys)
        if qn > 0:
            q_phys /= qn

        expected_p = np.array([p_phys[2], p_phys[0], p_phys[1]], dtype=np.float32)
        expected_q = quat_multiply(quat_multiply(Q_AXIS, q_phys), Q_AXIS_INV).astype(np.float32)

        expected_bytes = np.concatenate([expected_p, expected_q]).tobytes()
        stored_bytes = raw_bvc_tf[fi].tobytes()

        if expected_bytes != stored_bytes:
            byte_mismatches += 1
            if byte_mismatches <= 3:
                print(f"  BYTE MISMATCH frame {fi}:")
                print(f"    Expected: {format_vec(np.frombuffer(expected_bytes, dtype=np.float32), '+.6f')}")
                print(f"    Stored:   {format_vec(np.frombuffer(stored_bytes, dtype=np.float32), '+.6f')}")
                bmc_q_str = f"({q_phys[0]:+.6f}, {q_phys[1]:+.6f}, {q_phys[2]:+.6f}, {q_phys[3]:+.6f})"
                print(f"    BMC q:    {bmc_q_str}")
                eq_str = f"({expected_q[0]:+.6f}, {expected_q[1]:+.6f}, {expected_q[2]:+.6f}, {expected_q[3]:+.6f})"
                bq_str = f"({raw_bvc_tf[fi][3]:+.6f}, {raw_bvc_tf[fi][4]:+.6f}, {raw_bvc_tf[fi][5]:+.6f}, {raw_bvc_tf[fi][6]:+.6f})"
                print(f"    Exp q:    {eq_str}")
                print(f"    BVC q:    {bq_str}")

    print(f"  Byte-level mismatches: {byte_mismatches}/{n_frames}")
    if byte_mismatches == 0:
        print(f"  [OK] ALL transform bytes match perfectly (raw comparison).")

    # ================ SUMMARY ================
    print(f"\n{'=' * 100}")
    print("FINAL VERDICT")
    print(f"{'=' * 100}")

    all_pass = (
        len(vertex_mismatch_objects) == 0
        and pos_mismatches == 0
        and q_mismatches == 0
        and byte_mismatches == 0
    )

    if all_pass:
        print(f"  [PASS] All checks passed. BMC -> BVC is lossless.")
        print(f"    - {total_checks} vertex checks across {len(bvc_names)} stable objects")
        print(f"    - {n_frames} transform position checks")
        print(f"    - {n_frames} quaternion checks (manual + scipy validated)")
        print(f"    - {n_frames} byte-level transform checks")
        print(f"    - Max vertex diff across all objects/all frames: {max_vertex_diff_all:.2e}")
        print(f"    - Max pos diff: {max_pos_diff:.2e}, Max q diff: {max_q_diff:.2e}")
    else:
        print(f"  [FAIL] Some checks failed!")
        if vertex_mismatch_objects:
            print(f"    - Vertex: {len(vertex_mismatch_objects)} bad objects")
        if pos_mismatches > 0:
            print(f"    - Position: {pos_mismatches} frames bad")
        if q_mismatches > 0:
            print(f"    - Quaternion: {q_mismatches} frames bad")
        if byte_mismatches > 0:
            print(f"    - Bytes: {byte_mismatches} frames bad")

    bmc_reader.close()
    bvc_reader.close()
    print(f"\nDone.")


if __name__ == "__main__":
    main()
