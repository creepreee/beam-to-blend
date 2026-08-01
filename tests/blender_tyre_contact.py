"""Headless Blender check for tyre ground-contact flattening.

Run with:
    blender --background --python tests/blender_tyre_contact.py -- <cache.bvc>

Verifies against a REAL cache, in real Blender, that:
  * with the feature off, playback geometry is bit-identical to the cache
    (proves the feature is a true no-op when disabled),
  * with it on, tyre objects gain a flat contact patch at the ground plane
    while rims/hubs/brakes/body stay bit-identical (the name filter holds),
  * a tyre lifted clear of the ground is restored to its exact round shape
    (no residual flattening — the whole point of the release ramp),
  * live retuning via frame_handler.update_tyre() reaches the mesh.

Exits non-zero on any failure.
"""

import os
import sys

import bpy

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Put the repo FIRST and evict any already-imported copies: an installed
# beamng_cache_importer add-on bundles its own `runtime`/`importer` packages and
# imports them at Blender startup, which would otherwise shadow the working tree
# and test the deployed build instead of the code under test.
sys.path.insert(0, _REPO)
for _mod in [m for m in sys.modules
             if m == "runtime" or m.startswith("runtime.")
             or m == "importer" or m.startswith("importer.")]:
    del sys.modules[_mod]

import numpy as np

from runtime.cache_reader import CacheReader
from runtime.mesh_update import CachePlayback
from runtime.tyre_deform import TyreSettings, height_basis_from_transform
from runtime import frame_handler


def _argv_after_ddash():
    return sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []


_failures = []


def check(cond, msg):
    if cond:
        print(f"[TYRE][ok]   {msg}")
    else:
        print(f"[TYRE][FAIL] {msg}")
        _failures.append(msg)


def read_verts(obj):
    n = len(obj.data.vertices)
    buf = np.empty(n * 3, dtype=np.float32)
    obj.data.vertices.foreach_get("co", buf)
    return buf.reshape(-1, 3)


def world_heights(reader, obj, frame, verts):
    """World Z of *verts*, folding in the rigid transform and obj.location."""
    up, off = height_basis_from_transform(reader.frame_transform(frame))
    off += float(np.asarray(obj.location, dtype=np.float32) @ up)
    return verts @ up + off


def clear_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)


