from __future__ import annotations

"""BeamNG material/texture resolution — portable (bpy-free).

Parses BeamNG ``.materials.json`` files and resolves texture paths to actual
files on disk.  Designed to be used both from Blender (the add-on operator) and
from unit tests without requiring ``bpy``.

Adapted from "BeamNG Auto Texture Assign" by Probler (v2.4).
"""

import json
import os
import re
import zipfile
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


# JSON field → (Blender socket name, is_normal)
JSON_FIELD_MAP: Dict[str, Tuple[str, bool]] = {
    "baseColorMap":        ("Base Color",     False),
    "normalMap":           ("Normal",         True),
    "roughnessMap":        ("Roughness",      False),
    "metallicMap":         ("Metallic",       False),
    "ambientOcclusionMap": ("AO",             False),
    "emissiveMap":         ("Emission Color", False),
    "opacityMap":          ("Alpha",          False),
    "clearCoatMap":        ("Coat Weight",    False),
}

# DDS filename suffix → (Blender socket, is_normal)
SUFFIX_MAP: Dict[str, Tuple[str, bool]] = {
    "b.color":   ("Base Color",     False),
    "d.color":   ("Base Color",     False),
    "c.color":   ("Base Color",     False),
    "cc.data":   ("Base Color",     False),
    "p.color":   ("Base Color",     False),
    "da.color":  ("Base Color",     False),
    "b":         ("Base Color",     False),
    "d":         ("Base Color",     False),
    "n.normal":  ("Normal",         True),
    "nm.normal": ("Normal",         True),
    "n":         ("Normal",         True),
    "r.data":    ("Roughness",      False),
    "r":         ("Roughness",      False),
    "m.data":    ("Metallic",       False),
    "m":         ("Metallic",       False),
    "ao.data":   ("AO",             False),
    "ao":        ("AO",             False),
    "s.color":   ("Specular",       False),
    "s":         ("Specular",       False),
    "o.data":    ("Alpha",          False),
    "o":         ("Alpha",          False),
    "g.color":   ("Emission Color", False),
    "g":         ("Emission Color", False),
}

SUFFIX_KEYS_SORTED = sorted(SUFFIX_MAP.keys(), key=len, reverse=True)

LIGHT_SUFFIXES = (
    "brakelight_l", "brakelight_r", "chmsl",
    "drl", "drl_signal_l", "drl_signal_r",
    "highbeam", "lowbeam", "reverselight",
    "signal_l", "signal_r", "taillight",
)


def strip_blender_index(name: str) -> str:
    """Remove Blender's .001-style dedup suffix."""
    return re.sub(r'\.\d{3}$', '', name)


# --- Search roots: mod folders + base-game zips ---------------------------
#
# WHY THIS EXISTS
# ---------------
# A capture carries only material *names*; textures are resolved at import
# time by matching those names against ``*.materials.json``.  Scanning just the
# vehicle folder loses every material the mod inherits from the base game.
# Measured on a real 46-material capture: 8 resolved to nothing, and 5 of
# those 8 (``tire_01a``, ``disc_brake``, ``mirror``, ``licenseplate``,
# ``invis``) live in ``content/vehicles/common.zip``.  ``tire_01a`` is the
# material on all four tyres — hence bare, normal-map-less tyres.
#
# So resolution spans an ordered list of roots: the mod folder first (a mod may
# legitimately override a base material), then the game's vehicle zips.

TEXTURE_EXTS = (".dds", ".png", ".jpg", ".jpeg", ".tga")

# Relative to a BeamNG install root, where vehicle content zips live.
_GAME_VEHICLE_SUBDIR = os.path.join("content", "vehicles")


class SearchRoot:
    """A place to look for ``*.materials.json`` and texture files.

    Unifies a plain directory and a zip archive behind one tiny interface so
    :func:`parse_materials_json` and friends don't care which they're reading.
    Texture paths returned for zip members use Blender's ``archive.zip``
    +``/inner/path`` convention, which ``bpy.data.images.load`` understands
    only after extraction — see :func:`materialize_texture`.
    """

    def iter_materials_json(self) -> Iterable[Tuple[str, dict]]:
        """Yield ``(display_path, parsed_json)`` for each materials.json."""
        raise NotImplementedError

    def find_texture(self, stem: str) -> Optional[str]:
        """Return a path for the first texture file matching *stem*, or None."""
        raise NotImplementedError

    def iter_texture_stems(self) -> Iterable[Tuple[str, str]]:
        """Yield ``(file_stem, path)`` for every texture file in this root."""
        raise NotImplementedError


