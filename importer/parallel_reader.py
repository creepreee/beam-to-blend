from __future__ import annotations

"""Parallel GLB frame reading across worker processes.

BeamNG sequences run to thousands of frames, and each frame read is independent
CPU + I/O work (GLB parse, per-object vertex gather). The per-frame dedup remap
is frame-invariant (see :class:`~importer.gltf_reader.GLBSequenceReader`), so we
compute it **once** in the parent and seed every worker with it via the pool
initializer — every worker then reads at warm-cache speed from its first frame,
paying no re-warm cost.

Design constraints (why it looks the way it does):

* **bpy-free.** This module runs in plain CPython under the pool workers. It must
  never import Blender.
* **Importable by spawned workers.** On Windows (and inside Blender's bundled
  Python) ``multiprocessing`` uses *spawn*: a fresh interpreter that unpickles the
  worker callable by its qualified name, which requires ``import
  importer.parallel_reader`` to succeed *before* any user code runs. We therefore
  publish the package root on ``PYTHONPATH`` (inherited by the child env) in
  :func:`read_frames_parallel`.
* **Bounded memory.** Never hold the whole sequence. We keep at most
  ``workers * 2`` frames in flight and yield results strictly in frame order.
* **Always correct, even if the pool dies.** Any failure creating or running the
  pool falls back to a sequential read that produces identical output.
"""

import os
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

# One-object payload returned to the parent: (vertex_count, positions|None,
# indices|None, uvs|None, material_names|None, face_material_ids|None).
# Every element after vertex_count is omitted (None) when the caller does not
# need it, to keep the pickled payload small. The tuple is always length 6.
FrameRecord = Dict[str, Tuple[int, object, object, object, object, object]]

# Worker-process global: the shared remap cache, seeded once per worker by the
# pool initializer. Frame reads mutate only this worker's own copy (spawn gives
# each worker an independent unpickled dict), so there is no cross-process state.
_WORKER_REMAP: Optional[dict] = None
_WORKER_WANT_POS = True
_WORKER_WANT_IDX = True
_WORKER_WANT_MAT = False
_WORKER_MAT_NAMES: Optional[set] = None


def _worker_init(
    remap: dict,
    want_positions: bool,
    want_indices: bool,
    want_materials: bool,
    material_names: Optional[set],
) -> None:
    global _WORKER_REMAP, _WORKER_WANT_POS, _WORKER_WANT_IDX
    global _WORKER_WANT_MAT, _WORKER_MAT_NAMES
    _WORKER_REMAP = remap
    _WORKER_WANT_POS = want_positions
    _WORKER_WANT_IDX = want_indices
    _WORKER_WANT_MAT = want_materials
    _WORKER_MAT_NAMES = material_names


def _record_from_doc(
    doc, want_positions: bool, want_indices: bool, want_materials: bool
) -> FrameRecord:
    out: FrameRecord = {}
    for obj in doc.objects:
        if want_materials:
            out[obj.name] = (
                obj.vertex_count,
                obj.positions if want_positions else None,
                obj.indices if want_indices else None,
                obj.uvs,
                obj.material_names,
                obj.face_material_ids,
            )
        else:
            out[obj.name] = (
                obj.vertex_count,
                obj.positions if want_positions else None,
                obj.indices if want_indices else None,
                None, None, None,
            )
    return out


def _read_frame(path: str) -> FrameRecord:
    """Pool worker: read one frame through the seeded remap cache."""
    # Late import keeps Blender's copy of numpy/gltf_reader out of pickling and
    # ensures we resolve the same package the parent published on PYTHONPATH.
    from .gltf_reader import read_glb

    doc = read_glb(
        path, remap_cache=_WORKER_REMAP, verbose=False,
        want_materials=_WORKER_WANT_MAT, material_names=_WORKER_MAT_NAMES,
    )
    return _record_from_doc(doc, _WORKER_WANT_POS, _WORKER_WANT_IDX, _WORKER_WANT_MAT)


def _read_frame_sequential(
    path: str, remap: Optional[dict], want_positions: bool, want_indices: bool,
    want_materials: bool, material_names: Optional[set],
) -> FrameRecord:
    from .gltf_reader import read_glb

    doc = read_glb(
        path, remap_cache=remap, verbose=False,
        want_materials=want_materials, material_names=material_names,
    )
    return _record_from_doc(doc, want_positions, want_indices, want_materials)


