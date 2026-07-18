from __future__ import annotations

"""Write Alembic (.abc) files directly from cache data using the Ogawa format.

Blender's own alembic_export() does not re-evaluate the depsgraph per frame
when iterating, which means frame_change_pre handlers never fire for frames
2..N in background mode.  This module works around that by producing the
.abc file entirely from Python, feeding per-frame vertex positions straight
from CacheReader into an Ogawa binary stream.

Usage::

    from runtime.abc_writer import write_sequence
    from runtime.cache_reader import CacheReader

    reader = CacheReader("crash.bvc")
    write_sequence("crash.abc", reader, verbose=True)
    reader.close()
"""

import struct
import os
import sys
from typing import List, Optional, Dict, IO
import numpy as np

from .cache_reader import CacheReader


# ---------------------------------------------------------------------------
#  Pod / property constants  (Alembic / Abc)
# ---------------------------------------------------------------------------
POD_F32 = 9
POD_U32 = 6
POD_STR = 11
EXTENT_SCALAR = 1
EXTENT_VEC3 = 3
HAS_SAMPLES = 0x20000000


# ---------------------------------------------------------------------------
#  Internal tree nodes
# ---------------------------------------------------------------------------
class _DataBlock:
    __slots__ = ("data",)
    def __init__(self, data: bytes):
        self.data = data


class _Prop:
    __slots__ = ("name", "pod_type", "extent", "data_block",
                 "time_sampling", "is_scalar")
    def __init__(self, name: str, pod_type: int, extent: int,
                 data_block: _DataBlock, *,
                 time_sampling: int = 0, is_scalar: bool = False):
        self.name = name
        self.pod_type = pod_type
        self.extent = extent
        self.data_block = data_block
        self.time_sampling = time_sampling
        self.is_scalar = is_scalar


class _Group:
    __slots__ = ("children", "props", "data")
    def __init__(self):
        self.children: List[_Group] = []
        self.props: List[_Prop] = []
        self.data: Optional[_DataBlock] = None


# ---------------------------------------------------------------------------
#  Ogawa I/O helpers
# ---------------------------------------------------------------------------
def _wu64(fp: IO[bytes], v: int) -> None:
    fp.write(struct.pack(">Q", v))

def _wu32(fp: IO[bytes], v: int) -> None:
    fp.write(struct.pack(">I", v))

def _wu8(fp: IO[bytes], v: int) -> None:
    fp.write(struct.pack(">B", v))

def _wpad(fp: IO[bytes], n: int) -> None:
    if n > 0:
        fp.write(b'\x00' * n)


# ---------------------------------------------------------------------------
#  Collect all distinct data blocks from a group tree
# ---------------------------------------------------------------------------
def _collect_blocks(g: _Group, out: List[_DataBlock]) -> None:
    if g.data is not None and g.data not in out:
        out.append(g.data)
    for c in g.children:
        _collect_blocks(c, out)
    for p in g.props:
        if p.data_block not in out:
            out.append(p.data_block)


# ---------------------------------------------------------------------------
#  Write the data blocks (raw binary blobs with size prefix)
# ---------------------------------------------------------------------------
def _write_data_blocks(fp: IO[bytes],
                       blocks: List[_DataBlock]) -> Dict[int, int]:
    """Write data blocks, return {id: file_offset} map."""
    offsets: Dict[int, int] = {}
    for b in blocks:
        offsets[id(b)] = fp.tell()
        _wu64(fp, len(b.data))
        fp.write(b.data)
    return offsets


# ---------------------------------------------------------------------------
#  Write groups bottom-up (post-order), return {id: file_offset}
# ---------------------------------------------------------------------------
def _write_groups(fp: IO[bytes],
                  roots: List[_Group],
                  block_offsets: Dict[int, int]) -> Dict[int, int]:
    """Write all groups in post-order (children before parents).

    Returns {id(group): file_offset_of_group_block}.
    """
    written: Dict[int, int] = {}

    def _emit(g: _Group) -> int:
        if id(g) in written:
            return written[id(g)]
        # Write children first
        child_offsets = [_emit(c) for c in g.children]
        # Now write this group
        pos = fp.tell()
        _wu64(fp, len(g.children))
        _wu64(fp, len(g.props))
        for i, c in enumerate(g.children):
            _wu64(fp, child_offsets[i])
            _wu64(fp, len(c.children))
            _wu64(fp, len(c.props))
            _wpad(fp, 4)
        for p in g.props:
            doff = block_offsets.get(id(p.data_block), 0)
            pt = 0 if p.is_scalar else 1
            if p.time_sampling > 0:
                pt |= HAS_SAMPLES
            _wu64(fp, doff)
            _wu32(fp, pt)
            _wu8(fp, p.pod_type)
            _wu8(fp, p.extent)
            nb = p.name.encode("utf-8")
            _wu32(fp, len(nb))
            fp.write(nb)
            _wu32(fp, p.time_sampling)
        written[id(g)] = pos
        return pos

    for r in roots:
        _emit(r)
    return written


