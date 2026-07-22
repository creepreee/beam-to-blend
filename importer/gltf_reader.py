from __future__ import annotations

"""Low-level GLB (binary glTF 2.0) reader.

Pure CPython, no ``bpy``. Parses the GLB container and extracts, per object,
the mesh name, vertex positions, and triangle indices — everything the
scanner and cache builder need. Heavier glTF features (materials, animations,
skins) are intentionally ignored here; other modules own those.
"""

import hashlib
import json
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

# glTF constants
_GLB_MAGIC = b"glTF"
_CHUNK_JSON = b"JSON"
_CHUNK_BIN = b"BIN\x00"

# accessor.componentType
_COMPONENT_DTYPE = {
    5120: np.int8,
    5121: np.uint8,
    5122: np.int16,
    5123: np.uint16,
    5125: np.uint32,
    5126: np.float32,
}

# accessor.type -> number of components
_TYPE_COMPONENTS = {
    "SCALAR": 1,
    "VEC2": 2,
    "VEC3": 3,
    "VEC4": 4,
    "MAT2": 4,
    "MAT3": 9,
    "MAT4": 16,
}

# primitive.mode 4 == TRIANGLES (the only mode we support)
_MODE_TRIANGLES = 4


@dataclass
class GLBObject:
    """One drawable object extracted from a GLB frame.

    ``name`` is the glTF node name (BeamNG uses these as object identifiers).
    ``positions`` is an (N, 3) float32 array of vertex coordinates.
    ``indices`` is an (M, 3) int32 array of triangle vertex indices, or ``None``
    for non-indexed geometry (positions are then implicit triangles).

    The remaining fields are populated only when :func:`read_glb` is called with
    ``want_materials=True`` (otherwise they stay ``None`` and the object is a
    pure geometry record, as the scanner and cache position-stream need):

    ``uvs``               (N, 2) float32 TEXCOORD_0, aligned 1:1 with
                          ``positions`` (BeamNG's position dedup keeps exactly one
                          UV per deduped vertex — verified no seam splits).
    ``material_names``    first-seen-ordered list of the glTF material names this
                          object's faces reference (``"__no_material__"`` sentinel
                          for primitives that declare no material).
    ``face_material_ids`` (M,) uint16 index into ``material_names``, one per
                          triangle, aligned 1:1 with ``indices``.
    """

    name: str
    positions: np.ndarray
    indices: Optional[np.ndarray] = None
    uvs: Optional[np.ndarray] = None
    material_names: Optional[List[str]] = None
    face_material_ids: Optional[np.ndarray] = None

    @property
    def vertex_count(self) -> int:
        return int(self.positions.shape[0])

    @property
    def face_count(self) -> int:
        if self.indices is not None:
            return int(self.indices.shape[0])
        return self.vertex_count // 3


@dataclass
class GLBDocument:
    """Parsed contents of a single GLB file."""

    objects: List[GLBObject] = field(default_factory=list)

    def by_name(self) -> Dict[str, GLBObject]:
        return {obj.name: obj for obj in self.objects}


class GLBReadError(Exception):
    """Raised when a GLB file is malformed or uses unsupported features."""


def _read_chunks(data: bytes) -> tuple[dict, bytes]:
    """Split raw GLB bytes into the parsed JSON chunk and the binary chunk."""
    if len(data) < 12:
        raise GLBReadError("file too small to be a GLB")

    magic, version, total_length = struct.unpack_from("<4sII", data, 0)
    if magic != _GLB_MAGIC:
        raise GLBReadError(f"not a GLB file (magic={magic!r})")
    if version != 2:
        raise GLBReadError(f"unsupported GLB version {version}")
    if total_length > len(data):
        raise GLBReadError("declared length exceeds file size")

    gltf_json: Optional[dict] = None
    bin_chunk = b""

    offset = 12
    while offset + 8 <= total_length:
        chunk_len, chunk_type = struct.unpack_from("<I4s", data, offset)
        offset += 8
        chunk_data = data[offset : offset + chunk_len]
        if len(chunk_data) < chunk_len:
            raise GLBReadError("truncated chunk")
        offset += chunk_len

        if chunk_type == _CHUNK_JSON:
            gltf_json = json.loads(chunk_data)
        elif chunk_type == _CHUNK_BIN:
            bin_chunk = chunk_data
        # Unknown chunk types are skipped per spec.

    if gltf_json is None:
        raise GLBReadError("missing JSON chunk")
    return gltf_json, bin_chunk


