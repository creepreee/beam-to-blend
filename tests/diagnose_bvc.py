from __future__ import annotations

"""BVC diagnostic tool — reads a .bvc file and dumps per-object stats,
validates cache-reader round-trip, and checks for common data issues.

Usage:
    python tests/diagnose_bvc.py <path/to/cache.bvc>
"""

import sys
import os

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import numpy as np
from runtime.cache_reader import CacheReader


def main():
    if len(sys.argv) < 2:
        print("Usage: python tests/diagnose_bvc.py <cache.bvc>")
        sys.exit(1)

    path = sys.argv[1]
    if not os.path.exists(path):
        print(f"ERROR: {path} not found")
        sys.exit(1)

    print("=" * 72)
    print(f"BVC Diagnostic: {os.path.abspath(path)}")
    print("=" * 72)

    reader = CacheReader(path)
    try:
        h = reader.header
        print(f"\nHeader:")
        print(f"  version:       {h['version']}")
        print(f"  frame_count:   {h['frame_count']}")
        print(f"  object_count:  {h['object_count']}")
        print(f"  stable_count:  {h.get('stable_count', '?')}")
        print(f"  stable_vertex_total: {h.get('stable_vertex_total', '?')}")

        stable = reader.stable_objects()
        dynamic = reader.dynamic_objects()
        n_frames = reader.frame_count

        print(f"\nObjects: {len(stable)} stable, {len(dynamic)} dynamic")

        print(f"\n{'=' * 72}")
        print(f"Stable Objects:")
        print(f"{'Name':42s} {'Verts':>6s} {'Faces':>6s} {'IdxOffs':>10s} {'VtxOffs':>10s} {'UV Offs':>10s}")
        print(f"{'-'*42} {'-'*6} {'-'*6} {'-'*10} {'-'*10} {'-'*10}")
        for o in stable:
            print(f"{o.name:42s} {o.vertex_count:6d} {o.face_count:6d} {o._base_index_offset:10d} {o._frame_vertex_offset:10d} {o._uv_offset:10d}")
            # Validate indices
            try:
                idx = reader.base_indices(o.name)
                if len(idx) != o.face_count:
                    print(f"  *** WARNING: index count mismatch: face_count={o.face_count} but read {len(idx)}")
                if idx.shape[1] != 3:
                    print(f"  *** WARNING: indices not Nx3: shape={idx.shape}")
                # Check for degenerate triangles
                pos = reader.frame_positions(o.name, 0)
                v0 = pos[idx[:, 0]]
                v1 = pos[idx[:, 1]]
                v2 = pos[idx[:, 2]]
                cross_sq = np.sum(np.cross(v1 - v0, v2 - v0) ** 2, axis=1)
                n_degen = int((cross_sq < 1e-10).sum())
                if n_degen > 0:
                    print(f"  *** WARNING: {n_degen}/{len(idx)} degenerate triangles (zero area)")
                # Check for duplicate-index triangles
                a, b, c = idx[:, 0], idx[:, 1], idx[:, 2]
                n_idx_degen = int(((a == b) | (b == c) | (a == c)).sum())
                if n_idx_degen > 0:
                    print(f"  *** WARNING: {n_idx_degen}/{len(idx)} index-degenerate triangles")
            except ValueError as e:
                print(f"  *** ERROR: {e}")
                continue
            # Validate positions
            for f in range(min(n_frames, 5)):
                pos_f = reader.frame_positions(o.name, f)
                if pos_f.shape[0] != o.vertex_count:
                    print(f"  *** ERROR frame {f}: expected {o.vertex_count} verts, got {pos_f.shape[0]}")
                if pos_f.shape[1] != 3:
                    print(f"  *** ERROR frame {f}: positions not Nx3: shape={pos_f.shape}")
                # Check for NaN
                if np.any(np.isnan(pos_f)):
                    print(f"  *** ERROR frame {f}: NaN positions")
                # Check bounds
                mn = pos_f.min(axis=0)
                mx = pos_f.max(axis=0)
                print(f"  frame {f}: bounds x=[{mn[0]:.4f}, {mx[0]:.4f}] y=[{mn[1]:.4f}, {mx[1]:.4f}] z=[{mn[2]:.4f}, {mx[2]:.4f}]")

        if dynamic:
            print(f"\n{'=' * 72}")
            print(f"Dynamic Objects:")
            for o in dynamic:
                print(f"\n{o.name:42s} ({o.vertex_count} base verts)")
                for f in range(min(n_frames, 5)):
                    pos_f, idx_f = reader.frame_dynamic_geometry(o.name, f)
                    print(f"  frame {f}: {pos_f.shape[0]} verts, {idx_f.shape[0]} faces")

        # Check frame directory integrity
        print(f"\n{'=' * 72}")
        print(f"Frame Directory Integrity:")
        dir_off = h["frame_directory_offset"]
        for f in range(n_frames):
            block_off = reader._frame_block_offset(f)
            next_block = reader._frame_block_offset(f + 1) if f + 1 < n_frames else os.path.getsize(path)
            n_stable_f32 = sum(o.vertex_count * 3 for o in stable)
            expected_size = n_stable_f32 * 4
            actual_size = next_block - block_off
            if actual_size != expected_size:
                print(f"  frame {f}: block at {block_off}, size={actual_size}, expected={expected_size}")
                if actual_size < expected_size:
                    print(f"    *** WARNING: block smaller than expected (truncated?)")

        # Check frame N vs frame 0 motion
        print(f"\n{'=' * 72}")
        print(f"Frame 0 vs last-frame motion check:")
        for o in stable:
            p0 = reader.frame_positions(o.name, 0)
            pN = reader.frame_positions(o.name, n_frames - 1)
            displ = np.sqrt(np.sum((pN - p0) ** 2, axis=1))
            max_d = displ.max()
            mean_d = displ.mean()
            if max_d > 0.01:
                print(f"  {o.name:42s}  max={max_d:.4f}  mean={mean_d:.4f}")
            else:
                print(f"  {o.name:42s}  ** NO MOTION **  max={max_d:.6f}")

        print(f"\n{'=' * 72}")
        print("Diagnostic complete.")
    finally:
        reader.close()


if __name__ == "__main__":
    main()
