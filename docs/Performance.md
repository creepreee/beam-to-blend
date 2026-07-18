# Performance

## Real-world result: 30 fps in solid viewport

Import the 97-object crash (550K verts, 14 chunk groups) and scrub the timeline
at a solid **30 fps** in Blender's solid viewport — no Cycles, no tricks.

## How we got there

The original code had 1-5 fps. Headless profiling isolated **two** CPU bottlenecks:

### 1. Legacy vertex-write API (120× slower)

`mesh.vertices.foreach_set("co", ...)` was the dominant cost at **110 ms/frame**.

Blender 4.x provides a direct `position` attribute API that avoids the legacy
dispatch overhead:

| API | Time (550K verts) |
|---|---|
| `mesh.vertices.foreach_set("co", flat)` | 110 ms |
| `mesh.attributes["position"].data.foreach_set("vector", flat)` | **0.9 ms** |

**Fix:** `_write_positions()` helper in `runtime/mesh_update.py` uses the
attribute path on Blender 4.x, falls back to the legacy path on older versions.
`mesh.update()` recomputes normals + bounding box + edge data (C-level, <1ms per
call). Do NOT use `mesh.update_tag()` — it skips normal/bbox recompute, causing
severe viewport corruption (see `BUGS.md` item 1).

### 2. Hidden min/max on every frame

`_log_bounds()` computed `pos.min(axis=0)` / `pos.max(axis=0)` over 550K verts
for every chunk on every frame — even when logging was disabled.

**Fix:** `if self._log_fh is None: return` at the top of `_log_bounds()`.

## Measured improvement (headless `set_frame`)

| Mode | Before | After | Speedup |
|---|---|---|---|
| Chunked (14 groups) | 198 ms (5 fps) | **5.4 ms (185 fps)** | 37× |
| Individual (97 objects) | 320 ms (3 fps) | **32 ms (31 fps)** | 10× |

CPU is off the critical path. Real viewport fps is GPU-bound (~15 ms draw for
550K verts in solid mode) → **15-30+ fps**.

## Design rules going forward

- **Never** use `mesh.vertices.foreach_set("co", ...)` on the hot path — use
  the `position` attribute API.
- **Never** do heavy numpy work (min/max, copies) in per-frame helpers unless
  logging is explicitly enabled.
- Chunked mode is still preferred: fewer GPU draw calls/uploads.