def _read_accessor(gltf: dict, bin_chunk: bytes, accessor_index: int) -> np.ndarray:
    """Decode an accessor into a numpy array of shape (count, components).

    Only accessors backed by a bufferView in the embedded GLB buffer are
    supported (external/sparse accessors raise). Byte strides are honoured.
    """
    accessor = gltf["accessors"][accessor_index]
    if "sparse" in accessor:
        raise GLBReadError("sparse accessors are not supported")

    dtype = _COMPONENT_DTYPE.get(accessor["componentType"])
    if dtype is None:
        raise GLBReadError(f"unknown componentType {accessor['componentType']}")

    components = _TYPE_COMPONENTS[accessor["type"]]
    count = accessor["count"]
    accessor_offset = accessor.get("byteOffset", 0)

    bv_index = accessor.get("bufferView")
    if bv_index is None:
        # No bufferView: spec says values are all zero.
        return np.zeros((count, components), dtype=np.dtype(dtype))

    buffer_view = gltf["bufferViews"][bv_index]
    if buffer_view.get("buffer", 0) != 0:
        raise GLBReadError("only the embedded GLB buffer (index 0) is supported")

    bv_offset = buffer_view.get("byteOffset", 0)
    base = bv_offset + accessor_offset
    elem_size = np.dtype(dtype).itemsize * components
    stride = buffer_view.get("byteStride") or elem_size

    if stride == elem_size:
        flat = np.frombuffer(
            bin_chunk, dtype=dtype, count=count * components, offset=base
        )
        return flat.reshape(count, components)

    # Interleaved data: gather each element across the stride.
    out = np.empty((count, components), dtype=np.dtype(dtype))
    for i in range(count):
        start = base + i * stride
        out[i] = np.frombuffer(
            bin_chunk, dtype=dtype, count=components, offset=start
        )
    return out


def _object_name(node: dict, mesh: dict, node_index: int) -> str:
    """Pick the best available name for an object."""
    return node.get("name") or mesh.get("name") or f"object_{node_index}"


class _AccessorCache:
    """Reads each accessor at most once per GLB.

    BeamNG meshes split by material into many primitives that all reference the
    same POSITION accessor (one shared vertex pool). Caching avoids decoding a
    533k-vertex buffer dozens of times per frame.
    """

    def __init__(self, gltf: dict, bin_chunk: bytes):
        self._gltf = gltf
        self._bin = bin_chunk
        self._cache: Dict[int, np.ndarray] = {}

    def get(self, accessor_index: int) -> np.ndarray:
        arr = self._cache.get(accessor_index)
        if arr is None:
            arr = _read_accessor(self._gltf, self._bin, accessor_index)
            self._cache[accessor_index] = arr
        return arr


def _node_world_matrix(nodes: List[dict], node_index: int) -> np.ndarray:
    """Compute the world transform (4x4) for a glTF node by walking its parent chain.

    Supports ``matrix`` (column-major float32[16]) or ``translation``/``rotation``
    (quaternion xyzw)/``scale``.  Returns a numpy float64 4×4 matrix.
    """
    node = nodes[node_index]
    # Build local transform  —------ 4×4 identity
    m = np.eye(4, dtype=np.float64)

    if "matrix" in node:
        m = np.array(node["matrix"], dtype=np.float64).reshape(4, 4).T  # glTF is column-major

    else:
        t = node.get("translation", [0.0, 0.0, 0.0])
        r = node.get("rotation", [0.0, 0.0, 0.0, 1.0])  # xyzw
        s = node.get("scale", [1.0, 1.0, 1.0])

        # Rotation matrix from unit quaternion
        x, y, z, w = r
        R = np.array([
            [1 - 2*(y*y + z*z),   2*(x*y - z*w),       2*(x*z + y*w)],
            [2*(x*y + z*w),       1 - 2*(x*x + z*z),   2*(y*z - x*w)],
            [2*(x*z - y*w),       2*(y*z + x*w),       1 - 2*(x*x + y*y)],
        ], dtype=np.float64)

        m[:3, :3] = R * np.array(s, dtype=np.float64)
        m[:3, 3] = t

    # Walk up parent chain
    parent = node.get("parent")
    if parent is not None:
        m = _node_world_matrix(nodes, parent) @ m

    return m


