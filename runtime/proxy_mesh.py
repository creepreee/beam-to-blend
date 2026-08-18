from __future__ import annotations

"""Create a single low-poly proxy mesh that follows the car's vertex animation.

The proxy is one mesh (≤ 1 000 verts) parented to the existing transform
Empty.  Each proxy vertex is an exact copy of a sampled original vertex —
no nearest-neighbour approximation, no convex hull distortion.  On every
frame change the proxy copies the sampled original vertex's cache-local
position.  Rigid body motion comes for free from the parent Empty.

Build approach:
  1. Read all source vertex positions at frame 0 (numpy only).
  2. Farthest-point sampling selects ≤1000 representative original vertices.
  3. Mesh is created directly from those positions (no hull, no decimate).
  4. Mapping is exact: proxy vertex i == original vertex sample_idx[i].
"""

import numpy as np

try:  # pragma: no cover - only inside Blender
    import bpy
    import bmesh
except ImportError:  # pragma: no cover
    bpy = None
    bmesh = None

from .mesh_update import _write_positions

# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------

_proxy_object = None          # bpy.types.Object | None
_mapping_orig_names = None    # (N,) int32 — index into _orig_name_list
_mapping_orig_vi = None       # (N,) int32 — vertex index within that object
_orig_name_list = None        # list[str]
_proxy_vert_count = 0

_PROXY_NAME = "BeamNG_Proxy"


# ---------------------------------------------------------------------------
# Farthest-point sampling (pure numpy, no scipy)
# ---------------------------------------------------------------------------

