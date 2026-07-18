-- =============================================================================
--  Diagnostic Animation Dump + BMC — ground-truth capture with matching outputs
-- =============================================================================
--  Purpose: Export BeamNG's raw animation data to both JSON (debug) and BMC
--  (viewer) so the two are guaranteed to match frame-for-frame.
--
--  Output:
--    <path>.json  — human-readable diagnostic dump (all transform candidates,
--                   sample vertices, debug vertices)
--    <path>.bmc   — BMC v1 binary (full mesh positions per frame,
--                   transform = quatFromDir(-dir, up))
--
--  Usage (GE Lua console):
--    diagnostic_dump.start('tools/dump_debug', 300)
--    -- crash the car...
--    diagnostic_dump.stop()
-- =============================================================================

local M = {}
local logTag = 'diagDump'

local active = false
local frames = {}
local maxFrames = 0
local jsonPath = nil
local bmcPath = nil
local sampleIndices = nil
local NUM_SAMPLE_VERTS = 1000
local debugFlexId = nil

local ffi = require('ffi')
local bit = bit or require('bit')

-- ---------------------------------------------------------------------------
--  BMC state
-- ---------------------------------------------------------------------------
local bmcFile = nil
local bmcFrameCount = 0
local staticIndices = nil
local staticUvs = nil
local totalVerts = 0
local totalIndices = 0
local hasUvs = false
local primitives = {}
local materials = {}

-- ---------------------------------------------------------------------------
--  Vector/quaternion helpers
-- ---------------------------------------------------------------------------

local function v3t(v) return { v.x, v.y, v.z } end
local function q2t(q) return { q.x, q.y, q.z, q.w } end
local function q2tF(q) return { q.x, q.y, q.z, q.w } end

-- ---------------------------------------------------------------------------
--  Binary writing helpers (little-endian)
-- ---------------------------------------------------------------------------

local function writeU8(f, v)
    f:write(string.char(bit.band(v or 0, 0xFF)))
end

local function writeU16(f, v)
    v = (v or 0) % 65536
    f:write(string.char(
        bit.band(v, 0xFF),
        bit.band(bit.rshift(v, 8), 0xFF)
    ))
end

local function writeU32(f, v)
    v = (v or 0) % 4294967296
    f:write(string.char(
        bit.band(v, 0xFF),
        bit.band(bit.rshift(v, 8), 0xFF),
        bit.band(bit.rshift(v, 16), 0xFF),
        bit.band(bit.rshift(v, 24), 0xFF)
    ))
end

local function writeI32(f, v)
    v = v or 0
    if v < 0 then v = 4294967296 + v end
    writeU32(f, v)
end

local function writeFloat64(f, v)
    local b = ffi.new('double[1]', v)
    f:write(ffi.string(b, 8))
end

local function writeFloat32(f, v)
    local b = ffi.new('float[1]', v)
    f:write(ffi.string(b, 4))
end

-- ---------------------------------------------------------------------------
--  BMC header / entry writers
-- ---------------------------------------------------------------------------

local FLAG_HAS_TRANSFORM = 4

local function writeBmcHeader(f, vc, ic, pc, mc, flags, frameSize, staticSize)
    f:write('BMC1')
    writeU32(f, 1)    -- version
    writeU32(f, vc)
    writeU32(f, ic)
    writeU32(f, pc)
    writeU32(f, mc)
    writeU32(f, flags)
    writeU32(f, frameSize)
    writeU32(f, staticSize % 4294967296)
    writeU32(f, math.floor(staticSize / 4294967296))
end

local function writePrimitiveEntry(f, name, startIndex, indexCount, materialId, flexmeshIndex)
    local nbytes = #name
    writeU16(f, nbytes)
    f:write(name)
    writeU32(f, startIndex)
    writeU32(f, indexCount)
    writeI32(f, materialId)
    writeI32(f, flexmeshIndex)
end

