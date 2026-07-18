from __future__ import annotations

"""Verify the load-bearing claims in docs/ARCHITECTURE_v2.md against real data.

Runs WITHOUT Blender, WITHOUT the GPU backend — it reads the same shared vertex
pool from the GLB frames in ``testglt/`` that the future GPU-readback backend
will capture, so the conclusions transfer.

Claims under test (see ARCHITECTURE_v2.md):

  C1  Topology is stable: the shared index buffer is byte-identical every frame.
      (lines 107-119, 359)  -> if false, vertex identity is invalid, whole format breaks.

  C2  verticesCount is invariant across frames. (lines 112-114)

  C3  "No welding needed: the shared pool already defines correct vertex sharing
      at material seams." (lines 324-327, 336)  -> the risky simplification. Tested
      by finding vertices that are COINCIDENT at frame 0 and checking whether they
      stay coincident (zero relative drift) across all frames. Coincident vertices
      driven by the same physics node never drift; independent duplicates do, and
      dropping the weld step would let seams crack open (the 2026-07-15 bug).

This is a diagnostic, not a pytest case — it prints a verdict and exits non-zero
if any hard claim (C1/C2) fails.

SCOPE — READ THIS BEFORE ACTING ON C3:
  This tests the GLB *shared vertex pool* (one 533K pool indexed by ~88 objects),
  which is what ARCHITECTURE_v2's GPU-readback backend (bng_getGPUMesh /
  verticesGet) reads. C3's "weld not required" verdict applies ONLY to that
  shared-pool backend. It does NOT license removing weld from the CURRENT
  capture.lua path, which emits 238 DISJOINT per-material primitives whose seam
  vertices are independent — that path genuinely cracks without weld (the
  2026-07-15 bug). Different backend, different answer.
"""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from importer.gltf_reader import (  # noqa: E402
    _AccessorCache,
    _MODE_TRIANGLES,
    _read_chunks,
)

TESTGLT = Path(__file__).resolve().parent.parent / "testglt"


def raw_shared_pools(path: Path):
    """Return {pos_accessor_index: positions (N,3) f32} and the concatenated
    index buffer (per pool) for one GLB frame, WITHOUT per-object dedup.

    This is the raw shared pool exactly as the GPU backend would read it.
    """
    data = path.read_bytes()
    gltf, bin_chunk = _read_chunks(data)
    accessors = _AccessorCache(gltf, bin_chunk)

    pools: dict[int, np.ndarray] = {}
    indices_per_pool: dict[int, list[np.ndarray]] = {}

    for mesh in gltf.get("meshes", []):
        for prim in mesh["primitives"]:
            if prim.get("mode", _MODE_TRIANGLES) != _MODE_TRIANGLES:
                continue
            pos_index = prim["attributes"].get("POSITION")
            if pos_index is None:
                continue
            if pos_index not in pools:
                pools[pos_index] = accessors.get(pos_index).astype(np.float32, copy=False)
                indices_per_pool[pos_index] = []
            idx_accessor = prim.get("indices")
            if idx_accessor is not None:
                idx = accessors.get(idx_accessor).reshape(-1).astype(np.int64, copy=False)
                indices_per_pool[pos_index].append(idx)

    index_buffer = {
        p: (np.concatenate(v) if v else np.empty(0, dtype=np.int64))
        for p, v in indices_per_pool.items()
    }
    return pools, index_buffer


