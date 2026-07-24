from __future__ import annotations
"""Unit tests for the cross-frame-safe weld (the windshield fix)."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
from importer.cache_builder import _weld_keep_remap_multiframe


def test_coincident_everywhere_merges():
    # verts 0 and 1 are coincident in BOTH frames -> should merge.
    f0 = np.array([[0.0, 0, 0], [0.0, 0, 0], [5.0, 0, 0]])
    f1 = np.array([[1.0, 1, 1], [1.0, 1, 1], [9.0, 0, 0]])
    keep, remap = _weld_keep_remap_multiframe([f0, f1], epsilon=1e-4)
    assert len(keep) == 2, f"expected 2 kept, got {len(keep)}"
    assert remap[0] == remap[1], "coincident verts must share a welded id"
    assert remap[2] != remap[0]


def test_separating_verts_kept_distinct():
    # THE WINDSHIELD CASE: verts 0 and 1 coincide at rest (frame 0) but SEPARATE
    # in the crash (frame 1).  They must NOT be merged.
    f0 = np.array([[0.0, 0, 0], [0.0, 0, 0], [5.0, 0, 0]])
    f1 = np.array([[0.0, 0, 0], [2.0, 0, 0], [5.0, 0, 0]])  # vert 1 moved away
    keep, remap = _weld_keep_remap_multiframe([f0, f1], epsilon=1e-4)
    assert len(keep) == 3, f"separating verts must stay distinct, got {len(keep)}"
    assert remap[0] != remap[1], "windshield/body verts that separate must NOT merge"


def test_remap_is_dense_and_valid():
    f0 = np.random.RandomState(0).rand(50, 3)
    # duplicate 10 rows exactly across a second frame too
    dup = f0.copy()
    f0[10:20] = f0[0:10]
    dup[10:20] = dup[0:10]
    keep, remap = _weld_keep_remap_multiframe([f0, dup], epsilon=1e-5)
    assert remap.min() == 0
    assert remap.max() == len(keep) - 1
    assert set(remap.tolist()) == set(range(len(keep)))


if __name__ == "__main__":
    test_coincident_everywhere_merges()
    test_separating_verts_kept_distinct()
    test_remap_is_dense_and_valid()
    print("all weld tests passed")