def read_glb(
    path: Path | str,
    remap_cache: Optional[Dict[str, _RemapEntry]] = None,
    verbose: bool = True,
    want_materials: bool = False,
    material_names: Optional[set] = None,
) -> GLBDocument:
    """Read a GLB file and return self-contained per-object meshes.

    Objects are keyed by node name. Each object's primitives are gathered into
    a single mesh whose ``positions`` contain **only** the vertices that object
    references (deduplicated) and whose ``indices`` are remapped into that local
    0..k-1 range. This is essential for BeamNG exports, where up to ~90 objects
    index disjoint slices of one shared 533k-vertex pool: extracting per object
    yields the true per-object vertex count instead of the whole pool.

    Node transforms (translation/rotation/scale) are applied to vertex positions
    so the mesh matches the world-space layout that Blender's native GLB importer
    produces.

    Non-triangle primitives and primitives without POSITION are skipped.

    Pass *remap_cache* (a dict, e.g. from :class:`GLBSequenceReader`) to reuse the
    per-object dedup remap across frames — the ``np.unique`` argsort that dominates
    per-frame cost only runs on the first frame or when connectivity changes.

    Pass *want_materials* to also populate each object's ``uvs``,
    ``material_names`` and ``face_material_ids`` (see :class:`GLBObject`). When
    *material_names* is given (a set of node names), only those objects have
    material data gathered — used to keep per-frame dynamic reads cheap.
    """
    path = Path(path)
    data = path.read_bytes()
    if verbose:
        print(f"[BeamNG]     file size: {len(data) / 1e6:.1f} MB")
    gltf, bin_chunk = _read_chunks(data)

    nodes = gltf.get("nodes", [])
    meshes = gltf.get("meshes", [])
    accessors = _AccessorCache(gltf, bin_chunk)
    material_name_list = [
        m.get("name") or f"material_{i}"
        for i, m in enumerate(gltf.get("materials", []))
    ]

    # Populate parent links (glTF nodes store children, not parent, so we build reverse).
    for i, node in enumerate(nodes):
        for child in node.get("children", []):
            nodes[child]["parent"] = i

    objects: List[GLBObject] = []
    for node_index, node in enumerate(nodes):
        mesh_index = node.get("mesh")
        if mesh_index is None:
            continue
        mesh = meshes[mesh_index]
        name = _object_name(node, mesh, node_index)

        obj_want_mat = want_materials and (
            material_names is None or name in material_names
        )
        obj = _extract_object(
            name, mesh, accessors, remap_cache=remap_cache,
            want_materials=obj_want_mat, material_name_list=material_name_list,
        )
        if obj is not None:
            # Apply node world transform to vertex positions
            mat = _node_world_matrix(nodes, node_index)
            xyz1 = np.ones((obj.vertex_count, 4), dtype=np.float64)
            xyz1[:, :3] = obj.positions
            obj.positions = (mat @ xyz1.T).T[:, :3].astype(np.float32)
            objects.append(obj)

    # --- glTF Y-up → Blender Z-up conversion ---
    # glTF: X=right, Y=up, Z=toward_viewer.  Blender: X=right, Y=depth, Z=up.
    # Permutation: blender_x = gltf_x, blender_y = -gltf_z, blender_z = gltf_y
    _GLTF_TO_BLENDER = np.array([
        [1,  0,  0, 0],
        [0,  0, -1, 0],
        [0,  1,  0, 0],
        [0,  0,  0, 1],
    ], dtype=np.float64)

    for obj in objects:
        obj.positions = (
            (_GLTF_TO_BLENDER[:3, :3] @ obj.positions.T).T
        ).astype(np.float32)

    return GLBDocument(objects=objects)