class DirSearchRoot(SearchRoot):
    """A directory on disk (an extracted mod / vehicle folder)."""

    def __init__(self, folder: str):
        self.folder = str(folder)

    def iter_materials_json(self):
        for json_file in Path(self.folder).rglob("*.materials.json"):
            try:
                with open(json_file, "r", encoding="utf-8") as f:
                    yield str(json_file), json.load(f)
            except Exception as e:
                import sys
                sys.stderr.write(f"[BeamNG] Could not read {json_file}: {e}\n")

    def find_texture(self, stem: str) -> Optional[str]:
        # Fast path: same directory layout as the JSON reference.
        for ext in TEXTURE_EXTS:
            for cand in (stem + ext, stem + ext.upper()):
                p = os.path.join(self.folder, cand)
                if os.path.isfile(p):
                    return p
        # Recursive search by stem.
        try:
            for f in Path(self.folder).rglob(stem + ".*"):
                if f.suffix.lower() in TEXTURE_EXTS:
                    return str(f)
        except OSError:
            pass
        return None

    def iter_texture_stems(self):
        for f in Path(self.folder).rglob("*"):
            if f.suffix.lower() in TEXTURE_EXTS:
                yield f.stem, str(f)


class ZipSearchRoot(SearchRoot):
    """A BeamNG content ``.zip`` (e.g. ``content/vehicles/common.zip``).

    Reads members directly — no extraction — so indexing 123 game zips costs
    ~0.3s.  Only textures actually referenced get extracted, and only when
    Blender needs to load them.
    """

    def __init__(self, zip_path: str):
        self.zip_path = str(zip_path)
        self._names: Optional[List[str]] = None
        self._by_stem: Optional[Dict[str, str]] = None

    def _namelist(self) -> List[str]:
        if self._names is None:
            try:
                with zipfile.ZipFile(self.zip_path) as z:
                    self._names = z.namelist()
            except Exception:
                self._names = []
        return self._names

    def _stem_index(self) -> Dict[str, str]:
        if self._by_stem is None:
            idx: Dict[str, str] = {}
            for n in self._namelist():
                ext = os.path.splitext(n)[1].lower()
                if ext in TEXTURE_EXTS:
                    stem = os.path.basename(n)[: -len(ext)] if ext else ""
                    # First occurrence wins — matches DirSearchRoot behaviour.
                    idx.setdefault(stem.lower(), n)
            self._by_stem = idx
        return self._by_stem

    def iter_materials_json(self):
        names = [n for n in self._namelist() if n.endswith("materials.json")]
        if not names:
            return
        try:
            with zipfile.ZipFile(self.zip_path) as z:
                for n in names:
                    try:
                        raw = z.read(n).decode("utf-8", errors="replace")
                        yield f"{self.zip_path}!{n}", json.loads(raw)
                    except Exception:
                        # BeamNG ships some materials.json with trailing commas
                        # / comments that strict json rejects.  Skip quietly —
                        # 39 of 332 game files, none of them ones we need.
                        continue
        except Exception:
            return

    def find_texture(self, stem: str) -> Optional[str]:
        member = self._stem_index().get(stem.lower())
        return f"{self.zip_path}!{member}" if member else None

    def iter_texture_stems(self):
        for stem, member in self._stem_index().items():
            yield stem, f"{self.zip_path}!{member}"


def split_zip_path(path: str) -> Tuple[Optional[str], Optional[str]]:
    """Split a ``archive.zip!inner/member.png`` path.

    Returns ``(zip_path, member)`` or ``(None, None)`` for a plain file path.
    """
    if "!" not in path:
        return None, None
    zp, member = path.rsplit("!", 1)
    return (zp, member) if zp.lower().endswith(".zip") else (None, None)


def materialize_texture(path: str, extract_dir: str) -> Optional[str]:
    """Ensure *path* is a real file on disk, extracting from a zip if needed.

    Zip members are extracted to *extract_dir* (mirroring their inner path) and
    cached — a second call for the same member reuses the extracted file.
    Returns a plain filesystem path, or None if extraction failed.
    """
    zip_path, member = split_zip_path(path)
    if zip_path is None:
        return path if os.path.isfile(path) else None

    dest = os.path.join(extract_dir, member.replace("/", os.sep))
    if os.path.isfile(dest) and os.path.getsize(dest) > 0:
        return dest
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with zipfile.ZipFile(zip_path) as z:
            with z.open(member) as src, open(dest, "wb") as out:
                out.write(src.read())
        return dest
    except Exception as e:
        import sys
        sys.stderr.write(f"[BeamNG] Could not extract {member} from {zip_path}: {e}\n")
        return None