local function writeMaterialEntry(f, name)
    writeU16(f, #name)
    f:write(name)
end

-- ---------------------------------------------------------------------------
--  Flexmesh resolution
-- ---------------------------------------------------------------------------

local function resolveFlexmesh(veh, want)
  local vehData = core_vehicle_manager.getVehicleData(veh:getId())
  if not vehData or not vehData.vdata or not vehData.vdata.flexbodies then
    return nil, nil
  end
  local fb = vehData.vdata.flexbodies
  local namesToID = {}
  for i = 0, tableSizeC(fb) - 1 do
    namesToID[fb[i].mesh] = i
  end
  local sorted = tableKeysSorted(namesToID)
  log('I', logTag, string.format('vehicle has %d flexmeshes:', #sorted))
  for idx, nm in ipairs(sorted) do
    log('I', logTag, string.format('  [%d] %s', idx - 1, nm))
  end
  local pickName
  if type(want) == 'string' then
    local low = want:lower()
    for _, nm in ipairs(sorted) do
      if nm:lower():find(low, 1, true) then pickName = nm; break end
    end
    if not pickName then pickName = sorted[1] end
  else
    pickName = sorted[(want or 0) + 1] or sorted[1]
  end
  local flexbody = fb[namesToID[pickName]]
  return flexbody.fid, pickName
end

-- ---------------------------------------------------------------------------
--  Sample index selection
-- ---------------------------------------------------------------------------

local function pickSampleIndices(totalCount)
  local ids = {}
  local count = math.min(NUM_SAMPLE_VERTS, totalCount)
  local step = math.max(1, math.floor(totalCount / count))
  for i = 0, totalCount - 1, step do
    ids[#ids + 1] = i
    if #ids >= count then break end
  end
  if ids[1] ~= 0 then table.insert(ids, 1, 0) end
  if ids[#ids] ~= totalCount - 1 then ids[#ids + 1] = totalCount - 1 end
  return ids
end

-- ---------------------------------------------------------------------------
--  Sample-based hash (matches capture.lua)
-- ---------------------------------------------------------------------------

local function hashSample(data, nbytes)
    local ptr = ffi.cast('uint32_t*', data)
    local n = math.floor(nbytes / 4)
    local h = 0
    local limit = math.min(n, 1000)
    for i = 0, limit - 1 do
        h = bit.bxor(h, tonumber(ptr[i]))
    end
    for i = n - limit, n - 1 do
        h = bit.bxor(h, tonumber(ptr[i]))
    end
    return h
end

-- ---------------------------------------------------------------------------
--  Start — open both JSON + BMC, write BMC static section + frame 0
-- ---------------------------------------------------------------------------

function M.start(path, frameCount, flexSel)
  local veh = getPlayerVehicle(0)
  if not veh then log('E', logTag, 'No player vehicle'); return false end

  -- Derive JSON and BMC paths
  if path:match('%.json$') then
    jsonPath = path
    bmcPath = path:gsub('%.json$', '.bmc')
  else
    jsonPath = path .. '.json'
    bmcPath = path .. '.bmc'
  end
  maxFrames = frameCount or 300
  frames = {}
  debugFlexId = nil

  -- Reset BMC state
  bmcFile = nil
  bmcFrameCount = 0
  staticIndices = nil
  staticUvs = nil
  totalVerts = 0
  totalIndices = 0
  hasUvs = false
  primitives = {}
  materials = {}

  -- Resolve body shell flexmesh for getDebugVertexPos cross-check
  local pickName
  debugFlexId, pickName = resolveFlexmesh(veh, flexSel or 'body')
  if debugFlexId then
    veh:setFlexmeshDebugMode(true)
    log('I', logTag, string.format('Debug flexmesh "%s" (fid=%s) for getDebugVertexPos', tostring(pickName), tostring(debugFlexId)))
  else
    log('W', logTag, 'Could not resolve flexmesh for debug vertex capture')
  end

  -- Fetch GPU mesh for static data + frame 0
  local meshInfo = GPUMesh.bng_getGPUMesh(veh:getId())
  if meshInfo then
    local maxWait = 60
    while not meshInfo.dataIsReady and maxWait > 0 do
      coroutine.yield(1)
      maxWait = maxWait - 1
    end

    if meshInfo.dataIsReady then
      local vc = meshInfo.verticesCount
      local ic = meshInfo.indicesCount
      local uvCount = meshInfo.uv1Count or 0
      hasUvs = uvCount > 0

      -- Sample indices for JSON
      sampleIndices = pickSampleIndices(vc)
      log('I', logTag, string.format('Sampling %d out of %d GPU pool vertices', #sampleIndices, vc))

      -- === READ STATIC DATA FOR BMC ===

      -- Index buffer
      totalVerts = vc
      totalIndices = ic
      staticIndices = ffi.new('unsigned int[?]', ic)
      if not meshInfo:indicesGet(staticIndices) then
        log('E', logTag, 'Failed to read index buffer — no BMC written')
        meshInfo:free()
        active = true
        log('I', logTag, string.format('JSON-only diagnostic dump started: %s, up to %d frames', jsonPath, maxFrames))
        return true
      end

      -- UVs
      if hasUvs then
        staticUvs = ffi.new('float[?]', uvCount * 2)
        if not meshInfo:uv1Get(staticUvs) then
          staticUvs = nil
          hasUvs = false
          log('W', logTag, 'uv1Get failed — continuing without UVs')
        end
      end

      -- Build primitive table
      primitives = {}
      local fmCount = meshInfo.flexmeshesCount
      for i = 0, fmCount - 1 do
        local fm = meshInfo:flexmeshes(i)
        local meshName = fm.meshName or ('flexmesh_' .. i)
        local pCount = fm.primitivesCount
        if pCount > 0 then
          local prims = ffi.new('gpuPrimitive_t[?]', pCount)
          if fm:primitivesGet(prims) then
            for p = 0, pCount - 1 do
              local prim = prims[p]
              table.insert(primitives, {
                name = meshName,
                startIndex = tonumber(prim.startIndex),
                indexCount = tonumber(prim.indexCount),
                materialId = tonumber(prim.materialId),
                flexmeshIndex = i,
              })
            end
          end
        end
      end
      log('I', logTag, string.format('  %d primitives', #primitives))

      -- Build material table
      materials = {}
      if veh and veh.getMaterialNames then
        local names = veh:getMaterialNames()
        for _, n in ipairs(names) do
          table.insert(materials, n)
        end
      end
      log('I', logTag, string.format('  %d materials', #materials))

      -- === WRITE BMC HEADER + STATIC SECTION ===

      local flags = FLAG_HAS_TRANSFORM
      if hasUvs then flags = bit.bor(flags, 1) end

      local staticSize = totalIndices * 4
      if hasUvs then staticSize = staticSize + uvCount * 8 end
      for _, p in ipairs(primitives) do
        staticSize = staticSize + 2 + #p.name + 12 + 4
      end
      for _, m in ipairs(materials) do
        staticSize = staticSize + 2 + #m
      end

      local frameSize = 8 + totalVerts * 12 + 28  -- 8 timestamp, vc*12 positions, 28 transform

      -- Create dirs and open BMC file
      local dirEnd = bmcPath:match('^(.+)/[^/]+$')
      if dirEnd and FS then
        FS:directoryCreate(dirEnd, true)
      end

      local err
      bmcFile, err = io.open(bmcPath, 'wb')
      if bmcFile then
        writeBmcHeader(bmcFile, totalVerts, totalIndices, #primitives, #materials, flags, frameSize, staticSize)

        -- Index data
        bmcFile:write(ffi.string(staticIndices, totalIndices * 4))

        -- UV data
        if hasUvs and staticUvs then
          bmcFile:write(ffi.string(staticUvs, uvCount * 8))
        end

        -- Primitive table
        for _, p in ipairs(primitives) do
          writePrimitiveEntry(bmcFile, p.name, p.startIndex, p.indexCount, p.materialId, p.flexmeshIndex)
        end

        -- Material table
        for _, m in ipairs(materials) do
          writeMaterialEntry(bmcFile, m)
        end

        -- === FRAME 0: vertex positions (world-space) + identity transform ===
        local verts = ffi.new('float[?]', totalVerts * 3)
        if meshInfo:verticesGet(verts) then
          writeFloat64(bmcFile, 0.0)

          -- Transform to world-space using refNode matrix
          local ok, mat = pcall(function() return veh:getRefNodeMatrix() end)
          if ok and mat then
            local c0 = mat:getColumn(0)
            local c1 = mat:getColumn(1)
            local c2 = mat:getColumn(2)
            local c3 = mat:getColumn(3)
            local wv = ffi.new('float[?]', totalVerts * 3)
            for i = 0, totalVerts * 3 - 1, 3 do
              local lx, ly, lz = verts[i], verts[i+1], verts[i+2]
              wv[i]   = lx * c0.x + ly * c1.x + lz * c2.x + c3.x
              wv[i+1] = lx * c0.y + ly * c1.y + lz * c2.y + c3.y
              wv[i+2] = lx * c0.z + ly * c1.z + lz * c2.z + c3.z
            end
            bmcFile:write(ffi.string(wv, totalVerts * 12))
            writeFloat32(bmcFile, c3.x)
            writeFloat32(bmcFile, c3.y)
            writeFloat32(bmcFile, c3.z)
            writeFloat32(bmcFile, 0.0)
            writeFloat32(bmcFile, 0.0)
            writeFloat32(bmcFile, 0.0)
            writeFloat32(bmcFile, 1.0)
          else
            bmcFile:write(ffi.string(verts, totalVerts * 12))
            local p = veh:getPosition()
            local dir = veh:getDirectionVector()
            local up = veh:getDirectionVectorUp()
            local q = quatFromDir(vec3(-dir.x, -dir.y, -dir.z), up)
            writeFloat32(bmcFile, p.x)
            writeFloat32(bmcFile, p.y)
            writeFloat32(bmcFile, p.z)
            writeFloat32(bmcFile, q.x)
            writeFloat32(bmcFile, q.y)
            writeFloat32(bmcFile, q.z)
            writeFloat32(bmcFile, q.w)
          end
          bmcFile:flush()

          bmcFrameCount = 1
          log('I', logTag, string.format('BMC frame 0 written: %d verts', totalVerts))
        else
          log('E', logTag, 'Failed to read frame 0 vertices — BMC may be incomplete')
        end

        log('I', logTag, string.format('BMC opened: %s  (static=%d bytes, frame=%d bytes)',
          bmcPath, staticSize, frameSize))
      else
        log('E', logTag, 'Failed to open BMC: ' .. tostring(err))
      end
    else
      log('W', logTag, 'GPU mesh never became ready — falling back to sample-only')
      sampleIndices = {}
      for i = 0, 15 do sampleIndices[#sampleIndices + 1] = i end
    end

    meshInfo:free()
  else
    log('W', logTag, 'Could not read GPU mesh, using fallback sample')
    sampleIndices = {}
    for i = 0, 15 do sampleIndices[#sampleIndices + 1] = i end
  end

  if not sampleIndices then
    sampleIndices = {}
    for i = 0, 15 do sampleIndices[#sampleIndices + 1] = i end
  end

  active = true
  log('I', logTag, string.format('Diagnostic dump started: %s + %s, up to %d frames',
    jsonPath, bmcPath, maxFrames))
  return true
end

-- ---------------------------------------------------------------------------
--  collectFrame — capture one frame of data, write to JSON record + BMC
-- ---------------------------------------------------------------------------

local function collectFrame()
  local veh = getPlayerVehicle(0)
  if not veh then return nil end

  local rec = {}
  rec.pos = v3t(veh:getPosition())
  rec.dir = v3t(veh:getDirectionVector())
  rec.up = v3t(veh:getDirectionVectorUp())
  rec.rot = q2t(veh:getRotation())
  rec.clusterRot = q2t(veh:getClusterRotationSlow(veh:getRefNodeId()))

  -- Full refNode matrix
  local ok, mat = pcall(function() return veh:getRefNodeMatrix() end)
  if ok and mat then
    local col3 = mat:getColumn(3)
    local mq = quat(mat:toQuatF())
    rec.refNodePos = { col3.x, col3.y, col3.z }
    rec.refNodeQuat = { mq.x, mq.y, mq.z, mq.w }
  end

  -- GPU pool vertices (for both JSON sample and BMC frame)
  local meshInfo = GPUMesh.bng_getGPUMesh(veh:getId())
  if meshInfo then
    rec.meshVertCount = meshInfo.verticesCount
    rec.meshIdxCount = meshInfo.indicesCount
    rec.meshFmCount = meshInfo.flexmeshesCount

    local maxWait = 30
    while not meshInfo.dataIsReady and maxWait > 0 do
      coroutine.yield(1)
      maxWait = maxWait - 1
    end

    if meshInfo.dataIsReady then
      local vc = meshInfo.verticesCount
      local verts = ffi.new('float[?]', vc * 3)

      if meshInfo:verticesGet(verts) then
        -- === JSON: extract sample vertices ===
        local sv = {}
        for _, idx in ipairs(sampleIndices) do
          if idx < vc then
            local s = idx * 3
            sv[#sv + 1] = { verts[s], verts[s + 1], verts[s + 2] }
          end
        end
        rec.sampleVerts = sv

        -- === BMC: write frame data (timestamp + WORLD-SPACE vertices + identity transform) ===
        if bmcFile and vc == totalVerts then
          local ts = bmcFrameCount / 60.0
          writeFloat64(bmcFile, ts)

          -- Transform every vertex using refNode matrix (the EXACT render matrix).
          -- world_v = refNodeMatrix @ local_v   (column-vector convention,
          -- matching the GPU vertex shader).
          local ok, mat = pcall(function() return veh:getRefNodeMatrix() end)
          if ok and mat then
            local c0 = mat:getColumn(0)   -- right   (X basis)
            local c1 = mat:getColumn(1)   -- forward (Y basis)
            local c2 = mat:getColumn(2)   -- up      (Z basis)
            local c3 = mat:getColumn(3)   -- translation
            local world_verts = ffi.new('float[?]', vc * 3)
            for i = 0, vc * 3 - 1, 3 do
              local lx, ly, lz = verts[i], verts[i+1], verts[i+2]
              world_verts[i]   = lx * c0.x + ly * c1.x + lz * c2.x + c3.x
              world_verts[i+1] = lx * c0.y + ly * c1.y + lz * c2.y + c3.y
              world_verts[i+2] = lx * c0.z + ly * c1.z + lz * c2.z + c3.z
            end
            bmcFile:write(ffi.string(world_verts, vc * 12))

            -- Identity quaternion (viewer reads positions directly, no rotation)
            writeFloat32(bmcFile, c3.x)
            writeFloat32(bmcFile, c3.y)
            writeFloat32(bmcFile, c3.z)
            writeFloat32(bmcFile, 0.0)
            writeFloat32(bmcFile, 0.0)
            writeFloat32(bmcFile, 0.0)
            writeFloat32(bmcFile, 1.0)
          else
            -- Fallback: raw local-space positions + original transform
            bmcFile:write(ffi.string(verts, vc * 12))
            local p = veh:getPosition()
            local dir = veh:getDirectionVector()
            local up = veh:getDirectionVectorUp()
            local q = quatFromDir(vec3(-dir.x, -dir.y, -dir.z), up)
            writeFloat32(bmcFile, p.x)
            writeFloat32(bmcFile, p.y)
            writeFloat32(bmcFile, p.z)
            writeFloat32(bmcFile, q.x)
            writeFloat32(bmcFile, q.y)
            writeFloat32(bmcFile, q.z)
            writeFloat32(bmcFile, q.w)
          end
          bmcFile:flush()

          bmcFrameCount = bmcFrameCount + 1
        end
      end
    end
    meshInfo:free()
  else
    rec.meshVertCount = 0
    rec.meshIdxCount = 0
    rec.meshFmCount = 0
  end

  -- Debug vertices (getDebugVertexPos)
  if debugFlexId then
    local flexObj = veh:getFlexmesh(debugFlexId)
    if flexObj then
      local dv = {}
      for _, idx in ipairs(sampleIndices) do
        local ok, v = pcall(function() return flexObj:getDebugVertexPos(idx) end)
        if ok and v then
          dv[#dv + 1] = { v.x, v.y, v.z }
        end
      end
      if #dv > 0 then
        rec.debugVerts = dv
      end
    end
  end

  return rec
end

-- ---------------------------------------------------------------------------
--  Stop / Flush
-- ---------------------------------------------------------------------------

function M.stop()
  active = false
  M.flush()
end

function M.onUpdate()
  if not active then return end
  local rec = collectFrame()
  if rec then frames[#frames + 1] = rec end
  if #frames >= maxFrames then active = false; M.flush() end
end

function M.onInit()
  setExtensionUnloadMode(M, 'manual')
  log('I', logTag, 'Diagnostic Dump + BMC — produces matching .json + .bmc outputs')
end

function M.onReset()
  active = false
  bmcFrameCount = 0
  if bmcFile then
    bmcFile:close()
    bmcFile = nil
  end
end

-- ---------------------------------------------------------------------------
--  Flush — write JSON, close BMC, reset state
-- ---------------------------------------------------------------------------

function M.flush()
  if #frames == 0 then
    log('W', logTag, 'No frames captured, nothing to write')
    return
  end

  -- Close BMC file
  if bmcFile then
    bmcFile:close()
    bmcFile = nil
    log('I', logTag, string.format('BMC closed: %s (%d frames)', bmcPath, bmcFrameCount))
  end

  -- Disable flexmesh debug mode
  local veh = getPlayerVehicle(0)
  if veh and debugFlexId then
    pcall(function() veh:setFlexmeshDebugMode(false) end)
  end

  -- Write JSON
  local data = {
    sampleIndices = sampleIndices,
    totalFrames = #frames,
    bmcFrames = bmcFrameCount,
    note = 'BMC positions are WORLD-SPACE (transformed by refNode matrix in Lua). '
         .. 'Positions in physics convention (Z-up, X=right, Y=forward). '
         .. 'BMC transform is identity (positions are pre-baked). '
         .. 'sampleVerts from GPU pool (Y-up: X=length, Y=up, Z=width). '
         .. 'debugVerts from getDebugVertexPos (world-oriented, ref-relative). ',
    frames = frames,
  }
  local json = jsonEncodePretty(data)
  local dirEnd = jsonPath:match('^(.+)/[^/]+$')
  if dirEnd and FS then
    FS:directoryCreate(dirEnd, true)
  end
  local f, err = io.open(jsonPath, 'w')
  if f then
    f:write(json)
    f:close()
    log('I', logTag, string.format('Wrote %d frames to %s (%d KB)',
      #frames, jsonPath, math.floor(#json / 1024)))
  else
    log('E', logTag, 'Failed to write ' .. jsonPath .. ': ' .. tostring(err))
  end

  -- Reset
  frames = {}
  sampleIndices = nil
  debugFlexId = nil
  active = false
  bmcFrameCount = 0
  staticIndices = nil
  staticUvs = nil
  totalVerts = 0
  totalIndices = 0
  hasUvs = false
  primitives = {}
  materials = {}
end

return M
