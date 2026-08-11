"""Headless Blender check for the laminated-glass crack shader (W1).

Run with:
    blender --background --python tests/blender_glass_crack.py

Builds a tilted quad pane object, resolves an impact onto it with
``crack_placement``, then builds the full crack material node tree with
``build_crack_material``, assigns it and keyframes the fade-in driver.
Exits non-zero on any failure.
"""

import os
import sys

import bpy

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)
for _mod in [m for m in sys.modules
             if m == "runtime" or m.startswith("runtime.")
             or m == "importer" or m.startswith("importer.")]:
    del sys.modules[_mod]

import numpy as np

from runtime.glass_crack import (
    assign_crack_material,
    build_crack_material,
    crack_placement,
    hole_radius_for,
    keyframe_crack,
    world_to_cache_local,
)

_failures = []


def check(cond, msg):
    if cond:
        print(f"[CRACK][ok]   {msg}")
    else:
        print(f"[CRACK][FAIL] {msg}")
        _failures.append(msg)


def _rotz(deg):
    t = np.deg2rad(deg)
    c, s = np.cos(t), np.sin(t)
    return np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])


bpy.ops.wm.read_factory_settings(use_empty=True)

verts = np.array([[0.0, 0.0, 0.0],
                  [2.0, 0.0, 0.0],
                  [2.0, 1.2, 0.0],
                  [0.0, 1.2, 0.0],
                  [1.0, 0.6, 0.0],
                  [1.8, 0.3, 0.0]], dtype=np.float64)
verts = verts @ _rotz(25.0)  # raked windshield-like pane

mesh = bpy.data.meshes.new("pane")
mesh.from_pydata([tuple(float(c) for c in v) for v in verts],
                 [], [[0, 1, 4], [1, 2, 4], [2, 3, 4], [3, 0, 4], [1, 5, 2]])
mesh.update()
pane = bpy.data.objects.new("windshield", mesh)
bpy.context.collection.objects.link(pane)

impact_world = np.array([2.5, 1.0, 8.0])  # off-plane, outside the pane
transform = np.concatenate((np.array([10.0, -4.0, 2.5]), _rotz(90.0).flatten()))
impact_local = world_to_cache_local(impact_world, transform, ground_shift=1.7)
back_world = impact_local @ transform[3:12].reshape(3, 3).T + transform[0:3] \
    + np.array([0.0, 0.0, 1.7])
check(np.allclose(back_world, impact_world, atol=1e-9),
      "world_to_cache_local inverts local_to_world with ground_shift")

placement = crack_placement(verts, impact_local, hole_radius_for(0.05, 0.9))
check(placement.pane_radius > 0.0, f"pane radius resolved ({placement.pane_radius:.3f} m)")
check(0.0 < placement.hole_radius <= 0.6 * placement.pane_radius,
      f"hole clamped inside pane ({placement.hole_radius:.4f} m)")

mat = None
try:
    mat = build_crack_material("beamng_glass_crack_windshield", placement, 0.8,
                               12345, pane)
except Exception:
    import traceback
    traceback.print_exc()
    raise
check(mat is not None, "material built")

nt = mat.node_tree
check(len(nt.nodes) > 30, f"node tree has {len(nt.nodes)} nodes")
check(len(nt.links) >= 50, f"node tree has {len(nt.links)} links")

out = next((n for n in nt.nodes if n.type == "OUTPUT_MATERIAL"), None)
final = next((n for n in nt.nodes if n.bl_idname == "ShaderNodeMixShader"
              and n.location.x > 800), None)
check(final is not None, "final mix node present")
check(out is not None and out.inputs["Surface"].is_linked,
      "mix chain reaches the output surface")
drv_ok = False
ad = mat.node_tree.animation_data
if ad is not None:
    drv_ok = any(fc.driver is not None and fc.driver.type == "SCRIPTED"
                 and fc.driver.expression == "crack" for fc in ad.drivers)
check(drv_ok, "crack fade-in driver attached to the final mix factor")

assign_crack_material(pane, mat)
check(pane.active_material is mat,
      "crack material assigned to the pane's first material slot")
check(all(p.material_index == 0 for p in pane.data.polygons),
      "every face points at the crack slot")

keyframe_crack(pane, 24, ramp=2)
amount = pane.get("_beamng_crack_amount")
check(amount is not None and float(amount) == 1.0,
      f"crack amount property set ({amount})")
check(pane.animation_data is not None and pane.animation_data.action is not None,
      "fade-in keyframes recorded")

print()
if _failures:
    print(f"[CRACK] {len(_failures)} FAILURES")
    sys.exit(1)
print("[CRACK] all checks passed")
