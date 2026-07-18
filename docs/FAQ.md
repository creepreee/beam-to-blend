# FAQ

## Why not just import all GLBs directly?
Because that duplicates mesh datablocks frame by frame and consumes huge amounts of RAM.

## Why not use Geometry Nodes only?
Geometry Nodes is not the same as a compact vertex cache. It can still leave you with a very heavy scene if implemented as one object per frame.

## What is the ideal target?
One mesh per object, plus frame-based vertex animation data.

## What is the odd object?
`flanje_e180_tierod_F` appears to be the only topology-changing object found so far.

## Why can't I merge vertices or remove doubles on the cached animation?
The cache animates by writing positions to fixed vertex indices. Frame 0 vertex
100 → frame N vertex 100, every frame. If you remove doubles, the vertex count
and order change → index mapping is destroyed → positions write to wrong
vertices → animation breaks.

This is not a bug — it is inherent to **every indexed geometry cache system**
(Alembic, MDD, USD, Point Cache). You cannot change topology and keep indexed
animation.

If welding is needed, it must be done during cache build in `CacheBuilder`
(option A in BUGS.md), which requires rebuilding the .bvc from source GLBs.

## Does our cache store normals?
No. The cache stores only positions (float32 N×3) and indices (int32 M×3).
Normals, UVs, vertex colors, and custom split normals are not stored.
The mesh in Blender gets its normals recomputed every frame via `mesh.update()`.
