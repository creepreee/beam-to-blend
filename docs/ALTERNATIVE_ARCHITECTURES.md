# Alternative Architectures for BeamNG → Blender Animation Pipeline

## Core Problem

**Current approach fails because:**
- Capture mod drives `be:step(1)` → conflicts with BKS controller (which drives simulation onUpdate)
- BKS detects "export stopped" → shuts off animation
- BeamNG replay is unreliable for VFX (jitter, non-deterministic)
- GPU mesh capture is fragmented (238 draw calls, no shared vertex pool)
- Replay-based GLTF pipeline is fundamentally brittle for VFX

---

## Architecture Options (Ranked by Viability)

---

## 1. PASSIVE LIVE RECORDER + OFFLINE BAKER (RECOMMENDED)

### Architecture
```
┌─────────────────────────────────────────────────────────────────┐
│  BEAMNG (LIVE SIMULATION)                                       │
│  ┌─────────────┐    ┌──────────────────┐    ┌──────────────┐  │
│  │ BKS / Game  │───▶│ PASSIVE RECORDER │───▶│ TRUTH LOG    │  │
│  │ CONTROLLER  │    │ (onUpdate only)  │    │ (binary)     │  │
│  └─────────────┘    └──────────────────┘    └──────┬───────┘  │
└─────────────────────────────────────────────────────┼──────────┘
                                                      ▼
┌─────────────────────────────────────────────────────────────────┐
│  OFFLINE BAKER (post-run, headless or GUI)                     │
│  ┌─────────────┐    ┌──────────────────┐    ┌──────────────┐  │
│  │ TRUTH LOG   │───▶│ BAKER            │───▶│ BLENDER CACHE│  │
│  │ (binary)    │    │ (rest mesh +     │    │ (BVC/Alembic/│  │
│  └─────────────┘    │  point cache)    │    │  USD)        │  │
│                     └──────────────────┘    └──────────────┘  │
└─────────────────────────────────────────────────────────────────┘
                                                      ▼
┌─────────────────────────────────────────────────────────────────┐
│  BLENDER (VFX WORK)                                             │
│  ┌─────────────┐    ┌──────────────────┐                       │
│  │ REST MESH   │───▶│ POINT CACHE      │                       │
│  │ (static)    │    │ (per-frame)      │                       │
│  └─────────────┘    └──────────────────┘                       │
└─────────────────────────────────────────────────────────────────┘
```

### Key Principle
**Recorder is a PASSENGER, not the DRIVER.**

### Truth Log Format (per frame)
```lua
-- Written onUpdate (dt from game, not be:step)
{
  time = 0.016666,           -- real sim time
  frame = 42,
  vehicle = {
    pos = {x,y,z},           -- root transform
    rot = {x,y,z,w},
    vel = {x,y,z},
    angvel = {x,y,z}
  },
  nodes = {                  -- physics nodes (if available)
    {name="FL_wheel", pos={}, rot={}, vel={}},
    ...
  },
  parts = {                  -- part transforms (rigid pieces)
    {name="body", pos={}, rot={}},
    {name="door_FL", pos={}, rot={}},
    ...
  },
  mesh = {                   -- OPTIONAL: vertex positions if needed
    -- only for deformable parts (crumple zones)
    {name="body_crumple", verts={...}},
  }
}
```

### Offline Baker
- Reads truth log
- Creates **one rest mesh** per part (from first frame or reference model)
- Writes **point cache** (per-frame vertex positions OR part transforms)
- Outputs: **Alembic / USD / BVC / custom**

### Why This Works
| Problem | Solution |
|---------|----------|
| BKS conflict | Recorder never calls `be:step(1)` — only observes `onUpdate` |
| Replay jitter | Uses live sim data, not replay |
| Non-deterministic replay | Logs actual sim state per frame |
| GPU mesh fragmentation | Can log **physics nodes/part transforms** instead of draw calls |
| Deformable parts | Optional per-vertex logging only for crumple zones |

---

## 2. TWO-PASS BAKE (DETERMINISTIC REPLAY)

### Architecture
```
PASS 1: RECORD INPUTS
┌─────────────────────────────────────────────────────────────────┐
│ BKS runs LIVE                                                    │
│   └─▶ INPUT TAPE: {time, throttle, brake, steer, gear, ...}    │
└─────────────────────────────────────────────────────────────────┘

PASS 2: HEADLESS REPLAY + EXPORT
┌─────────────────────────────────────────────────────────────────┐
│ BeamNG HEADLESS (no BKS, no UI)                                 │
│   ├─▶ Load INPUT TAPE                                           │
│   ├─▶ Deterministic replay (fixed dt)                           │
│   └─▶ EXPORTER samples every frame (no BKS conflict)            │
│       └─▶ Alembic / USD / BVC                                    │
└─────────────────────────────────────────────────────────────────┘
```

### When to Use
- Motion must be **perfectly reproducible**
- You can accept two passes
- BKS logic is complex but inputs are simple