def find_beamng_install(hint: Optional[str] = None) -> Optional[str]:
    """Locate a BeamNG.drive install containing ``content/vehicles``.

    Tries *hint* first, then walks up from it (a user may point at any depth),
    then a handful of common install locations.  Returns the install root or
    None.
    """
    candidates: List[str] = []
    if hint:
        h = os.path.normpath(hint)
        candidates.append(h)
        # Walk up — the hint may be .../BeamNG.drive/content/vehicles or deeper.
        p = Path(h)
        candidates.extend(str(a) for a in p.parents)

    for env in ("BEAMNG_INSTALL", "BEAMNG_PATH"):
        v = os.environ.get(env)
        if v:
            candidates.append(os.path.normpath(v))

    # Steam library folders can live on any drive; scan them all.
    import string
    for drive in string.ascii_uppercase:
        candidates += [
            f"{drive}:\\SteamLibrary\\steamapps\\common\\BeamNG.drive",
            f"{drive}:\\Steam\\steamapps\\common\\BeamNG.drive",
            f"{drive}:\\Games\\BeamNG.drive",
            f"{drive}:\\BeamNG.drive",
        ]
    candidates += [
        r"C:\Program Files (x86)\Steam\steamapps\common\BeamNG.drive",
        r"C:\Program Files\Steam\steamapps\common\BeamNG.drive",
    ]

    for c in candidates:
        if c and os.path.isdir(os.path.join(c, _GAME_VEHICLE_SUBDIR)):
            return c
    return None


def build_search_roots(
    vehicle_folder: str,
    game_dir: Optional[str] = None,
    include_game: bool = True,
) -> List[SearchRoot]:
    """Ordered roots to resolve materials/textures against.

    The vehicle folder comes first so a mod can override a base-game material.
    Then ``common.zip`` (where shared tyre/brake/mirror materials live), then
    the remaining vehicle zips.
    """
    roots: List[SearchRoot] = [DirSearchRoot(vehicle_folder)]
    if not include_game:
        return roots

    install = find_beamng_install(game_dir)
    if not install:
        return roots

    vdir = os.path.join(install, _GAME_VEHICLE_SUBDIR)
    try:
        zips = sorted(
            os.path.join(vdir, f)
            for f in os.listdir(vdir)
            if f.lower().endswith(".zip")
        )
    except OSError:
        return roots

    # common.zip holds the shared materials (tire_01a, disc_brake, ...) — put it
    # ahead of the per-vehicle zips so it wins ties.
    common = [z for z in zips if os.path.basename(z).lower() == "common.zip"]
    others = [z for z in zips if z not in common]
    roots.extend(ZipSearchRoot(z) for z in common + others)
    return roots


def _as_roots(
    folder_or_roots: "str | Sequence[SearchRoot]",
) -> List[SearchRoot]:
    """Accept either a plain folder path (legacy) or a list of roots."""
    if isinstance(folder_or_roots, (str, os.PathLike)):
        return [DirSearchRoot(str(folder_or_roots))]
    return list(folder_or_roots)


def resolve_texture_path(
    json_path: str,
    folder_or_roots: "str | Sequence[SearchRoot]",
) -> Optional[str]:
    """Find an actual texture file from a JSON texture path.

    BeamNG JSON paths look like ``/vehicles/flanje_e180/textures/color.dds``.
    We strip the directory, keep the stem, and search each root in order.
    Accepts a plain folder path (legacy callers) or a list of
    :class:`SearchRoot`.  A returned path may be a ``archive.zip!member`` ref —
    pass it through :func:`materialize_texture` before loading.
    """
    if not json_path or json_path.startswith("@"):
        return None
    stem = Path(os.path.basename(json_path)).stem
    for root in _as_roots(folder_or_roots):
        hit = root.find_texture(stem)
        if hit:
            return hit
    return None


