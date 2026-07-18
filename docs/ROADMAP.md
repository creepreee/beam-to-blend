# Roadmap

## Phase 0 — Research
- [x] Verify topology stability on real BeamNG exports
- [x] Verify object matching across frames
- [x] Identify the one topology-changing object

## Phase 1 — Scanner
- [x] Sequence folder discovery
- [x] GLB frame reading (real parser, handles BeamNG shared vertex pool)
- [x] Topology hash (index-buffer based)
- [x] Stable vs dynamic classification across frames
- [ ] Material and UV metadata
- [ ] Better frame diffs

## Phase 2 — Cache format
- [x] Design binary cache header (BVC1)
- [x] Frame table (frame directory, seekable)
- [x] Object table
- [x] Chunking / streaming (memory-light build + memmap read)
- [ ] Compression strategy (currently uncompressed)

## Phase 3 — Blender runtime
- [x] Import meshes once
- [x] Update vertex positions by frame (foreach_set)
- [x] Frame-change handler
- [ ] Viewport preview mode (verify inside Blender)

## Phase 4 — Fallbacks
- [ ] Handle `flanje_e180_tierod_F`
- [ ] Handle future topology-changing parts
- [ ] Optional per-object fallback sequences

## Phase 5 — Polish
- [ ] Blender UI
- [ ] Error reporting
- [ ] Benchmarks
- [ ] Packaging
