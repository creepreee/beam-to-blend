from __future__ import annotations

"""Blender-side mesh creation and per-frame vertex updates.

Creates each stable object's mesh **once** from the cache's base indices and
first-frame positions, then rewrites vertex coordinates in place on frame
change via ``foreach_set("co", ...)``.  Dynamic (topology-changing) objects
get per-frame full mesh rebuilds from the cache's dynamic section.

Supports optional **chunked playback**: merge multiple source objects into a
single mesh to reduce per-frame ``foreach_set`` calls and GPU buffer uploads.
Individual objects are kept in a hidden source collection for editing; chunk
meshes live in a visible playback collection.
"""
from pathlib import Path

from typing import Dict, List, Optional, Tuple

import numpy as np

from .cache_reader import CacheReader
from .tyre_deform import TyreSettings, flatten_tyre, height_basis_from_transform

try:
    import bpy
    import mathutils
    _IDENTITY_4X4 = mathutils.Matrix.Identity(4)
except ImportError:
    bpy = None
    _IDENTITY_4X4 = None

try:
    import bmesh as _bmesh_mod
except ImportError:
    _bmesh_mod = None


# ---------------------------------------------------------------------------
#  Chunk map helpers
# ---------------------------------------------------------------------------

def _name_contains(needle: str) -> str:
    """Return a matcher that checks if a cache-object name contains *needle*."""
    return needle


# Default chunk map for the Gavril E180 (flanje_e180_*).
# Override by passing your own dict to CachePlayback.
CHUNK_MAP_E180: Dict[str, List[str]] = {
    "body": ["flanje_e180_body"],
    "hood": ["flanje_e180_hood"],
    "trunk": ["flanje_e180_trunk", "flanje_e180_taillight_trunk"],
    "door_FL": ["flanje_e180_door_FL"],
    "door_FR": ["flanje_e180_door_FR"],
    "door_RL": ["flanje_e180_door_RL"],
    "door_RR": ["flanje_e180_door_RR"],
    "wheels": [
        "flanje_e180_wheel", "flanje_e180_wheelD", "flanje_e180_wheelDD",
        "flanje_e180_wheelDDD",
        "flanje_e180_wheelcap", "flanje_e180_wheelcapD", "flanje_e180_wheelcapDD",
        "flanje_e180_wheelcapDDD",
        "tire_01a_16x7_26", "tire_01a_16x7_26D", "tire_01a_16x7_26DD",
        "tire_01a_16x7_26DDD",
        "brake_disc_plain", "brake_disc_plainD",
        "brake_disc_solid", "brake_disc_solidD",
        "brake_caliper_standard_plain", "brake_caliper_standard_plainD",
        "brake_caliper_standard_plainDD", "brake_caliper_standard_plainDDD",
        "brake_hub_5l", "brake_hub_5lD", "brake_hub_5lDD", "brake_hub_5lDDD",
    ],
    "glass": [
        "flanje_e180_windshield",
        "flanje_e180_backlight",
        "flanje_e180_doorglass_FL", "flanje_e180_doorglass_FR",
        "flanje_e180_doorglass_RL", "flanje_e180_doorglass_RR",
        "flanje_e180_taillight_trunkglass",
        "flanje_e180_taillightglass_L", "flanje_e180_taillightglass_R",
        "flanje_e180_headlightglass_L", "flanje_e180_headlightglass_L_intense",
        "flanje_e180_headlightglass_R", "flanje_e180_headlightglass_R_intense",
    ],
    "lights": [
        "flanje_e180_taillight_L", "flanje_e180_taillight_R",
        "flanje_e180_headlight_L", "flanje_e180_headlight_R",
        "flanje_e180_headlight_L_bake", "flanje_e180_headlight_R_bake",
    ],
    "fenders_bumpers": [
        "flanje_e180_fender_L", "flanje_e180_fender_R",
        "flanje_e180_bumper_F", "flanje_e180_bumper_R",
        "flanje_e180_bumperbar_F",
        "flanje_e180_mirror_L", "flanje_e180_mirror_R",
    ],
    "engine_bay": [
        "flanje_e180_engbaycrap", "flanje_e180_underbody",
        "flanje_e180_subframe_F", "flanje_e180_subframe_R",
        "flanje_e180_fueltank", "flanje_e180_exhaust_R",
        "flanje_e180_transmission_awd", "flanje_e180_brace",
    ],
    "suspension": [
        "flanje_e180_coilover_R", "flanje_e180_strut_F",
        "flanje_e180_lowerarm_F", "flanje_e180_lowerarm_R",
        "flanje_e180_upperarm_R", "flanje_e180_swaybar_F",
        "flanje_e180_swaybar_R", "flanje_e180_halfshaft_F",
        "flanje_e180_hub_F", "flanje_e180_hub_R",
        "flanje_e180_trailingarm_R", "flanje_e180_tierod_F",
        "licenseplateD", "licenseplateDD",
    ],
    "interior": [
        "flanje_e180_dash", "flanje_e180_steer",
        "flanje_e180_seats_FL", "flanje_e180_seats_FR", "flanje_e180_seats_R",
        "flanje_e180_shifter_knob", "flanje_e180_shifter_A",
        "flanje_e180_shifter_boot", "flanje_e180_signalstalk",
        "flanje_e180_gaspedal", "flanje_e180_brakepedal",
        "flanje_e180_brakepedalD",
        "flanje_e180_needle_temp", "flanje_e180_needle_fuel",
        "flanje_e180_needle_tacho", "flanje_e180_needle_speedo",
        "flanje_e180_decals_gauges",
    ],
}


