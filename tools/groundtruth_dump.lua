-- =============================================================================
--  Ground-truth transform dumper v2 — RENDER-MESH VERTEX based (READ-ONLY)
-- =============================================================================
--  Why v2: v1 sampled softbody NODES by index, which included wheels/suspension
--  that articulate independently (pairwise distances swung up to 10 m). A rigid
--  rotation cannot be solved from a non-rigid cloud. v2 uses the RENDER-MESH
--  vertices of one flexmesh (the body shell) via getDebugVertexPos — the exact
--  data we store in the pool, and rigid except at genuine deformation.
--
--  Key facts from BeamNG's own code (veFlexbodyDebug.lua):
--    worldVert(i) = flexbodyObj:getDebugVertexPos(i) + veh:getPosition()   (:260)
--    localVert(i) = rotInv * getDebugVertexPos(i) + refPos                 (:320)
--       where rotInv = quatFromDir(-getDirectionVector(), getDirectionVectorUp()):inversed()
--  => getDebugVertexPos(i) is in WORLD ORIENTATION (rotates with the tumble);
--     applying the inverse vehicle rotation recovers the constant rest vertex.
--  So the CORRECT rotation R satisfies: R^T * debugVert(i,f) is frame-invariant.
--  The calibrator uses exactly that (translation-immune, no pool correspondence).
--
--  We record, per frame, for ~16 vertices of the chosen flexmesh:
--     debugVert(i) = getDebugVertexPos(i)              -- world-oriented, ref-relative
--  plus every rotation candidate so the calibrator can pick the winner:
--     rot            = getRotation()                    (identity for softbody)
--     clusterRot     = getClusterRotationSlow(refNode)
--     dir, up        = getDirectionVector(), getDirectionVectorUp()
--       (calibrator forms quatFromDir(dir,up) AND quatFromDir(-dir,up))
--     pos            = getPosition()
--
--  Usage (GE Lua console). Place this file so it can be loaded as an extension,
--  or paste its body, then:
--    groundtruth_dump.start('tools/gt_dump.json', 300)   -- flexmesh index 0 = body
--    -- ...crash the car so it TUMBLES...
--    groundtruth_dump.stop()   -- or auto-flush at frameCount
-- =============================================================================

local M = {}
local logTag = 'gtDump'

local active = false
local frames = {}
local maxFrames = 0
local outPath = nil
local vertIds = nil
local flexId = nil        -- fid passed to getFlexmesh
local flexIndexRequested = 0

local function v2t(v) return { v.x, v.y, v.z } end
local function q2t(q) return { q.x, q.y, q.z, q.w } end

-- Resolve a flexbody fid. `want` may be a number (index into sorted names) OR a
-- string (case-insensitive substring of the mesh name, e.g. "body"). Logs the
-- full sorted list so the right part is easy to pick.
local function resolveFlex(veh, want)
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
    log('I', logTag, string.format('   [%d] %s', idx - 1, nm))
  end

  local pickName
  if type(want) == 'string' then
    local low = want:lower()
    for _, nm in ipairs(sorted) do
      if nm:lower():find(low, 1, true) then pickName = nm; break end
    end
    if not pickName then
      log('W', logTag, 'no flexmesh matched "' .. want .. '"; falling back to index 0')
      pickName = sorted[1]
    end
  else
    pickName = sorted[(want or 0) + 1] or sorted[1]
  end
  local flexbody = fb[namesToID[pickName]]
  return flexbody.fid, pickName
end

local function pickVerts(flexObj)
  local vc = flexObj:getVertexCount()
  local ids = {}
  local count = math.min(16, vc)
  local step = math.max(1, math.floor(vc / count))
  for i = 0, vc - 1, step do
    ids[#ids + 1] = i
    if #ids >= count then break end
  end
  return ids
end

-- flexSel: number index OR string substring (e.g. 'body'). Default 'body'.
function M.start(path, frameCount, flexSel)
  local veh = getPlayerVehicle(0)
  if not veh then log('E', logTag, 'no player vehicle'); return false end
  outPath = path or 'tools/gt_dump.json'
  maxFrames = frameCount or 300
  if flexSel == nil then flexSel = 'body' end
  flexIndexRequested = flexSel
  frames = {}

  veh:setFlexmeshDebugMode(true)   -- REQUIRED for getDebugVertexPos
  local pickName
  flexId, pickName = resolveFlex(veh, flexSel)
  if not flexId then log('E', logTag, 'could not resolve flexmesh'); return false end
  local flexObj = veh:getFlexmesh(flexId)
  if not flexObj then log('E', logTag, 'getFlexmesh returned nil'); return false end
  vertIds = pickVerts(flexObj)

  active = true
  log('I', logTag, string.format('gt dump v2 started: flexmesh "%s" (fid=%s), %d verts, up to %d frames',
    tostring(pickName), tostring(flexId), #vertIds, maxFrames))
  return true
end

local function collectFrame(veh)
  local flexObj = veh:getFlexmesh(flexId)
  if not flexObj then return nil end
  local rec = {}
  rec.pos = v2t(veh:getPosition())
  rec.rot = q2t(veh:getRotation())
  rec.clusterRot = q2t(veh:getClusterRotationSlow(veh:getRefNodeId()))
  rec.dir = v2t(veh:getDirectionVector())
  rec.up = v2t(veh:getDirectionVectorUp())
  local verts = {}
  for _, i in ipairs(vertIds) do
    verts[#verts + 1] = v2t(flexObj:getDebugVertexPos(i))
  end
  rec.debugVerts = verts
  return rec
end

local function flush()
  jsonWriteFile(outPath, {
    flexIndex = flexIndexRequested,
    vertIds = vertIds,
    frames = frames,
    note = 'debugVert is world-oriented (ref-relative); correct R makes R^T*debugVert frame-invariant',
  }, true)
  log('I', logTag, string.format('wrote %d frames to %s', #frames, outPath))
  local veh = getPlayerVehicle(0)
  if veh then veh:setFlexmeshDebugMode(false) end
end

function M.stop()
  active = false
  flush()
end

function M.onUpdate()
  if not active then return end
  local veh = getPlayerVehicle(0)
  if not veh then return end
  local rec = collectFrame(veh)
  if rec then frames[#frames + 1] = rec end
  if #frames >= maxFrames then active = false; flush() end
end

function M.onInit()
  setExtensionUnloadMode(M, 'manual')
  log('I', logTag, 'groundtruth_dump v2 loaded')
end

return M
