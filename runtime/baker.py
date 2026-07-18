from __future__ import annotations

"""Bake cache animation into Blender's animation system via .mdd + Mesh Cache modifier.

The runtime frame handler updates mesh positions per frame via Python, but
Blender's Alembic C++ exporter evaluates the depsgraph once and reuses it,
bypassing our ``frame_change_pre`` handler for all frames after the first.

This module provides a **bake step**: it samples the cache for every frame,
writes a .mdd (point-cache) file per object, and attaches a
``MESH_CACHE`` modifier that reads from the .mdd.  The modifier becomes
part of the depsgraph evaluation, so Alembic export sees per-frame
positions as if they were native animation.

Pipeline::

    Import cache (preview)  ->  Bake to .mdd  ->  Alembic export
    (frame handler)             (MESH_CACHE)       (depsgraph sees it)

Usage::

    from runtime.baker import bake_to_mdd
    from runtime.cache_reader import CacheReader

    reader = CacheReader("crash.bvc")
    bake_to_mdd(reader, output_dir="//baked")
    # then export Alembic from Blender normally
    reader.close()
"""

import os
import sys
import tempfile
from typing import List, Optional

import numpy as np

from .cache_reader import CacheReader


class MddWriter:
    """Write a .mdd (Point Cache) file.

    The MDD format stores per-frame vertex positions:
      uint32_t num_frames  (big endian)
      uint32_t num_points  (big endian)
      float   times[num_frames]  (big endian)  # frame timestamps
      float   points[num_frames][num_points][3]  (big endian)

    Blender's Mesh Cache modifier reads MDD in big-endian and byte-swaps
    on little-endian systems.
    """

    def __init__(self, path: str):
        self._path = path
        self._frames: List[np.ndarray] = []

    def add_frame(self, positions: np.ndarray) -> None:
        self._frames.append(positions)

    def write(self) -> None:
        if not self._frames:
            raise ValueError("no frames to write")
        n_frames = len(self._frames)
        n_points = self._frames[0].shape[0]
        with open(self._path, "wb") as f:
            # Header: big-endian uint32
            f.write(np.array([n_frames, n_points], dtype=">u4").tobytes())
            # Timestamps: one float per frame (big-endian)
            times = np.arange(n_frames, dtype=">f4")
            f.write(times.tobytes())
            # Per-frame vertex positions (big-endian floats)
            for pos in self._frames:
                f.write(pos.astype(">f4").tobytes())
        sys.stderr.write(
            "[baker] wrote .mdd: {0} ({1} frames, {2} points, {3:.1f} MB)\n".format(
                self._path, n_frames, n_points,
                os.path.getsize(self._path) / 1e6))
        sys.stderr.flush()


def bake_to_mdd(reader: CacheReader,
                output_dir: str,
                object_names: Optional[List[str]] = None,
                frame_start: int = 0,
                frame_end: Optional[int] = None,
                blend_file_relative: bool = True,
                ) -> List[str]:
    """Sample cache frames and write .mdd files for every stable object.

    Returns the list of object names that were baked (stable objects only;
    dynamic objects are skipped with a warning).

    Parameters
    ----------
    reader:
        Initialised CacheReader.
    output_dir:
        Directory to write .mdd files into.  If ``blend_file_relative``
        is true this should be a ``//``-prefixed Blender-relative path.
    object_names:
        Subset to bake (default: all stable objects).
    frame_start, frame_end:
        0-based cache frame range (default: all frames).
    blend_file_relative:
        If true, ``output_dir`` is treated as Blender-relative (``//``).
    """
    if object_names is None:
        object_names = [o.name for o in reader.stable_objects()]

    n_frames = reader.frame_count
    if frame_end is None:
        frame_end = n_frames - 1

    os.makedirs(output_dir, exist_ok=True)

    baked: List[str] = []
    stable_names = set(o.name for o in reader.stable_objects())

    for name in object_names:
        if name not in stable_names:
            sys.stderr.write("[baker] skip dynamic object: {0}\n".format(name))
            sys.stderr.flush()
            continue

        mdd_path = os.path.join(output_dir, "{0}.mdd".format(name))
        writer = MddWriter(mdd_path)

        fi = reader.base_indices(name)
        sys.stderr.write("[baker] sampling {0} ({1} tris, {2} frames)...\n".format(
            name, len(fi), frame_end - frame_start + 1))
        sys.stderr.flush()

        for f in range(frame_start, frame_end + 1):
            pos = reader.frame_positions(name, f)
            writer.add_frame(pos)

        writer.write()
        baked.append(name)

    return baked


def apply_mdd_modifiers(bpy, object_names: List[str],
                        mdd_dir: str,
                        scene_frame_start: int = 0,
                        ) -> None:
    """Attach MESH_CACHE modifiers to existing mesh objects.

    Must be called from within Blender (``import bpy``).

    Parameters
    ----------
    bpy:
        The ``bpy`` module.
    object_names:
        Objects to attach modifiers to.
    mdd_dir:
        Blender-relative path to .mdd files (e.g. ``//baked``).
    scene_frame_start:
        Scene frame that maps to cache frame 0.
    """
    bpy.context.view_layer.update()
    for name in object_names:
        obj = bpy.data.objects.get(name)
        if obj is None or obj.type != 'MESH':
            sys.stderr.write("[baker] object not found (skipping): {0}\n".format(name))
            sys.stderr.flush()
            continue

        # Remove any existing MESH_CACHE modifier
        for mod in list(obj.modifiers):
            if mod.type == 'MESH_CACHE':
                obj.modifiers.remove(mod)

        mod = obj.modifiers.new(name="BeamNG_MDD", type='MESH_CACHE')
        mod.cache_format = 'MDD'
        mod.filepath = "{0}{1}.mdd".format(mdd_dir, name)
        mod.time_mode = 'FRAME'
        mod.play_mode = 'SCENE'
        mod.frame_start = float(scene_frame_start)
        mod.frame_scale = 1.0
        mod.interpolation = 'LINEAR'
        mod.deform_mode = 'OVERWRITE'
        # Map file coords (X=right, Y=forward, Z=up) to Blender coords.
        # Blender's forward is -Y, so POS_Y maps file +Y → Blender forward.
        # Tested empirically: POS_Y, POS_Z gives identity mapping.
        mod.forward_axis = 'POS_Y'
        mod.up_axis = 'POS_Z'

        sys.stderr.write(
            "[baker] MESH_CACHE on {0}: file={1}, frame_start={2}\n".format(
                name, mod.filepath, mod.frame_start))
        sys.stderr.flush()


def bake_and_prepare_for_export(reader: CacheReader,
                                 bpy,
                                 output_dir: str = "//baked",
                                 scene_frame_start: int = 1,
                                 ) -> None:
    """One-step bake: write .mdd files + attach modifiers.

    After calling this, the scene is ready for Alembic export with
    ``export_animation=True``.
    """
    abs_dir = os.path.abspath(bpy.path.abspath(output_dir))
    names = bake_to_mdd(reader, abs_dir, frame_start=0,
                        frame_end=reader.frame_count - 1)
    apply_mdd_modifiers(bpy, names, output_dir, scene_frame_start)

    sys.stderr.write(
        "[baker] ready for Alembic export: {0} objects baked, "
        "frame range {1}..{2}\n".format(
            len(names), scene_frame_start,
            scene_frame_start + reader.frame_count - 1))
    sys.stderr.flush()