# ---------------------------------------------------------------------------
#  Build Alembic mesh object
# ---------------------------------------------------------------------------
def _make_obj(name: str,
              fc_bytes: bytes,
              fi_bytes: bytes,
              pos_list: List[bytes]) -> _Group:
    """Build an Alembic mesh group: constant topology + time-sampled positions."""

    geom = _Group()
    # .faceCounts (constant array)
    geom.props.append(_Prop(
        ".faceCounts", POD_U32, EXTENT_SCALAR, _DataBlock(fc_bytes)))
    # .faceIndices (constant array)
    geom.props.append(_Prop(
        ".faceIndices", POD_U32, EXTENT_SCALAR, _DataBlock(fi_bytes)))
    # .positions (time-sampled, one sample per frame)
    for pb in pos_list:
        geom.props.append(_Prop(
            ".positions", POD_F32, EXTENT_VEC3,
            _DataBlock(pb), time_sampling=1))

    obj = _Group()
    obj.children.append(geom)
    # Compound property ".geom" referencing the geom child group
    obj.props.append(_Prop(
        ".geom", POD_STR, EXTENT_SCALAR, _DataBlock(b"")))

    # Metadata
    meta = '{{"name":"{0}","type":"AbcGeom::MeshSchema_v1"}}'.format(name)
    meta_b = struct.pack(">I", len(meta)) + meta.encode("utf-8")
    obj.props.append(_Prop(
        ".mesh", POD_STR, EXTENT_SCALAR,
        _DataBlock(meta_b), is_scalar=True))

    return obj


# ---------------------------------------------------------------------------
#  Public API
# ---------------------------------------------------------------------------

def write_sequence(output_path: str, reader: CacheReader,
                   object_names: Optional[List[str]] = None,
                   frame_start: int = 0, frame_end: Optional[int] = None,
                   verbose: bool = False) -> None:
    """Write a multi-frame Alembic file from a CacheReader.

    Parameters
    ----------
    output_path:
        Path for the .abc file.
    reader:
        Initialised CacheReader.
    object_names:
        Subset of objects to include (default: all stable + dynamic).
    frame_start, frame_end:
        0-based cache-frame indices (default: all frames).
    verbose:
        Print progress to stderr.
    """
    if object_names is None:
        object_names = [o.name for o in reader.stable_objects()]
        object_names += [o.name for o in reader.dynamic_objects()]

    n_frames = reader.frame_count
    if frame_end is None:
        frame_end = n_frames - 1

    stable_names = set(o.name for o in reader.stable_objects())
    # dynamic_set = set(reader.dynamic_objects())

    if verbose:
        sys.stderr.write(
            "[abc] {0} objects, {1} frames -> {2}\n".format(
                len(object_names), frame_end - frame_start + 1, output_path))
        sys.stderr.flush()

    obj_groups: List[_Group] = []

    for name in object_names:
        if name not in stable_names:
            if verbose:
                sys.stderr.write("[abc]  skip dynamic '{0}'\n".format(name))
                sys.stderr.flush()
            continue

        fi = reader.base_indices(name)
        pos_list: List[bytes] = []
        for f in range(frame_start, frame_end + 1):
            pos = reader.frame_positions(name, f)
            if pos is None:
                raise ValueError("Stable object '{0}' missing frame {1}".format(name, f))
            pos_list.append(pos.astype("<f4").tobytes())

        n_tri = len(fi)
        fc_b = np.full(n_tri, 3, dtype=np.uint32).tobytes()
        fi_b = fi.astype(np.uint32).ravel().tobytes()

        obj_groups.append(_make_obj(name, fc_b, fi_b, pos_list))

        if verbose:
            sys.stderr.write("[abc]  + {0}: {1} tris, {2} frames\n".format(
                name, n_tri, len(pos_list)))
            sys.stderr.flush()

    if not obj_groups:
        raise ValueError("No objects to export (is the cache imported?)")

    # Time-sampling block: uniform, 1 sample per frame, 24 fps
    ts = struct.pack(">II", 0, 1)
    ts += struct.pack(">ddd", 0.0, (n_frames - 1) / 24.0, 1.0 / 24.0)
    ts_block = _DataBlock(ts)

    root = _Group()
    root.data = ts_block
    root.children = obj_groups

    # Collect all data blocks
    all_blocks: List[_DataBlock] = []
    _collect_blocks(root, all_blocks)

    with open(output_path, "wb") as fp:
        # ---- Header ----
        fp.write(b"Ogawa\x05\x00\x00")
        root_off_pos = fp.tell()
        _wu64(fp, 0)

        # ---- Data blocks ----
        block_offsets = _write_data_blocks(fp, all_blocks)
        _wpad(fp, 7)

        # ---- Groups (post-order) ----
        group_offsets = _write_groups(fp, [root], block_offsets)
        root_off = group_offsets[id(root)]

        # ---- Patch header root offset ----
        fp.seek(root_off_pos)
        _wu64(fp, root_off)

    if verbose:
        abspath = os.path.abspath(output_path)
        size_mb = os.path.getsize(abspath) / 1e6
        sys.stderr.write("[abc] {0:.1f} MB -> {1}\n".format(size_mb, abspath))
        sys.stderr.flush()
