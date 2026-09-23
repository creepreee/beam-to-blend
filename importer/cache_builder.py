from __future__ import annotations

"""Builds a BVC binary vertex cache from a BeamNG Architecture-v3 capture.

The capture (``.bmc``) contains, per frame:
  * the deforming mesh in GPU-pool local space (Y-up, left-handed), and
  * the vehicle's live rigid transform (position + rotation) in physics
    space (Z-up, right-handed) read from the VLUA vehicle VM.

This builder does the minimum:
  * converts each frame's pool vertices to Blender space via a single
    axis permutation (``_pool_to_blender``),
  * passes the rigid transform through untouched (quaternion component
    order is handled at import time by ``runtime/mesh_update``).

No welding, no reconstruction, no calibration, no quaternion guessing.
"""

import struct
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

from . import binary
from .topology import TopologyHasher
from .capture_reader import BmcReader


class CacheBuildError(Exception):
    pass


class CaptureBuildError(CacheBuildError):
    """Raised when building from a capture file fails."""


def _pool_to_blender(pos: np.ndarray) -> np.ndarray:
    """GPU pool space (Y-up, left-handed) -> Blender space (Z-up), EXACTLY as
    the stock glTF Sequence Exporter maps it.

    THE DRAG FIX (2026-07-24, proven on test_drag.bmc)
    --------------------------------------------------
    ``verticesGet()`` returns each vertex **world-oriented** (the full rigid
    rotation is baked in) but expressed **origin-relative in the pool frame**,
    i.e. relative to the vehicle's live ``getPosition()``.  The rigid motion is
    re-applied as a single verbatim ``getPosition()`` translation (physics Z-up
    == Blender Z-up), mirroring the exporter's root-node translation.

    For a DETACHED part that comes to rest in the world, its pool coordinates
    must *recede* by exactly the vehicle's motion so that
    ``map(pool) + getPosition`` stays constant (the part does not drag).  That
    cancellation only happens when ``map`` sends the pool frame into the SAME
    axis frame the translation lives in.

    The exporter achieves this in two steps that compose to a single map:
      1. dump ``verticesGet()`` verbatim into glTF (Y-up),
      2. Blender's glTF importer converts glTF Y-up -> Blender Z-up as
         ``(x, y, z) -> (x, -z, y)``.
    Net:  ``blender = (pool_x, -pool_z, pool_y)``.

    The OLD map ``(pool_z, pool_x, pool_y)`` yawed the mesh 90 deg relative to
    the translation frame (measured: body nose 88 deg off ``getDirectionVector``
    instead of ~0 deg), so a detached part's recession pointed perpendicular to
    ``getPosition`` and never cancelled -> the part was dragged along with the
    car.  The exporter map puts the nose 0.7 deg on heading and cancels the
    recession, so rested parts stay put.

    Pool axes:  X = length (forward), Y = up, Z = width (right)
    Blender:    X = right(-ish),      Y = forward-ish, Z = up
    (The exact BeamNG world-axis labels don't matter; what matters is that this
    is the SAME frame ``getPosition()`` is reported in, so translation cancels.)
    """
    out = np.empty_like(pos)
    out[:, 0] = pos[:, 0]
    out[:, 1] = -pos[:, 2]
    out[:, 2] = pos[:, 1]
    return out


def _basis_from_dirs(fwd: np.ndarray, up: np.ndarray) -> np.ndarray:
    """World-space orientation matrix (columns = right, fwd, up) from the
    captured forward/up direction vectors.

    The .bmc stores ``getDirectionVector()`` (fwd) and ``getDirectionVectorUp()``
    (up) every frame, already in Blender Z-up world space (physics Z-up ==
    Blender Z-up, no permutation — see CLAUDE.md).  We build a right-handed
    orthonormal basis ``right = fwd x up``, re-orthogonalizing ``up`` so tiny
    non-perpendicularity in the captured vectors can't shear the result.
    Returns identity for degenerate input.
    """
    f = np.asarray(fwd, dtype=np.float64).reshape(3)
    u = np.asarray(up, dtype=np.float64).reshape(3)
    fn = np.linalg.norm(f)
    un = np.linalg.norm(u)
    if fn < 1e-9 or un < 1e-9:
        return np.eye(3, dtype=np.float64)
    f = f / fn
    u = u / un
    r = np.cross(f, u)
    rn = np.linalg.norm(r)
    if rn < 1e-9:
        return np.eye(3, dtype=np.float64)
    r = r / rn
    u = np.cross(r, f)  # re-orthogonalize up against right x fwd
    return np.column_stack([r, f, u])


def _quat_to_matrix(q: np.ndarray) -> np.ndarray:
    """Unit quaternion (xyzw) -> 3x3 rotation matrix (float64)."""
    x, y, z, w = (float(q[0]), float(q[1]), float(q[2]), float(q[3]))
    n = x * x + y * y + z * z + w * w
    if n < 1e-12:
        return np.eye(3, dtype=np.float64)
    s = 2.0 / n
    return np.array([
        [1 - s * (y * y + z * z), s * (x * y - z * w),     s * (x * z + y * w)],
        [s * (x * y + z * w),     1 - s * (x * x + z * z), s * (y * z - x * w)],
        [s * (x * z - y * w),     s * (y * z + x * w),     1 - s * (x * x + y * y)],
    ], dtype=np.float64)


