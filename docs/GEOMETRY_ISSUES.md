# Geometry Issues — Capture Path Import (2026-07-15)

## Detailed symptom list (from user visual inspection)

Every issue below is on the **capture-path BVC** (`capture_latest/mycap_final.bvc`),
built from `capture.bin` (650 frames, 238 primitives → 84 merged parts after weld).

---

### 1. `flanje_e180_taillight_L` — stretched face from bulb to front door

- The taillight housing mesh is mostly correct.
- The **light-bulb** sub-part is mangled: bulb vertices form spike-like shapes.
- **One face** from the bulb vertex group stretches in a long triangle all the way to
  the front-right door region, creating an elongated face whose silhouette looks like
  the door — but it is entirely within the `taillight_L` object, not a separate door
  mesh.
- Root cause hypothesis: the capture mod gathered bulb vertices from a pool offset that
  belongs to the door, placing door-position vertices inside the taillight's vertex
  pool. A triangle referencing those vertices then stretches across the car.

### 2. `flanje_e180_headlight_L` — entire headlight stretched to rear

- The headlight housing is severely distorted.
- Multiple faces stretch from the headlight toward the **rear** of the car.
- Same root cause as #1: wrong pool offsets in the capture mod place rear-vehicle
  vertices inside the headlight's vertex pool.

### 3. `flanje_e180_taillight_R` — mirrored version of issue #1

- The taillight housing mesh is mostly correct.
- The bulb sub-part is mangled: faces from the bulb stretch to create a
  roughly-symmetrical shape on the opposite side of the car — but with extreme
  stretching and distorted vertices.
- Confirms the pool-offset bug is systemic (affects both left and right objects).

### 4. `flanje_e180_body` — gap at fuel tank cap

- The fuel-filler-cap area on the body has **missing faces / no geometry**.
- The fuel cap itself is floating (disconnected from the body mesh).
- Root cause hypothesis: the weld collapsed vertices around the fuel cap opening,
  removing the hole boundary and leaving the cap orphaned.

### 5. `flanje_e180_seats_R` — black faces

- Many faces on the right-seat object render **black** (no shading).
- Likely cause: face normals are flipped (winding order reversed) or the material
  slot assignment is wrong after the merge, causing Blender to shade the back face.

### 6. `tire_01a_16x7_26DD` — one prominent missing triangle

- The front-right tyre has exactly **one visible triangle gap**.
- Root cause hypothesis: degenerate-triangle filtering removed a nearly-zero-area
  triangle that was actually valid geometry (very thin but real).

---

## Common root cause

The capture Lua mod (`capture.lua`) writes **one object per material primitive** (~238).
Each primitive has its own private vertex pool. The mod identifies each primitive by
`flexmeshCache[obj.name]` where `obj.name` is the Lua key.

**Duplicate-name bug:** When two primitives share the same name (e.g. two
`flanje_e180_body_flanje_e180_paint` entries in capture.meta), Lua's table overwrite
the second primitive's `minIdx` onto the first. The first primitive then reads
positions from the wrong pool offset → vertices from unrelated parts of the car end up
in its vertex pool.

This explains issues #1, #2, and #3 (stretched faces connecting unrelated parts).

The weld (Approach 6) was intended to fix seam cracks but introduces additional
problems:
- Collapses vertices around openings (fuel cap gap — issue #4)
- Can reverse face winding (black faces — issue #5)
- Degenerate triangle filter removes thin-but-valid geometry (issue #6)

---

## Corruption evidence from raw capture data

Edge-length analysis on frame 0 confirms widespread corruption. A clean mesh has max
edge length < 0.1m. Corrupted primitives have edges up to **4.18m** spanning the entire
car.

| Index | Object name                          | Verts  | Max edge | Big edges (>0.3m) | Status    |
|-------|--------------------------------------|--------|----------|--------------------|-----------|
|   0   | flanje_e180_body                     | 40785  | 1.315m   | 148                | CORRUPTED |
|   2   | body_paint                           | 54658  | 1.945m   | 2895               | CORRUPTED |
|   4   | body_leather                         |  8992  | 1.183m   |  56                | CORRUPTED |
|  54   | headlight_R_chrome                   |  1725  | 1.466m   | 306                | CORRUPTED |
|  67   | headlight_L_chrome                   |  1725  | 4.179m   | 206                | CORRUPTED |
|  78   | taillight_R_chrome                   |  7740  | 1.465m   |  46                | CORRUPTED |
|  88   | taillight_L_chrome                   |  7742  | 2.692m   | 1431               | CORRUPTED |
| 147   | seats_R                              |  1713  | 0.939m   |   8                | CORRUPTED |

Out of 238 primitives, many are severely corrupted. The corruption pattern:
- Objects with **duplicate names** in capture.meta get wrong pool offsets from the Lua
  mod's `flexmeshCache[name]` overwrite
- Even non-duplicate objects (like `flanje_e180_body` itself) can be corrupted because
  the mod gathers from a shared vertex pool and the body's pool overlaps with other parts

**Key finding:** corruption is in the RAW `capture.bin` data — it cannot be fixed by
any importer-side post-processing. The vertex positions are already wrong at frame 0.

---

## New approach: Per-primitive BVC with corruption filtering

### Why merge+weld fails

The merge+weld (Approach 6) was designed to fix seam cracks by combining material-
split primitives and welding shared boundary vertices. But with corrupted primitives:

1. **Merging** brings corrupted vertices (from wrong pool offsets) into the same mesh as
   clean vertices → triangles stretch across the entire car
2. **Welding** connects vertices that happen to be at similar positions but belong to
   different parts → fuel cap gap, reversed normals (black faces)
3. **Degenerate filtering** removes thin-but-valid triangles → missing tire triangle

### New approach: skip merge, filter corruption

1. **No merge** — keep each of the 238 capture primitives as a separate object. This
   eliminates cross-contamination: a corrupted taillight can't stretch faces into a door
   because they're separate objects.
2. **No weld** — no vertex collapse, no fuel-cap gaps, no reversed normals.
3. **Corruption filter** — detect primitives with max edge > threshold (0.3m) and
   **exclude them from the BVC**. This removes the worst geometry (taillight chrome
   spanning the whole car) while keeping clean primitives (individual trim pieces, glass,
   tires, etc.).
4. **Degenerate filter relaxation** — don't remove triangles with edge > 0.3m if the
   whole primitive is clean (fixes the single missing tire triangle).

### Tradeoffs

| Metric | Merge+weld (current) | Per-primitive + filter (new) |
|--------|----------------------|------------------------------|
| Objects | 84 | ~220 (238 minus ~18 corrupted) |
| Stretched faces | SEVERE | NONE |
| Missing faces | YES (fuel cap, tire) | NONE |
| Black faces | YES (seats) | NONE |
| Seam cracks | NONE (welded) | YES (material boundaries, minor) |
| Corrupted parts | Mixed into good parts | Excluded (small gaps) |

The per-primitive approach trades **seam cracks** (minor, sub-millimeter visual gaps at
material boundaries) for **elimination of all stretched/missing/black face issues**.

### Implementation plan

1. In `cache_builder.py:_build_from_capture_impl`: skip merge and weld steps
2. Add corruption detection: max edge > 0.3m → exclude primitive
3. Deduplicate names in the object table (append `_0`, `_1`, etc.)
4. Each primitive's vertex pool + indices written directly to BVC as a separate object