def main() -> int:
    frames = sorted(TESTGLT.glob("export_frame_*.glb"))
    if len(frames) < 2:
        print(f"FAIL: need >=2 frames in {TESTGLT}, found {len(frames)}")
        return 1

    print(f"Frames: {len(frames)}  ({frames[0].name} .. {frames[-1].name})")
    print("(NOTE: these are consecutive end-of-crash frames, so absolute motion")
    print(" between them is small — see the drift-magnitude caveat below.)\n")

    # --- Load frame 0 -----------------------------------------------------
    pools0, idx0 = raw_shared_pools(frames[0])
    main_pool = max(pools0, key=lambda p: pools0[p].shape[0])
    print(f"Distinct POSITION accessors: {len(pools0)}")
    print(f"Largest shared pool: accessor {main_pool}, "
          f"{pools0[main_pool].shape[0]} vertices\n")

    # ================= C1: index buffers byte-identical ==================
    c1_ok = True
    c2_ok = True
    for f in frames[1:]:
        pools_n, idx_n = raw_shared_pools(f)
        for p in idx0:
            if p not in idx_n or not np.array_equal(idx0[p], idx_n.get(p, np.empty(0))):
                c1_ok = False
                print(f"  C1 FAIL: index buffer for pool {p} changed at {f.name}")
        for p in pools0:
            if p not in pools_n or pools_n[p].shape[0] != pools0[p].shape[0]:
                c2_ok = False
                print(f"  C2 FAIL: vertex count for pool {p} changed at {f.name}")

    print(f"C1 (index buffers byte-identical across frames): {'PASS' if c1_ok else 'FAIL'}")
    print(f"C2 (vertex counts invariant across frames):      {'PASS' if c2_ok else 'FAIL'}\n")

    # ============ C3: do coincident vertices stay coincident? ============
    # Find pairs of DISTINCT vertex indices in the main pool that are coincident
    # at frame 0 (these are the seam duplicates the weld step would collapse).
    pos0 = pools0[main_pool]
    WELD_DECIMALS = 4  # same epsilon the builder uses (WELD_DECIMALS=4)
    keys = np.round(pos0, WELD_DECIMALS)
    # Group vertex ids by rounded position.
    order = np.lexsort((keys[:, 2], keys[:, 1], keys[:, 0]))
    ks = keys[order]
    same_as_prev = np.all(ks[1:] == ks[:-1], axis=1)
    # boundaries of coincident groups
    group_id = np.concatenate([[0], np.cumsum(~same_as_prev)])
    coincident_groups = 0
    representative_pairs: list[tuple[int, int]] = []
    start = 0
    for i in range(1, len(order) + 1):
        if i == len(order) or group_id[i] != group_id[start]:
            if i - start > 1:  # group of >1 vertex at the same rounded position
                coincident_groups += 1
                grp = order[start:i]
                # store one pair from this group for drift measurement
                representative_pairs.append((int(grp[0]), int(grp[1])))
            start = i

    n_coincident_verts = sum(1 for _ in representative_pairs)
    print(f"C3 setup: {coincident_groups} groups of coincident vertices at frame 0 "
          f"(eps=1e-{WELD_DECIMALS})")
    if coincident_groups == 0:
        print("C3 VERDICT: no coincident duplicates in the shared pool at all —")
        print("            weld would collapse nothing. 'No weld needed' is TRIVIALLY TRUE")
        print("            for this pool. (Consistent with a fully-shared seam pool.)\n")
        weld_needed = False
    else:
        # Measure relative drift of each coincident pair, keeping the WORST drift
        # seen for each pair across all frames (so we catch cracks that open only
        # at peak deformation).
        pair_a = np.array([a for a, _ in representative_pairs])
        pair_b = np.array([b for _, b in representative_pairs])
        worst_rel = np.zeros(len(pair_a), dtype=np.float64)
        max_abs_motion = 0.0
        for f in frames:
            pools_n, _ = raw_shared_pools(f)
            pn = pools_n[main_pool]
            rel = np.linalg.norm(pn[pair_a] - pn[pair_b], axis=1)
            worst_rel = np.maximum(worst_rel, rel)
            motion = np.linalg.norm(pn[pair_a] - pos0[pair_a], axis=1)
            max_abs_motion = max(max_abs_motion, float(motion.max()))

        print(f"C3 measure: {n_coincident_verts} coincident pairs tracked")
        print(f"  max ABSOLUTE motion of a tracked vertex (frame0->frameN): {max_abs_motion:.4f} m")
        print("  RELATIVE drift within coincident pairs (worst per pair, over all frames):")
        for pct in (50, 90, 99, 99.9, 100):
            print(f"    p{pct:<5}: {np.percentile(worst_rel, pct)*1000:8.3f} mm")
        # Count pairs whose drift exceeds visually-meaningful thresholds.
        for mm in (0.5, 1.0, 5.0):
            n = int((worst_rel > mm / 1000).sum())
            print(f"    pairs drifting > {mm:>4} mm: {n:>7}  ({100*n/len(worst_rel):.3f}%)")

        # Verdict thresholds:
        #   - VISIBLE crack risk if a non-trivial fraction of seams drift past ~1mm.
        #   - "same physics node" if essentially all pairs stay sub-0.5mm.
        p999 = float(np.percentile(worst_rel, 99.9))
        frac_visible = float((worst_rel > 1e-3).mean())  # >1mm
        weld_needed = frac_visible > 0.001  # >0.1% of seams visibly crack
        print()
        if weld_needed:
            print("C3 VERDICT: a non-trivial fraction of coincident vertices DRIFT PAST 1mm")
            print("            -> independent duplicates that crack seams. Weld REQUIRED for")
            print("            correctness. *** ARCHITECTURE_v2 'no weld needed' is UNSAFE. ***")
        elif p999 > 5e-4:
            print("C3 VERDICT: coincident vertices stay sub-mm but not bit-locked (p99.9 =")
            print(f"            {p999*1000:.3f} mm). Visually safe to drop weld, but it is NOT")
            print("            a pure size-optimization — there is real micro-drift. Keep weld")
            print("            OR document the sub-mm tolerance. 'No weld needed' is OPTIMISTIC.")
        else:
            print("C3 VERDICT: coincident vertices stay locked (<0.5mm at p99.9). Same physics")
            print("            point; weld is a size optimization only, correctness-safe to drop.")

        if max_abs_motion < 0.10:
            print("\n  CAVEAT: peak deformation in these frames is small; re-run on early crash")
            print("  frames (large deformation) to stress the seams harder before freezing.")

    print("\n" + "=" * 68)
    hard_fail = not (c1_ok and c2_ok)
    print(f"HARD CLAIMS (C1,C2): {'PASS' if not hard_fail else 'FAIL'}")
    print(f"WELD CLAIM (C3): weld {'REQUIRED' if weld_needed else 'not required for correctness'}")
    print("=" * 68)
    return 1 if hard_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
