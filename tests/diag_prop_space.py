"""Diagnose whether prop vertices are in the same coordinate space as flexmesh."""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
from importer.capture_reader import CaptureReader

BIN = r"C:\Users\ubaid_i2c\AppData\Local\BeamNG\BeamNG.drive\current\captures\mycap2\capture.bin"
with CaptureReader(BIN) as r:
    meta = r.meta
    names = [o.name for o in meta.objects]
    print(f"Total objects: {len(names)}")
    props = [(i, n) for i, n in enumerate(names) if n.startswith("prop_")]
    flex = [(i, n) for i, n in enumerate(names) if not n.startswith("prop_")]
    print(f"Props: {len(props)}, Flexmesh: {len(flex)}")

    # Compare a prop with a nearby flexmesh
    pi, pn = props[0]
    fi, fn = flex[0]

    pp = r.frame_positions(0, pi).astype(np.float64)
    fp = r.frame_positions(0, fi).astype(np.float64)

    print(f"\nProp '{pn}' (frame 0):")
    print(f"  center=({pp[:,0].mean():.4f},{pp[:,1].mean():.4f},{pp[:,2].mean():.4f})")
    print(f"  span =({pp[:,0].ptp():.4f},{pp[:,1].ptp():.4f},{pp[:,2].ptp():.4f})")

    print(f"\nFlexmesh '{fn}' (frame 0):")
    print(f"  center=({fp[:,0].mean():.4f},{fp[:,1].mean():.4f},{fp[:,2].mean():.4f})")
    print(f"  span =({fp[:,0].ptp():.4f},{fp[:,1].ptp():.4f},{fp[:,2].ptp():.4f})")

    # Bounding box check: prop should be inside the car bbox
    print(f"\nCar (flexmesh) bbox: X=[{fp[:,0].min():.4f},{fp[:,0].max():.4f}] "
          f"Y=[{fp[:,1].min():.4f},{fp[:,1].max():.4f}] "
          f"Z=[{fp[:,2].min():.4f},{fp[:,2].max():.4f}]")
    print(f"Prop bbox:         X=[{pp[:,0].min():.4f},{pp[:,0].max():.4f}] "
          f"Y=[{pp[:,1].min():.4f},{pp[:,1].max():.4f}] "
          f"Z=[{pp[:,2].min():.4f},{pp[:,2].max():.4f}]")

    # If prop is in Z-up physics space while flexmesh is Y-up pool,
    # the prop's Y and Z would be swapped relative to flexmesh.
    # Check if prop's vertical axis (phys Z=up) aligns with flexmesh's Y (pool up)
    # or flexmesh's Z (pool width).
    pp_span_y = pp[:, 1].ptp()  # prop's Y
    pp_span_z = pp[:, 2].ptp()  # prop's Z
    print(f"\nIf prop is Z-up physics:            Y=up({pp_span_y:.4f}) Z=fwd({pp_span_z:.4f})")
    print(f"  (steering wheel: up≈0.3, fwd≈0.1)")
    print(f"If prop is Y-up pool:               Y=up({pp_span_y:.4f}) Z=right({pp_span_z:.4f})")
    print(f"  (steering wheel: up≈0.3, right≈0.2)")

    # Check specific props
    for p_name in ["prop_flanje_e180_steer", "prop_flanje_e180_needle_speedo",
                   "prop_flanje_e180_gaspedal", "prop_flanje_e180_brakepedal"]:
        for idx, name in names:
            if name == p_name:
                pos = r.frame_positions(0, idx).astype(np.float64)
                print(f"\n'{name}':")
                print(f"  span=({pos[:,0].ptp():.4f},{pos[:,1].ptp():.4f},{pos[:,2].ptp():.4f})")
                print(f"  center=({pos[:,0].mean():.4f},{pos[:,1].mean():.4f},{pos[:,2].mean():.4f})")
                break
