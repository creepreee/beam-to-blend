from __future__ import annotations

import json
from dataclasses import dataclass, asdict, field
from typing import Dict, List, Optional


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