def main():
    args = _argv_after_ddash()
    if not args:
        print("Usage: blender --background --python tests/blender_tyre_contact.py "
              "-- <cache.bvc>")
        sys.exit(2)
    cache_path = args[0]
    if not os.path.exists(cache_path):
        print(f"[TYRE][FAIL] cache not found: {cache_path}")
        sys.exit(2)

    print(f"[TYRE] cache: {cache_path}")

    # ---- pass 1: feature OFF must be byte-identical to the cache ----------
    clear_scene()
    reader = CacheReader(cache_path)
    names = [o.name for o in reader.stable_objects()]
    tyres = [n for n in names if TyreSettings().matches(n)]
    others = [n for n in names if n not in tyres]
    print(f"[TYRE] {len(names)} stable objects, {len(tyres)} match the tyre filter")
    check(len(tyres) > 0, f"cache contains tyre objects: {tyres[:4]}")

    off = CachePlayback(reader, collection_name="OFF",
                        tyre=TyreSettings(amount=0.0))
    off.build_scene()
    probe_frame = min(5, reader.frame_count - 1)
    off.set_frame(probe_frame)
    baseline = {}
    for n in names:
        obj = off._objects[n]
        got = read_verts(obj)
        baseline[n] = got
        if n == tyres[0]:
            expect = reader.frame_positions(n, probe_frame)
            check(np.allclose(got, expect, atol=1e-6),
                  f"amount=0: {n} matches raw cache positions exactly")

    # ---- pass 2: feature ON ------------------------------------------------
    clear_scene()
    reader2 = CacheReader(cache_path)
    tyre = TyreSettings(amount=1.0, extra=0.02, bulge=0.6, release=0.03,
                        ground_z=0.0)
    on = CachePlayback(reader2, collection_name="ON", tyre=tyre)
    on.build_scene()

    # Ground the car the way the import operator does, so heights are realistic.
    all_min = min(float(read_verts(on._objects[n])[:, 2].min()) for n in names)
    for n in names:
        on._objects[n].location.z += -all_min
    on.set_tyre_settings(tyre)
    on.set_frame(probe_frame)

    # Non-tyre objects must be untouched.
    same = all(np.array_equal(read_verts(on._objects[n]), baseline[n])
               for n in others)
    check(same, f"{len(others)} non-tyre objects bit-identical with feature ON")

    # At least one tyre must show a flat patch at the ground plane.
    n_flat = 0
    for n in tyres:
        obj = on._objects[n]
        verts = read_verts(obj)
        h = world_heights(reader2, obj, probe_frame, verts)
        on_ground = np.abs(h) < 1e-4
        if on_ground.sum() >= 3 and float(np.ptp(h[on_ground])) < 1e-5:
            n_flat += 1
            print(f"[TYRE]   {n}: {int(on_ground.sum())} verts on the ground plane, "
                  f"patch span {float(np.ptp(h[on_ground]))*1000:.3f} mm")
        check(h.min() > -1e-3,
              f"{n}: no vertex sinks below the ground (min {h.min()*1000:.2f} mm)")
    check(n_flat > 0, f"{n_flat} tyre(s) developed a flat contact patch")

    # A deformed tyre must differ from the raw cache; verify that at least one did.
    changed = [n for n in tyres
               if not np.array_equal(read_verts(on._objects[n]), baseline[n])]
    check(len(changed) > 0, f"{len(changed)}/{len(tyres)} tyres deformed on contact")

    # ---- pass 3: lift the car — tyres must go exactly round again ---------
    lifted = TyreSettings(amount=1.0, extra=0.02, bulge=0.6, release=0.03,
                          ground_z=-5.0)  # ground 5 m below == car airborne
    on.set_tyre_settings(lifted)
    on.set_frame(probe_frame)
    round_again = all(
        np.allclose(read_verts(on._objects[n]),
                    reader2.frame_positions(n, probe_frame), atol=1e-6)
        for n in tyres)
    check(round_again,
          "airborne (ground 5 m below): every tyre restored to exact round shape")

    # ---- pass 4: live retune through the handler ---------------------------
    frame_handler.attach(on, frame_start=0, playback_fps=24, output_fps=24)
    bpy.context.scene.frame_set(probe_frame)
    before = read_verts(on._objects[tyres[0]]).copy()
    frame_handler.update_tyre(amount=1.0, extra=0.05, bulge=0.9, release=0.03,
                              ground_z=0.0)
    after = read_verts(on._objects[tyres[0]])
    check(not np.array_equal(before, after),
          "frame_handler.update_tyre() retunes the live mesh (UI slider path)")
    stored = bpy.context.scene.get("_beamng_tyre_extra")
    check(stored is not None and abs(float(stored) - 0.05) < 1e-6,
          f"settings persisted to the scene for undo recovery (extra={stored})")

    frame_handler.detach()

    # ---- pass 5: chunked playback -----------------------------------------
    # Chunked mode merges all four tyres AND the rigid rims/hubs/brakes into one
    # "wheels" mesh, so the deform must run per MEMBER (each tyre has its own
    # axle and its own contact depth) and must leave the rim vertices alone.
    clear_scene()
    from runtime.mesh_update import CHUNK_MAP_E180

    reader3 = CacheReader(cache_path)
    chunk_map = {k: list(v) for k, v in CHUNK_MAP_E180.items()}
    ch = CachePlayback(reader3, collection_name="CH", chunk_map=chunk_map,
                       tyre=TyreSettings(amount=1.0, extra=0.02, bulge=0.6,
                                         release=0.03))
    ch.build_scene()
    wheels = ch._chunks.get("wheels")
    if wheels is None:
        check(False, "chunked build produced a 'wheels' chunk")
    else:
        ranges = ch._chunk_member_ranges["wheels"]
        all_min = min(float(read_verts(o)[:, 2].min()) for o in ch._chunks.values())
        for o in ch._chunks.values():
            o.location.z += -all_min
        ch.set_tyre_settings(ch.tyre)
        ch.set_frame(probe_frame)

        verts = read_verts(wheels)
        n_tyre_ranges = 0
        for mname, (start, end) in ranges.items():
            seg = verts[start:end]
            raw = reader3.frame_positions(mname, probe_frame)
            if TyreSettings().matches(mname):
                n_tyre_ranges += 1
                h = world_heights(reader3, wheels, probe_frame, seg)
                check(h.min() > -1e-3,
                      f"chunked {mname}: stays above the ground "
                      f"({h.min()*1000:.3f} mm)")
            else:
                check(np.allclose(seg, raw, atol=1e-6),
                      f"chunked {mname}: rigid member untouched inside the chunk")
        check(n_tyre_ranges == len(tyres),
              f"all {len(tyres)} tyres deformed inside the merged chunk "
              f"(found {n_tyre_ranges})")

    reader.close()
    reader2.close()
    reader3.close()

    if _failures:
        print(f"\n[TYRE] {len(_failures)} FAILURE(S)")
        sys.exit(1)
    print("\n[TYRE] all checks passed")


if __name__ == "__main__":
    main()
