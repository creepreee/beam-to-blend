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

import math

from typing import Dict, List, Optional, Tuple

import numpy as np

from .cache_reader import CacheReader
from .glass_shatter import rim_mask_and_anchors
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

# Number of trailing cache frames used to fit the car's residual oscillation for
# the smooth-stop tail.  ~2.5 periods of the ~10-frame settle rock.
_TAIL_FIT_WINDOW = 24
# Period grid (cache frames) searched for the dominant oscillation.
_TAIL_PERIOD_GRID = np.linspace(4.0, 30.0, 40)
# Per-frame amplitude below which a degree of freedom counts as "already at
# rest" (metres for translation, radians for rotation).
_TAIL_AMPLITUDE_EPS = 1e-4
# How many members to sample when measuring the swing frequency from vertex data
# (only used for caches with no rigid transform block).  A handful is plenty —
# every part of the car rocks at the same frequency.
_TAIL_VFIT_OMEGA_MEMBERS = 4


def _tail_envelope(u: float) -> float:
    """Amplitude taper across the tail: raised cosine ``cos²(πu/2)``.

    This scales the amplitude of the CONTINUED oscillation, so the car keeps
    rocking through its rest centre the whole way down and each successive swing
    is a little smaller than the last::

        <-------->, <------->, <----->, <--->, <->, rest

    Properties that matter, and why this shape rather than the previous
    ``(1-u)·e^{-λu}``:

    * ``env(0) = 1`` — the tail opens at the car's CURRENT full swing amplitude,
      so there is no amplitude step at the seam.
    * ``env'(0) = 0`` — the taper starts flat, so the decay eases in instead of
      the amplitude falling off a cliff on the first tail frame (the old
      envelope's slope at ``u=0`` was ``-(1+λ) = -2.5``, which read as a sudden
      stop even when the swing itself was continued correctly).
    * ``env(1) = 0`` and ``env'(1) = 0`` — it reaches exact rest with zero
      velocity, so the settle lands softly rather than being clipped.
    * Evenly spaced swings shrink by similar-looking steps (1.00, 0.96, 0.85,
      0.69, 0.50, 0.31, 0.15, 0.04, 0.00 over eight half-periods), which is the
      "true smooth" taper — not most of the decay crammed into the first swing.
    """
    if u <= 0.0:
        return 1.0
    if u >= 1.0:
        return 0.0
    return math.cos(0.5 * math.pi * u) ** 2


def _fit_sine_at(values: np.ndarray, omega: float,
                 eps: Optional[float] = None) -> Optional[np.ndarray]:
    """Least-squares ``c + Ac·cos(ωn) + As·sin(ωn)`` on the sample index grid.

    Returns the 3-vector ``(c, Ac, As)``, or ``None`` when the series is too
    short or (with ``eps``) has no variance above the noise threshold.
    """
    values = np.asarray(values, dtype=np.float64)
    if values.ndim != 1 or len(values) < 8:
        return None
    if eps is not None and np.ptp(values) < eps:
        return None
    n = np.arange(len(values), dtype=np.float64)
    x = np.column_stack([np.ones_like(n), np.cos(omega * n), np.sin(omega * n)])
    coef, *_ = np.linalg.lstsq(x, values, rcond=None)
    return coef  # (c, Ac, As)