def _write_positions(mesh: "bpy.types.Mesh", flat: np.ndarray,
                     obj: "bpy.types.Object" = None) -> None:
    """Overwrite a mesh's vertex positions from a flat float32 array.

    In Blender 4.x the legacy ``mesh.vertices.foreach_set("co", ...)`` path is
    ~120x slower than writing the ``position`` attribute directly (measured:
    110 ms vs 0.9 ms/frame for the E180 at 550K verts).  Prefer the attribute
    API; fall back to the legacy path on older Blender that lacks it.

    After writing positions, ``mesh.update()`` recomputes normals, edges, and
    the bounding box so that faces are visible from all camera angles
    (prevents clipping / disappearing faces on rotated views).  The depsgraph
    flags ensure modifier stacks (Weighted Normal, etc.) also re-evaluate.
    """
    attr = mesh.attributes.get("position")
    if attr is not None:
        attr.data.foreach_set("vector", flat)
    else:  # pragma: no cover - only on pre-4.x Blender
        mesh.vertices.foreach_set("co", flat)

    mesh.update()  # recompute normals + edge data + bbox
    mesh.update_tag()  # notify depsgraph that mesh data changed
    if obj is not None:
        obj.update_tag()  # force modifier stack re-evaluation


def _finalize_mesh(mesh: "bpy.types.Mesh") -> None:
    """Post-process: ensure faces visible, material exists, and normals split.

    **2026-07-14 critical finding:** ``from_pydata`` with its default
    ``shade_flat=True`` marks every face with the ``sharp_face`` attribute,
    which in Blender 4.5 can cause face fills to disappear in the viewport
    even with backface culling off.  Our fix: pass ``shade_flat=False`` so
    that ``from_pydata`` creates smooth-shaded faces without ``sharp_face``.
    Then we set ``use_smooth = True`` explicitly and let Blender's lazy
    depsgraph evaluation compute normals — no explicit ``mesh.update()``
    needed (and in fact harmful for viewport face visibility).

    **2026-07-16 tyre shading fix:**  Mark edges as sharp where the angle
    between adjacent faces exceeds a threshold (60°).  This splits the
    vertex normals at hard edges (tyre tread/sidewall boundary, panel
    seams) so Blender renders them as crisp creases instead of smoothed
    gradients.  Uses BMesh to walk edge→face adjacency and compute the
    dihedral angle — done once at import, not per frame.
    """
    import math

    for p in mesh.polygons:
        p.hide = False
    if not mesh.materials:
        mat = bpy.data.materials.get("BeamNG_Default")
        if mat is None:
            mat = bpy.data.materials.new("BeamNG_Default")
        mesh.materials.append(mat)
    mesh.update()

    # --- auto-smooth via sharp edges (split normals at hard creases) ---
    try:
        bm = _bmesh_mod.new()
        bm.from_mesh(mesh)
        bm.edges.ensure_lookup_table()
        sharp_angle = math.radians(60)
        n_sharp = 0
        for edge in bm.edges:
            if len(edge.link_faces) == 2:
                f0, f1 = edge.link_faces
                angle = f0.normal.angle(f1.normal)
                if angle > sharp_angle:
                    edge.smooth = False
                    n_sharp += 1
        if n_sharp > 0:
            bm.to_mesh(mesh)
            mesh.update()
        bm.free()
    except Exception:
        pass  # graceful fallback — smooth shading still works


def _fill_mesh_via_bmesh(mesh: "bpy.types.Mesh", pos: np.ndarray, idx: np.ndarray) -> None:
    """Rebuild an existing (cleared) mesh from Numpy data via BMesh."""
    bm = _bmesh_mod.new()
    for v in pos:
        bm.verts.new(v.tolist())
    bm.verts.ensure_lookup_table()
    for tri in idx:
        try:
            bm.faces.new([bm.verts[int(i)] for i in tri])
        except ValueError:
            pass
    bm.normal_update()
    bm.to_mesh(mesh)
    bm.free()