def parse_materials_json(
    folder_or_roots: "str | Sequence[SearchRoot]",
) -> Dict[str, Dict[str, Tuple[str, bool]]]:
    """Parse all ``*.materials.json`` across the given roots.

    Returns a dict keyed by *lowercase* ``mapTo`` name, where each value is
    ``{socket_name: (path, is_normal)}``.
    Only includes entries that have at least one resolved texture.

    Roots are searched in order and *earlier roots win* per socket, so a mod's
    own definition overrides the base game's.
    """
    roots = _as_roots(folder_or_roots)
    material_map: Dict[str, Dict[str, Tuple[str, bool]]] = {}

    for root in roots:
        for _display, data in root.iter_materials_json():
            if not isinstance(data, dict):
                continue
            for mat_key, mat_def in data.items():
                if not isinstance(mat_def, dict):
                    continue
                map_to = mat_def.get("mapTo") or mat_def.get("name") or mat_key
                map_to_lower = map_to.lower()
                tex_paths: Dict[str, Tuple[str, bool]] = {}

                for stage in mat_def.get("Stages", []):
                    if not isinstance(stage, dict):
                        continue
                    for field, (socket, is_normal) in JSON_FIELD_MAP.items():
                        val = stage.get(field)
                        if val and isinstance(val, str) and not val.startswith("@"):
                            # Resolve against ALL roots, not just the one that
                            # held the JSON: a mod material can reference a
                            # base-game texture and vice versa.
                            resolved = resolve_texture_path(val, roots)
                            if resolved and socket not in tex_paths:
                                tex_paths[socket] = (resolved, is_normal)

                if tex_paths:
                    existing = material_map.get(map_to_lower)
                    if existing is None:
                        material_map[map_to_lower] = tex_paths
                    else:
                        for socket, val in tex_paths.items():
                            if socket not in existing:
                                existing[socket] = val

    return material_map


def parse_stem_filename(stem: str) -> Tuple[Optional[str], Optional[str]]:
    """Parse a filename stem into ``(material_name_lower, suffix_key)``.

    Example: ``"flanje_e180_paint_b"`` → ``("flanje_e180_paint", "b")``
    """
    stem_lower = stem.lower()
    for key in SUFFIX_KEYS_SORTED:
        if stem_lower.endswith("_" + key):
            return stem_lower[: -(len(key) + 1)], key
    return None, None


def build_filename_lookup(
    folder_or_roots: "str | Sequence[SearchRoot]",
) -> Dict[str, Dict[str, str]]:
    """Fallback: scan texture files by name across all roots.

    Returns ``{material_name_lower: {suffix: path}}``.  Earlier roots win, so a
    mod's texture shadows a base-game one of the same name.
    """
    lookup: Dict[str, Dict[str, str]] = {}
    for root in _as_roots(folder_or_roots):
        for stem, path in root.iter_texture_stems():
            mat_name, suffix_key = parse_stem_filename(stem)
            if mat_name is None:
                continue
            lookup.setdefault(mat_name, {}).setdefault(suffix_key, path)
    return lookup


def filename_lookup_to_socket_map(
    suffix_dict: Dict[str, str],
) -> Dict[str, Tuple[str, bool]]:
    """Convert suffix→path dict to socket→(path, is_normal) dict."""
    result: Dict[str, Tuple[str, bool]] = {}
    for suffix, path in suffix_dict.items():
        entry = SUFFIX_MAP.get(suffix)
        if entry:
            socket, is_normal = entry
            if socket not in result:
                result[socket] = (path, is_normal)
    return result


def find_by_filename(
    mat_name_lower: str,
    filename_lookup: Dict[str, Dict[str, str]],
    car_prefix: Optional[str] = None,
) -> Optional[Dict[str, Tuple[str, bool]]]:
    """Try to match a material via filename, including light fallback and prefix."""
    candidates: List[Optional[str]] = [mat_name_lower]

    if car_prefix and not mat_name_lower.startswith(car_prefix):
        candidates.append(f"{car_prefix}_{mat_name_lower}")

    if car_prefix and mat_name_lower == car_prefix:
        candidates += [f"{car_prefix}_main", f"{car_prefix}_body"]

    for light_suffix in LIGHT_SUFFIXES:
        if mat_name_lower.endswith("_" + light_suffix):
            prefix_part = mat_name_lower[: -(len(light_suffix) + 1)]
            candidates += [
                f"{prefix_part}_lights",
                f"{prefix_part}_light",
                f"{car_prefix}_lights" if car_prefix else None,
            ]
            break

    for candidate in candidates:
        if candidate and candidate in filename_lookup:
            return filename_lookup_to_socket_map(filename_lookup[candidate])

    return None


