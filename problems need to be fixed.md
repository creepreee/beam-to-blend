# Problems to be fixed

Write-up of the remaining problems, one by one, as reported by the user.
This file is the source of truth for what still does not work.

---

## Problem 1 — debris stuck in the air (2 keyframes only)

**Symptom.** When debris is generated on frame 2457, the generated debris
contains exactly 2 keyframes — one on frame **2457** and one on frame **2458**.
Because the two keyframes are extremely close together, there is effectively no
animation, and the debris remains sitting at its starting point in the air.
This happens to **all** of the debris.

**Update from user (after closer inspection).** Not everything is stuck. What
was actually observed:

- `glassfrag_...` → **moves** (animates correctly).
- `debris_paint_...` → **moves**.
- `debris_glass_...` → **moves**.
- `debris_plastic_...` → **moves**.
- `debris_emit_...` → **STUCK**. 2 keyframes only, no movement.

So the stuck class is specifically every object whose name starts with
`debris_emit_...`. The other debris families all animate fine. The problem is
therefore scoped to the `debris_emit_...` objects.

(Note: `debris_emit_...` is almost certainly the fine-particle **emitter**
objects. Emitters are hidden quads that instance the fine debris — they are not
supposed to travel. If the emitter objects themselves carry baked location keys,
something is feeding them through the rigid-body bake or the launch path. Needs
confirmation, but the observed "2 keyframes, frame 2457/2458" matches a launch /
bake step applied to the emitter.)

---

## Problem 2 — glass fragments still follow the car

**Symptom.** Glass fragments are still following the car (the pane / its shards
remain attached to, or driven by, the wreck motion instead of becoming
independent when the pane shatters).

**Goal.** Find a way to make the **entire window** independent — the whole pane
must stop following the car once it shatters, so the pieces fall freely instead
of riding the wreck.

**User's idea for the windshield (selected-vertex independence):**
1. Go into **edit mode** on the windshield.
2. **Select the vertices** that should get proper rigid-body physics.
3. **Most importantly:** those selected vertices will become **independent at a
   certain frame** — i.e. at the shatter frame, the selected region detaches
   from the car (stops following its parent/transform) and becomes its own rigid
   body, so it falls / behaves physically from that frame onward.

---
