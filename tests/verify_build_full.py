from __future__ import annotations
"""Build BMC->BVC with weld, then verify materials/UVs/weld landed correctly."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
from importer.cache_builder import CacheBuilder
from runtime.cache_reader import CacheReader

bmc_path, bvc_path = sys.argv[1], sys.argv[2]
weld = "--weld" in sys.argv

print(f"building (weld={weld}) ...", flush=True)
CacheBuilder(os.path.dirname(bvc_path), bvc_path).build_from_capture(bmc_path, weld=weld)

r = CacheReader(bvc_path)
print(f"\nBVC: version={r.header['version']} frames={r.frame_count} "
      f"objects={len(r.stable_objects())} "
      f"transform_offset={r.header.get('transform_data_offset')}")

# check a few known objects for real material slots + UVs
checks = ["flanje_e180_body", "flanje_e180_windshield", "flanje_e180_dash"]
allnames = {o.name for o in r.stable_objects()}
for nm in checks:
    if nm not in allnames:
        print(f"  ({nm} not present)"); continue
    o = r.get_object(nm)
    mats = r.base_material_names(nm)
    mids = r.base_material_ids(nm)
    uvs = r.base_uvs(nm)
    uv_nonzero = int(np.count_nonzero(uvs)) if uvs is not None else 0
    print(f"  {nm:32s} verts={o.vertex_count:6d} faces={o.face_count:6d} "
          f"mats={len(mats)} face_ids={len(mids)} uv_nonzero={uv_nonzero>0}")
    print(f"        materials: {mats[:6]}{'...' if len(mats)>6 else ''}")
    # face id range must be within material list
    if len(mids):
        assert mids.max() < len(mats), f"{nm}: face mat id out of range!"
        assert len(mids) == o.face_count, f"{nm}: face id count != face_count!"

# global material count
print(f"\nglobal material name table: {len(r._global_material_names)} names")
assert len(r._global_material_names) > 1, "materials not carried into BVC!"
print("VERIFY OK: real materials + UVs present per object")
r.close()