# --- Shader parameter extraction (pure-parameter materials) ---------------

# JSON shader parameter → (Blender Principled BSDF input, conversion func)
SHADER_PARAM_MAP: Dict[str, Tuple[str, str]] = {
    "baseColorFactor":     ("Base Color",        "color"),
    "diffuseColor":        ("Base Color",        "color"),
    "emissiveFactor":      ("Emission Color",    "emissive"),
    "roughnessFactor":     ("Roughness",         "float"),
    "metallicFactor":      ("Metallic",          "float"),
    "clearCoatFactor":     ("Coat Weight",       "float"),
    "clearCoatRoughnessFactor": ("Coat Roughness", "float"),
    "normalMapStrength":   ("Normal Map Strength", "float"),
    "opacityFactor":       ("Alpha",             "float"),
}

# BeamNG emissive factors use HDR values (often >1, up to ~41 for brake lights).
# Principled BSDF Emission Strength clips above sensible values.
_EMISSIVE_SCALE = 0.05  # rough scale to bring BeamNG HDR into Blender range


def _extract_shader_params(mat_def: dict) -> Dict[str, object]:
    """Extract shader parameters (baseColorFactor, roughnessFactor, etc.) from a
    BeamNG material definition.

    Returns ``{blender_socket_name: value}`` for every parameter found across
    all stages (first non-null value wins).  Color values are converted from
    BeamNG's 0-1 float array to ``(r, g, b, a)`` tuples.
    """
    params: Dict[str, object] = {}
    for stage in mat_def.get("Stages", []):
        if not isinstance(stage, dict):
            continue
        for field, (socket, kind) in SHADER_PARAM_MAP.items():
            if socket in params:
                continue
            val = stage.get(field)
            if val is None:
                continue
            if kind == "color":
                if isinstance(val, (list, tuple)) and len(val) >= 3:
                    r, g, b = val[0], val[1], val[2]
                    a = val[3] if len(val) > 3 else 1.0
                    params[socket] = (r, g, b, a)
            elif kind == "emissive":
                if isinstance(val, (list, tuple)) and len(val) >= 3:
                    r, g, b = val[0], val[1], val[2]
                    params["Emission Color"] = (r, g, b, 1.0)
                    params["Emission Strength"] = r * _EMISSIVE_SCALE
            elif kind == "float":
                if isinstance(val, (int, float)):
                    params[socket] = float(val)

    return params


def parse_material_shader_params(
    folder_or_roots: "str | Sequence[SearchRoot]",
) -> Dict[str, Dict[str, object]]:
    """Parse all ``*.materials.json`` and return shader parameters for
    materials that do NOT have any resolvable texture files.

    Returns ``{lowercase_mapTo: {blender_socket: value}}``.
    """
    roots = _as_roots(folder_or_roots)
    material_params: Dict[str, Dict[str, object]] = {}

    for root in roots:
        for _display, data in root.iter_materials_json():
            if not isinstance(data, dict):
                continue
            for mat_key, mat_def in data.items():
                if not isinstance(mat_def, dict):
                    continue
                map_to = mat_def.get("mapTo") or mat_def.get("name") or mat_key
                map_to_lower = map_to.lower()

                # Skip if this material already has texture paths.
                has_textures = False
                for stage in mat_def.get("Stages", []):
                    if not isinstance(stage, dict):
                        continue
                    for field in JSON_FIELD_MAP:
                        val = stage.get(field)
                        if val and isinstance(val, str) and not val.startswith("@"):
                            if resolve_texture_path(val, roots):
                                has_textures = True
                                break
                    if has_textures:
                        break

                if has_textures:
                    continue

                params = _extract_shader_params(mat_def)
                if params:
                    existing = material_params.get(map_to_lower)
                    if existing is None:
                        material_params[map_to_lower] = params
                    else:
                        existing.update(params)

    return material_params


def _normalize_material_key(key: str) -> str:
    """Normalize a material key for lookup — try replacing ``_`` with ``.``
    and vice versa, since BeamNG sometimes uses both forms (e.g.
    ``flanje_e180_black_001`` vs ``flanje_e180_black.001``).
    """
    return key.lower()