### Implementation
```lua
-- Pass 1: Record inputs (add to BKS or separate mod)
local inputTape = {}
function onUpdate(dt)
  table.insert(inputTape, {
    time = time,
    throttle = input.throttle,
    brake = input.brake,
    steer = input.steer,
    gear = input.gear,
    handbrake = input.handbrake,
    -- any custom forces
  })
end
```

```lua
-- Pass 2: Headless replay
-- beamng -headless -lua "replay_and_export.lua" --inputTape=tape.json
```

---

## 3. NODE/BEAM CACHE (PHYSICS-LEVEL CAPTURE)

### Architecture
```
┌─────────────────────────────────────────────────────────────────┐
│ BEAMNG SIMULATION                                               │
│   ├─▶ Physics nodes (particles)                                 │
│   ├─▶ Beams (springs)                                           │
│   ├─▶ Part nodes (rigid groups of nodes)                        │
│   └─▶ Wheel nodes                                               │
└─────────────────────────────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│ RECORDER (onUpdate)                                             │
│   ├─▶ Node positions + velocities (per frame)                   │
│   ├─▶ Part transforms (rigid groups)                            │
│   ├─▶ Wheel transforms                                          │
│   └─▶ Optional: deformed node positions for crumple zones       │
└─────────────────────────────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│ BAKER                                                            │
│   ├─▶ Rest mesh: import vehicle model (or generate from nodes)  │
│   ├─▶ Rigging: bind mesh to part nodes / wheel nodes            │
│   ├─▶ Skinning: for crumple zones, bind to deforming nodes      │
│   └─▶ Cache: part transforms (light) + vertex cache (heavy)     │
└─────────────────────────────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│ BLENDER                                                          │
│   ├─▶ Rest mesh + armature (part nodes as bones)                │
│   ├─▶ Animation: part transforms drive bones                    │
│   └─▶ Optional: vertex cache for crumple zones                  │
└─────────────────────────────────────────────────────────────────┘
```

### Why This Is Stronger
| Aspect | GPU Mesh | Physics Nodes |
|--------|----------|---------------|
| Topology | Fragmented (238 draw calls) | Clean hierarchy (parts/nodes) |
| Determinism | Replay-dependent | Native simulation state |
| Size | ~4.5 GB (vertex cache) | ~50 MB (transform cache) |
| Deformation | Hard to reconstruct | Native (node positions) |
| BKS conflict | High (needs replay) | Low (observes live) |

### Implementation
```lua
-- Minimal node capture
local function captureNodes()
  local veh = be:getPlayerVehicle(0)
  if not veh then return end
  
  local data = { frame = frame, time = time }
  
  -- Part transforms (rigid groups)
  data.parts = {}
  for _, part in pairs(veh.parts) do
    if part.nodes then
      local center = vec3(0,0,0)
      for _, nid in ipairs(part.nodes) do
        center = center + veh.nodes[nid].pos
      end
      center = center / #part.nodes
      -- orientation from 3 nodes or beam directions
      data.parts[part.name] = { pos = center, rot = getPartRotation(part) }
    end
  end
  
  -- Wheel transforms (critical for animation)
  data.wheels = {}
  for i, wheel in ipairs(veh.wheels) do
    data.wheels[i] = { pos = wheel.pos, rot = wheel.rot, rotSpeed = wheel.rotSpeed }
  end
  
  return data
end
```

---

## 4. USD / ALEMBIC DIRECT EXPORT FROM BEAMNG

### Architecture
```
BeamNG (with USD/Alembic library linked)
    │
    ├─▶ Rest mesh (once)
    ├─▶ Per-frame: part transforms + vertex deltas
    └─▶ Write .usd / .abc directly
```

### Pros
- Industry standard
- Blender reads natively
- Handles topology changes (Alembic)

### Cons
- Need to compile USD/Alembic into BeamNG (C++)
- Or use Lua bindings (none exist currently)
- Still needs recorder architecture

---

## 5. HYBRID: REST MESH + POINT CACHE (BEST OF BOTH)

### Core Idea
**Don't capture mesh topology. Capture motion. Reconstruct in Blender.**

```
CAPTURE (live)          BAKER (offline)         BLENDER
──────────────────────────────────────────────────────────────
Part transforms  ───▶  Rig/armature          Armature anim
    (per frame)            (bones = parts)        (native)
    
Vertex cache      Point cache (Alembic)   Mesh deform
(crumple zones)      (.abc/.bvc)            (modifier)
```

### Data Flow
1. **Live sim**: Record part transforms + optional vertex cache for crumple zones
2. **Baker**: 
   - Build rest mesh (clean topology, proper UVs)
   - Create armature (bones = parts)
   - Write animation (part transforms → bone keyframes)
   - Write point cache (vertex deltas for crumple zones)
3. **Blender**: Import rest mesh + armature + point cache → full animation

---

## COMPARISON MATRIX

