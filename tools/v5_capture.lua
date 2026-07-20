-- =============================================================================
--  v5capture.lua  --  Local-pool capture with export.lua-faithful rigid motion
-- =============================================================================
--  Mirrors the stock sequence exporter (lua/ge/extensions/util/export.lua):
--
--    * Body mesh: verticesGet() returns the LOCAL GPU-pool positions
--      (left-handed Y-up: X=length, Y=up, Z=width).  We dump these VERBATIM,
--      exactly like export.lua's flexmesh POSITION buffer.  NO world baking.
--
--    * Rigid motion is captured SEPARATELY (so it can drive a Blender parent
--      empty and be split from the deformation animation):
--          - translation = veh:getPosition()          (physics space)
--          - forward     = veh:getDirectionVector()
--          - up          = veh:getDirectionVectorUp()
--      Stored raw in physics space; the builder reconstructs the orthonormal
--      basis and drives a parent empty, mirroring export.lua's intent.
--
--  Output: <path>.bmc (BMC v1, local-pool verts + per-frame transform)
--    static: index buffer + UVs + primitives + materials
--    per frame: timestamp(8) + N*3 f32 LOCAL pool vertex positions
--               + 9 f32 rigid transform (px,py,pz, fx,fy,fz, ux,uy,uz)
--                 in raw physics space.
--  The builder reconstructs an orthonormal basis R=[right|fwd|up] from
--  (forward,up) and drives a parent empty, identical to the exporter's intent.
--
--  Deploy as extension "v5capture" (no underscore). File at
--  lua/ge/extensions/v5capture.lua
--
--  Usage (GE console):
--    extensions.load("v5capture")
--    v5capture.start('captures/v3run', 700)
--    -- crash --
--    v5capture.stop()
-- =============================================================================

local M = {}
local logTag = 'v5capture'

local ffi = require('ffi')
local bit = bit or require('bit')

local FLAG_HAS_UVS = 1
local FLAG_HAS_TRANSFORM = 4  -- 1 << 2: per-frame rigid transform block present

local active      = false
local bmcFile     = nil
local bmcPath     = nil
local maxFrames   = 0
local frameCount  = 0
local vehId       = nil

local totalVerts  = 0
local totalIndices = 0
local hasUvs      = false

-- ---------------------------------------------------------------------------
local function writeU16(f, v)
  v = (v or 0) % 65536
  f:write(string.char(bit.band(v, 0xFF), bit.band(bit.rshift(v, 8), 0xFF)))
end
local function writeU32(f, v)
  v = (v or 0) % 4294967296
  f:write(string.char(
    bit.band(v, 0xFF), bit.band(bit.rshift(v, 8), 0xFF),
    bit.band(bit.rshift(v, 16), 0xFF), bit.band(bit.rshift(v, 24), 0xFF)))
end
local function writeF64(f, v) f:write(ffi.string(ffi.new('double[1]', v), 8)) end
local function writeF32(f, v) f:write(ffi.string(ffi.new('float[1]', v), 4)) end
local function writeI32(f, v)
  v = v or 0
  if v < 0 then v = 4294967296 + v end
  writeU32(f, v)
end

local function writeHeader(f, vc, ic, pc, mc, flags, frameSize, staticSize)
  f:write('BMC1')
  writeU32(f, 1)
  writeU32(f, vc); writeU32(f, ic); writeU32(f, pc); writeU32(f, mc)
  writeU32(f, flags)
  writeU32(f, frameSize)
  writeU32(f, staticSize % 4294967296)
  writeU32(f, math.floor(staticSize / 4294967296))