def _mesh_from_data(pos: np.ndarray, idx: np.ndarray, name: str = "") -> "bpy.types.Mesh":
    """Create mesh via ``from_pydata`` with ``shade_flat=False``.
    
    See ``_finalize_mesh`` docstring for the rationale.
    """
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(pos.tolist(), [], idx.tolist(), shade_flat=False)
    return mesh


def _get_or_create_material(name: str) -> "bpy.types.Material":
    """Get or create a Blender material datablock by name.

    Sharing exact-named datablocks across objects avoids .001 clones and is
    exactly what the texture operator matches on.
    """
    mat = bpy.data.materials.get(name)
    if mat is None:
        mat = bpy.data.materials.new(name)
    return mat


def _apply_uvs_and_materials(
    mesh: "bpy.types.Mesh",
    uvs: Optional[np.ndarray],
    loop_uvs: Optional[np.ndarray],
    matnames: Optional[List[str]],
    matids: Optional[np.ndarray],
) -> None:
    """Set up UV layer and material slots on *mesh*.

    *uvs* is (N,2) float32 per-vertex UVs (v3/v4 fallback).
    *loop_uvs* is (F,3,2) float32 per-face-corner UVs (v5).  When present
    these are used directly — they preserve UV seams after a position-only
    weld because each face corner carries its own UV.
    *matnames* is a list of material names for this mesh's material slots.
    *matids* is (face_count,) uint16 indices into *matnames*, one per polygon.

    glTF uses top-left UV origin; Blender uses bottom-left, so the V
    coordinate is flipped (v = 1 - v).
    """
    if loop_uvs is not None and len(mesh.loops) == loop_uvs.shape[0] * 3:
        # v5 loop UVs: expand (F,3,2) -> (F*3, 2) flat for foreach_set
        flat = loop_uvs.reshape(-1, 2).astype(np.float32)
        loop_uvs_flat = np.empty(len(mesh.loops) * 2, dtype=np.float32)
        loop_uvs_flat[0::2] = flat[:, 0]
        loop_uvs_flat[1::2] = 1.0 - flat[:, 1]
        uv_layer = mesh.uv_layers.new(name="UVMap", do_init=False)
        uv_layer.data.foreach_set("uv", loop_uvs_flat)
    elif uvs is not None and uvs.shape[0] == len(mesh.vertices):
        uv_layer = mesh.uv_layers.new(name="UVMap", do_init=False)
        loop_vidx = np.empty(len(mesh.loops), dtype=np.int32)
        mesh.loops.foreach_get("vertex_index", loop_vidx)
        loop_uvs_flat = np.empty(len(mesh.loops) * 2, dtype=np.float32)
        loop_uvs_flat[0::2] = uvs[loop_vidx, 0]
        loop_uvs_flat[1::2] = 1.0 - uvs[loop_vidx, 1]
        uv_layer.data.foreach_set("uv", loop_uvs_flat)

    if matnames:
        for mn in matnames:
            slot = mesh.materials.get(mn)
            if slot is None:
                mat = _get_or_create_material(mn)
                mesh.materials.append(mat)
        if matids is not None and len(matids) == len(mesh.polygons):
            mesh.polygons.foreach_set("material_index", matids.astype(np.int32))


def validate_chunk_map(chunk_map: Dict[str, List[str]],
                       available_names: List[str],
                       dynamic_names: Optional[set] = None) -> None:
    """Filter chunk_map to only include objects that exist in the cache.

    Objects missing from the cache are silently dropped from their chunk.
    Objects not covered by any chunk are imported individually (no merge).
    Dynamic (topology-changing) objects cannot be merged into chunks.
    Mutates *chunk_map* in place.
    """
    if dynamic_names is None:
        dynamic_names = set()
    assigned: set = set()
    missing = []
    for chunk_name, members in list(chunk_map.items()):
        filtered = []
        for m in members:
            if m in assigned:
                filtered.append(m)
                continue
            if m not in available_names:
                missing.append(m)
                continue
            assigned.add(m)
            filtered.append(m)
        if filtered:
            chunk_map[chunk_name] = filtered
        else:
            del chunk_map[chunk_name]

    if missing:
        print(f"[BeamNG] chunk map: {len(missing)} object(s) not in cache "
              f"(imported individually): {missing}")


# ---------------------------------------------------------------------------
#  CachePlayback
# ---------------------------------------------------------------------------