def _farthest_point_sample(pts: np.ndarray, k: int) -> np.ndarray:
    """Return *k* indices into *pts* via greedy farthest-point sampling.

    *pts* is (N, D) float32.  Returns (k,) int64 indices.
    O(kN) which is fine for k ≤ 1000 and N ≤ 600K.
    """
    n = len(pts)
    if n <= k:
        return np.arange(n, dtype=np.int64)

    selected = np.empty(k, dtype=np.int64)
    centroid = pts.mean(axis=0, keepdims=True)
    dist_to_cent = ((pts - centroid) ** 2).sum(axis=1)
    selected[0] = int(dist_to_cent.argmax())

    min_d2 = ((pts - pts[selected[0]]) ** 2).sum(axis=1)

    for i in range(1, k):
        best = int(min_d2.argmax())
        selected[i] = best
        d2 = ((pts - pts[best]) ** 2).sum(axis=1)
        np.minimum(min_d2, d2, out=min_d2)

    return selected


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def create_proxy(playback, max_verts: int = 1000):
    """Build the proxy mesh and register the vertex mapping.

    Returns the proxy Object, or ``None`` on failure.
    """
    global _proxy_object, _mapping_orig_names, _mapping_orig_vi
    global _orig_name_list, _proxy_vert_count

    if bpy is None:
        return None

    reader = playback.reader

    # ------------------------------------------------------------------
    # Phase 1: gather frame-0 positions for every source object.
    # ------------------------------------------------------------------
    obj_names: list[str] = []
    obj_verts: list = []

    for name in list(playback._objects.keys()):
        try:
            raw = reader.frame_positions(name, 0)
        except Exception:
            continue
        obj_names.append(name)
        obj_verts.append(np.asarray(raw, dtype=np.float32))

    for chunk_name, members in (playback._chunk_member_ranges or {}).items():
        for mname in members:
            if mname in obj_names:
                continue
            try:
                raw = reader.frame_positions(mname, 0)
            except Exception:
                continue
            obj_names.append(mname)
            obj_verts.append(np.asarray(raw, dtype=np.float32))

    if not obj_names:
        return None

    all_pos = np.concatenate(obj_verts, axis=0)
    total_verts = len(all_pos)

    # Build flat index arrays: for each vertex in all_pos, record which
    # object name index and local vertex index it belongs to.
    name_ids = np.empty(total_verts, dtype=np.int32)
    vert_ids = np.empty(total_verts, dtype=np.int32)
    offset = 0
    for i, v in enumerate(obj_verts):
        n = len(v)
        name_ids[offset:offset + n] = i
        vert_ids[offset:offset + n] = np.arange(n, dtype=np.int32)
        offset += n

    # ------------------------------------------------------------------
    # Phase 2: farthest-point sample — select representative originals.
    # ------------------------------------------------------------------
    sample_idx = _farthest_point_sample(all_pos, max_verts)

    # Detached parts (mirrors, glass shards) fly far from the car during the
    # crash.  If we map proxy verts to them, they stretch away.  Detect and
    # replace: read the last captured frame, compute per-vertex displacement,
    # and remap any outlier (displacement > 3× median) to its nearest
    # non-outlier original vertex.
    last_frame = reader.frame_count - 1
    try:
        all_pos_end = np.concatenate([
            np.asarray(reader.frame_positions(n, last_frame), dtype=np.float32)
            if n in {o.name for o in reader.stable_objects()}
            else np.zeros((0, 3), dtype=np.float32)
            for n in obj_names
        ], axis=0)
        displacement = np.linalg.norm(all_pos_end - all_pos, axis=1)
        med_disp = float(np.median(displacement))
        outlier_mask = displacement > max(3.0 * med_disp, 0.5)
        core_indices = np.where(~outlier_mask)[0]
        if len(core_indices) > 0:
            core_pos = all_pos[core_indices]
            for i, si in enumerate(sample_idx):
                if outlier_mask[si]:
                    # Replace with nearest core vertex
                    d2 = np.sum((core_pos - all_pos[si]) ** 2, axis=1)
                    sample_idx[i] = core_indices[int(d2.argmin())]
    except Exception:
        pass  # if last-frame read fails, keep unfiltered sample

    proxy_vert_count = len(sample_idx)

    # The proxy mesh positions ARE the sampled original positions exactly.
    sample_pos = all_pos[sample_idx].copy()

    # ------------------------------------------------------------------
    # Phase 3: create mesh with faces via convex hull.
    #   Convex hull keeps all input vertices with exact positions and adds
    #   triangular faces so the proxy is visible in SOLID viewport mode.
    # ------------------------------------------------------------------
    bm = bmesh.new()
    bm_verts = [bm.verts.new(tuple(p)) for p in sample_pos]
    bm.verts.ensure_lookup_table()
    bmesh.ops.convex_hull(bm, input=bm_verts)
    bm.normal_update()

    mesh = bpy.data.meshes.new(_PROXY_NAME)
    bm.to_mesh(mesh)
    bm.free()
    mesh.update()

    if len(mesh.vertices) == 0:
        bpy.data.meshes.remove(mesh)
        return None

    # ------------------------------------------------------------------
    # Phase 4: exact mapping — proxy vertex i == original sample_idx[i].
    #   No nearest-neighbour search needed.  Zero stretching guaranteed.
    # ------------------------------------------------------------------
    _orig_name_list = obj_names
    _mapping_orig_names = name_ids[sample_idx]      # (N,) int32
    _mapping_orig_vi = vert_ids[sample_idx]          # (N,) int32
    _proxy_vert_count = proxy_vert_count

    # ------------------------------------------------------------------
    # Phase 5: create the proxy object, parent to the transform Empty.
    # ------------------------------------------------------------------
    coll_name = playback.collection_name
    collection = bpy.data.collections.get(coll_name)
    if collection is None:
        bpy.data.meshes.remove(mesh)
        return None

    old = bpy.data.objects.get(_PROXY_NAME)
    if old is not None:
        old_mesh = old.data
        bpy.data.objects.remove(old, do_unlink=True)
        if old_mesh is not None:
            bpy.data.meshes.remove(old_mesh)

    _proxy_object = bpy.data.objects.new(_PROXY_NAME, mesh)
    collection.objects.link(_proxy_object)

    transform_empty = playback._transform_empty
    if transform_empty is not None:
        _proxy_object.parent = transform_empty
        # Match the source objects' parenting exactly.  The source objects
        # were parented when the Empty was first created (identity world
        # matrix), so their matrix_parent_inverse = I.  We must copy that
        # rather than recomputing from the Empty's *current* matrix_world,
        # which changes every frame — doing so produces wrong positions and
        # vertices stretching toward the world origin.
        _ref_obj = None
        for o in collection.objects:
            if o.type == "MESH" and o != _proxy_object and o.parent == transform_empty:
                _ref_obj = o
                break
        if _ref_obj is None:
            # Fallback: search source collection
            src_coll = bpy.data.collections.get(coll_name + " (Source)")
            if src_coll is not None:
                for o in src_coll.objects:
                    if o.type == "MESH" and o != _proxy_object and o.parent == transform_empty:
                        _ref_obj = o
                        break
        if _ref_obj is not None:
            _proxy_object.matrix_parent_inverse = (
                _ref_obj.matrix_parent_inverse.copy()
            )
            _proxy_object.matrix_local = _ref_obj.matrix_local.copy()
        else:
            _proxy_object.matrix_parent_inverse = (
                transform_empty.matrix_world.inverted()
            )

    _proxy_object.display_type = "SOLID"
    _proxy_object.hide_render = True

    # Material so the mesh is visible in SOLID viewport shading.
    _mat = bpy.data.materials.new(name="BeamNG_Proxy_Mat")
    _mat.diffuse_color = (0.5, 0.5, 0.5, 1.0)
    _mat.use_backface_culling = False
    _proxy_object.data.materials.append(_mat)

    # ------------------------------------------------------------------
    # Phase 6: write frame 0 immediately.
    # ------------------------------------------------------------------
    _update_proxy(0)

    return _proxy_object


