import json, collections
from pathlib import Path

DATA = Path(__file__).resolve().parents[1] / "tests" / "data"
evs = json.load(open(DATA / "beamng_impact_events.json"))
frames = [e["blender_frame"] for e in evs]
b = collections.Counter((f // 120) * 120 for f in frames)
print("per-120-blender-frame bucket:", dict(sorted(b.items())))
print()
for e in evs:
    print(
        "bf={:5d} cf={:4d} {:12s} {:34s} x={:6.1f} y={:6.1f} z={:6.2f} spd={:.2f} defm={:.3f}".format(
            e["blender_frame"], e["cache_frame"], e["type"], e["part"],
            e["x"], e["y"], e["z"], e["spd"], e["defm"],
        )
    )
