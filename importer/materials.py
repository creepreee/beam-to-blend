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
from pathlib import Path
from typing import Dict, List, Optional, Tuple


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


def resolve_texture_path(json_path: str, vehicle_folder: str) -> Optional[str]:
    """Find actual file on disk from a JSON texture path.

    BeamNG JSON paths look like ``/vehicles/flanje_e180/textures/color.dds``.
    We strip the path, keep the filename + stem, and try extensions in a
    list of candidate directories under *vehicle_folder*.
    """
    if not json_path or json_path.startswith("@"):
        return None
    filename = os.path.basename(json_path)
    stem = Path(filename).stem
    # Try same directory as JSON file
    for ext in (".dds", ".DDS", ".png", ".jpg", ".PNG", ".JPG"):
        candidate = os.path.join(vehicle_folder, stem + ext)
        if os.path.isfile(candidate):
            return candidate
    # Recursive search
    for f in Path(vehicle_folder).rglob(stem + ".*"):
        if f.suffix.lower() in (".dds", ".png", ".jpg"):
            return str(f)
    return None


def parse_materials_json(vehicle_folder: str) -> Dict[str, Dict[str, Tuple[str, bool]]]:
    """Parse all ``*.materials.json`` files under *vehicle_folder*.

    Returns a dict keyed by *lowercase* ``mapTo`` name, where each value is
    ``{socket_name: (abs_path, is_normal)}``.
    Only includes entries that have at least one resolved texture.
    """
    material_map: Dict[str, Dict[str, Tuple[str, bool]]] = {}
    for json_file in Path(vehicle_folder).rglob("*.materials.json"):
        try:
            with open(json_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            import sys
            sys.stderr.write(f"[BeamNG] Could not read {json_file}: {e}\n")
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
                        resolved = resolve_texture_path(val, vehicle_folder)
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


def build_filename_lookup(vehicle_folder: str) -> Dict[str, Dict[str, str]]:
    """Fallback: scan texture files by name.

    Returns ``{material_name_lower: {suffix: abs_path}}``.
    """
    lookup: Dict[str, Dict[str, str]] = {}
    for f in Path(vehicle_folder).rglob("*"):
        if f.suffix.lower() not in (".dds", ".png", ".jpg"):
            continue
        mat_name, suffix_key = parse_stem_filename(f.stem)
        if mat_name is None:
            continue
        if mat_name not in lookup:
            lookup[mat_name] = {}
        if suffix_key not in lookup[mat_name]:
            lookup[mat_name][suffix_key] = str(f)
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
    vehicle_folder: str,
) -> Dict[str, Dict[str, object]]:
    """Parse all ``*.materials.json`` files and return shader parameters for
    materials that do NOT have any resolvable texture files.

    Returns ``{lowercase_mapTo: {blender_socket: value}}``.
    """
    from pathlib import Path as _Path
    import json as _json

    material_params: Dict[str, Dict[str, object]] = {}
    for json_file in _Path(vehicle_folder).rglob("*.materials.json"):
        try:
            with open(json_file, "r", encoding="utf-8") as f:
                data = _json.load(f)
        except Exception:
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
                        resolved = resolve_texture_path(val, vehicle_folder)
                        if resolved:
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
) -> None:
    """Build a Principled BSDF node tree for *mat* from a texture map.

    *tex_paths* is the output of :func:`parse_materials_json` or
    :func:`find_by_filename`: ``{socket_name: (abs_path, is_normal)}``.

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

    for socket_name, (abs_path, is_normal) in tex_paths.items():
        img_node = nodes.new("ShaderNodeTexImage")
        img_node.location = (x_offset, y_offset)
        img_node.label = socket_name
        y_offset -= 300

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
