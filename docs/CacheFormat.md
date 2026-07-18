# Cache Format

This project should eventually use a compact binary cache rather than a giant folder of duplicated Blender meshes.

## Planned layout

```text
Magic
Version
Frame count
Object count
Object table
Frame table
Compressed vertex blocks
Optional metadata
```

## Per-object data

- name
- vertex count
- edge count
- face count
- topology hash
- fallback flag
- base mesh reference

## Per-frame data

- only vertex positions for stable objects
- only the objects that actually need animation
- no duplicate topology data

## Goal

Minimize disk and memory pressure while making playback fast enough for Blender viewport use.