def _publish_pythonpath() -> None:
    """Ensure the package root is on PYTHONPATH so spawned workers can import us.

    The worker callable is unpickled by qualified name (``importer.parallel_reader
    ._read_frame``), which triggers ``import importer`` in a bare interpreter. We
    add the directory that *contains* the ``importer`` package to both this
    process's ``sys.path`` and the inherited ``PYTHONPATH`` env var.
    """
    pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if pkg_parent not in sys.path:
        sys.path.insert(0, pkg_parent)
    existing = os.environ.get("PYTHONPATH", "")
    parts = existing.split(os.pathsep) if existing else []
    if pkg_parent not in parts:
        os.environ["PYTHONPATH"] = os.pathsep.join([pkg_parent, *parts])


def read_frames_parallel(
    paths: List[Path | str],
    remap: Optional[dict] = None,
    *,
    want_positions: bool = True,
    want_indices: bool = True,
    want_materials: bool = False,
    material_names: Optional[set] = None,
    workers: int = 1,
) -> Iterator[Tuple[int, FrameRecord]]:
    """Yield ``(frame_index, FrameRecord)`` in frame order.

    With ``workers <= 1`` (or if the process pool cannot be created / a task
    fails) this reads sequentially, producing byte-identical output. With
    ``workers > 1`` frames are read across a process pool seeded with *remap*.

    Pass *want_materials* (optionally scoped to a *material_names* set) to also
    gather UVs + material data for those objects — used for dynamic objects whose
    UV/material data changes per frame.

    Memory is bounded to roughly ``workers * 2`` frames in flight.
    """
    str_paths = [os.fspath(p) for p in paths]
    n = len(str_paths)
    if n == 0:
        return

    if workers <= 1:
        yield from _sequential(
            str_paths, remap, want_positions, want_indices,
            want_materials, material_names,
        )
        return

    _publish_pythonpath()
    # Windows spawn re-imports __main__ in the child process, which fails
    # if __main__ imports bpy (or any Blender-only module). Our worker
    # lives in an importable module, so the child doesn't need __main__.
    # Temporarily blank its spec/file so get_preparation_data skips the
    # __main__ re-import, then restore afterward.
    import __main__ as _main_mod
    _saved_spec = getattr(_main_mod, '__spec__', None)
    _saved_file = getattr(_main_mod, '__file__', None)
    _main_mod.__spec__ = None
    _main_mod.__file__ = None  # type: ignore[assignment]
    try:
        pool = ProcessPoolExecutor(
            max_workers=workers,
            initializer=_worker_init,
            initargs=(remap, want_positions, want_indices,
                      want_materials, material_names),
        )
    except Exception as exc:  # pragma: no cover - platform-dependent
        _main_mod.__spec__ = _saved_spec
        _main_mod.__file__ = _saved_file
        sys.stderr.write(
            f"[BeamNG] parallel pool unavailable ({exc}); reading sequentially\n"
        )
        sys.stderr.flush()
        yield from _sequential(
            str_paths, remap, want_positions, want_indices,
            want_materials, material_names,
        )
        return

    # Sliding window of in-flight futures keyed by frame index; emit in order.
    max_inflight = max(2, workers * 2)
    try:
        with pool:
            futures: Dict[int, object] = {}
            next_submit = 0
            next_emit = 0
            while next_emit < n:
                while next_submit < n and len(futures) < max_inflight:
                    futures[next_submit] = pool.submit(
                        _read_frame, str_paths[next_submit]
                    )
                    next_submit += 1
                rec = futures.pop(next_emit).result()
                yield next_emit, rec
                next_emit += 1
    except Exception as exc:
        # A worker died (e.g. spawn import failure). Restart sequentially from
        # where we left off so the caller still gets every frame, in order.
        sys.stderr.write(
            f"[BeamNG] parallel read failed at frame {next_emit} ({exc}); "
            f"falling back to sequential for the remainder\n"
        )
        sys.stderr.flush()
        yield from _sequential(
            str_paths[next_emit:], remap, want_positions, want_indices,
            want_materials, material_names, index_offset=next_emit,
        )
    finally:
        _main_mod.__spec__ = _saved_spec
        _main_mod.__file__ = _saved_file


def _sequential(
    str_paths: List[str],
    remap: Optional[dict],
    want_positions: bool,
    want_indices: bool,
    want_materials: bool = False,
    material_names: Optional[set] = None,
    index_offset: int = 0,
) -> Iterator[Tuple[int, FrameRecord]]:
    """Sequential reader sharing one warm remap cache across frames."""
    # A private cache seeded from *remap* so the first frame is already warm and
    # topology changes still recompute per frame (mirrors the parallel path).
    cache: dict = dict(remap) if remap else {}
    for i, p in enumerate(str_paths):
        rec = _read_frame_sequential(
            p, cache, want_positions, want_indices, want_materials, material_names
        )
        yield index_offset + i, rec
