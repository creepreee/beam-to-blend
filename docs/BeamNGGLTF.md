# BeamNG GLTF Notes

BeamNG exports long crash sequences as `.glb` frames.

## Observations from research

- Most meshes keep the same topology across frames.
- One part (`flanje_e180_tierod_F`) changed topology.
- The sequence contains many objects, but only a tiny fraction appear to need special handling.

## Important implication

The exporter data itself is usable for cache-based import. The current bottleneck is the Blender importer architecture.
