from __future__ import annotations

import sys
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Dict, List, Optional
import json

from .gltf_reader import GLBSequenceReader
from .topology import TopologyHasher


@dataclass
class ObjectSignature:
    name: str
    vertex_count: int
    edge_count: int
    face_count: int
    topology_hash: str
    cacheable: bool = True
    fallback_reason: Optional[str] = None


@dataclass
class SequenceManifest:
    sequence_name: str
    frame_count: int
    objects: Dict[str, ObjectSignature] = field(default_factory=dict)
    stable_objects: List[str] = field(default_factory=list)
    dynamic_objects: List[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(
            {
                "sequence_name": self.sequence_name,
                "frame_count": self.frame_count,
                "objects": {k: asdict(v) for k, v in self.objects.items()},
                "stable_objects": self.stable_objects,
                "dynamic_objects": self.dynamic_objects,
            },
            indent=2,
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, text: str) -> SequenceManifest:
        data = json.loads(text)
        objects = {}
        for k, v in data.get("objects", {}).items():
            objects[k] = ObjectSignature(**v)
        return cls(
            sequence_name=data["sequence_name"],
            frame_count=data["frame_count"],
            objects=objects,
            stable_objects=data.get("stable_objects", []),
            dynamic_objects=data.get("dynamic_objects", []),
        )


def _edge_count(indices, face_count: int) -> int:
    """Unique undirected edge count from triangle indices.

    Cheap and only needs computing once (first frame) since it is derived from
    connectivity, which is what we are classifying as stable. Accepts the raw
    index array (or ``None`` for implicit triangle soup) plus the face count.
    """
    if indices is None:
        # Implicit triangle soup: 3 edges per triangle, no shared edges assumed.
        return face_count * 3
    tris = indices
    e = tris[:, [0, 1, 1, 2, 2, 0]].reshape(-1, 2)
    e.sort(axis=1)
    unique = {(int(a), int(b)) for a, b in e}
    return len(unique)


def _face_count(indices, vertex_count: int) -> int:
    return int(indices.shape[0]) if indices is not None else vertex_count // 3


class SequenceScanner:
    """Scans a folder of GLB frames and classifies objects by topology stability.

    Memory-light: only one frame's meshes are held in RAM at a time. The first
    frame establishes each object's baseline signature; every later frame is
    checked for topology drift (changed vertex count or connectivity, or an
    object appearing/disappearing).
    """

    def __init__(self, sequence_dir: Path):
        self.sequence_dir = Path(sequence_dir)

    def discover_frames(self) -> List[Path]:
        return sorted(self.sequence_dir.glob("*.glb"))

    def scan(
        self, sequence_name: Optional[str] = None, workers: int = 1
    ) -> SequenceManifest:
        frames = self.discover_frames()
        if not frames:
            raise FileNotFoundError(f"no .glb frames in {self.sequence_dir}")

        total = len(frames)
        name = sequence_name or self.sequence_dir.name
        manifest = SequenceManifest(sequence_name=name, frame_count=total)

        sys.stderr.write(f"[BeamNG] Scanning {total} frames in {self.sequence_dir} ..." + chr(10))
        sys.stdout.flush()

        # Warm the remap cache on frame 0 so parallel workers start warm. The
        # cache only recomputes on genuine topology changes — exactly what we
        # are scanning for — so parallel reads stay correct.
        from .parallel_reader import read_frames_parallel

        reader = GLBSequenceReader()

        # --- Frame 0: establish baseline signatures ---------------------
        sys.stderr.write(f"[BeamNG]   frame 0/{total} (baseline) ..." + chr(10))
        sys.stdout.flush()
        first = reader.read(frames[0]).by_name()
        baseline_hash: Dict[str, str] = {}
        for obj_name, obj in first.items():
            topo_hash = TopologyHasher.hash_indices(obj.vertex_count, obj.indices)
            baseline_hash[obj_name] = topo_hash
            manifest.objects[obj_name] = ObjectSignature(
                name=obj_name,
                vertex_count=obj.vertex_count,
                edge_count=_edge_count(obj.indices, obj.face_count),
                face_count=obj.face_count,
                topology_hash=topo_hash,
            )
        sys.stderr.write(f"[BeamNG]   frame 0/{total} — {len(manifest.objects)} objects" + chr(10))
        sys.stdout.flush()

        if workers > 1:
            sys.stderr.write(
                f"[BeamNG]   scanning frames 1..{total - 1} with {workers} worker processes ...\n"
            )
            sys.stderr.flush()

        # --- Frames 1..N: detect drift ----------------------------------
        # Positions are irrelevant to classification; read indices only.
        for i, record in read_frames_parallel(
            frames[1:], reader.remap_cache,
            want_positions=False, want_indices=True, workers=workers,
        ):
            frame_index = i + 1  # read_frames_parallel indexes the frames[1:] slice
            frame_path = frames[frame_index]
            sys.stderr.write(f"[BeamNG]   frame {frame_index}/{total} ({frame_path.name}) — checking topology ..." + chr(10))
            sys.stdout.flush()

            # Object present at baseline but missing this frame -> dynamic.
            for obj_name, sig in manifest.objects.items():
                if not sig.cacheable:
                    continue
                entry = record.get(obj_name)
                if entry is None:
                    self._mark_dynamic(
                        sig, f"missing in frame {frame_path.name}"
                    )
                    continue
                vcount_obj, _pos, indices, _uv, _mats, _matids = entry
                topo_hash = TopologyHasher.hash_indices(vcount_obj, indices)
                if topo_hash != baseline_hash[obj_name]:
                    reason = (
                        f"topology changed in {frame_path.name} "
                        f"({sig.vertex_count} verts -> {vcount_obj})"
                    )
                    self._mark_dynamic(sig, reason)

            # Objects appearing only in later frames -> record as dynamic.
            for obj_name, entry in record.items():
                if obj_name not in manifest.objects:
                    vcount_obj, _pos, indices, _uv, _mats, _matids = entry
                    fc = _face_count(indices, vcount_obj)
                    sig = ObjectSignature(
                        name=obj_name,
                        vertex_count=vcount_obj,
                        edge_count=_edge_count(indices, fc),
                        face_count=fc,
                        topology_hash=TopologyHasher.hash_indices(
                            vcount_obj, indices
                        ),
                    )
                    self._mark_dynamic(
                        sig, f"first appears in {frame_path.name}"
                    )
                    manifest.objects[obj_name] = sig
                    baseline_hash[obj_name] = sig.topology_hash

        manifest.stable_objects = sorted(
            n for n, s in manifest.objects.items() if s.cacheable
        )
        manifest.dynamic_objects = sorted(
            n for n, s in manifest.objects.items() if not s.cacheable
        )
        print(f"[BeamNG] scan done: {len(manifest.stable_objects)} stable, "
              f"{len(manifest.dynamic_objects)} dynamic "
              f"({total} frames)")
        return manifest

    @staticmethod
    def _mark_dynamic(sig: ObjectSignature, reason: str) -> None:
        if sig.cacheable:
            sig.cacheable = False
            sig.fallback_reason = reason