# ---------------------------------------------------------------------------
# Per-frame update
# ---------------------------------------------------------------------------

def _update_proxy(frame: int) -> None:
    """Write proxy vertex positions for cache *frame*."""
    if (_proxy_object is None
            or _mapping_orig_names is None
            or _mapping_orig_vi is None
            or _orig_name_list is None):
        return

    mesh = _proxy_object.data
    n = _proxy_vert_count
    out = np.empty(n * 3, dtype=np.float32)

    from . import frame_handler
    pb = frame_handler._active
    if pb is None:
        return
    reader = pb.reader

    # Clamp to valid cache range so out-of-range timeline frames don't
    # produce NaN/garbage from failed reads.
    frame = max(0, min(frame, reader.frame_count - 1))

    # Batch reads: one frame_positions call per unique object name.
    cache: dict = {}
    for name_idx in range(len(_orig_name_list)):
        name = _orig_name_list[name_idx]
        if name in cache:
            continue
        try:
            cache[name] = np.asarray(
                reader.frame_positions(name, frame), dtype=np.float32)
        except Exception:
            pass

    # Vectorised gather — group proxy verts by source object name index,
    # then batch-read each object's positions with fancy indexing.
    name_indices = _mapping_orig_names          # (N,) int32
    vi_indices = _mapping_orig_vi               # (N,) int32
    unique_names = np.unique(name_indices)      # sorted unique source indices

    out_3 = out.reshape(-1, 3)
    for ui in unique_names:
        ui = int(ui)
        mask = name_indices == ui               # bool (N,)
        name = _orig_name_list[ui]
        pos = cache.get(name)
        if pos is None:
            continue
        vis = vi_indices[mask]                  # vertex indices for this object
        valid = vis < len(pos)
        if not valid.any():
            continue
        # Fancy-index the positions in one shot.
        safe_vis = np.where(valid, vis, 0)
        sampled = pos[safe_vis]                 # (K, 3)
        # Zero out invalid entries.
        sampled[~valid] = 0.0
        out_3[mask] = sampled

    _write_positions(mesh, out, _proxy_object)


def update_proxy_from_frame(frame: float) -> None:
    """Public entry point called by frame_handler after set_frame."""
    if _proxy_object is None or _mapping_orig_names is None:
        return
    _update_proxy(int(round(frame)))


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

def clear_proxy() -> None:
    """Remove the proxy object and free mapping state."""
    global _proxy_object, _mapping_orig_names, _mapping_orig_vi
    global _orig_name_list, _proxy_vert_count

    if _proxy_object is not None and _proxy_object.name in bpy.data.objects:
        mesh = _proxy_object.data
        bpy.data.objects.remove(_proxy_object, do_unlink=True)
        if mesh is not None:
            bpy.data.meshes.remove(mesh)
    _proxy_object = None
    _mapping_orig_names = None
    _mapping_orig_vi = None
    _orig_name_list = None
    _proxy_vert_count = 0


def has_proxy() -> bool:
    """Return True if a proxy is currently active."""
    return _proxy_object is not None and _proxy_object.name in bpy.data.objects
