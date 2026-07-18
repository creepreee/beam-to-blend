from __future__ import annotations

"""EXTREME-DEBUG: import BVC into Blender headlessly, then for EVERY frame
dump exhaustive position / rotation / local-axis diagnostics.

Usage:
    blender --background --python tests/blender_debug_transform.py -- <cache.bvc>

Outputs a frame-by-frame debug table to stderr.
"""

import os
import sys
import math

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import numpy as np
import bpy

from runtime.cache_reader import CacheReader
from runtime.mesh_update import CachePlayback, CHUNK_MAP_E180
from runtime import frame_handler


# =========================================================================
#  Math helpers
# =========================================================================

def quat_to_matrix(q: np.ndarray) -> np.ndarray:
    """(x,y,z,w) -> 3x3 rotation matrix"""
    x, y, z, w = float(q[0]), float(q[1]), float(q[2]), float(q[3])
    return np.array([
        [1 - 2*(y*y + z*z),     2*(x*y - w*z),     2*(x*z + w*y)],
        [    2*(x*y + w*z), 1 - 2*(x*x + z*z),     2*(y*z - w*x)],
        [    2*(x*z - w*y),     2*(y*z + w*x), 1 - 2*(x*x + y*y)],
    ], dtype=np.float64)


def matrix_to_euler(R: np.ndarray) -> tuple[float, float, float]:
    """3x3 matrix -> (roll, pitch, yaw) in degrees (ZYX convention)"""
    sy = np.sqrt(R[0,0]**2 + R[1,0]**2)
    singular = sy < 1e-12
    if not singular:
        x = math.atan2(R[2,1], R[2,2])
        y = math.atan2(-R[2,0], sy)
        z = math.atan2(R[1,0], R[0,0])
    else:
        x = math.atan2(-R[1,2], R[1,1])
        y = math.atan2(-R[2,0], sy)
        z = 0.0
    return math.degrees(x), math.degrees(y), math.degrees(z)


def quat_to_euler(q: np.ndarray) -> tuple[float, float, float]:
    return matrix_to_euler(quat_to_matrix(q))


def detect_reflection(R: np.ndarray) -> bool:
    """Returns True if R has determinant -1 (reflection)."""
    return np.linalg.det(R) < 0


def local_axes_world(R: np.ndarray) -> dict:
    """Given a rotation matrix (local->world), return where local X/Y/Z point."""
    # R's columns are the local axes in world space
    return {
        "right":   R[:, 0],  # local +X in world
        "forward": R[:, 1],  # local +Y in world
        "up":      R[:, 2],  # local +Z in world
    }


def format_vec(v: np.ndarray, fmt: str = "+.4f") -> str:
    return f"({v[0]:{fmt}}, {v[1]:{fmt}}, {v[2]:{fmt}})"


# =========================================================================
#  Main
# =========================================================================