def _average_quaternion(qs):
    """Mean orientation of a sequence of quaternions (sign-aligned average).

    Returns ``mathutils.Quaternion`` or ``None`` when the input is empty.
    """
    if not qs:
        return None
    base = qs[0]
    w = x = y = z = 0.0
    for q in qs:
        # Align signs so antipodal quaternions (same rotation) don't cancel.
        if q.w * base.w + q.x * base.x + q.y * base.y + q.z * base.z < 0:
            q = mathutils.Quaternion((-q.w, -q.x, -q.y, -q.z))
        w += q.w
        x += q.x
        y += q.y
        z += q.z
    out = mathutils.Quaternion((w, x, y, z))
    out.normalize()
    return out


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
        #: Per-member FACE slices of each chunk mesh, ``{chunk: {member:
        #: (start, end)}}``.  Vertex ranges alone cannot address a member's
        #: polygons, and assigning a material to one pane inside a merged mesh
        #: is a per-FACE operation — see :func:`debris_spawn._apply_glass_crack`.
        self._chunk_member_faces: Dict[str, Dict[str, Tuple[int, int]]] = {}
        self._current_frame: Optional[float] = None
        self._log_fh = None
        self._transform_empty: Optional["bpy.types.Object"] = None
        #: Smooth-stop tail, in CACHE-frame units.  0 disables the settle: the
        #: timeline ends exactly on the last captured frame and the car freezes
        #: there.  When > 0, cache positions past the seam continue the whole
        #: car's fitted oscillation (rigid pose AND vertex deformation) with a
        #: shrinking amplitude, reaching a full rest ``tail`` cache-frames after
        #: the seam — no more instant stop.
        self._smooth_stop_tail: float = 0.0
        #: Seam (cache frame) at which the smooth-stop glide ONSET begins.
        #: ``None`` means the default: the seam is the last captured frame
        #: (``frame_count - 1``).  When set to an earlier frame the car settles
        #: from that point instead, cutting the remaining captured motion in
        #: favour of the damped continuation.  Only meaningful with a non-zero
        #: ``_smooth_stop_tail``.
        self._smooth_stop_start: Optional[float] = None
        #: Fitted sine continuation of the ROOT motion for the tail (see
        #: :meth:`_compute_tail_fit`), or ``None`` when disabled/unfittable.
        self._tail_fit: Optional[Dict] = None
        #: Fitted per-VERTEX sine continuation for the tail (see
        #: :meth:`_compute_tail_vfit`), or ``None`` when disabled/unfittable.
        self._tail_vfit: Optional[Dict] = None
        # Panes that have shattered: {member_name: cache_frame_it_broke}.  From
        # that frame on the member's vertices are collapsed to a point so the
        # intact glass disappears from the car and the spawned fragments take
        # over.  See :meth:`set_shattered_panes`.
        self._shattered: Dict[str, int] = {}
        #: Cache-local collapse target (pane centroid at its shatter frame).
        self._shattered_centres: Dict[str, np.ndarray] = {}
        #: Per-vertex rim keep-mask for each shattered member, ``{name: (N,)
        #: bool}`` — True where the vertex stays welded to the pane.  The rim
        #: band IS the fringe: it keeps animating with the pane's vertex cache,
        #: so it follows the deforming aperture where a parented fragment could
        #: only ride the wreck's rigid transform.
        self._shattered_keeps: Dict[str, np.ndarray] = {}
        #: Glue-anchor index per vertex for each shattered member's INTERIOR
        #: (``keep`` False) rows.  Each interior vertex is pulled to its anchor
        #: RIM vertex's CURRENT-frame position, so the collapsed faces stay
        #: degenerate along the break edge even while the pane keeps deforming —
        #: a static collapse target would stretch metres off the moving rim.
        self._shattered_anchors: Dict[str, np.ndarray] = {}
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
        face_ranges: Dict[str, Tuple[int, int]] = {}
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
            face_ranges[mname] = (face_offset, face_offset + n_faces)
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
        self._chunk_member_faces[chunk_name] = face_ranges
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

    def _matrix_from_transform(self, tf: np.ndarray) -> "mathutils.Matrix":
        """Build the Blender matrix_basis from a 12-float BVC transform block."""
        px, py, pz = float(tf[0]), float(tf[1]), float(tf[2])
        # The 12-float block is [px,py,pz, m00,m01,m02, m10,m11,m12, m20,m21,m22]
        # row-major -> directly the Blender Matrix.
        m = tf[3:12].reshape(3, 3).astype(np.float64)
        return mathutils.Matrix((
            (m[0, 0], m[0, 1], m[0, 2], px),
            (m[1, 0], m[1, 1], m[1, 2], py),
            (m[2, 0], m[2, 1], m[2, 2], pz),
            (0.0, 0.0, 0.0, 1.0),
        ))

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
        self._transform_empty.matrix_basis = self._matrix_from_transform(tf)
        self._transform_empty.rotation_mode = "QUATERNION"

    def set_smooth_stop(self, tail_cache: float, start_cache: float = 0.0) -> None:
        """Configure the smooth-stop tail (in cache frames; 0 disables).

        A non-zero tail lets timeline positions past the SEAM continue the car's
        residual SWING (the damped oscillation it is still rocking through when
        the smooth stop begins) and ease it to rest — instead of freezing it
        mid-pose or sliding it rigidly to a stop.  Called live from the panel
        checkbox/field (no re-import).

        ``start_cache`` is the seam in cache-frame units: the smooth stop begins
        *here* instead of at the end of the captured sequence.  ``0`` (or any
        value past the last frame) leaves the seam at ``frame_count - 1`` — the
        default behaviour.  Setting it earlier cuts the captured animation short
        and starts the settle from that point.

        The oscillation (frequency, amplitude, phase) is fitted from the last
        ``_TAIL_FIT_WINDOW`` captured frames at-or-before the seam, so the tail
        continues whatever the car is actually doing.  A car that is genuinely at
        rest at the seam (no measurable residual motion) gets a still tail — there
        is nothing to settle.

        The VERTEX deformation is fitted the same way, per vertex per axis
        (:meth:`_compute_tail_vfit`), rather than extrapolated from the last
        inter-frame step.  See that method for why the step is not usable.
        """
        self._smooth_stop_tail = max(0.0, float(tail_cache))
        if self._smooth_stop_tail > 0.0 and start_cache > 0.0:
            seam = min(start_cache, self.reader.frame_count - 1)
            self._smooth_stop_start = max(0.0, float(seam))
        else:
            self._smooth_stop_start = None
        if self._smooth_stop_tail > 0.0:
            self._tail_fit = self._compute_tail_fit()
            self._tail_vfit = self._compute_tail_vfit(self._tail_omega())
        else:
            self._tail_fit = None
            self._tail_vfit = None
        self._current_frame = None  # defeat the "same frame, return early" guard

    def _tail_omega(self) -> float:
        """Angular frequency (rad / cache frame) driving the tail oscillation.

        Prefers the frequency fitted from the rigid root motion; falls back to a
        fit on the vertex data itself when the cache carries no transform block,
        so a vertex-only cache still swings at its own real frequency instead of
        an arbitrary default.
        """
        fit = self._tail_fit
        if fit is not None:
            return float(fit["omega"])
        omega = self._fit_omega_from_vertices()
        if omega is not None:
            return omega
        tail = max(2.0, min(10.0, self._smooth_stop_tail or 10.0))
        return 2.0 * math.pi / tail

    def _tail_sample_frames(self) -> List[int]:
        """Cache frames used for every tail fit (root and vertex).

        Windowed on ``_TAIL_FIT_WINDOW`` frames ending at the smooth-stop seam
        — which is ``frame_count - 1`` by default, or the user-specified start
        frame when one is set.
        """
        n_src = self.reader.frame_count
        seam = self._smooth_stop_start
        if seam is None or seam >= n_src - 1:
            start = max(0, n_src - _TAIL_FIT_WINDOW)
            return list(range(start, n_src))
        start = max(0, int(seam) - _TAIL_FIT_WINDOW)
        end = min(n_src, int(seam) + 1)
        return list(range(start, end))

    def _tail_member_names(self) -> List[str]:
        """Every stable member whose vertices the tail has to animate."""
        if self._chunk_map:
            names: List[str] = []
            for chunk_name in self._chunks:
                names.extend(self._chunk_member_ranges[chunk_name].keys())
            return names
        return list(self._objects.keys())

    def _fit_omega_from_vertices(self) -> Optional[float]:
        """Dominant swing frequency measured from the vertex data.

        Used only when the cache has no rigid transform block.  Fits the mean
        per-frame vertex displacement magnitude (a scalar that oscillates at the
        swing frequency) over the period grid and takes the best joint SSE.
        """
        frames = self._tail_sample_frames()
        if len(frames) < 8:
            return None
        names = self._tail_member_names()[:_TAIL_VFIT_OMEGA_MEMBERS]
        series: List[np.ndarray] = []
        for name in names:
            try:
                stack = np.array(
                    [self.reader.frame_positions(name, f).astype(np.float64)
                     for f in frames])
            except ValueError:
                continue
            if stack.ndim != 3 or stack.shape[1] == 0:
                continue
            centre = stack.mean(axis=0)
            # Signed projection onto the dominant deviation direction, averaged
            # over vertices: oscillates at the swing frequency (a magnitude
            # would rectify it and double the apparent frequency).
            dev = stack - centre
            flat = dev.reshape(len(frames), -1)
            u, s, vt = np.linalg.svd(flat, full_matrices=False)
            if len(s) == 0 or s[0] <= 0:
                continue
            series.append(flat @ vt[0])
        if not series:
            return None
        best_sse, best_w = None, None
        n = np.arange(len(frames), dtype=np.float64)
        for period in _TAIL_PERIOD_GRID:
            w = 2.0 * math.pi / period
            sse = 0.0
            for col in series:
                c = _fit_sine_at(col, w)
                if c is None:
                    continue
                resid = col - (c[0] + c[1] * np.cos(w * n) + c[2] * np.sin(w * n))
                sse += float(resid @ resid)
            if best_sse is None or sse < best_sse:
                best_sse, best_w = sse, w
        return best_w

    def _compute_tail_vfit(self, omega: float) -> Optional[Dict]:
        """Fit each vertex's residual oscillation: ``c + Ac·cos(ωn) + As·sin(ωn)``.

        Returns ``{member_name: (centre, Ac, As, seam_residual)}`` with each
        value an ``(V, 3)`` float32 array, or ``None`` when nothing usable was
        fitted.

        **Why not extrapolate the last inter-frame step.**  The previous tail
        drove vertices with ``v_last + (v_last - v_prev)·k``, i.e. it inferred
        the swing's amplitude from the instantaneous velocity at capture end.
        A capture ends at an arbitrary phase, and typically near a TURNING POINT
        of the rock (measured on real data: the root is at 0.98 / -1.00 / -0.995
        of its per-axis amplitude on the final frame).  At a turning point the
        velocity is ~zero, so that estimate collapses: the true vertex swing was
        2.91 mm but ``|v_last - v_prev|/ω`` gave 0.86 mm — a 29% amplitude.  The
        result was a visible instant drop in swing size at the seam, then a
        smooth decay of the wrong, much smaller motion.  Fitting amplitude and
        phase over a whole window recovers the actual swing, so the tail opens
        at exactly the amplitude the car was already rocking at.

        The fit is a full-window least squares at the shared ``omega``, done
        vectorised over all vertices at once (3 dot products per member), so it
        costs one pass over ``_TAIL_FIT_WINDOW`` frames per member and is
        computed ONCE per ``set_smooth_stop`` rather than per frame.

        **Seam residual.**  A least-squares sine does not pass exactly through
        the final captured sample (measured: 0.14 mm mean, 0.57 mm worst — under
        the per-frame motion already present, but non-zero).  Left alone, the
        first tail frame would step by that much.  So the per-vertex residual
        ``v_last - fit(t_last)`` is stored and added back scaled by ``env``: at
        the seam it cancels exactly, and it fades out along with the swing, so
        the tail starts from precisely the pose the capture ended on.
        """
        frames = self._tail_sample_frames()
        if len(frames) < 8:
            return None
        n = np.arange(len(frames), dtype=np.float64)
        # Shared design matrix: the fit grid is identical for every member.
        basis = np.column_stack(
            [np.ones_like(n), np.cos(omega * n), np.sin(omega * n)])
        pinv = np.linalg.pinv(basis)  # (3, T)
        t_last = float(len(frames) - 1)   # fit-grid index of the final sample
        ct_l, st_l = math.cos(omega * t_last), math.sin(omega * t_last)
        out: Dict[str, Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]] = {}
        for name in self._tail_member_names():
            try:
                stack = np.array(
                    [self.reader.frame_positions(name, f).astype(np.float64)
                     for f in frames])
            except ValueError:
                continue
            if stack.ndim != 3 or stack.shape[1] == 0:
                continue
            t = len(frames)
            coef = pinv @ stack.reshape(t, -1)      # (3, V*3)
            coef = coef.reshape(3, stack.shape[1], 3)
            centre, ac, as_ = coef[0], coef[1], coef[2]
            seam = stack[-1] - (centre + ac * ct_l + as_ * st_l)
            out[name] = (centre.astype(np.float32),
                         ac.astype(np.float32),
                         as_.astype(np.float32),
                         seam.astype(np.float32))
        if not out:
            return None
        return {"omega": float(omega), "start": frames[0], "members": out}

    def _glide_positions(self, name: str, seam: int, t: float, env: float
                         ) -> Optional[np.ndarray]:
        """Vertex positions for tail time *t* (absolute fit units), or ``None``.

        Continues the fitted per-vertex oscillation with its amplitude scaled by
        *env*, plus the ``env``-scaled seam residual::

            c + (Ac·cos(ωt) + As·sin(ωt) + seam)·env

        At ``env = 1`` and ``t = t_last`` this is EXACTLY the seam pose
        (the residual cancels the fit error), so the tail starts where the
        smooth stop onset begins; at ``env = 0`` every vertex sits exactly on
        its oscillation CENTRE ``c`` — the pose the car settles into.
        """
        vfit = self._tail_vfit
        if vfit is None:
            return None
        entry = vfit["members"].get(name)
        if entry is None:
            return None
        centre, ac, as_, seam = entry
        ct = math.cos(vfit["omega"] * t)
        st = math.sin(vfit["omega"] * t)
        swing = ac * np.float32(ct) + as_ * np.float32(st) + seam
        return centre + swing * np.float32(env)

    def _compute_tail_fit(self) -> Optional[Dict]:
        """Fit the car's residual oscillation from the recent captured frames.

        Returns a dict of fitted curves (or ``None`` when there is no usable
        rigid transform / not enough frames — then the tail eases vertices with
        a default swing but no rigid continuation):

        ``omega``       shared angular frequency (cache frames, radians/frame)
        ``pos``         per-axis ``(c, Ac, As)`` for the root translation
        ``rot_mean``    mean orientation quaternion
        ``rot``         per-axis ``(c, Ac, As)`` for the rotation vector part
                        (components of ``q_n · rot_mean⁻¹``, ≈ half-angles)

        The fit window is taken from :meth:`_tail_sample_frames`, which honours
        the smooth-stop seam — defaulting to the last captured frames but
        shrinking to the frames at-or-before an earlier seam when the user
        starts the settle sooner.
        """
        frames = self._tail_sample_frames()
        if len(frames) < 8:
            return None
        start = frames[0]
        seam = frames[-1]
        tfs = [self.reader.frame_transform(f) for f in frames]
        if any(t is None for t in tfs):
            return None  # cache has no rigid transform block

        # --- shared omega from the root translation (best joint fit) ------
        p = np.array([t[0:3] for t in tfs], dtype=np.float64)
        best_sse, best_w = None, None
        for period in _TAIL_PERIOD_GRID:
            w = 2.0 * math.pi / period
            sse = 0.0
            for k in range(3):
                c = _fit_sine_at(p[:, k], w)
                n = np.arange(len(frames), dtype=np.float64)
                resid = p[:, k] - (c[0] + c[1] * np.cos(w * n) + c[2] * np.sin(w * n))
                sse += float(resid @ resid)
            if best_sse is None or sse < best_sse:
                best_sse, best_w = sse, w
        if best_w is None:
            return None

        pos_fit = {k: _fit_sine_at(p[:, k], best_w) for k in range(3)}
        amp = max((float(np.hypot(c[1], c[2])) for c in pos_fit.values()), default=0.0)

        # --- rotation continuation about the mean orientation -------------
        # The rotation offset of each frame from the mean, `q_n · rot_mean⁻¹`,
        # has a small vector part (≈ axis·angle/2) that rocks with the swing;
        # fit each component and continue it about the mean orientation.
        qs = [self._matrix_from_transform(t).to_quaternion() for t in tfs]
        q_mean = _average_quaternion(qs)
        if q_mean is None:
            return None
        offs = np.array([(lambda o: (o.x, o.y, o.z))(q * q_mean.inverted())
                         for q in qs], dtype=np.float64)
        rot_comps = [_fit_sine_at(offs[:, k], best_w, eps=1e-6)
                     for k in range(3)]
        if any(c is None for c in rot_comps) or all(
                float(np.hypot(c[1], c[2])) < _TAIL_AMPLITUDE_EPS
                for c in rot_comps):
            rot_fit = None
        else:
            rot_fit = {k: rot_comps[k] for k in range(3)}

        return {
            "omega": float(best_w),
            "start": start,
            "pos": pos_fit,
            "rot_mean": q_mean,
            "rot": rot_fit,
            "amplitude": amp,
        }

    def _glide_transform(self, s: float, env: float, seam: float
                         ) -> Optional[np.ndarray]:
        """Synthetic 12-float transform block for the shrinking swing pose.

        Continues the per-axis sine fitted from the recent frames:
        ``p = c + (Ac·cos(ω(t)) + As·sin(ω(t)))·env`` with ``t = seam + s`` in
        ABSOLUTE frame index (the fit's sample grid was ``frame - start``).
        The rotation follows the mean orientation plus the same fitted swing on
        its small vector part.  Only ``env`` decays, so the car keeps rocking to
        BOTH sides of its rest centre all the way down.  At ``env = 0`` (the end
        of the tail) this is the oscillation CENTRE: the pose the car rocks
        through and settles at, which is exactly where the swing is heading.
        """
        fit = self._tail_fit
        if fit is None:
            # No usable continuation (cache without transform data, or the car
            # is genuinely at rest) — hold the seam rigid pose.
            return self.reader.frame_transform(int(seam))
        omega = fit["omega"]
        start = fit["start"]
        t = (seam - start) + s          # absolute frame index in fit units
        ct, st = math.cos(omega * t), math.sin(omega * t)
        pos = np.empty(3, dtype=np.float64)
        for k in range(3):
            c, ac, as_ = fit["pos"][k]
            pos[k] = c + (ac * ct + as_ * st) * env

        q_mean = fit["rot_mean"]
        if fit["rot"] is None or q_mean is None:
            q = q_mean if q_mean is not None else mathutils.Quaternion()
        else:
            vx = vy = vz = 0.0
            for k, comp in enumerate((fit["rot"][0], fit["rot"][1], fit["rot"][2])):
                c, ac, as_ = comp
                val = c + (ac * ct + as_ * st) * env
                if k == 0:
                    vx = val
                elif k == 1:
                    vy = val
                else:
                    vz = val
            q = (q_mean * mathutils.Quaternion((1.0, vx, vy, vz))).normalized()

        m = np.asarray(q.to_matrix(), dtype=np.float64).reshape(9)
        return np.concatenate([pos, m]).astype(np.float64)

    # --- glass shatter ---------------------------------------------------
    def set_shattered_panes(self, panes: Dict[str, int],
                            edge_retain: Optional[float] = 0.05) -> None:
        """Register which glass panes shattered and when (cache frames).

        From ``panes[name]`` onward the member's INTERIOR vertices are
        collapsed out of the car, so the intact glass disappears and the
        spawned fragments take over — while the rim band stays welded to the
        pane and keeps animating, acting as the fringe.  ``edge_retain`` is the
        band width (fraction of the pane's half-extent) and must match the
        value the debris build used for :func:`glass_shatter.shatter_pane`, so
        the surviving rim lines up with the cells the build kept.  The
        collapse targets are computed here from the cache, so callers only
        need the name→frame map (see :func:`runtime.impact_detect.
        resolve_glass_damage`).

        The map is REPLACED, not merged: an empty ``panes`` un-collapses every
        pane, so turning "shatter glass" off (or clearing debris) brings the
        intact glass back.  Before this, an empty map was a silent no-op and a
        pane collapsed by one build stayed collapsed in every later one.
        """
        if edge_retain is None:
            edge_retain = 0.05
        self._shattered = {}
        self._shattered_centres = {}
        self._shattered_keeps = {}
        self._shattered_anchors = {}
        for name, frame in panes.items():
            frame = int(frame)
            if not (0 <= frame < self.reader.frame_count):
                continue
            try:
                raw = self.reader.frame_positions(name, frame)
            except ValueError:  # pragma: no cover - not a stable object
                continue
            pos = self._gltf_to_blender(raw)
            if len(pos):
                self._shattered[name] = frame
                self._shattered_centres[name] = pos.mean(axis=0)
                keep, anchors = rim_mask_and_anchors(pos, float(edge_retain))
                self._shattered_keeps[name] = keep
                self._shattered_anchors[name] = anchors
        # Defeat the "same frame, return early" guard so the change (collapse
        # OR un-collapse) takes effect on the next refresh.
        self._current_frame = None

    def _collapse_shattered(self, name: str, pos: np.ndarray) -> np.ndarray:
        """Collapse a shattered member's INTERIOR vertices onto the rim.

        Rows the rim band keeps (``_shattered_keeps[name]`` True) stay at
        their live per-frame positions, so they follow the pane's vertex
        animation exactly.  Each interior row is replaced by the CURRENT-frame
        position of its anchor RIM vertex, so the collapsed faces ride the
        deforming break edge and never stretch away from it.  Falls back to the
        old whole-member centroid collapse when the mask is missing (e.g. a
        build older than this change) or nothing was retained (the pane
        vanishes wholesale).
        """
        keep = self._shattered_keeps.get(name)
        anchors = self._shattered_anchors.get(name)
        if keep is not None and anchors is not None and len(keep) == len(pos):
            if keep.sum() > 0:
                out = pos.copy()
                out[~keep] = pos[anchors[~keep]]
                return out
        centre = self._shattered_centres.get(name)
        if centre is None:
            return pos
        return np.broadcast_to(centre, pos.shape).copy()

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

    def _set_tyre_basis(self, tf: Optional[np.ndarray]) -> None:
        """(Re)compute the local→world-height basis from a transform block."""
        if not self.tyre.enabled:
            self._tyre_basis = None
            return
        up, offset = height_basis_from_transform(tf, ground_z=self.tyre.ground_z)
        self._tyre_basis = (up, offset)

    def _update_tyre_basis(self, frame: int) -> None:
        """Recompute the local→world-height basis for *frame* (once per frame).

        The meshes' vertices live in the cache's space; the parent empty (when
        present) carries the rigid transform, and the importer's auto-ground adds
        a Z shift on each object.  Both have to be folded in before we can ask
        "how far is this vertex above the ground".
        """
        self._set_tyre_basis(self.reader.frame_transform(frame))

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
    def set_frame(self, pos: float) -> None:
        """Advance playback to *pos* (cache-frame units; may pass the seam).

        Positions at or below the smooth-stop seam use the existing integer
        cache frames (rounded).  Positions PAST the seam enter the smooth-stop
        tail when :meth:`set_smooth_stop` configured one: the car keeps gliding
        along its residual motion (rigid pose AND vertex deformation) with
        linearly-decaying velocity and comes to a full rest exactly at the seam
        plus the tail length.  With no tail configured, overshooting just holds
        the seam pose.

        The seam defaults to the last captured frame, but can be set earlier via
        :meth:`set_smooth_stop`'s ``start_cache`` — in which case the smooth stop
        begins at that frame instead, cutting the remaining captured motion in
        favour of the damped continuation.
        """
        pos = float(pos)
        last = self.reader.frame_count - 1
        seam = (self._smooth_stop_start if self._smooth_stop_start is not None
                else float(last))
        if pos <= seam:
            frame = max(0, min(int(round(pos)), last))
            if frame == self._current_frame:
                self._log(f"--- set_frame({pos} -> cache {frame})  "
                          f"current={self._current_frame}  (no change) ---")
                return
            self._log(f"--- set_frame({pos} -> cache {frame})  "
                      f"current={self._current_frame} ---")
            if self._chunk_map:
                self._set_frame_chunked(frame)
            else:
                self._set_frame_individual(frame)
            self._current_frame = frame
        else:
            if pos == self._current_frame:
                self._log(f"--- set_frame({pos})  (no change, returning early) ---")
                return
            self._log(f"--- set_frame({pos}) glide past seam={seam} ---")
            self._set_frame_glide(pos, seam)
            self._current_frame = pos

    def _set_frame_glide(self, pos: float, seam: float) -> None:
        """Continue the car's residual SWING past the smooth-stop seam.

        With ``_smooth_stop_tail`` configured, cache position ``s`` past the seam
        keeps the oscillation the car was still rocking through when the smooth
        stop begins — fitted in amplitude AND phase from the frames at-or-before
        the seam — and shrinks it to rest.  Both the rigid root and every vertex
        continue their own fitted sine; only the AMPLITUDE is scaled down, by
        :func:`_tail_envelope`.  So the car keeps rocking both ways the whole
        time, each swing smaller than the last::

            <-------->, <------->, <----->, <--->, <->, rest

        It does not brake in a straight line, and it does not drop to a small
        swing at the seam and then decay that: ``env(0) = 1`` means the first
        tail frame continues the swing at exactly the size it already had.
        A car that is truly at rest at the seam gets a still tail (nothing to
        settle).
        """
        tail = self._smooth_stop_tail
        if tail <= 0:
            # No settle configured — clamp to the seam pose.
            if self._chunk_map:
                self._set_frame_chunked(int(seam))
            else:
                self._set_frame_individual(int(seam))
            return
        s = max(0.0, pos - seam)
        u = 1.0 if s >= tail else s / tail
        env = _tail_envelope(u)

        tf = self._glide_transform(s, env, seam)
        if self._transform_empty is not None and tf is not None:
            self._transform_empty.matrix_basis = self._matrix_from_transform(tf)
            self._transform_empty.rotation_mode = "QUATERNION"
        self._set_tyre_basis(tf)

        # Absolute phase for the vertex fit: its sample grid was `frame - start`,
        # so continuing past the seam means t = (seam - start) + s.  Using the
        # same phase convention as the root keeps vertices and root in step.
        vfit = self._tail_vfit
        t = ((seam - vfit["start"]) + s) if vfit is not None else s

        if self._chunk_map:
            self._glide_chunked(int(seam), t, env)
        else:
            self._glide_individual(int(seam), t, env)

    def _glide_individual(self, seam: int, t: float, env: float) -> None:
        for name, obj in self._objects.items():
            raw = self._glide_positions(name, seam, t, env)
            if raw is None:
                # Nothing fitted for this member (too few frames, or a cache
                # that cannot serve it) — hold its seam pose.
                raw = self.reader.frame_positions(name, seam)
            positions = self._gltf_to_blender(raw)
            positions = self._deform_tyre(name, obj, positions, seam)
            # The rim band rides the glided geometry, so the collapsed glass
            # keeps moving with the settling car instead of snapping away.
            if self._shattered and seam >= self._shattered.get(name, 1 << 30):
                positions = self._collapse_shattered(name, positions)
            flat = np.ascontiguousarray(positions, dtype=np.float32).reshape(-1)
            _write_positions(obj.data, flat, obj)
        for name, obj in self._dynamic_objects.items():
            self._write_dynamic_frame(name, obj, seam)

    def _glide_chunked(self, seam: int, t: float, env: float) -> None:
        for chunk_name, obj in self._chunks.items():
            member_ranges = self._chunk_member_ranges[chunk_name]
            parts = []
            for mname, (start, end) in member_ranges.items():
                raw = self._glide_positions(mname, seam, t, env)
                if raw is None:
                    raw = self.reader.frame_positions(mname, seam)
                pos = self._gltf_to_blender(raw)
                pos = self._deform_tyre(mname, obj, pos, seam)
                if self._shattered and seam >= self._shattered.get(mname, 1 << 30):
                    pos = self._collapse_shattered(mname, pos)
                parts.append(pos)
            combined = np.concatenate(parts, axis=0)
            flat = np.ascontiguousarray(combined, dtype=np.float32).reshape(-1)
            _write_positions(obj.data, flat, obj)
        for name, obj in self._dynamic_objects.items():
            self._write_dynamic_frame(name, obj, seam)

    def _write_dynamic_frame(self, name: str, obj: "bpy.types.Object",
                             frame: int) -> None:
        """Render one dynamic (topology-changing) object for *frame*."""
        try:
            positions, indices = self.reader.frame_dynamic_geometry(name, frame)
        except (KeyError, ValueError):
            return  # proxy or unknown object — skip
        self._log_bounds("RAW_GLTF", name, frame, positions)
        if len(positions) == 0:
            obj.hide_viewport = True
            obj.hide_render = True
            self._log(f"  {name}: hidden (empty)")
            return
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

    def _set_frame_individual(self, frame: int) -> None:
        """Original per-object position update (96 calls)."""
        logging = self._log_fh is not None
        self._apply_transform(frame)
        self._update_tyre_basis(frame)
        for name, obj in self._objects.items():
            raw = self.reader.frame_positions(name, frame)
            positions = self._gltf_to_blender(raw)
            positions = self._deform_tyre(name, obj, positions, frame)
            # Shattered panes collapse to a point from their break frame, so the
            # intact glass leaves the car and the spawned fragments take over.
            if self._shattered and frame >= self._shattered.get(name, 1 << 30):
                positions = self._collapse_shattered(name, positions)
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
            self._write_dynamic_frame(name, obj, frame)

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
                # Per MEMBER, so one shattered pane vanishes without touching
                # the other panes sharing the merged 'glass' chunk.
                if self._shattered and frame >= self._shattered.get(mname, 1 << 30):
                    pos = self._collapse_shattered(mname, pos)
                parts.append(pos)
            combined = np.concatenate(parts, axis=0)

            self._log_bounds("CHUNK", chunk_name, frame, combined)
            flat = np.ascontiguousarray(combined, dtype=np.float32).reshape(-1)
            _write_positions(obj.data, flat, obj)

        for name, obj in self._dynamic_objects.items():
            self._write_dynamic_frame(name, obj, frame)
