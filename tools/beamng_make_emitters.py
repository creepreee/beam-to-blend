import json, math
from pathlib import Path

DATA = Path(__file__).resolve().parents[1] / "tests" / "data"
evs = json.load(open(DATA / "beamng_impact_events.json"))

# merge events of same type with nearby position and time
TYPE_ORDER = ["paint_white", "glass", "plastic", "steel_dark", "rubber_steel"]

def cluster_events(events, time_gap=6, dist_gap=6.0):
    events = sorted(events, key=lambda e: (e["blender_frame"], e["part"]))
    clusters = []
    for e in events:
        placed = False
        for c in clusters:
            if e["type"] != c["type"]:
                continue
            d = math.hypot(e["x"] - c["x"], e["y"] - c["y"])
            if abs(e["blender_frame"] - c["frame"]) <= time_gap and d <= dist_gap:
                n = c["n"]
                c["x"] = (c["x"] * n + e["x"]) / (n + 1)
                c["y"] = (c["y"] * n + e["y"]) / (n + 1)
                c["frame"] = min(c["frame"], e["blender_frame"])
                c["energy"] += e["energy"]
                c["maxspd"] = max(c["maxspd"], e["spd"])
                c["parts"].append(e["part"])
                c["n"] += 1
                placed = True
                break
        if not placed:
            clusters.append({
                "type": e["type"], "frame": e["blender_frame"],
                "x": e["x"], "y": e["y"], "energy": e["energy"],
                "maxspd": e["spd"], "parts": [e["part"]], "n": 1,
            })
    clusters.sort(key=lambda c: (c["frame"], TYPE_ORDER.index(c["type"])))
    return clusters

cls = cluster_events(evs)
print("emitter clusters:", len(cls))
for c in cls:
    print("  type={:12s} frame={:5d} x={:6.1f} y={:6.1f} n={:2d} maxspd={:.2f} parts={}".format(
        c["type"], c["frame"], c["x"], c["y"], c["n"], c["maxspd"], c["parts"]))

json.dump(cls, open(DATA / "beamng_emitters.json", "w"), indent=1)