| Criterion | Passive Recorder | Two-Pass | Node Cache | USD Export | Hybrid |
|-----------|-----------------|----------|------------|------------|--------|
| **BKS conflict** | ✅ None | ✅ None | ✅ None | ⚠️ Needs C++ | ✅ None |
| **Determinism** | ✅ Live data | ✅ Inputs | ✅ Live | ✅ Native | ✅ Live |
| **Deformation** | ⚠️ Optional | ⚠️ Optional | ✅ Native | ✅ Native | ✅ Hybrid |
| **File size** | Medium | Medium | **Small** | Medium | Medium |
| **Blender native** | ✅ Alembic | ✅ Alembic | ✅ Armature | ✅ USD/ABC | ✅ Alembic |
| **Implementation** | **Lua only** | Lua + headless | **Lua only** | C++ required | **Lua only** |
| **BKS conflict risk** | **Zero** | **Zero** | **Zero** | Low | **Zero** |
| **Deformation fidelity** | Good (if vertex cache) | Good | Best (nodes) | Best | Best (hybrid) |
| **Time to implement** | **1-2 days** | 3-5 days | **1 day** | Weeks | **2 days** |

---

## RECOMMENDATION: START WITH PASSIVE RECORDER + HYBRID BAKER

### Week 1: Passive Recorder (Lua)
```lua
-- recorder.lua
local log = {}
local frame = 0

function onUpdate(dt)
  local veh = be:getPlayerVehicle(0)
  if not veh then return end
  
  frame = frame + 1
  local data = {
    frame = frame,
    time = obj:getTime(),
    vehicle = getVehicleTransform(veh),
    parts = getPartTransforms(veh),
    wheels = getWheelTransforms(veh),
  }
  
  -- Optional: vertex cache for crumple zones
  if config.captureVertices then
    data.crumple = getCrumpleVertices(veh)
  end
  
  table.insert(log, data)
  
  -- Flush every 100 frames to avoid memory pressure
  if frame % 100 == 0 then flushLog(log) end
end

function onShutdown()
  flushLog(log)
  writeMeta(meta)
end
```

### Week 2: Offline Baker (Python)
```python
# baker.py
def bake(log_path, rest_mesh_path, output_path):
    log = load_log(log_path)
    rest = load_mesh(rest_mesh_path)  # clean topology, UVs
    
    # 1. Build armature from part hierarchy
    armature = build_armature(log[0].parts)
    
    # 2. Bake animation (part transforms → bone keyframes)
    animation = bake_animation(log, armature)
    
    # 3. Optional: vertex cache for crumple zones
    if has_crumple_data(log):
        point_cache = build_point_cache(log, rest)
    
    # 4. Write Alembic/USD/BVC
    write_alembic(output_path, rest, armature, animation, point_cache)
```

---

## FILE FORMAT: TRUTH LOG (BINARY)

```
HEADER:
  magic: "BNGLOG" (6 bytes)
  version: u16
  frame_count: u32
  part_count: u16
  has_crumple: u8
  timestamp: f64

PER FRAME (repeated):
  frame_index: u32
  time: f64
  vehicle: { pos[3], rot[4], vel[3], angvel[3] }  -- f32 x 13
  parts: [part_count] { pos[3], rot[4] }           -- f32 x 7 per part
  wheels: [4] { pos[3], rot[4], rotSpeed }         -- f32 x 8 per wheel
  crumple_verts: [N] { pos[3] } (if enabled)       -- f32 x 3 per vert

FOOTER:
  frame_offsets: [frame_count] u64
```

---

## MIGRATION PATH FROM CURRENT CODE

| Current Component | New Role |
|-------------------|----------|
| `capture.lua` (be:step) | **DELETE** — replace with passive `onUpdate` recorder |
| `cache_builder.py` (scanner) | **DELETE** — replace with offline baker |
| `runtime/mesh_update.py` | **KEEP** — runtime playback unchanged |
| `runtime/cache_reader.py` | **KEEP** — reads new BVC format |
| `addon/` | **KEEP** — UI for import, add "Record Live" button |

---

## DECISION: WHICH TO BUILD FIRST?

**Build order:**
1. **Passive Recorder (Lua)** — 1 day, validates live data quality
2. **Hybrid Baker (Python)** — 1 day, produces Alembic/BVC
3. **Test in Blender** — verify deformation, armature, cache
4. **Iterate** — add vertex cache for crumple zones if needed

**Total: ~2 days to working pipeline. Zero BKS conflicts. Deterministic. VFX-ready.**

---

## FALLBACK: IF ALL ELSE FAILS

**Render + Depth + Masks sequence** → Composite in Blender/Nuke
- 100% reliable
- No geometry reconstruction
- Not "proper" 3D but unblocks VFX work

---

## CONCLUSION

**Stop fighting the replay system.** It's the wrong tool for VFX.

**Build a passive observer.** Let BKS drive. Record truth. Bake offline. Import clean.

This is the only architecture that:
- ✅ Respects BKS ownership of simulation
- ✅ Captures actual simulation state (not replay artifacts)
- ✅ Produces clean, deterministic Blender assets
- ✅ Scales to any vehicle/crash
- ✅ Uses 100% Lua + Python (no C++ recompile)
- ✅ Matches industry VFX pipelines (point cache + armature)