class GLBSequenceReader:
    """Reads a sequence of GLB frames, caching the per-object dedup remap.

    BeamNG exports every frame with identical connectivity — only vertex
    positions move — so the expensive ``np.unique`` remap in :func:`_extract_object`
    yields the same mapping every frame. This reader computes that remap on the
    first frame it sees each object and reuses it thereafter, gathering positions
    through the cached mapping instead of re-sorting. Measured on real BeamNG data
    this cuts per-frame read cost from ~2.2s to ~0.04s after the first frame.

    Correctness is preserved for topology-changing objects (e.g. the tierod): the
    cache is keyed on the exact reference arrays, so a connectivity change misses
    the cache and recomputes — the scanner still sees the drift.

    Usage::

        reader = GLBSequenceReader()
        for frame in frames:
            doc = reader.read(frame)   # first frame slow, rest fast
    """

    def __init__(self) -> None:
        self._remap_cache: Dict[str, _RemapEntry] = {}

    @property
    def remap_cache(self) -> Dict[str, _RemapEntry]:
        """The per-object remap cache, seedable into parallel workers."""
        return self._remap_cache

    def read(
        self,
        path: Path | str,
        verbose: bool = False,
        want_materials: bool = False,
        material_names: Optional[set] = None,
    ) -> GLBDocument:
        return read_glb(
            path, remap_cache=self._remap_cache, verbose=verbose,
            want_materials=want_materials, material_names=material_names,
        )


@dataclass
class _RemapEntry:
    """Cached per-object remap so the expensive ``np.unique`` runs once.

    BeamNG exports the same connectivity every frame — only vertex *positions*
    move — so the mapping from an object's (pool, vertex) references to its
    compact local vertex set is frame-invariant. This entry stores that mapping
    plus the raw reference arrays used to validate a cache hit exactly (via
    ``array_equal``, no hashing). A miss (e.g. the topology-changing tierod)
    simply recomputes, so classification stays correct.
    """

    pools: Optional[np.ndarray]        # concat pool-accessor index per ref (or None)
    verts: Optional[np.ndarray]        # concat global vertex index per ref (or None)
    uniq: Optional[np.ndarray]         # (k, 2) unique (pool, vertex) pairs
    inverse: Optional[np.ndarray]      # ref -> local id (0..k-1)
    indexed_base: int                  # local-id count contributed by indexed part
    non_indexed_counts: Tuple[int, ...]  # vertex count of each non-indexed primitive


NO_MATERIAL = "__no_material__"


@dataclass
class _MatRefs:
    """Per-object UV + material data gathered alongside the geometry refs.

    Only built when ``want_materials`` is set. ``uv_of_ref`` is parallel to the
    concatenated indexed ``vert_of_ref`` (one UV per referenced index);
    ``non_indexed_uv`` is parallel to ``non_indexed_pos``. ``indexed_prim_mats``
    and ``non_indexed_prim_mats`` list ``(material_name, triangle_count)`` in the
    same primitive order the index/position buffers are concatenated, so
    :func:`_extract_object` can build a per-face material id aligned 1:1 with the
    final ``indices``.
    """

    uv_of_ref: Optional[np.ndarray]           # (R, 2) f32 parallel to verts, or None
    non_indexed_uv: List[np.ndarray]          # parallel to non_indexed_pos
    indexed_prim_mats: List[Tuple[str, int]]  # (name, n_faces) per indexed prim
    non_indexed_prim_mats: List[Tuple[str, int]]  # (name, n_faces) per non-indexed prim


def _material_name(prim: dict, material_name_list: List[str]) -> str:
    mi = prim.get("material")
    if mi is None or mi >= len(material_name_list):
        return NO_MATERIAL
    return material_name_list[mi]


def _prim_uv(prim: dict, accessors: _AccessorCache, n_verts: int) -> np.ndarray:
    """TEXCOORD_0 for a primitive's own vertex pool as (n_verts, 2) f32.

    Falls back to zeros when the primitive has no TEXCOORD_0 (rare, but the cache
    layout still needs a UV per vertex to stay aligned).
    """
    tc = prim["attributes"].get("TEXCOORD_0")
    if tc is None:
        return np.zeros((n_verts, 2), dtype=np.float32)
    return accessors.get(tc)[:, :2].astype(np.float32, copy=False)