end
local function writePrimitive(f, name, startIndex, indexCount, materialId, flexmeshIndex)
  writeU16(f, #name); f:write(name)
  writeU32(f, startIndex); writeU32(f, indexCount)
  writeI32(f, materialId); writeI32(f, flexmeshIndex)
end
local function writeMaterial(f, name) writeU16(f, #name); f:write(name) end

-- ---------------------------------------------------------------------------
function M.start(path, frames)
  local veh = getPlayerVehicle(0)
  if not veh then log('E', logTag, 'No player vehicle'); return false end

  bmcPath = path:match('%.bmc$') and path or (path .. '.bmc')
  maxFrames = frames or 300
  frameCount = 0
  vehId = veh:getId()

  local meshInfo = GPUMesh.bng_getGPUMesh(vehId)
  if not meshInfo then log('E', logTag, 'GPUMesh unavailable'); return false end

  local wait = 60
  while not meshInfo.dataIsReady and wait > 0 do coroutine.yield(1); wait = wait - 1 end
  if not meshInfo.dataIsReady then
    log('E', logTag, 'GPU mesh never became ready'); meshInfo:free(); return false
  end

  totalVerts   = meshInfo.verticesCount
  totalIndices = meshInfo.indicesCount
  local uvCount = meshInfo.uv1Count or 0
  hasUvs = uvCount > 0

  local indices = ffi.new('unsigned int[?]', totalIndices)
  if not meshInfo:indicesGet(indices) then
    log('E', logTag, 'indicesGet failed'); meshInfo:free(); return false
  end

  local uvs = nil
  if hasUvs then
    uvs = ffi.new('float[?]', uvCount * 2)
    if not meshInfo:uv1Get(uvs) then uvs = nil; hasUvs = false end
  end

  local primitives = {}
  for i = 0, meshInfo.flexmeshesCount - 1 do
    local fm = meshInfo:flexmeshes(i)
    local meshName = fm.meshName or ('flexmesh_' .. i)
    local pCount = fm.primitivesCount
    if pCount > 0 then
      local prims = ffi.new('gpuPrimitive_t[?]', pCount)
      if fm:primitivesGet(prims) then
        for p = 0, pCount - 1 do
          local prim = prims[p]
          primitives[#primitives + 1] = {
            name = meshName, startIndex = tonumber(prim.startIndex),
            indexCount = tonumber(prim.indexCount),
            materialId = tonumber(prim.materialId), flexmeshIndex = i,
          }
        end
      end
    end
  end

  local materials = {}
  if veh.getMaterialNames then
    for _, n in ipairs(veh:getMaterialNames()) do materials[#materials + 1] = n end
  end

  local flags = 0
  if hasUvs then flags = bit.bor(flags, FLAG_HAS_UVS) end
  -- Store the per-frame rigid transform (pos + forward + up, all Blender Z-up)
  -- so the builder can drive a parent empty and separate object motion from
  -- the deformation animation — mirroring export.lua's rigid-motion capture.
  flags = bit.bor(flags, FLAG_HAS_TRANSFORM)

  local staticSize = totalIndices * 4
  if hasUvs then staticSize = staticSize + uvCount * 8 end
  for _, p in ipairs(primitives) do staticSize = staticSize + 2 + #p.name + 12 + 4 end
  for _, m in ipairs(materials)  do staticSize = staticSize + 2 + #m end

  -- per-frame: timestamp + N LOCAL pool vertex positions + 9 f32 rigid
  -- transform (px,py,pz, fx,fy,fz, ux,uy,uz), all in Blender Z-up ({x,z,-y}).
  local frameSize = 8 + totalVerts * 12 + 9 * 4

  local dirEnd = bmcPath:match('^(.+)/[^/]+$')
  if dirEnd and FS then FS:directoryCreate(dirEnd, true) end

  local err
  bmcFile, err = io.open(bmcPath, 'wb')
  if not bmcFile then log('E', logTag, 'open failed: ' .. tostring(err)); meshInfo:free(); return false end

  writeHeader(bmcFile, totalVerts, totalIndices, #primitives, #materials, flags, frameSize, staticSize)
  bmcFile:write(ffi.string(indices, totalIndices * 4))
  if hasUvs and uvs then bmcFile:write(ffi.string(uvs, uvCount * 8)) end
  for _, p in ipairs(primitives) do
    writePrimitive(bmcFile, p.name, p.startIndex, p.indexCount, p.materialId, p.flexmeshIndex)
  end
  for _, m in ipairs(materials) do writeMaterial(bmcFile, m) end

  meshInfo:free()
  active = true
  log('I', logTag, string.format(
    'v5capture started: %s (%d verts, %d idx, uvs=%s, up to %d frames)',
    bmcPath, totalVerts, totalIndices, tostring(hasUvs), maxFrames))
  return true
end

-- ---------------------------------------------------------------------------
--  Dump LOCAL pool vertices + the export.lua-style rigid transform for a frame.
-- ---------------------------------------------------------------------------
local function dumpWorldFrame()
  local veh = getPlayerVehicle(0)
  if not veh then return false end
  local meshInfo = GPUMesh.bng_getGPUMesh(veh:getId())
  if not meshInfo then return false end
  local wait = 30
  while not meshInfo.dataIsReady and wait > 0 do coroutine.yield(1); wait = wait - 1 end
  if not meshInfo.dataIsReady then meshInfo:free(); return false end

  local vc = meshInfo.verticesCount
  if vc ~= totalVerts then
    log('W', logTag, string.format('vertex count changed %d -> %d; skipping', totalVerts, vc))
    meshInfo:free(); return true
  end

  local verts = ffi.new('float[?]', vc * 3)
  if not meshInfo:verticesGet(verts) then meshInfo:free(); return true end

  -- LOCAL pool vertices, dumped VERBATIM (no world transform).  This matches
  -- export.lua's flexmesh POSITION buffer exactly.  The pool is left-handed
  -- Y-up; the rigid transform below carries the orientation separately.
  if bmcFile then
    writeF64(bmcFile, frameCount / 60.0)
    bmcFile:write(ffi.string(verts, vc * 12))

    -- Rigid motion captured SEPARATELY from the deformation, exactly as the
    -- stock sequence exporter does: translation from getPosition() and
    -- orientation from the live direction vectors (refNode-offset-free).
    -- Stored raw in physics space; the builder reconstructs the orthonormal
    -- basis and drives a parent empty (mirroring export.lua's intent of
    -- keeping object motion separable from the mesh animation).
    local p = veh:getPosition()
    local d = veh:getDirectionVector()
    local u = veh:getDirectionVectorUp()
    writeF32(bmcFile, p.x); writeF32(bmcFile, p.y); writeF32(bmcFile, p.z)
    writeF32(bmcFile, d.x); writeF32(bmcFile, d.y); writeF32(bmcFile, d.z)
    writeF32(bmcFile, u.x); writeF32(bmcFile, u.y); writeF32(bmcFile, u.z)
    bmcFile:flush()
    frameCount = frameCount + 1
  end
  meshInfo:free()
  return true
end

function M.onUpdate()
  if not active then return end
  dumpWorldFrame()
  if frameCount >= maxFrames then M.stop() end
end
function M.stop()
  active = false
  if bmcFile then bmcFile:close(); bmcFile = nil end
  log('I', logTag, string.format('v5capture stopped: %s (%d frames)', bmcPath, frameCount))
end
function M.onInit()
  setExtensionUnloadMode(M, 'manual')
  log('I', logTag, 'v5capture loaded — local-pool verts + export.lua-style rigid transform')
end
function M.onReset()
  active = false; frameCount = 0
  if bmcFile then bmcFile:close(); bmcFile = nil end
end

return M