# glTF Y-up -> Blender Z-up permutation (x, y, z) -> (x, -z, y), the exact
# transform Blender's native glTF importer applies.  Used to bake props into the
# same Blender space the flexmesh pool lands in.
_GLTF_TO_BLENDER3 = np.array([
    [1, 0, 0],
    [0, 0, -1],
    [0, 1, 0],
], dtype=np.float64)


def _prop_to_blender_frozen(
    local_verts: np.ndarray,
    position: np.ndarray,
    rotation: np.ndarray,
) -> np.ndarray:
    """Bake one rigid prop's local verts into vehicle-origin-relative Blender space.

    Mirrors the stock exporter's prop placement (util/export.lua:411-412) exactly,
    then applies the same glTF Y-up -> Blender Z-up conversion Blender's importer
    uses, so props land in the SAME frame as the flexmesh pool (``_pool_to_blender``)
    and can parent to the ``__root`` motion empty that carries ``getPosition()``.

    Exporter placement (glTF space):
        translation = (px, pz, -py)
        rotation    = (-rx, -rz,  ry, rw)      # quaternion xyzw
    So in glTF space:  v_gltf = R(q_gltf) @ v_local + t_gltf
    Then Blender:      v_bl   = (x, -z, y) applied to v_gltf.

    NOTE (unverified until a live capture): this assumes ``prop.position`` is
    vehicle-origin-relative and ``verticesGet`` returns prop-local coords in the
    same Y-up frame the exporter consumes.  Raw data is preserved in the .bmc, so
    if props land wrong this bake can be re-tuned and the BVC rebuilt WITHOUT a
    fresh capture.
    """
    v = np.ascontiguousarray(local_verts, dtype=np.float64).reshape(-1, 3)
    px, py, pz = float(position[0]), float(position[1]), float(position[2])
    rx, ry, rz, rw = (float(rotation[0]), float(rotation[1]),
                      float(rotation[2]), float(rotation[3]))

    q_gltf = np.array([-rx, -rz, ry, rw], dtype=np.float64)
    t_gltf = np.array([px, pz, -py], dtype=np.float64)
    R = _quat_to_matrix(q_gltf)

    v_gltf = (R @ v.T).T + t_gltf
    v_bl = (_GLTF_TO_BLENDER3 @ v_gltf.T).T
    return np.ascontiguousarray(v_bl, dtype=np.float32)


def _world_to_blender(pos: np.ndarray) -> np.ndarray:
    """BeamNG absolute world space -> Blender space.

    The v5 capture already re-frames the vertices to Blender Z-up at capture
    time (``tools/v5_capture.lua`` applies ``world = (x, -z, y)`` after
    ``getRefNodeMatrix():mulP3F``, baking the Y-up -> Z-up rotation into the
    stored coordinates). So the .bmc is already Blender-world: right-handed
    Z-up, same axes/handedness as Blender.  No conversion is needed here — the
    float values are copied through unchanged (identity).

    This is the v5 "zero-matrix" design: deformation, rotation and world
    translation are baked into every vertex at capture; the BVC is a flat
    verbatim array stream with no parent empty and no per-frame matrix.
    """
    return np.ascontiguousarray(pos, dtype=np.float32)


def _weld_object(
    positions: np.ndarray,
    indices: Optional[np.ndarray],
    uvs: Optional[np.ndarray],
    epsilon: float = 1e-5,
    uv_epsilon: float = 1e-4,
) -> Tuple[np.ndarray, Optional[np.ndarray], Optional[np.ndarray], np.ndarray]:
    """Remove exact-duplicate vertices (position AND UV, tight epsilon).

    Snaps positions to a grid of ``epsilon`` precision, finds unique rows,
    and builds a remap table.

    THE UV SEAM FIX
    ---------------
    A UV seam is *by definition* two vertices at the SAME position carrying
    DIFFERENT UVs — that's how a tyre tread wraps (u=0 meets u=1).  A
    position-only weld merges them and then keeps a single arbitrary survivor
    UV (``uvs[unique_ids]``), which tears the texture across the seam.
    Measured on a real capture, 44 of 49 merges on ``tire_01a_16x7_26``
    collapsed a genuine seam with a UV jump of ~1.0 — the whole texture width.

    So the merge key is ``(snapped position, snapped uv)``: vertices fuse only
    when they agree on both.  Coincident vertices that share a UV (the bulk of
    the win — e.g. 818/818 merges on ``flanje_e180_wheelcap``) still merge.

    Returns:
      * welded_positions  (K, 3) float32 — K unique vertices
      * welded_indices    (F, 3) int32   — remapped triangle indices (or None)
      * welded_uvs        (K, 2) float32 — UVs of the kept vertices (or None)
      * remap             (N,) int32     — old_vertex_id -> new_vertex_id
    """
    n = positions.shape[0]
    if n == 0:
        return positions, indices, uvs, np.zeros(0, dtype=np.int32)

    # Snap to grid and find unique rows.  When UVs are present they join the
    # key so seam-split vertices survive as distinct vertices.
    snapped = np.round(positions / epsilon).astype(np.int64)
    if uvs is not None and uvs.size > 0 and uvs.shape[0] == n:
        snapped_uv = np.round(uvs / uv_epsilon).astype(np.int64)
        key = np.column_stack([snapped, snapped_uv])
    else:
        key = snapped
    _, unique_ids, inverse = np.unique(
        key, axis=0, return_index=True, return_inverse=True
    )
    remap = inverse.astype(np.int32).ravel()

    k = unique_ids.shape[0]
    welded_positions = positions[unique_ids].astype(np.float32, copy=False)

    welded_indices = None
    if indices is not None:
        welded_indices = remap[indices]

    welded_uvs = None
    if uvs is not None and uvs.size > 0:
        welded_uvs = uvs[unique_ids].astype(np.float32, copy=False)

    return welded_positions, welded_indices, welded_uvs, remap


