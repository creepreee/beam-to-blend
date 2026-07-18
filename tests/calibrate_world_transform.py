from __future__ import annotations

"""Calibrate the vehicle world-rotation source & basis — NO guessing.

Consumes the ground-truth JSON from ``tools/groundtruth_dump.lua`` (v2, render-
mesh vertices) and determines which engine rotation candidate reproduces the
render mesh's true world orientation, to sub-degree accuracy.

Ground-truth principle (from BeamNG's veFlexbodyDebug.lua):
    debugVert(i, f) = R_world(f) * restVert(i)     (ref-relative, world-oriented)
So for the CORRECT world rotation R_world, the quantity
    R_world(f)^T * debugVert(i, f)
is INVARIANT across frames (it recovers the constant rest vertex). This is
translation-immune and needs no pool correspondence. We score each candidate by
how invariant it makes the back-rotated vertices.

We also report node/vertex-cloud RIGIDITY (pairwise distance drift) so a
non-rigid capture is caught immediately rather than silently skewing the solve.

Run:
    python tests/calibrate_world_transform.py path/to/gt_dump.json
"""

import json
import sys
from itertools import combinations

import numpy as np


def quat_to_matrix(q: np.ndarray) -> np.ndarray:
    x, y, z, w = [float(v) for v in q]
    n = (x * x + y * y + z * z + w * w) ** 0.5
    if n == 0:
        return np.eye(3)
    x, y, z, w = x / n, y / n, z / n, w / n
    return np.array([
        [1 - 2 * (y * y + z * z),     2 * (x * y - w * z),     2 * (x * z + w * y)],
        [    2 * (x * y + w * z), 1 - 2 * (x * x + z * z),     2 * (y * z - w * x)],
        [    2 * (x * z - w * y),     2 * (y * z + w * x), 1 - 2 * (x * x + y * y)],
    ], dtype=np.float64)


def mat_from_dir_up(dir_v: np.ndarray, up_v: np.ndarray) -> np.ndarray:
    """Columns = right, forward, up. Matches quatFromDir(dir, up)."""
    f = dir_v / (np.linalg.norm(dir_v) + 1e-12)
    u = up_v / (np.linalg.norm(up_v) + 1e-12)
    r = np.cross(f, u); r /= (np.linalg.norm(r) + 1e-12)
    u2 = np.cross(r, f)
    return np.column_stack([r, f, u2])


def rigidity_report(verts: np.ndarray) -> float:
    """verts: (n_frames, n_verts, 3). Returns max pairwise-distance drift (m)."""
    n_v = verts.shape[1]
    pairs = list(combinations(range(n_v), 2))
    if len(pairs) > 80:
        pairs = pairs[:80]
    drifts = []
    for a, b in pairs:
        d = np.linalg.norm(verts[:, a] - verts[:, b], axis=1)
        drifts.append(d.max() - d.min())
    drifts = np.array(drifts)
    return float(drifts.mean()), float(drifts.max())


def invariance_error(R_list: list[np.ndarray], verts: np.ndarray) -> float:
    """Mean per-vertex spread (m) of R(f)^T * debugVert(f) across frames.

    Translation-robust: the per-frame cloud centroid is removed before
    back-rotation, so a constant world translation in debugVert cannot inflate
    the score. 0 => this rotation exactly explains the mesh's orientation."""
    n_f, n_v, _ = verts.shape
    back = np.empty_like(verts)
    for f in range(n_f):
        c = verts[f].mean(axis=0)
        back[f] = (verts[f] - c) @ R_list[f]     # (R^T (x-c))^T = (x-c)^T R
    spread = np.linalg.norm(back.std(axis=0), axis=1)   # (n_v,)
    return float(spread.mean())


def kabsch(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Rotation R with b ~= a @ R.T (both centered)."""
    H = a.T @ b
    U, S, Vt = np.linalg.svd(H)
    d = np.sign(np.linalg.det(Vt.T @ U.T))
    return Vt.T @ np.diag([1.0, 1.0, d]) @ U.T


def true_rotation_fit(R_list: list[np.ndarray], verts: np.ndarray) -> float:
    """Best-case: does the candidate's per-frame DELTA match the vertex cloud's
    own rigid delta (Kabsch, frame f vs 0)? Basis-invariant angular error (deg).

    This ignores any fixed basis change between the quaternion and pool frames —
    it only asks whether the candidate rotates by the right AMOUNT about the
    right (possibly-rebased) axis. Low here + high invariance_error => the source
    is correct but a fixed basis B is still needed."""
    n_f = verts.shape[0]
    c0 = verts[0].mean(axis=0)
    v0 = verts[0] - c0
    R0 = R_list[0]
    errs = []
    for f in range(n_f):
        vf = verts[f] - verts[f].mean(axis=0)
        D_true = kabsch(v0, vf)                       # true cloud delta 0->f
        D_cand = R_list[f] @ R0.T                     # candidate delta 0->f
        Rrel = D_true @ D_cand.T
        c = (np.trace(Rrel) - 1) / 2
        errs.append(np.degrees(np.arccos(np.clip(c, -1, 1))))
    return float(np.mean(errs))


def main(path: str) -> int:
    with open(path) as f:
        data = json.load(f)
    frames = data["frames"]
    n = len(frames)

    if "debugVerts" not in frames[0]:
        print("ERROR: this is a v1 (node-based) dump. Re-run tools/groundtruth_dump.lua "
              "(v2, render-mesh vertices).")
        return 2

    verts = np.array([fr["debugVerts"] for fr in frames], dtype=np.float64)  # (n, v, 3)
    print(f"loaded {n} frames, {verts.shape[1]} render-mesh vertices "
          f"(flexmesh index {data.get('flexIndex')})")

    # --- rigidity gate ---
    mean_drift, max_drift = rigidity_report(verts)
    print(f"\nvertex-cloud rigidity: pairwise-distance drift mean {mean_drift:.3f} m, "
          f"max {max_drift:.3f} m")
    if max_drift > 0.3:
        print("  [!] cloud is NOT rigid enough (>0.3 m). Pick body-panel vertices away "
              "from crumple zones, or a different flexmesh. Results below are suspect.")
    else:
        print("  [ok] rigid enough to trust the rotation solve.")

    # --- candidate rotations per frame ---
    cands = {
        "getRotation": [quat_to_matrix(fr["rot"]) for fr in frames],
        "clusterRot": [quat_to_matrix(fr["clusterRot"]) for fr in frames],
        "quatFromDir(dir,up)": [
            mat_from_dir_up(np.array(fr["dir"]), np.array(fr["up"])) for fr in frames],
        "quatFromDir(-dir,up)": [
            mat_from_dir_up(-np.array(fr["dir"]), np.array(fr["up"])) for fr in frames],
    }

    print(f"\n{'candidate':24s} {'invariance err (m)':>20s}   verdict")
    best = None
    for name, R_list in cands.items():
        e = invariance_error(R_list, verts)
        verdict = "MATCH" if e < 0.02 else ("close" if e < 0.1 else "wrong")
        print(f"{name:24s} {e:19.4f}   {verdict}")
        if best is None or e < best[1]:
            best = (name, e)

    print(f"\n==> Correct world-rotation source: {best[0]}  (invariance err {best[1]:.4f} m)")
    print("    This is the rotation to apply (in Blender space) to place the mesh in the world.")
    print("    Pool->Blender axis swap is applied separately in the builder; if the tumble axis")
    print("    is still wrong after using this source, the fixed basis change is between the")
    print("    quaternion frame and the pool frame — solve B next with vertex correspondence.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "tools/gt_dump.json"))