def _alternate_keys(key: str) -> list:
    """Generate alternate key forms for fuzzy matching.

    BeamNG sometimes uses ``_001`` (underscore) where Blender uses
    ``.001`` (dot) for dedup suffixes, and vice versa in JSON mapTo keys.
    Only the Blender-style ``_NNN`` / ``.NNN`` suffix is swapped.
    """
    import re as _re
    keys = [key]
    m = _re.search(r'[_]\d{3}$', key)
    if m:
        alt = key[:m.start()] + '.' + key[m.start() + 1:]
        keys.append(alt)
    m = _re.search(r'[.]\d{3}$', key)
    if m:
        alt = key[:m.start()] + '_' + key[m.start() + 1:]
        keys.append(alt)
    return keys


# --- Blender-dependent helpers (used by the addon operator) ---------------

def build_material_from_params(
    mat: "bpy.types.Material",  # noqa: F821
    params: Dict[str, object],
) -> None:
    """Set shader parameters (baseColorFactor, roughnessFactor, etc.) on *mat*
    without loading any texture files.  Falls back gracefully if ``bpy`` is
    unavailable (no-op).
    """
    import bpy  # noqa: F811

    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    nodes.clear()

    out = nodes.new("ShaderNodeOutputMaterial")
    out.location = (700, 0)
    bsdf = nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.location = (300, 0)
    links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])

    for socket_name, value in params.items():
        if socket_name in bsdf.inputs:
            input_socket = bsdf.inputs[socket_name]
            if isinstance(value, (list, tuple)):
                input_socket.default_value = value
            else:
                input_socket.default_value = value


def build_material_node_tree(
    mat: "bpy.types.Material",  # noqa: F821 — only called from Blender
    tex_paths: Dict[str, Tuple[str, bool]],
    extract_dir: Optional[str] = None,
) -> None:
    """Build a Principled BSDF node tree for *mat* from a texture map.

    *tex_paths* is the output of :func:`parse_materials_json` or
    :func:`find_by_filename`: ``{socket_name: (path, is_normal)}``.  A path may
    be a ``archive.zip!member`` reference; *extract_dir* is where such members
    get extracted so Blender can load them (required if any zip refs present).

    This function requires ``bpy`` (Blender Python API) and should only be
    called from addon operators or Blender scripts.
    """
    import bpy  # noqa: F811

    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    nodes.clear()

    out = nodes.new("ShaderNodeOutputMaterial")
    out.location = (700, 0)
    bsdf = nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.location = (300, 0)
    links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])

    x_offset = -400
    y_offset = 600

    for socket_name, (raw_path, is_normal) in tex_paths.items():
        img_node = nodes.new("ShaderNodeTexImage")
        img_node.location = (x_offset, y_offset)
        img_node.label = socket_name
        y_offset -= 300

        # Zip members must hit the filesystem before Blender can load them.
        abs_path = raw_path
        if split_zip_path(raw_path)[0] is not None:
            if not extract_dir:
                import sys
                sys.stderr.write(
                    f"[BeamNG] {raw_path} is inside a zip but no extract_dir "
                    f"was given; skipping\n"
                )
                nodes.remove(img_node)
                continue
            extracted = materialize_texture(raw_path, extract_dir)
            if not extracted:
                nodes.remove(img_node)
                continue
            abs_path = extracted

        img_name = os.path.basename(abs_path)
        img = bpy.data.images.get(img_name)
        if img is None:
            try:
                img = bpy.data.images.load(abs_path)
            except Exception as e:
                import sys
                sys.stderr.write(f"[BeamNG] Could not load {abs_path}: {e}\n")
                nodes.remove(img_node)
                continue

        img_node.image = img

        if is_normal:
            img.colorspace_settings.name = "Non-Color"
            nm_node = nodes.new("ShaderNodeNormalMap")
            nm_node.location = (x_offset + 280, y_offset + 160)
            links.new(img_node.outputs["Color"], nm_node.inputs["Color"])
            if "Normal" in bsdf.inputs:
                links.new(nm_node.outputs["Normal"], bsdf.inputs["Normal"])
        elif socket_name == "AO":
            img.colorspace_settings.name = "Non-Color"
            img_node.label = "AO (not wired - bake separately)"
        elif socket_name in ("Roughness", "Metallic", "Alpha"):
            img.colorspace_settings.name = "Non-Color"
            if socket_name in bsdf.inputs:
                links.new(img_node.outputs["Color"], bsdf.inputs[socket_name])
        else:
            if socket_name in bsdf.inputs:
                links.new(img_node.outputs["Color"], bsdf.inputs[socket_name])