def main():
    args = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    if not args:
        print("[DEBUG] Usage: blender --background --python tests/blender_debug_transform.py -- <cache.bvc>", file=sys.stderr)
        sys.exit(1)
    cache_path = args[0]
    if not os.path.exists(cache_path):
        print(f"[DEBUG] ERROR: {cache_path} not found", file=sys.stderr)
        sys.exit(1)

    print(f"[DEBUG] BVC: {os.path.abspath(cache_path)}", file=sys.stderr)
    print(f"[DEBUG] file size: {os.path.getsize(cache_path) / 1e9:.2f} GB", file=sys.stderr)

    # ---- 1. Open BVC reader ----
    reader = CacheReader(cache_path)
    h = reader.header
    n_frames = reader.frame_count
    tdo = h.get("transform_data_offset", 0)
    has_transform = tdo != 0

    print(f"[DEBUG] version={h['version']} frames={n_frames} objects={h['object_count']} stable={h.get('stable_count','?')}", file=sys.stderr)
    print(f"[DEBUG] transform_data_offset={tdo}", file=sys.stderr)

    # ---- 2. Pick the biggest stable object (body) ----
    stable = reader.stable_objects()
    if not stable:
        print("[DEBUG] ERROR: no stable objects!", file=sys.stderr)
        reader.close()
        sys.exit(1)

    body = max(stable, key=lambda o: o.vertex_count)
    body_name = body.name
    body_vc = body.vertex_count
    print(f"[DEBUG] body object: {body_name!r}  vc={body_vc}", file=sys.stderr)

    # Read frame 0 body vertices once to get baseline
    body_pos_f0 = reader.frame_positions(body_name, 0)
    body_centroid_f0 = body_pos_f0.mean(axis=0)
    print(f"[DEBUG] body frame0 centroid (local): {format_vec(body_centroid_f0)}", file=sys.stderr)

    # ---- 3. Import into Blender ----
    print(f"[DEBUG] Importing into Blender...", file=sys.stderr)
    bpy.ops.wm.read_factory_settings(use_empty=True)

    # Use chunk_map=None to get individual objects (no chunking, easier to debug)
    playback = CachePlayback(reader, log_path=None, chunk_map=None)
    playback.build_scene()
    frame_handler.attach(playback, frame_start=1)

    # Get the empty and body object
    empty = bpy.data.objects.get("Vehicle Transform")
    body_obj = bpy.data.objects.get(body_name)

    if empty is None:
        print("[DEBUG] ERROR: Vehicle Transform empty not found!", file=sys.stderr)
        reader.close()
        return
    if body_obj is None:
        print("[DEBUG] ERROR: body object not found!", file=sys.stderr)
        reader.close()
        return

    # ---- 4. Per-frame diagnostic dump ----
    # Table header
    sep = "=" * 220
    print(f"\n{sep}", file=sys.stderr)
    print(f"{'FRM':>4s} | {'BVC_pos':>30s} | {'BVC_q':>40s} | {'BVC_Euler(rpy)':>30s} | {'det':>5s} | {'BL_empty_pos':>30s} | {'BL_empty_q':>40s} | {'BL_Euler(rpy)':>30s} | {'BL_body_centroid':>30s} | {'BL_world_centroid':>30s}", file=sys.stderr)
    print(sep, file=sys.stderr)

    for fi in range(0, n_frames):

        # --- A: BVC transform data ---
        if has_transform:
            tf = reader.frame_transform(fi)
            p_bvc = tf[:3].copy()
            q_bvc = tf[3:7].copy()
            # Normalize
            qn = np.linalg.norm(q_bvc)
            if qn > 0:
                q_bvc = q_bvc / qn
            R_bvc = quat_to_matrix(q_bvc)
            euler_bvc = quat_to_euler(q_bvc)
            det_bvc = "REFL" if detect_reflection(R_bvc) else "OK"
            axes_bvc = local_axes_world(R_bvc)
        else:
            p_bvc = np.zeros(3)
            q_bvc = np.array([0.0, 0.0, 0.0, 1.0])
            R_bvc = np.eye(3)
            euler_bvc = (0.0, 0.0, 0.0)
            det_bvc = "N/A"
            axes_bvc = local_axes_world(R_bvc)

        # --- B: Blender scene ---
        # Set frame
        bpy.context.scene.frame_set(1 + fi)
        bpy.context.view_layer.update()

        # Empty world matrix
        M_empty = np.array(empty.matrix_world, dtype=np.float64)
        p_empty = M_empty[:3, 3].copy()
        R_empty = M_empty[:3, :3].copy()
        det_empty = "REFL" if detect_reflection(R_empty) else "OK"
        # Convert empty matrix to quaternion
        # From matrix to quaternion (xyzw)
        q_empty_bl = np.array(empty.rotation_quaternion, dtype=np.float64)  # (w,x,y,z) in Blender
        q_empty = np.array([q_empty_bl[1], q_empty_bl[2], q_empty_bl[3], q_empty_bl[0]])  # -> (x,y,z,w)
        euler_empty = quat_to_euler(q_empty)
        axes_empty = local_axes_world(R_empty)

        # Body mesh vertices from Blender
        body_verts = np.empty(len(body_obj.data.vertices) * 3, dtype=np.float32)
        body_obj.data.vertices.foreach_get("co", body_verts)
        body_verts = body_verts.reshape(-1, 3).astype(np.float64)

        # Body centroid in local (empty) space
        body_centroid_local = body_verts.mean(axis=0)

        # Body centroid in world space = empty transform * local centroid
        body_centroid_world = R_empty @ body_centroid_local + p_empty

        # Also compute AABB
        bb_local_min = body_verts.min(axis=0)
        bb_local_max = body_verts.max(axis=0)
        bb_center_local = (bb_local_min + bb_local_max) / 2.0
        bb_world_min = R_empty @ bb_local_min + p_empty
        bb_world_max = R_empty @ bb_local_max + p_empty
        bb_center_world = (bb_world_min + bb_world_max) / 2.0

        # ---- Print row ----
        print(
            f"{fi:4d} | "
            f"{format_vec(p_bvc):>30s} | "
            f"{format_vec(q_bvc, '+.4f'):>40s} | "
            f"({euler_bvc[0]:+8.2f}, {euler_bvc[1]:+8.2f}, {euler_bvc[2]:+8.2f}) | "
            f"{det_bvc:>5s} | "
            f"{format_vec(p_empty):>30s} | "
            f"{format_vec(q_empty, '+.4f'):>40s} | "
            f"({euler_empty[0]:+8.2f}, {euler_empty[1]:+8.2f}, {euler_empty[2]:+8.2f}) | "
            f"{format_vec(body_centroid_local):>30s} | "
            f"{format_vec(body_centroid_world):>30s}",
            file=sys.stderr,
        )

        # Every 50 frames, also dump local axes and AABB
        if fi % 50 == 0:
            print(
                f"  AXES_BVC:   right={format_vec(axes_bvc['right'])}  "
                f"forward={format_vec(axes_bvc['forward'])}  "
                f"up={format_vec(axes_bvc['up'])}",
                file=sys.stderr,
            )
            print(
                f"  AXES_BL:    right={format_vec(axes_empty['right'])}  "
                f"forward={format_vec(axes_empty['forward'])}  "
                f"up={format_vec(axes_empty['up'])}",
                file=sys.stderr,
            )
            print(
                f"  BB_local:   min={format_vec(bb_local_min)}  "
                f"max={format_vec(bb_local_max)}  "
                f"center={format_vec(bb_center_local)}",
                file=sys.stderr,
            )
            print(
                f"  BB_world:   min={format_vec(bb_world_min)}  "
                f"max={format_vec(bb_world_max)}  "
                f"center={format_vec(bb_center_world)}",
                file=sys.stderr,
            )
            print(
                f"  BODY_AABB_span_local:  "
                f"dx={bb_local_max[0]-bb_local_min[0]:.2f}  "
                f"dy={bb_local_max[1]-bb_local_min[1]:.2f}  "
                f"dz={bb_local_max[2]-bb_local_min[2]:.2f}",
                file=sys.stderr,
            )
            if has_transform:
                # Compare BVC empty position vs Blender empty position
                pos_diff = np.linalg.norm(p_bvc - p_empty)
                print(
                    f"  POS_DIFF:   |p_bvc - p_bl| = {pos_diff:.6f}",
                    file=sys.stderr,
                )
                # Compare BVC quaternion vs Blender quaternion
                # Need to handle sign ambiguity: q and -q represent same rotation
                q_diff_0 = np.linalg.norm(q_bvc - q_empty)
                q_diff_1 = np.linalg.norm(q_bvc + q_empty)
                q_diff = min(q_diff_0, q_diff_1)
                print(
                    f"  Q_DIFF(ambig):   min(|q_bvc - q_bl|, |q_bvc + q_bl|) = {q_diff:.6f}",
                    file=sys.stderr,
                )

        if fi == 0:
            print(
                f"  BODY frame0 mesh centroid from BVC reader (local): {format_vec(body_centroid_f0)}",
                file=sys.stderr,
            )
            print(
                f"  BODY frame0 mesh centroid from Blender (local): {format_vec(body_centroid_local)}",
                file=sys.stderr,
            )
            centroid_diff = np.linalg.norm(body_centroid_f0.astype(np.float64) - body_centroid_local)
            print(
                f"  CENTROID_DIFF between BVC reader and Blender: {centroid_diff:.6f}",
                file=sys.stderr,
            )

    print(sep, file=sys.stderr)
    final_frame = n_frames - 1

    # Final summary
    print(f"\n{'=' * 100}", file=sys.stderr)
    print(f"SUMMARY", file=sys.stderr)
    print(f"{'=' * 100}", file=sys.stderr)

    # Overall motion
    p_first = reader.frame_transform(0)[:3] if has_transform else np.zeros(3)
    p_last = reader.frame_transform(final_frame)[:3] if has_transform else np.zeros(3)
    total_displacement = np.linalg.norm(p_last - p_first)
    print(f"Total displacement (BVC): {total_displacement:.4f} m", file=sys.stderr)

    # Check if body has meaningful deformation (not rigid motion)
    body_first = reader.frame_positions(body_name, 0)
    body_last = reader.frame_positions(body_name, final_frame)
    max_vertex_drift = float(np.abs(body_last - body_first).max())
    print(f"Body max vertex drift (local): {max_vertex_drift:.4f} m", file=sys.stderr)

    # Also dump very first and last frame for orientation sanity
    if has_transform:
        # Frame 0
        tf0 = reader.frame_transform(0)
        q0 = tf0[3:7] / (np.linalg.norm(tf0[3:7]) + 1e-12)
        R0 = quat_to_matrix(q0)
        e0 = quat_to_euler(q0)
        print(f"\nFrame 0:", file=sys.stderr)
        print(f"  pos      = {format_vec(tf0[:3])}", file=sys.stderr)
        print(f"  quat     = ({q0[0]:+.6f}, {q0[1]:+.6f}, {q0[2]:+.6f}, {q0[3]:+.6f})", file=sys.stderr)
        print(f"  euler    = ({e0[0]:+.2f}, {e0[1]:+.2f}, {e0[2]:+.2f}) deg", file=sys.stderr)
        print(f"  matrix   =", file=sys.stderr)
        for row in R0:
            print(f"    [{row[0]:+8.4f}  {row[1]:+8.4f}  {row[2]:+8.4f}]", file=sys.stderr)
        print(f"  det(R)   = {np.linalg.det(R0):+.6f}", file=sys.stderr)
        print(f"  axes:", file=sys.stderr)
        axes0 = local_axes_world(R0)
        print(f"    right   = {format_vec(axes0['right'])}  (car's +X in world)", file=sys.stderr)
        print(f"    forward = {format_vec(axes0['forward'])}  (car's +Y in world)", file=sys.stderr)
        print(f"    up      = {format_vec(axes0['up'])}  (car's +Z in world)", file=sys.stderr)

        # Frame last
        tfn = reader.frame_transform(final_frame)
        qn = tfn[3:7] / (np.linalg.norm(tfn[3:7]) + 1e-12)
        Rn = quat_to_matrix(qn)
        en = quat_to_euler(qn)
        print(f"\nFrame {final_frame}:", file=sys.stderr)
        print(f"  pos      = {format_vec(tfn[:3])}", file=sys.stderr)
        print(f"  quat     = ({qn[0]:+.6f}, {qn[1]:+.6f}, {qn[2]:+.6f}, {qn[3]:+.6f})", file=sys.stderr)
        print(f"  euler    = ({en[0]:+.2f}, {en[1]:+.2f}, {en[2]:+.2f}) deg", file=sys.stderr)
        print(f"  matrix   =", file=sys.stderr)
        for row in Rn:
            print(f"    [{row[0]:+8.4f}  {row[1]:+8.4f}  {row[2]:+8.4f}]", file=sys.stderr)
        print(f"  det(R)   = {np.linalg.det(Rn):+.6f}", file=sys.stderr)
        print(f"  axes:", file=sys.stderr)
        axes_n = local_axes_world(Rn)
        print(f"    right   = {format_vec(axes_n['right'])}  (car's +X in world)", file=sys.stderr)
        print(f"    forward = {format_vec(axes_n['forward'])}  (car's +Y in world)", file=sys.stderr)
        print(f"    up      = {format_vec(axes_n['up'])}  (car's +Z in world)", file=sys.stderr)

    # Dump the chunk of BMC (capture.lua) side for comparison
    print(f"\n{'=' * 100}", file=sys.stderr)
    print(f"VERIFICATION: reading raw transform from BMC capture file", file=sys.stderr)
    print(f"{'=' * 100}", file=sys.stderr)

    bmc_path = os.path.join(os.path.dirname(cache_path) if os.path.dirname(cache_path) else ".", "capture.bmc")
    alt_bmc = r"C:\Users\ubaid_i2c\AppData\Local\BeamNG\BeamNG.drive\current\captures\mycrash\capture.bmc"

    # Try to read from BMC directly via capture_format
    sys.path.insert(0, os.path.join(_REPO, "importer"))
    from importer import capture_format as bmc_fmt

    bmc_actual = None
    for cand in [bmc_path, alt_bmc]:
        if os.path.exists(cand):
            bmc_actual = cand
            break

    if bmc_actual:
        print(f"Reading BMC: {bmc_actual}", file=sys.stderr)
        bmc_hdr = bmc_fmt.read_bmc_header(bmc_actual)
        print(f"  BMC version={bmc_hdr.version} vc={bmc_hdr.vertex_count} ic={bmc_hdr.index_count}", file=sys.stderr)
        print(f"  BMC flags=0x{bmc_hdr.flags:x} has_transform={bmc_hdr.has_transform}", file=sys.stderr)
        print(f"  BMC frame_size={bmc_hdr.frame_size}", file=sys.stderr)

        if bmc_hdr.has_transform:
            # Read raw transform from BMC and compare with BVC transform
            print(f"\n  {'FRM':>4s} | {'BMC_pos':>30s} | {'BMC_q':>40s} | {'BVC_pos':>30s} | {'BVC_q':>40s} | {'pos_diff':>10s} | {'q_diff':>10s}", file=sys.stderr)
            print(f"  {'-'*4}-+-{'-'*30}-+-{'-'*40}-+-{'-'*30}-+-{'-'*40}-+-{'-'*10}-+-{'-'*10}", file=sys.stderr)
            check_frames = list(range(0, n_frames, 10))  # every 10th frame
            if n_frames - 1 not in check_frames:
                check_frames.append(n_frames - 1)
            for fi in check_frames:
                bmc_tf = bmc_fmt.read_frame_transform(bmc_actual, bmc_hdr, fi)
                bmc_p = bmc_tf[:3]
                bmc_q = bmc_tf[3:7]
                qn_bmc = np.linalg.norm(bmc_q)
                if qn_bmc > 0:
                    bmc_q = bmc_q / qn_bmc

                if has_transform:
                    bvc_tf = reader.frame_transform(fi)
                    bvc_p = bvc_tf[:3]
                    bvc_q = bvc_tf[3:7]
                    qn_bvc = np.linalg.norm(bvc_q)
                    if qn_bvc > 0:
                        bvc_q = bvc_q / qn_bvc
                    pd = np.linalg.norm(bmc_p - bvc_p)
                    qd = min(np.linalg.norm(bmc_q - bvc_q), np.linalg.norm(bmc_q + bvc_q))
                    print(f"  {fi:4d} | {format_vec(bmc_p):>30s} | {format_vec(bmc_q, '+.4f'):>40s} | {format_vec(bvc_p):>30s} | {format_vec(bvc_q, '+.4f'):>40s} | {pd:10.6f} | {qd:10.6f}", file=sys.stderr)
                else:
                    print(f"  {fi:4d} | {format_vec(bmc_p):>30s} | {format_vec(bmc_q, '+.4f'):>40s} | {'(no BVC transform)':>30s} | {'':>40s} | {'':>10s} | {'':>10s}", file=sys.stderr)
    else:
        print(f"No BMC file found (searched {bmc_path}, {alt_bmc})", file=sys.stderr)

    # ---- 5. Cleanup ----
    frame_handler.detach()
    reader.close()
    print(f"\n[DEBUG] Done. {n_frames} frames dumped.", file=sys.stderr)


if __name__ == "__main__":
    main()
