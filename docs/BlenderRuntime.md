# Blender Runtime

The runtime side of the project should do as little work as possible.

## Runtime responsibilities

- load the binary cache
- create each mesh once
- update vertex coordinates on frame change
- support playback and rendering
- handle fallback objects separately

## What it should not do

- create hundreds of thousands of duplicate mesh datablocks
- keep a full copy of every frame in memory
- rely on Geometry Nodes as a giant frame-swap hack unless absolutely necessary