def _gather_refs(
    mesh: dict,
    accessors: _AccessorCache,
    want_materials: bool = False,
    material_name_list: Optional[List[str]] = None,
) -> Tuple[Optional[np.ndarray], Optional[np.ndarray], List[np.ndarray], Optional[_MatRefs]]:
    """Collect (pool, vertex) reference arrays and non-indexed position arrays.

    This is the cheap part of extraction (accessor reads + concatenation), kept
    separate from the expensive ``np.unique`` remap so the latter can be cached.

    When *want_materials* is set, also gathers UV coords and per-primitive
    material names (returned as the fourth element); otherwise the fourth element
    is ``None`` and no TEXCOORD/material accessors are touched (the fast path used
    by the scanner and per-frame position stream).
    """
    if material_name_list is None:
        material_name_list = []
    pool_of_ref: List[np.ndarray] = []
    vert_of_ref: List[np.ndarray] = []
    non_indexed_pos: List[np.ndarray] = []

    uv_of_ref: List[np.ndarray] = []
    non_indexed_uv: List[np.ndarray] = []
    indexed_prim_mats: List[Tuple[str, int]] = []
    non_indexed_prim_mats: List[Tuple[str, int]] = []

    for prim in mesh["primitives"]:
        if prim.get("mode", _MODE_TRIANGLES) != _MODE_TRIANGLES:
            continue
        pos_index = prim["attributes"].get("POSITION")
        if pos_index is None:
            continue

        idx_accessor = prim.get("indices")
        if idx_accessor is None:
            # Non-indexed: positions are implicit triangle soup, taken as-is.
            pos = accessors.get(pos_index).astype(np.float32, copy=False)
            non_indexed_pos.append(pos)
            if want_materials:
                uv = _prim_uv(prim, accessors, pos.shape[0])
                non_indexed_uv.append(uv)
                non_indexed_prim_mats.append(
                    (_material_name(prim, material_name_list), pos.shape[0] // 3)
                )
            continue

        idx = accessors.get(idx_accessor).reshape(-1).astype(np.int64, copy=False)
        pool_of_ref.append(np.full(idx.shape, pos_index, dtype=np.int64))
        vert_of_ref.append(idx)
        if want_materials:
            pool_uv = _prim_uv(prim, accessors, accessors.get(pos_index).shape[0])
            uv_of_ref.append(pool_uv[idx])
            indexed_prim_mats.append(
                (_material_name(prim, material_name_list), idx.shape[0] // 3)
            )

    pools = np.concatenate(pool_of_ref) if pool_of_ref else None
    verts = np.concatenate(vert_of_ref) if vert_of_ref else None

    mat_refs: Optional[_MatRefs] = None
    if want_materials:
        mat_refs = _MatRefs(
            uv_of_ref=np.concatenate(uv_of_ref) if uv_of_ref else None,
            non_indexed_uv=non_indexed_uv,
            indexed_prim_mats=indexed_prim_mats,
            non_indexed_prim_mats=non_indexed_prim_mats,
        )
    return pools, verts, non_indexed_pos, mat_refs


def _extract_object(
    name: str,
    mesh: dict,
    accessors: _AccessorCache,
    remap_cache: Optional[Dict[str, _RemapEntry]] = None,
    want_materials: bool = False,
    material_name_list: Optional[List[str]] = None,
) -> Optional[GLBObject]:
    """Build one self-contained object mesh from a glTF mesh's primitives.

    Each primitive contributes triangle indices into its POSITION accessor's
    vertex pool. We collect every (pool, index) reference, then remap the used
    vertices into a compact local array so the object owns exactly the vertices
    it touches — no matter how the source shares pools between objects.

    When *remap_cache* is supplied, the ``(pool, vertex) -> local id`` remap is
    reused across frames whenever the reference arrays are byte-identical to the
    cached ones. This skips the ``np.unique`` argsort that dominates per-frame
    cost (~98%); positions are simply gathered through the cached mapping.

    When *want_materials* is set, ``uvs``, ``material_names`` and
    ``face_material_ids`` are also populated (see :class:`GLBObject`). UV/material
    gathering is cheap and independent of the remap cache, so it runs every call.
    """
    pools, verts, non_indexed_pos, mat_refs = _gather_refs(
        mesh, accessors, want_materials=want_materials,
        material_name_list=material_name_list,
    )

    if pools is None and not non_indexed_pos:
        return None

    # --- Resolve the remap (cached when possible) -----------------------
    cached = remap_cache.get(name) if remap_cache is not None else None
    hit = (
        cached is not None
        and _refs_match(cached.pools, pools)
        and _refs_match(cached.verts, verts)
    )
    if hit:
        entry = cached
    else:
        if pools is not None:
            # A (pool, vertex) pair uniquely identifies a source vertex. Dedupe
            # to the compact set this object uses, remapping refs to local ids.
            keys = np.stack([pools, verts], axis=1)
            uniq, inverse = np.unique(keys, axis=0, return_inverse=True)
            indexed_base = int(uniq.shape[0])
        else:
            uniq = inverse = None
            indexed_base = 0
        entry = _RemapEntry(
            pools=pools,
            verts=verts,
            uniq=uniq,
            inverse=inverse,
            indexed_base=indexed_base,
            non_indexed_counts=tuple(p.shape[0] for p in non_indexed_pos),
        )
        if remap_cache is not None:
            remap_cache[name] = entry

    # --- Gather positions + build local index buffer --------------------
    parts_pos: List[np.ndarray] = []
    parts_idx: List[np.ndarray] = []
    parts_uv: List[np.ndarray] = []
    base = 0

    if entry.uniq is not None:
        uniq = entry.uniq
        inverse = entry.inverse.astype(np.int64).reshape(-1)
        local = np.empty((uniq.shape[0], 3), dtype=np.float32)
        for pool_index in np.unique(uniq[:, 0]):
            pool_pos = accessors.get(int(pool_index)).astype(np.float32, copy=False)
            sel = uniq[:, 0] == pool_index
            local[sel] = pool_pos[uniq[sel, 1]]
        parts_pos.append(local)
        parts_idx.append((inverse.astype(np.int32) + base).reshape(-1, 3))
        if want_materials and mat_refs is not None and mat_refs.uv_of_ref is not None:
            # One UV per deduped vertex: scatter each ref's UV to its local id.
            # BeamNG keeps a single UV per position-vertex (verified), so writes
            # to the same local id are identical — order-independent.
            local_uv = np.zeros((uniq.shape[0], 2), dtype=np.float32)
            local_uv[inverse] = mat_refs.uv_of_ref
            parts_uv.append(local_uv)
        base += uniq.shape[0]

    for i, pos in enumerate(non_indexed_pos):
        n = pos.shape[0]
        parts_pos.append(pos)
        parts_idx.append((np.arange(n, dtype=np.int32) + base).reshape(-1, 3))
        if want_materials and mat_refs is not None:
            parts_uv.append(mat_refs.non_indexed_uv[i])
        base += n

    positions = np.concatenate(parts_pos, axis=0)
    indices = np.concatenate(parts_idx, axis=0) if parts_idx else None

    uvs = None
    material_names = None
    face_material_ids = None
    if want_materials and mat_refs is not None:
        uvs = np.concatenate(parts_uv, axis=0) if parts_uv else \
            np.zeros((positions.shape[0], 2), dtype=np.float32)
        material_names, face_material_ids = _build_face_materials(
            mat_refs.indexed_prim_mats + mat_refs.non_indexed_prim_mats
        )

    return GLBObject(
        name=name, positions=positions, indices=indices,
        uvs=uvs, material_names=material_names,
        face_material_ids=face_material_ids,
    )


def _build_face_materials(
    prim_mats: List[Tuple[str, int]]
) -> Tuple[List[str], np.ndarray]:
    """Turn ``[(material_name, n_faces), ...]`` (in face-build order) into a
    first-seen-ordered unique name list plus a per-face uint16 id array.
    """
    names: List[str] = []
    index_of: Dict[str, int] = {}
    id_runs: List[np.ndarray] = []
    for mat_name, n_faces in prim_mats:
        local = index_of.get(mat_name)
        if local is None:
            local = len(names)
            index_of[mat_name] = local
            names.append(mat_name)
        id_runs.append(np.full(n_faces, local, dtype=np.uint16))
    face_ids = (
        np.concatenate(id_runs) if id_runs else np.empty(0, dtype=np.uint16)
    )
    return names, face_ids


def _refs_match(a: Optional[np.ndarray], b: Optional[np.ndarray]) -> bool:
    """True if two reference arrays are byte-identical (both None counts)."""
    if a is None or b is None:
        return a is None and b is None
    return a.shape == b.shape and np.array_equal(a, b)
