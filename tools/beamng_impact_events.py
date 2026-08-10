import json
from pathlib import Path
import numpy as np

DATA = Path(__file__).resolve().parents[1] / "tests" / "data"
NPZ = DATA / "beamng_impact_data.npz"
OUT = DATA / "beamng_impact_events.json"

d = np.load(NPZ)
zmin, cx, cy, defm, spd, names = d["zmin"], d["cx"], d["cy"], d["defm"], d["spd"], d["names"]
NF = zmin.shape[1]
E = 0.5 * spd * spd

GLASS = ["windshield", "backlight", "doorglass", "headlightglass", "taillightglass", "taillight_trunkglass"]
PAINT = ["body", "hood", "trunk", "door_", "fender", "mirror"]
PLASTIC = ["bumper", "headlight_", "taillight_", "licenseplate"]
WHEEL = ["tire_", "wheel", "hub_", "brake_", "caliper"]
OTHER = []  # steel_dark default

def classify(name):
    if any(g in name for g in GLASS):
        return "glass"
    if any(p in name for p in PAINT):
        return "paint_white"
    if any(p in name for p in PLASTIC):
        return "plastic"
    if any(w in name for w in WHEEL):
        return "rubber_steel"
    return "steel_dark"

def cluster(frames, gap):
    groups = []
    for f in frames:
        if groups and f - groups[-1][-1] <= gap:
            groups[-1].append(f)
        else:
            groups.append([f])
    return groups

def detect_strikes(i, zthresh=0.0, spdthresh=0.30, mindown=0.10, cluster_gap=30):
    z, s, v = zmin[i], spd[i], np.zeros(NF)
    v[1:] = np.diff(z)
    frames = []
    prev_below = False
    for f in range(1, NF):
        below = z[f] < zthresh
        if below and not prev_below and v[f] < -mindown and s[f] > spdthresh:
            frames.append(f)
        prev_below = below
    return frames

def detect_spd_spikes(i, thresh, min_dur=0, cluster_gap=40):
    s = spd[i]
    on = s > thresh
    frames = []
    run = 0
    for f in range(NF):
        if on[f]:
            run += 1
            if run >= 1 + min_dur:
                frames.append(f)
        else:
            run = 0
    return cluster(frames, cluster_gap)

events = []
for idx, name in enumerate(names):
    cat = classify(name)
    is_wheel = cat == "rubber_steel"
    if is_wheel:
        groups = detect_spd_spikes(idx, thresh=0.55)
    else:
        groups = cluster(detect_strikes(idx), 30)
    for grp in groups:
        g = np.array(grp)
        fe = grp[int(np.argmax(E[idx][g]) if len(grp) > 1 else 0)]
        if is_wheel and zmin[idx][fe] > 0.2:
            continue
        ev = {
            "part": name,
            "type": cat,
            "cache_frame": int(fe),
            "blender_frame": int(round(fe * 60 / 24 + 400)),
            "x": float(cx[idx][fe]),
            "y": float(cy[idx][fe]),
            "z": float(zmin[idx][fe]),
            "spd": float(spd[idx][fe]),
            "defm": float(defm[idx][fe]),
            "energy": float(E[idx][fe]),
        }
        events.append(ev)

# sort by blender frame
events.sort(key=lambda e: (e["blender_frame"], e["part"]))

with open(OUT, "w") as f:
    json.dump(events, f, indent=1)

print(f"total events: {len(events)}")
from collections import Counter
print("by type:", dict(Counter(e["type"] for e in events)))
print("by part count top:", dict(Counter(e["part"] for e in events).most_common(12)))
