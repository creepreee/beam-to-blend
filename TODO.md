# TODO — Roadmap & Known Limitations

Working order, most important first.

## 1. Debris physics

**Status: WIP.** The debris system detects impacts in the cache and spawns
rigid-body shards + particle debris at the right frames, but the physics
still needs polish:

- [ ] Rigid-body shard settling is not always stable — pieces can tunnel or
      jitter against the ground plane.
- [ ] Detached debris sometimes inherits the wrong initial velocity from the
      impact point (it should match the colliding part's impact velocity).
- [ ] Impact-frame accuracy: the exact spawn frame can shift a frame or two
      depending on playback fps (position is exact; the *frame* depends on
      the fps mapping).
- [ ] Collision margins / solver substeps need tuning so shards stack, not
      float or sink.
- [ ] Performance: spawning + baking debris on a long capture is slow; look
      at caching the generated keyframes.

## 2. Particles — make them efficient

**Status: WIP.** Fine particle debris (the dusty/small-fragment spray) is
spawned as real particle systems, which is correct but heavy:

- [ ] Particle count is currently high to hide low-per-emitter visibility —
      switch to instanced points / a single emitter with per-frame emission
      windows so the same density needs far fewer particles.
- [ ] Current solver integration runs at scene fps; a fixed-timestep emission
      would make trajectories deterministic regardless of Output FPS.
- [ ] Reuse baked debris point caches on render instead of re-solving every
      frame.
- [ ] Memory: particles + their cached positions should stream from the BVC
      the way the mesh does, instead of living in RAM.

## 3. Glass

- [ ] Crack shader (hole + radiating web) looks great up close but reads too
      "decalled" in wide shots — consider a geometric crack lattice.
- [ ] Tempered-glass fragment count is a preset; expose per-window control.

## 4. Tyre contact (tyre_deform)

- [ ] Bulge is computed by projecting the axle into the ground plane; on
      heavily cambered or lifted wheels the approximation weakens.
- [ ] `MAX_SQUASH_RATIO` is a hard cap; consider a speed/impact-based adaptive
      squash for harder crashes.

## 5. Fluids

- [ ] The fluid-effector proxy bake works, but the .mdd + MESH_CACHE pairing
      needs a check run across Blender 4.1/4.2/4.3 LTS.
- [ ] Particle proxy / foam sources for the fluid system are not supported yet.

## 6. Capture

- [ ] Per-part capture is exact but a bit heavy inside BeamNG — consider an
      optional low-poly proxy capture mode for very long scenes.
- [ ] Add configurable capture fps (currently hard-locked to 60).
- [ ] Auto-stop guard: `v5capture` should self-stop on the frame limit and
      print the exact .bmc path.

## 7. Blender add-on

- [ ] Asciify/sanitize captured part names (some modded cars use non-ASCII or
      duplicated names that confuse Blender's unique-name system).
- [ ] Background `load_post` recovery is solid; add a scene-level "Reload
      Cache" button as a manual recovery fallback.
- [ ] UI: show cache frame count + file size after build.

## 8. Docs & QA

- [ ] Sample `.bmc`/`.bvc` captures for the Releases page (so users can test
      import without BeamNG).
- [ ] Golden renders for regression testing.
- [ ] CI linting (ruff) with `--fix` clean.

---

## Contribute — try it, break it, report it

This project lives on real-world testing. If something explodes, please
**open an issue** with:

- Blender version + OS (and GPU if it looks render-related)
- BeamNG version (if capture-side)
- The `.bmc`/`.bvc` (drops or links are fine) or a screenshot of the panel
- The exact steps that broke it

Every report directly shapes the order of the list above.