class CachePlayback:
    def __init__(self, reader: CacheReader,
                 collection_name: str = "BeamNG Cache",
                 log_path: Optional[str] = None,
                 chunk_map: Optional[Dict[str, List[str]]] = None,
                 tyre: Optional[TyreSettings] = None):
        if bpy is None:
            raise RuntimeError("CachePlayback requires Blender (bpy)")
        self.reader = reader
        self.collection_name = collection_name
        self.tyre = tyre if tyre is not None else TyreSettings()
        # Cache of which object names the tyre filter matches, plus the per-frame
        # height basis, so the hot path does no string work and reads the rigid
        # transform once per frame instead of once per object.
        self._is_tyre: Dict[str, bool] = {}
        self._tyre_basis: Optional[Tuple[np.ndarray, float]] = None
        self._objects: Dict[str, "bpy.types.Object"] = {}
        self._dynamic_objects: Dict[str, "bpy.types.Object"] = {}
        self._chunk_map: Optional[Dict[str, List[str]]] = chunk_map
        self._chunks: Dict[str, "bpy.types.Object"] = {}
        self._chunk_member_ranges: Dict[str, Dict[str, Tuple[int, int]]] = {}
        self._current_frame: Optional[int] = None
        self._log_fh = None
        self._transform_empty: Optional["bpy.types.Object"] = None
        if log_path:
            self._log_fh = open(Path(log_path), "w", encoding="utf-8")
            self._log("=== CachePlayback debug log ===")
            self._log(f"  frame_count: {reader.frame_count}")
            self._log(f"  objects: {reader.object_names()}")

    def _log(self, msg: str) -> None:
        if self._log_fh:
            self._log_fh.write(msg + "\n")
            self._log_fh.flush()

    def _log_bounds(self, tag: str, name: str, frame: int, pos: np.ndarray) -> None:
        if self._log_fh is None:
            return  # skip min/max over 550K verts when logging is off (hot path)
        if len(pos) == 0:
            self._log(f"  {tag}  {name:40s}  frame {frame}:  (empty)")
            return
        mn = pos.min(axis=0)
        mx = pos.max(axis=0)
        self._log(
            f"  {tag}  {name:40s}  frame {frame}:  "
            f"verts={pos.shape[0]:6d}  "
            f"x=[{mn[0]:8.3f}, {mx[0]:8.3f}]  "
            f"y=[{mn[1]:8.3f}, {mx[1]:8.3f}]  "
            f"z=[{mn[2]:8.3f}, {mx[2]:8.3f}]"
        )

    def close_log(self) -> None:
        if self._log_fh:
            self._log("=== log end ===")
            self._log_fh.close()
            self._log_fh = None

    def _log_mesh_stats(self, tag: str, name: str, mesh: "bpy.types.Mesh") -> None:
        if self._log_fh is None:
            return
        vc = len(mesh.vertices)
        fc = len(mesh.polygons)
        ec = len(mesh.edges)
        lc = len(mesh.loops)
        mc = len(mesh.materials)
        self._log(
            f"  {tag}  {name:40s}  "
            f"verts={vc:6d}  faces={fc:6d}  edges={ec:6d}  "
            f"loops={lc:6d}  mats={mc}"
        )

    def _create_transform_empty(self, collection: "bpy.types.Collection") -> None:
        """Create a parent empty that carries the rigid transform per frame.

        The builder writes a 12-float transform block (position + 3x3 matrix)
        into the BVC when the capture has a separable rigid motion (local-pool
        verts + direction-vector orientation).  All stable/dynamic meshes are
        parented to this empty so the object's motion (translation + rotation)
        is separated from the per-vertex deformation animation.
        """
        if self.reader.header.get("transform_data_offset", 0) == 0:
            self._transform_empty = None
            return
        import mathutils  # noqa: F401  (imported lazily; bpy available here)
        empty = bpy.data.objects.new(f"{self.collection_name}__root", None)
        empty.empty_display_type = "ARROWS"
        collection.objects.link(empty)
        self._transform_empty = empty

    # --- setup ---------------------------------------------------------
    def build_scene(self) -> None:
        self._log("--- build_scene ---")
        stable = list(self.reader.stable_objects())
        dynamic = list(self.reader.dynamic_objects())
        self._log(f"  total stable:  {len(stable)}")
        self._log(f"  total dynamic: {len(dynamic)}")
        for s in stable:
            self._log(f"    stable:  {s.name:40s}  vc={s.vertex_count:6d}  fc={s.face_count:6d}")

        # Create parent empty first
        collection = self._get_or_create_collection(self.collection_name)
        self._create_transform_empty(collection)

        if self._chunk_map:
            self._build_scene_chunked()
        else:
            self._build_scene_individual()

        # Log total mesh datablocks created
        total_meshes = len(bpy.data.meshes)
        self._log(f"  total mesh datablocks after build_scene: {total_meshes}")

        # Force depsgraph evaluation so the viewport sees the new meshes
        # immediately (without waiting for lazy depsgraph update).
        try:
            bpy.context.view_layer.update()
        except Exception:
            pass

    def _build_scene_individual(self) -> None:
        """Original per-object creation (no chunking)."""
        collection = self._get_or_create_collection(self.collection_name)
        self._log("  [stable objects:]")
        for cobj in self.reader.stable_objects():
            raw = self.reader.frame_positions(cobj.name, 0)
            self._log_bounds("RAW_GLTF", cobj.name, 0, raw)
            mesh = self._create_mesh(cobj.name)
            obj = bpy.data.objects.new(cobj.name, mesh)
            collection.objects.link(obj)
            self._objects[cobj.name] = obj
            if self._transform_empty:
                obj.parent = self._transform_empty

        self._log("  [dynamic objects:]")
        for cobj in self.reader.dynamic_objects():
            raw_pos, raw_idx = self.reader.frame_dynamic_geometry(cobj.name, 0)
            self._log_bounds("RAW_GLTF", cobj.name, 0, raw_pos)
            self._create_dynamic_mesh(cobj.name, collection)

        self.set_frame(0)

    def _build_scene_chunked(self) -> None:
        """Hidden source collection + visible chunked playback collection."""
        # Source layer: individual meshes (hidden, for editing/export)
        source_coll = self._get_or_create_collection(
            f"{self.collection_name} (Source)")
        source_coll.hide_viewport = True
        source_coll.hide_render = True

        self._log("  [stable objects — source layer:]")
        for cobj in self.reader.stable_objects():
            raw = self.reader.frame_positions(cobj.name, 0)
            self._log_bounds("RAW_GLTF", cobj.name, 0, raw)
            mesh = self._create_mesh(cobj.name)
            obj = bpy.data.objects.new(cobj.name, mesh)
            source_coll.objects.link(obj)
            self._objects[cobj.name] = obj
            if self._transform_empty:
                obj.parent = self._transform_empty

        # Playback layer: chunked meshes (visible)
        playback_coll = self._get_or_create_collection(self.collection_name)

        self._log("  [chunk meshes:]")
        all_names = [c.name for c in self.reader.stable_objects()]
        dynamic_names = {c.name for c in self.reader.dynamic_objects()}
        validate_chunk_map(self._chunk_map, all_names + list(dynamic_names),
                           dynamic_names=dynamic_names)

        chunked_objects: set = set()
        for chunk_name, member_names in self._chunk_map.items():
            # Skip dynamic (topology-changing) members — they are created
            # individually below and can't be part of a static chunk mesh.
            filtered = [m for m in member_names if m not in dynamic_names]
            skipped = [m for m in member_names if m in dynamic_names]
            if skipped:
                self._log(
                    f"  chunk {chunk_name!r}: skipping dynamic members {skipped}"
                )
            self._log(f"  building chunk {chunk_name!r} ({len(filtered)} objects)")
            self._build_chunk(chunk_name, filtered, playback_coll)
            chunked_objects.update(filtered)

        # Any stable object not covered by a chunk → import individually
        # in the visible collection (they only exist in the hidden source
        # collection otherwise).
        uncovered = [n for n in self._objects
                     if n not in chunked_objects and n not in dynamic_names]
        if uncovered:
            self._log(f"  [uncovered — importing individually: {len(uncovered)}]")
            for name in uncovered:
                obj = self._objects[name]
                playback_coll.objects.link(obj)
                if self._transform_empty:
                    obj.parent = self._transform_empty

        # Dynamic objects — always individual (topology-changing parts
        # can't be chunked safely).
        self._log("  [dynamic objects:]")
        for cobj in self.reader.dynamic_objects():
            raw_pos, raw_idx = self.reader.frame_dynamic_geometry(cobj.name, 0)
            self._log_bounds("RAW_GLTF", cobj.name, 0, raw_pos)
            self._create_dynamic_mesh(cobj.name, playback_coll)

        self.set_frame(0)

    def _build_chunk(self, chunk_name: str, member_names: List[str],
                     collection: "bpy.types.Collection") -> None:
        """Create one merged mesh from *member_names*."""
        all_verts: List[np.ndarray] = []
        all_faces: List[np.ndarray] = []
        all_loop_uvs: List[np.ndarray] = []
        all_mat_ids: List[np.ndarray] = []
        chunk_mat_names: List[str] = []
        chunk_mat_index_of: Dict[str, int] = {}
        ranges: Dict[str, Tuple[int, int]] = {}
        vert_offset = 0
        face_offset = 0

        for mname in member_names:
            raw = self.reader.frame_positions(mname, 0)
            pos = self._gltf_to_blender(raw)
            idx = self.reader.base_indices(mname)
            n_verts = pos.shape[0]
            n_faces = idx.shape[0]
            all_verts.append(pos)
            all_faces.append(idx + vert_offset)
            ranges[mname] = (vert_offset, vert_offset + n_verts)
            vert_offset += n_verts

            # Merge UVs: v5 loop UVs (F,3,2) when available, else expand
            # per-vertex UVs.  Always append n_faces*3*2 entries so the
            # concatenation stays aligned with the merged face/loop order.
            uvs = self.reader.base_uvs(mname)
            lu = self.reader.base_loop_uvs(mname)
            loop_uvs = np.zeros(n_faces * 3 * 2, dtype=np.float32)
            if lu is not None and lu.shape[0] == n_faces:
                # v5 loop UVs: flip V and flatten (F,3,2) -> (F*3, 2)
                flat = lu.reshape(-1, 2)
                loop_uvs[0::2] = flat[:, 0]
                loop_uvs[1::2] = 1.0 - flat[:, 1]
            elif uvs is not None and uvs.shape[0] == n_verts:
                loop_vidx = idx.reshape(-1)
                loop_uvs[0::2] = uvs[loop_vidx, 0]
                loop_uvs[1::2] = 1.0 - uvs[loop_vidx, 1]
            all_loop_uvs.append(loop_uvs)

            # Merge material ids: remap local material ids into chunk-level ids
            matnames = self.reader.base_material_names(mname)
            matids = self.reader.base_material_ids(mname)
            if matnames and len(matids) == n_faces:
                remapped = np.empty(n_faces, dtype=np.uint16)
                for local_i, mn in enumerate(matnames):
                    chunk_i = chunk_mat_index_of.get(mn)
                    if chunk_i is None:
                        chunk_i = len(chunk_mat_names)
                        chunk_mat_names.append(mn)
                        chunk_mat_index_of[mn] = chunk_i
                    remapped[matids == local_i] = chunk_i
                all_mat_ids.append(remapped)
            else:
                all_mat_ids.append(np.zeros(n_faces, dtype=np.uint16))
            face_offset += n_faces

        verts = np.concatenate(all_verts, axis=0)
        faces = np.concatenate(all_faces, axis=0)

        mesh = _mesh_from_data(verts, faces, chunk_name)

        # Apply the merged material slots + per-face ids and per-loop UVs that we
        # accumulated above.  Without this the chunk mesh has no materials and
        # _finalize_mesh falls back to a single "BeamNG_Default" slot — the
        # long-standing "chunk collection has no material slots" bug (the hidden
        # Source collection had them because the individual path applies them).
        if chunk_mat_names and all_mat_ids:
            merged_matids = np.concatenate(all_mat_ids, axis=0)
            for mn in chunk_mat_names:
                if mesh.materials.get(mn) is None:
                    mesh.materials.append(_get_or_create_material(mn))
            if len(merged_matids) == len(mesh.polygons):
                mesh.polygons.foreach_set(
                    "material_index", merged_matids.astype(np.int32))
        if all_loop_uvs:
            merged_loop_uvs = np.concatenate(all_loop_uvs, axis=0)
            if len(merged_loop_uvs) == len(mesh.loops) * 2:
                uv_layer = mesh.uv_layers.new(name="UVMap", do_init=False)
                uv_layer.data.foreach_set("uv", merged_loop_uvs)

        _finalize_mesh(mesh)

        obj = bpy.data.objects.new(chunk_name, mesh)
        collection.objects.link(obj)
        self._chunks[chunk_name] = obj
        self._chunk_member_ranges[chunk_name] = ranges
        if self._transform_empty:
            obj.parent = self._transform_empty

        total_k = vert_offset / 1000
        n_members = len(member_names)
        self._log(
            f"  chunk {chunk_name}: {total_k:.0f}K verts, "
            f"{n_members} members"
        )

    def _get_or_create_collection(self, name: str) -> "bpy.types.Collection":
        coll = bpy.data.collections.get(name)
        if coll is None:
            coll = bpy.data.collections.new(name)
            bpy.context.scene.collection.children.link(coll)
        return coll

    @staticmethod
    def _gltf_to_blender(pos: np.ndarray) -> np.ndarray:
        """No-op — BVC positions are already in Blender Z-up space.

        For capture BVCs: the builder's vehicle rotation maps pool Y-up
        to world Z-up (= Blender Z-up).
        For GLB BVCs: the builder applies glTF→Blender conversion at write time.
        """
        return pos

    def _create_mesh(self, name: str) -> "bpy.types.Mesh":
        raw = self.reader.frame_positions(name, 0)
        positions = self._gltf_to_blender(raw)
        faces = self.reader.base_indices(name)
        mesh = _mesh_from_data(positions, faces, name)
        # Apply UVs + per-face material slots so the part is ready for texture
        # assignment in Blender (individual/non-chunked path).  Guarded so a
        # cache without material data still imports cleanly.
        try:
            uvs = self.reader.base_uvs(name)
            loop_uvs = self.reader.base_loop_uvs(name)
            matnames = self.reader.base_material_names(name)
            matids = self.reader.base_material_ids(name)
            _apply_uvs_and_materials(mesh, uvs, loop_uvs, matnames, matids)
        except Exception as exc:  # pragma: no cover - defensive
            self._log(f"  UV/material apply skipped for {name}: {exc}")
        _finalize_mesh(mesh)
        self._log_mesh_stats("CREATE", name, mesh)
        return mesh

    def _create_dynamic_mesh(self, name: str, collection: "bpy.types.Collection") -> None:
        positions, indices = self.reader.frame_dynamic_geometry(name, 0)
        mesh = bpy.data.meshes.new(name)
        if len(positions) > 0:
            pos_blender = self._gltf_to_blender(positions.copy())
            mesh = _mesh_from_data(pos_blender, indices, name)
            _finalize_mesh(mesh)
        else:
            self._log(f"  DYNAMIC SKIP {name:40s}  (empty)")
        obj = bpy.data.objects.new(name, mesh)
        collection.objects.link(obj)
        self._dynamic_objects[name] = obj
        if self._transform_empty:
            obj.parent = self._transform_empty
        if len(positions) == 0:
            obj.hide_viewport = True
            obj.hide_render = True
        self._log_mesh_stats("DYNCREATE", name, mesh)

    def _apply_transform(self, frame: int) -> None:
        """Animate the parent empty from per-frame transform data.

        Transform = position(3) + 3x3 orientation matrix(9, row-major).  The
        matrix is the FINAL world matrix computed in the builder (proven
        calibrator basis: x=forward, z=up, y=z x x).  It maps directly to the
        Blender matrix_basis — no flip, no PCA, no quaternion guessing.
        """
        if self._transform_empty is None:
            return
        tf = self.reader.frame_transform(frame)
        if tf is None:
            return
        px, py, pz = float(tf[0]), float(tf[1]), float(tf[2])
        # The 12-float block is [px,py,pz, m00,m01,m02, m10,m11,m12, m20,m21,m22]
        # row-major -> directly the Blender Matrix.
        m = tf[3:12].reshape(3, 3).astype(np.float64)

        mat = mathutils.Matrix((
            (m[0, 0], m[0, 1], m[0, 2], px),
            (m[1, 0], m[1, 1], m[1, 2], py),
            (m[2, 0], m[2, 1], m[2, 2], pz),
            (0.0, 0.0, 0.0, 1.0),
        ))
        self._transform_empty.matrix_basis = mat
        self._transform_empty.rotation_mode = "QUATERNION"

    # --- tyre ground-contact deformation -------------------------------
    def set_tyre_settings(self, tyre: TyreSettings) -> None:
        """Replace the tyre tunables and force the next set_frame to redraw.

        Called from the add-on's property callbacks so dragging a slider updates
        the viewport live.  The name filter may have changed, so the match cache
        is dropped too.
        """
        self.tyre = tyre
        self._is_tyre.clear()
        self._current_frame = None  # defeat the "same frame, return early" guard

    def _tyre_match(self, name: str) -> bool:
        hit = self._is_tyre.get(name)
        if hit is None:
            hit = self.tyre.matches(name)
            self._is_tyre[name] = hit
        return hit

    def _update_tyre_basis(self, frame: int) -> None:
        """Recompute the local→world-height basis for *frame* (once per frame).

        The meshes' vertices live in the cache's space; the parent empty (when
        present) carries the rigid transform, and the importer's auto-ground adds
        a Z shift on each object.  Both have to be folded in before we can ask
        "how far is this vertex above the ground".
        """
        if not self.tyre.enabled:
            self._tyre_basis = None
            return
        tf = self.reader.frame_transform(frame)
        up, offset = height_basis_from_transform(tf, ground_z=self.tyre.ground_z)
        self._tyre_basis = (up, offset)

    def _deform_tyre(self, name: str, obj: "bpy.types.Object",
                     pos: np.ndarray, frame: int) -> np.ndarray:
        """Apply ground-contact flattening to *pos* if *name* is a tyre."""
        if self._tyre_basis is None or not self._tyre_match(name):
            return pos
        up, offset = self._tyre_basis
        if obj is not None:
            # The auto-ground shift (and any manual move) lives on the object,
            # outside the vertex data — include it in the height measurement.
            offset += float(np.asarray(obj.location, dtype=np.float32) @ up)
        out, squash = flatten_tyre(pos, up, offset, self.tyre)
        if squash > 0.0:
            self._log(f"  TYRE {name:40s}  frame {frame}:  squash={squash*1000:.1f} mm")
        return out

    # --- per-frame update ---------------------------------------------
    def set_frame(self, frame: int) -> None:
        frame = max(0, min(frame, self.reader.frame_count - 1))
        self._log(f"--- set_frame({frame})  current={self._current_frame} ---")
        if frame == self._current_frame:
            self._log("  (no change, returning early)")
            return

        if self._chunk_map:
            self._set_frame_chunked(frame)
        else:
            self._set_frame_individual(frame)

        self._current_frame = frame

    def _set_frame_individual(self, frame: int) -> None:
        """Original per-object position update (96 calls)."""
        logging = self._log_fh is not None
        self._apply_transform(frame)
        self._update_tyre_basis(frame)
        for name, obj in self._objects.items():
            raw = self.reader.frame_positions(name, frame)
            positions = self._gltf_to_blender(raw)
            positions = self._deform_tyre(name, obj, positions, frame)
            flat = np.ascontiguousarray(positions, dtype=np.float32).reshape(-1)
            _write_positions(obj.data, flat, obj)

            # Diagnostic read-back: only when logging is enabled.  Reading all
            # ~550K verts back with foreach_get every frame is a large hidden
            # per-frame cost that both slows playback and makes fps uneven, so
            # it MUST stay behind the log guard (the hot path skips it).
            if logging:
                self._log_bounds("RAW_GLTF", name, frame, raw)
                self._log_bounds("AFTER_XFORM", name, frame, positions)
                got = np.empty(len(obj.data.vertices) * 3, dtype=np.float32)
                obj.data.vertices.foreach_get("co", got)
                self._log_bounds("BLENDER", name, frame, got.reshape(-1, 3))

        for name, obj in self._dynamic_objects.items():
            positions, indices = self.reader.frame_dynamic_geometry(name, frame)
            if logging:
                self._log_bounds("RAW_GLTF", name, frame, positions)
            if len(positions) == 0:
                obj.hide_viewport = True
                obj.hide_render = True
                self._log(f"  {name}: hidden (empty)")
                continue
            obj.hide_viewport = False
            obj.hide_render = False
            mesh = obj.data
            existing_vc = len(mesh.vertices)
            new_vc = positions.shape[0]
            existing_fc = len(mesh.polygons)
            new_fc = indices.shape[0]
            pos_blender = self._gltf_to_blender(positions.copy())
            pos_blender = self._deform_tyre(name, obj, pos_blender, frame)
            if existing_vc != new_vc or existing_fc != new_fc:
                mesh.clear_geometry()
                _fill_mesh_via_bmesh(mesh, pos_blender, indices)
                _finalize_mesh(mesh)
                mesh.update_tag()
                obj.update_tag()
            else:
                flat = np.ascontiguousarray(pos_blender, dtype=np.float32).reshape(-1)
                _write_positions(mesh, flat, obj)

            if logging:
                self._log_bounds("AFTER_XFORM", name, frame, pos_blender)
                got = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
                mesh.vertices.foreach_get("co", got)
                self._log_bounds("BLENDER", name, frame, got.reshape(-1, 3))

    def _set_frame_chunked(self, frame: int) -> None:
        """Update chunk meshes (one foreach_set per chunk)."""
        self._apply_transform(frame)
        self._update_tyre_basis(frame)
        for chunk_name, obj in self._chunks.items():
            member_ranges = self._chunk_member_ranges[chunk_name]
            parts = []
            for mname, (start, end) in member_ranges.items():
                raw = self.reader.frame_positions(mname, frame)
                pos = self._gltf_to_blender(raw)
                # Deform per MEMBER, not per chunk: each tyre needs its own axle
                # axis and its own contact depth, which a merged 'wheels' chunk
                # would smear across all four wheels.  The chunk object carries
                # the auto-ground offset for its members.
                pos = self._deform_tyre(mname, obj, pos, frame)
                parts.append(pos)
            combined = np.concatenate(parts, axis=0)

            self._log_bounds("CHUNK", chunk_name, frame, combined)
            flat = np.ascontiguousarray(combined, dtype=np.float32).reshape(-1)
            _write_positions(obj.data, flat, obj)

        for name, obj in self._dynamic_objects.items():
            positions, indices = self.reader.frame_dynamic_geometry(name, frame)
            self._log_bounds("RAW_GLTF", name, frame, positions)
            if len(positions) == 0:
                obj.hide_viewport = True
                obj.hide_render = True
                self._log(f"  {name}: hidden (empty)")
                continue
            obj.hide_viewport = False
            obj.hide_render = False
            mesh = obj.data
            existing_vc = len(mesh.vertices)
            new_vc = positions.shape[0]
            existing_fc = len(mesh.polygons)
            new_fc = indices.shape[0]
            pos_blender = self._deform_tyre(
                name, obj, self._gltf_to_blender(positions.copy()), frame)
            if existing_vc != new_vc or existing_fc != new_fc:
                mesh.clear_geometry()
                _fill_mesh_via_bmesh(mesh, pos_blender, indices)
                _finalize_mesh(mesh)
                mesh.update_tag()
                obj.update_tag()
            else:
                self._log_bounds("AFTER_XFORM", name, frame, pos_blender)
                flat = np.ascontiguousarray(pos_blender, dtype=np.float32).reshape(-1)
                _write_positions(mesh, flat, obj)