def _weld_keep_remap_multiframe(
    pos_frames: List[np.ndarray],
    epsilon: float = 1e-4,
    uvs: Optional[np.ndarray] = None,
    uv_epsilon: float = 1e-4,
) -> Tuple[np.ndarray, np.ndarray]:
    """Cross-frame-safe vertex weld: merge only vertices that stay coincident
    across EVERY sampled frame.

    THE WINDSHIELD FIX (2026-07-24)
    -------------------------------
    The old single-frame grid-snap weld merged vertices that were coincident in
    ONE pose (e.g. a windshield edge touching the body at rest).  During the
    crash those points separate, but the weld had already fused them into one —
    so the surviving vertex was pulled between two diverging positions, creating
    the stretched spikes.  A manual Blender "merge by distance" only looked fine
    because it was applied to a single pose.

    This weld computes, per vertex, a *multi-frame signature* (its snapped
    position in every sampled frame) and merges only vertices whose signatures
    are identical — i.e. points that never separate anywhere in the animation.
    A windshield vertex and a body vertex that ever move apart get different
    signatures and are kept distinct.  Safe by construction.

    UV seams are also preserved: when *uvs* is given, the signature is seeded
    with the snapped UV, so two coincident vertices carrying different UVs never
    merge.  See :func:`_weld_object` for why that matters (tyre tread wrap).

    Args:
      pos_frames: list of (N,3) float arrays, one per sampled frame, all in the
                  SAME vertex order (this object's local order).
      epsilon:    merge distance (grid cell size).
      uvs:        optional (N,2) float array — when present, vertices only merge
                  if their UVs agree to *uv_epsilon* as well.

    Returns:
      keep:  (K,) int64 — indices of the vertices to KEEP (first of each group)
      remap: (N,) int64 — old local vertex id -> new welded id in [0, K)
    """
    n = pos_frames[0].shape[0]
    if n == 0:
        return np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.int64)

    # Incrementally refine group labels frame by frame (memory O(N), never holds
    # all frames at once).  Two verts keep the same label only while they share
    # the same snapped cell in every processed frame.
    labels = np.zeros(n, dtype=np.int64)

    # Seed the signature with the UV so seam-split vertices start out in
    # different groups and can never be fused by later frames.
    if uvs is not None and uvs.size > 0 and uvs.shape[0] == n:
        snapped_uv = np.round(uvs / uv_epsilon).astype(np.int64)
        _, labels = np.unique(snapped_uv, axis=0, return_inverse=True)
        labels = labels.astype(np.int64).ravel()

    for pos in pos_frames:
        snapped = np.round(pos / epsilon).astype(np.int64)
        # combine current label with this frame's cell -> refined label
        combined = np.column_stack([labels, snapped])
        _, labels = np.unique(combined, axis=0, return_inverse=True)
    labels = labels.astype(np.int64).ravel()

    # keep = first occurrence of each final label; remap via label re-index.
    uniq, keep, inverse = np.unique(
        labels, return_index=True, return_inverse=True
    )
    remap = inverse.astype(np.int64)
    return keep.astype(np.int64), remap


def _loop_uvs_from_vertex_uvs(
    uvs: Optional[np.ndarray], indices: Optional[np.ndarray],
) -> Optional[np.ndarray]:
    """Expand per-vertex UVs into per-face-corner (loop) UVs.

    Returns ``(face_count, 3, 2)`` float32 — ``out[f, c]`` is the UV of corner
    *c* of face *f*.  This is the form that survives a position-only weld: two
    corners landing on the same merged vertex keep their own UV, which is
    exactly how Blender's "merge by distance" preserves seams.

    Must be called with the PRE-weld ``uvs``/``indices`` so the original seam
    UVs are captured before any vertices are fused.
    """
    if uvs is None or indices is None or uvs.size == 0 or indices.size == 0:
        return None
    idx = np.asarray(indices).reshape(-1, 3)
    if idx.size == 0 or int(idx.max()) >= uvs.shape[0]:
        return None
    return np.ascontiguousarray(uvs[idx], dtype=np.float32)


