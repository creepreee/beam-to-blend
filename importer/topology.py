from __future__ import annotations

import hashlib
from typing import Iterable, Optional, Tuple

import numpy as np


class TopologyHasher:
    @staticmethod
    def hash_connectivity(
        vertex_count: int,
        edges: Iterable[Tuple[int, int]],
        faces: Iterable[Tuple[int, ...]],
    ) -> str:
        h = hashlib.sha256()
        h.update(str(vertex_count).encode("utf-8"))
        for a, b in edges:
            h.update(f"{a},{b};".encode("utf-8"))
        for face in faces:
            h.update(("(" + ",".join(map(str, face)) + ")").encode("utf-8"))
        return h.hexdigest()

    @staticmethod
    def hash_indices(vertex_count: int, indices: Optional[np.ndarray]) -> str:
        """Fast connectivity hash for a mesh from its triangle index array.

        Hashes the vertex count plus the raw index buffer. Two frames of the
        same object share this hash iff they have the same vertex count and the
        same triangle connectivity in the same order — exactly the condition
        that makes a shared base mesh + per-frame position cache valid. For
        non-indexed geometry, only the vertex count contributes (implicit
        triangle soup, order-defined).
        """
        h = hashlib.sha256()
        h.update(np.int64(vertex_count).tobytes())
        if indices is not None:
            idx = np.ascontiguousarray(indices, dtype=np.int32)
            h.update(np.int64(idx.size).tobytes())
            h.update(idx.tobytes())
        else:
            h.update(b"non-indexed")
        return h.hexdigest()