class CacheBuilder:
    def __init__(self, sequence_dir: Path, out_path: Path):
        self.sequence_dir = Path(sequence_dir)
        self.out_path = Path(out_path)

    # Public entry point — builds a BVC from a BMC v1 capture.
    def build(
        self,
        capture_bmc_path: str | None = None,
        weld: bool = False,
        **_ignored,
    ) -> "SequenceManifest":
        src = Path(capture_bmc_path) if capture_bmc_path else self.sequence_dir
        if src.is_dir():
            # Accept the captures FOLDER (the addon points at one): pick the
            # first .bmc inside it.
            bmcs = sorted(src.glob("*.bmc"))
            if not bmcs:
                raise CacheBuildError(f"no .bmc capture in {src}")
            src = bmcs[0]
        return self.build_from_capture(str(src), weld=weld)

    def build_from_capture(self, capture_bmc_path: str,
                           weld: bool = False) -> "SequenceManifest":
        """Build a BVC directly from a BMC v1 ``capture.bmc`` file.

        Uses the shared-pool reader (BmcReader).  All objects are
        topology-stable.  Positions are converted from pool space to Blender
        space via ``_pool_to_blender``; the rigid transform is copied through
        byte-for-byte.  Materials are carried from the BMC primitive/material
        tables so each part gets its real material slots.

        ``weld`` (default False): when True, run the CROSS-FRAME-SAFE weld
        (:func:`_weld_keep_remap_multiframe`) that merges only vertices which
        stay coincident in EVERY sampled frame — collapsing seam-split
        duplicates without breaking parts (like the windshield) whose edges
        separate during the crash.
        """
        with BmcReader(capture_bmc_path) as reader:
            total_frames = reader.frame_count()
            obj_names = reader.object_names()
            obj_count = len(obj_names)

            print(
                f"[BeamNG] Building cache from BMC v1: "
                f"{obj_count} objects, {total_frames} frames -> {self.out_path}",
                flush=True,
            )

            base_indices: Dict[str, np.ndarray] = {}
            base_uvs: Dict[str, np.ndarray] = {}
            base_matnames: Dict[str, List[str]] = {}
            base_matids: Dict[str, np.ndarray] = {}
            vcounts: Dict[str, int] = {}
            face_counts: Dict[str, int] = {}
            index_range: Dict[str, np.ndarray] = {}

            manifest = SequenceManifest(
                sequence_name=self.out_path.stem, frame_count=total_frames
            )

            for name in obj_names:
                info = reader.object_info(name)
                idx = reader.base_indices(name)
                uvs = reader.base_uvs(name)

                base_indices[name] = np.ascontiguousarray(idx, dtype=np.int32)
                base_uvs[name] = (
                    np.ascontiguousarray(uvs, dtype=np.float32)
                    if uvs is not None
                    else np.zeros((info.vertex_count, 2), dtype=np.float32)
                )
                base_matnames[name] = reader.base_material_names(name)
                base_matids[name] = reader.base_material_ids(name)
                vcounts[name] = info.vertex_count
                face_counts[name] = info.index_count
                index_range[name] = np.asarray(info.index_range, dtype=np.int64)

            # --- LOOP UVs (v5) --------------------------------------------
            # Snapshot per-corner UVs BEFORE the weld.  Once these exist the
            # weld no longer has to keep seam vertices apart to protect the
            # texture, so it can merge on position alone (recovering ~78K
            # vertices on the E180 that Blender's own merge-by-distance also
            # collapses).  Face order is preserved through the weld's
            # degenerate-triangle filter below, so this stays aligned.
            base_loop_uvs: Dict[str, np.ndarray] = {}
            for name in obj_names:
                lu = _loop_uvs_from_vertex_uvs(
                    base_uvs.get(name), base_indices.get(name))
                if lu is not None:
                    base_loop_uvs[name] = lu

            # --- CROSS-FRAME-SAFE WELD (optional) --------------------------
            # Merge seam-split duplicate vertices that stay coincident in EVERY
            # sampled frame.  Parts whose edges separate during the crash (e.g.
            # the windshield) keep distinct vertices and are never spiked.
            if weld:
                n_sample = min(total_frames, 16)
                sample_frames = (
                    np.linspace(0, total_frames - 1, n_sample).astype(int)
                    if total_frames > 1 else np.array([0])
                )
                # Read each sampled frame's shared pool once; slice per object.
                sampled_shared = [
                    reader._read_shared_positions(int(fi)).astype(np.float32)
                    for fi in sample_frames
                ]
                total_before = sum(vcounts[n] for n in obj_names)
                for name in obj_names:
                    ir = index_range[name]
                    per_frame = [sh[ir] for sh in sampled_shared]
                    # Position-only weld: the seam now lives in the loop UVs
                    # (captured above), so fusing coincident vertices that
                    # differ only in UV is safe and saves real memory.
                    keep, remap = _weld_keep_remap_multiframe(
                        per_frame, epsilon=1e-4,
                        uvs=None if name in base_loop_uvs else base_uvs.get(name),
                    )
                    if len(keep) == vcounts[name]:
                        continue  # nothing merged for this object
                    # Rewrite geometry with welded vertex set.
                    index_range[name] = ir[keep]
                    base_uvs[name] = base_uvs[name][keep]
                    new_idx = remap[base_indices[name]].astype(np.int32)
                    # Drop triangles that became degenerate after merging.
                    a, b, c = new_idx[:, 0], new_idx[:, 1], new_idx[:, 2]
                    valid = (a != b) & (b != c) & (a != c)
                    new_idx = new_idx[valid]
                    fmids = base_matids[name]
                    if fmids is not None and len(fmids) == len(valid):
                        base_matids[name] = fmids[valid]
                    # Keep loop UVs aligned with the surviving faces.
                    lu = base_loop_uvs.get(name)
                    if lu is not None and lu.shape[0] == len(valid):
                        base_loop_uvs[name] = lu[valid]
                    base_indices[name] = new_idx
                    vcounts[name] = int(len(keep))
                    face_counts[name] = int(new_idx.shape[0])
                total_after = sum(vcounts[n] for n in obj_names)
                print(
                    f"[BeamNG]   weld (cross-frame safe): "
                    f"{total_before} -> {total_after} vertices "
                    f"({total_before - total_after} merged)",
                    flush=True,
                )

            for name in obj_names:
                topo_hash = TopologyHasher.hash_indices(
                    vcounts[name], base_indices[name])
                manifest.objects[name] = ObjectSignature(
                    name=name,
                    vertex_count=vcounts[name],
                    edge_count=_edge_count(base_indices[name], face_counts[name]),
                    face_count=face_counts[name],
                    topology_hash=topo_hash,
                    cacheable=True,
                )

            # --- Rigid props (frozen pose, separate section) ---------------
            # Steering wheel, pedals, gauge needles and the indicator stalk are
            # PROPMESHES, not flexmeshes, so they were missing from BMC caches.
            # They live in a trailing .bmc section that never touched the pool
            # vertex_count (so the flexmesh per-frame math is byte-identical).
            # Bake each prop's frozen pose into Blender space here and emit it as
            # a stable object with a CONSTANT per-frame position — it parents to
            # the __root motion empty and thus rides along with the car.
            prop_names: List[str] = []
            prop_frozen_pos: Dict[str, np.ndarray] = {}
            props = reader.props()
            if props:
                mat_names_global = getattr(reader, "_material_names", [])
                name_counter: Dict[str, int] = {}
                for prop in props:
                    pname = prop.name or "prop"
                    c = name_counter.get(pname, 0)
                    name_counter[pname] = c + 1
                    if c > 0:
                        pname = f"{pname}_{c}"
                    frozen = _prop_to_blender_frozen(
                        prop.vertices, prop.position, prop.rotation)
                    idx = np.ascontiguousarray(
                        prop.indices, dtype=np.int32).reshape(-1, 3)
                    # Drop degenerate triangles (mirror the flexmesh path).
                    a, b, c2 = idx[:, 0], idx[:, 1], idx[:, 2]
                    valid = (a != b) & (b != c2) & (a != c2)
                    idx = idx[valid]
                    mid = int(prop.material_id)
                    mname = (mat_names_global[mid]
                             if 0 <= mid < len(mat_names_global)
                             else f"material_{mid}")
                    vc = frozen.shape[0]
                    base_indices[pname] = idx
                    base_uvs[pname] = (
                        np.ascontiguousarray(prop.uvs, dtype=np.float32)
                        if prop.uvs is not None
                        else np.zeros((vc, 2), dtype=np.float32)
                    )
                    base_matnames[pname] = [mname]
                    base_matids[pname] = np.zeros(idx.shape[0], dtype=np.uint16)
                    plu = _loop_uvs_from_vertex_uvs(base_uvs[pname], idx)
                    if plu is not None:
                        base_loop_uvs[pname] = plu
                    vcounts[pname] = vc
                    face_counts[pname] = int(idx.shape[0])
                    prop_frozen_pos[pname] = frozen
                    prop_names.append(pname)
                    manifest.objects[pname] = ObjectSignature(
                        name=pname,
                        vertex_count=vc,
                        edge_count=_edge_count(idx, idx.shape[0]),
                        face_count=int(idx.shape[0]),
                        topology_hash=TopologyHasher.hash_indices(vc, idx),
                        cacheable=True,
                    )
                print(
                    f"[BeamNG]   props: added {len(prop_names)} rigid prop "
                    f"object(s) (frozen pose)",
                    flush=True,
                )

            current_stable = list(obj_names) + prop_names
            current_dynamic: List[str] = []

            # --- Layout computation ------------------------------------
            frame_vertex_offset: Dict[str, int] = {}
            cursor = 0
            for name in current_stable:
                frame_vertex_offset[name] = cursor
                cursor += vcounts[name] * 3
            max_stable_vertex_total = sum(vcounts[n] for n in current_stable)

            all_material_names: List[str] = ["__no_material__"]
            name_index: Dict[str, int] = {"__no_material__": 0}

            def _ensure_name(n: str) -> int:
                idx = name_index.get(n)
                if idx is None:
                    idx = len(all_material_names)
                    name_index[n] = idx
                    all_material_names.append(n)
                return idx

            initial_table_size = sum(
                2 + len(n.encode("utf-8")) + binary._OBJ_TAIL.size
                + binary.OBJ_EXTRA_V5
                for n in current_stable
            )
            padded_table_size = initial_table_size + 4096
            object_table_offset = binary.HEADER_SIZE
            base_mesh_offset = object_table_offset + padded_table_size

            base_index_bytes: Dict[str, bytes] = {}
            base_index_offset: Dict[str, int] = {}
            index_count: Dict[str, int] = {}
            off = base_mesh_offset
            for name in current_stable:
                ib = np.ascontiguousarray(base_indices[name], dtype=np.int32).tobytes()
                base_index_bytes[name] = ib
                base_index_offset[name] = off
                index_count[name] = face_counts[name]
                off += len(ib)

            uv_block_bytes: Dict[str, bytes] = {}
            uv_off: Dict[str, int] = {}
            for name in current_stable:
                uv = base_uvs.get(name)
                ub = (
                    np.ascontiguousarray(uv, dtype=np.float32).tobytes()
                    if uv is not None and uv.size > 0
                    else b""
                )
                uv_block_bytes[name] = ub
                uv_off[name] = off
                off += len(ub)

            uv_blocks_offset = off

            # --- Loop (per-face-corner) UV blocks, v5 ---------------------
            loop_uv_block_bytes: Dict[str, bytes] = {}
            loop_uv_off: Dict[str, int] = {}
            for name in current_stable:
                lu = base_loop_uvs.get(name)
                ok = (
                    lu is not None and lu.size > 0
                    and lu.shape[0] == face_counts[name]
                )
                lb = (
                    np.ascontiguousarray(lu, dtype=np.float32).tobytes()
                    if ok else b""
                )
                loop_uv_block_bytes[name] = lb
                loop_uv_off[name] = off if lb else 0
                off += len(lb)

            # --- Per-object materials (from the BMC primitive/material tables) --
            # The capture records, per primitive, a material id into a 90-entry
            # name table.  The reader compacts these to a per-object local set +
            # a per-face local id.  We register the names globally (dedup) and
            # write one material block per object so Blender gets a proper
            # material slot per part — required for texture assignment later.
            obj_local_names: Dict[str, List[str]] = {}
            obj_face_ids: Dict[str, np.ndarray] = {}
            for name in current_stable:
                lnames = base_matnames.get(name) or []
                fids = base_matids.get(name)
                if not lnames:
                    lnames = ["__no_material__"]
                    fids = np.zeros(face_counts[name], dtype=np.uint16)
                # Register globally and remember each name's global id.
                for mn in lnames:
                    _ensure_name(mn)
                obj_local_names[name] = lnames
                # Guard: face-id array must match this object's face count.
                if fids is None or len(fids) != face_counts[name]:
                    fids = np.zeros(face_counts[name], dtype=np.uint16)
                obj_face_ids[name] = fids

            mat_table_bytes = binary.pack_material_name_table(all_material_names)
            material_table_offset = off
            off += len(mat_table_bytes)

            mat_block_bytes: Dict[str, bytes] = {}
            mat_off: Dict[str, int] = {}
            for name in current_stable:
                lnames = obj_local_names[name]
                global_ids = [name_index[mn] for mn in lnames]
                mids = obj_face_ids[name]
                mb = binary.pack_material_block(global_ids, mids)
                mat_block_bytes[name] = mb
                mat_off[name] = off
                off += len(mb)

            material_blocks_offset = off

            frame_block_size = max_stable_vertex_total * 3 * 4
            frame_directory_offset = off
            off += total_frames * 8
            frame_blocks_offset = off

            # --- Write BVC file ---------------------------------------
            print(f"[BeamNG]   writing {self.out_path} ...", flush=True)
            self.out_path.parent.mkdir(parents=True, exist_ok=True)
            if self.out_path.exists():
                try:
                    self.out_path.unlink()
                except OSError:
                    pass

            with open(self.out_path, "wb") as fh:
                fh.write(
                    binary.pack_header(
                        frame_count=total_frames,
                        object_count=len(current_stable),
                        stable_count=len(current_stable),
                        stable_vertex_total=max_stable_vertex_total,
                        object_table_offset=object_table_offset,
                        base_mesh_offset=base_mesh_offset,
                        uv_blocks_offset=uv_blocks_offset,
                        material_table_offset=material_table_offset,
                        material_blocks_offset=material_blocks_offset,
                        frame_directory_offset=frame_directory_offset,
                        frame_blocks_offset=frame_blocks_offset,
                        dynamic_directory_offset=0,
                        dynamic_data_offset=0,
                        transform_data_offset=0,
                    )
                )
                fh.write(b"\x00" * padded_table_size)

                for name in current_stable:
                    fh.write(base_index_bytes[name])
                for name in current_stable:
                    fh.write(uv_block_bytes[name])
                for name in current_stable:
                    fh.write(loop_uv_block_bytes[name])

                fh.write(mat_table_bytes)
                for name in current_stable:
                    fh.write(mat_block_bytes[name])

                for i in range(total_frames):
                    fh.write(struct.pack("<Q", frame_blocks_offset + i * frame_block_size))

                # --- Per-frame vertex + rigid transform -----------------
                # v4: transform = position + forward + up direction vectors.
                # Reconstruct a clean orientation matrix (right, fwd, up) via
                # cross product, bypassing the refNode-bound quaternion.
                #
                # Spawn-flip correction: BeamNG's captured fwd vector can snap
                # ~90 deg between the pre-settle frames (0..k) and the settled
                # state (k+1..) with almost no XY displacement. That is a capture
                # artifact, not real motion. We unwrap it so the sequence is
                # continuous AND anchored so the car's rest facing is Blender -Y
                # (the user's hard requirement).  We do this by rotating the
                # pre-flip frames by the detected yaw delta; the settled frames
                # already carry the true orientation.
                has_transform = reader.header.has_transform
                world_space = reader.header.has_world_space
                per_frame_transforms: list[np.ndarray] = []

                if world_space:
                    # v5 absolute world-space dump: vertices are already in
                    # BeamNG world coords (engine render matrix applied at
                    # capture).  No pool->Blender perm, no transform block, no
                    # matrix.  Just copy frames straight through, then fall
                    # through to the shared header patch (transform_data_offset=0).
                    print("[BeamNG]   world-space dump: applying single global "
                          "S (Y-up world -> Blender Z-up), no transform block",
                          flush=True)
                    for fi in range(total_frames):
                        shared_pos = reader._read_shared_positions(fi)
                        pos_blender = np.ascontiguousarray(
                            _world_to_blender(shared_pos.astype(np.float32)))
                        for name in current_stable:
                            if name in prop_frozen_pos:
                                # Frozen prop: constant world position every frame.
                                pos = np.ascontiguousarray(
                                    prop_frozen_pos[name], dtype=np.float32)
                            else:
                                obj_idx = index_range[name]
                                pos = np.ascontiguousarray(
                                    pos_blender[obj_idx], dtype=np.float32)
                            fh.write(pos.tobytes())
                        written = sum(vcounts[n] * 12 for n in current_stable)
                        pad = frame_block_size - written
                        if pad > 0:
                            fh.write(b"\x00" * pad)
                        if fi == 0:
                            print(
                                f"[BeamNG]   frame 0 written "
                                f"({len(current_stable)} objects, "
                                f"{max_stable_vertex_total} verts)",
                                flush=True,
                            )
                    # no transform data for world-space dumps
                    transform_data_offset = 0
                else:
                    transform_data_offset = 0  # set by transform block below

                # --- Rigid transform from direction vectors (PROVEN in calibrator) ---
                # Calibrator verified basis (blender_import_csv.py::basis_to_quat):
                #   x_axis = forward   (mesh local +X is the nose)
                #   z_axis = up        (mesh local +Z is the roof)
                #   y_axis = z x x     (re-orthogonalize, right-handed)
                # No PCA, no spawn-flip unwrap, no L matrix. The direction vectors
                # are already physics Z-up == Blender Z-up (same axes/handedness),
                # so the basis maps directly to the Blender matrix.
                if not world_space and has_transform:
                    # =========================================================
                    #  POSITION-ONLY transform  (rotation is already in the mesh)
                    # =========================================================
                    # PROVEN (Kabsch on raw verticesGet output, 2026-07-20):
                    #   verticesGet() returns the vehicle mesh already ROTATED and
                    #   DEFORMED in place around the vehicle's own origin — the full
                    #   rigid rotation is BAKED INTO THE VERTICES.  It is NOT
                    #   translated into world space (vertex centroid stays ~1 m from
                    #   origin while getPosition() travels 60+ m).
                    #
                    #   So the ONLY missing piece is TRANSLATION.  Applying a rotation
                    #   from getRotation()/getDirectionVector() on top of the mesh
                    #   double-rotates it (a 90 deg turn rendered as 180 deg — the
                    #   long-standing bug).  The calibrator never hit this because it
                    #   dumped the mesh ONCE (a static snapshot) and rotated that
                    #   single frame; the importer re-reads the (already-rotated)
                    #   mesh every frame.
                    #
                    #   Fix: store IDENTITY rotation + getPosition() translation.
                    #   The permuted pool mesh already sits in Blender axes (physics
                    #   Z-up == Blender Z-up), so getPosition() needs no conversion.
                    IDENTITY_ROT = np.eye(3, dtype=np.float32).reshape(-1)
                    # --- Prop rotation baking (BMC pipeline) ---------------
                    # Flexmesh pool verts are re-sampled every frame, so the
                    # car's rotation is already baked into them per-frame.  Props
                    # are captured ONCE (frozen rest pose), so on their own they
                    # only ride the __root empty's TRANSLATION and never turn —
                    # when the car yaws the props stick out.  We fix that here by
                    # rotating each frozen prop by the car's rotation SINCE FRAME
                    # 0, reconstructed from the captured fwd/up direction vectors,
                    # around the (origin-relative) vehicle origin.  This bakes
                    # prop rotation into the vertex stream exactly as the flexmesh
                    # pool already bakes its own — keeping the pipeline invariant
                    # (verts carry rotation, empty carries translation only).
                    R0 = None
                    if prop_frozen_pos:
                        vtx0 = reader.frame_vehicle_transform(0)
                        R0 = _basis_from_dirs(vtx0[3:6], vtx0[6:9])
                    for fi in range(total_frames):
                        shared_pos = reader._read_shared_positions(fi)
                        pos_blender = _pool_to_blender(shared_pos.astype(np.float64))

                        vtx = reader.frame_vehicle_transform(fi)
                        p = np.asarray(vtx[:3], dtype=np.float64)

                        # translation only; mesh already carries rotation+deformation
                        p_blender = p.astype(np.float32)
                        tf_blender = IDENTITY_ROT

                        # Car rotation since frame 0 (identity at fi==0), applied
                        # to frozen props below.  None when there are no props.
                        dR = None
                        if R0 is not None:
                            Rf = _basis_from_dirs(vtx[3:6], vtx[6:9])
                            dR = (Rf @ R0.T).astype(np.float32)

                        for name in current_stable:
                            if name in prop_frozen_pos:
                                # Frozen prop rotated by the car's rotation since
                                # frame 0, so it turns with the body.  It still
                                # parents to the __root empty (which carries
                                # getPosition translation), so it rides along too.
                                frozen = prop_frozen_pos[name]
                                pos = np.ascontiguousarray(
                                    (frozen @ dR.T) if dR is not None else frozen,
                                    dtype=np.float32)
                            else:
                                obj_idx = index_range[name]
                                pos = np.ascontiguousarray(pos_blender[obj_idx], dtype=np.float32)
                            fh.write(pos.tobytes())

                        written = sum(vcounts[n] * 12 for n in current_stable)
                        pad = frame_block_size - written
                        if pad > 0:
                            fh.write(b"\x00" * pad)

                        if fi == 0:
                            print(
                                f"[BeamNG]   frame 0 written "
                                f"({len(current_stable)} objects, "
                                f"{max_stable_vertex_total} verts)",
                                flush=True,
                            )

                        per_frame_transforms.append(np.concatenate([p_blender, tf_blender]))
    
                    # Write transform data section (after frame blocks)
                    transform_data_offset = 0
                    if has_transform and per_frame_transforms:
                        transform_data_offset = fh.tell()
                        for tf in per_frame_transforms:
                            fh.write(tf.tobytes())

                # Patch header and write object table
                fh.seek(0)
                fh.write(
                    binary.pack_header(
                        frame_count=total_frames,
                        object_count=len(current_stable),
                        stable_count=len(current_stable),
                        stable_vertex_total=max_stable_vertex_total,
                        object_table_offset=object_table_offset,
                        base_mesh_offset=base_mesh_offset,
                        uv_blocks_offset=uv_blocks_offset,
                        material_table_offset=material_table_offset,
                        material_blocks_offset=material_blocks_offset,
                        frame_directory_offset=frame_directory_offset,
                        frame_blocks_offset=frame_blocks_offset,
                        dynamic_directory_offset=0,
                        dynamic_data_offset=0,
                        transform_data_offset=transform_data_offset,
                    )
                )

                fh.seek(object_table_offset)
                for name in current_stable:
                    sig = manifest.objects[name]
                    fh.write(
                        binary.pack_object_entry(
                            name=name,
                            stable=True,
                            vertex_count=vcounts[name],
                            face_count=face_counts[name],
                            topology_hash=sig.topology_hash,
                            base_index_offset=base_index_offset[name],
                            index_count=index_count[name],
                            frame_vertex_offset=frame_vertex_offset[name],
                            welded=False,
                            uv_offset=uv_off[name],
                            material_offset=mat_off[name],
                            loop_uv_offset=loop_uv_off[name],
                        )
                    )

            manifest.stable_objects = current_stable
            manifest.dynamic_objects = current_dynamic

            size_mb = self.out_path.stat().st_size / 1e6
            print(
                f"[BeamNG] cache written from capture: {size_mb:.1f} MB "
                f"({total_frames} frames, {len(current_stable)} objects)",
                flush=True,
            )

        return manifest


# Local import kept at the bottom to avoid a circular import at module load.
from .scanner import ObjectSignature, SequenceManifest, _edge_count  # noqa: E402,F401
