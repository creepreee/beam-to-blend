-- =============================================================================
--  BeamNG Motion Capture (BMC) — GPU readback backend for BMC v1 format
-- =============================================================================
--  Architecture rule (non-negotiable):
--    The capture backend must never interpret the mesh. It only copies the
--    evaluated vehicle state from BeamNG into the capture format. All
--    reconstruction, optimization, compression, and engine-specific processing
--    belong to later stages.
--
--  This mod reads the full shared GPU vertex pool via GPUMesh.bng_getGPUMesh()
--  and writes a single capture.bmc file. It does NOT decompose per-primitive
--  local arrays; it does NOT call indicesMinMax; it does NOT weld or repair.
--
--  Usage:
--    1. Enable mod "beamng_capture_v7" in BeamNG's Mods menu
--    2. extensions.reload("beamng_capture")
--    3. M.startCapture("captures/mycrash", 600)
--
--  Output: <dir>/capture.bmc  (BMC v1 format)
-- =============================================================================

local M                     = {}
local logTag                = 'beamngCapture'

local ffi = require('ffi')
local bit = bit or require('bit')

-- State machine
local CAPTURE_IDLE          = 0
local CAPTURE_WAIT_MESH     = 1
local state                 = CAPTURE_IDLE

local captureDir            = nil
local maxFrames             = 0
local currentFrame          = 0
local binFile               = nil
local currentMeshInfo       = nil

-- Static data (captured once from frame 0)
local staticIndices         = nil
local staticUvs             = nil
local totalVerts            = 0
local totalIndices          = 0
local primitives            = {}
local materials             = {}
local hasUvs                = false

-- Frame-0 reference hashes for validation
local refIdxHash            = nil
local refUvHash             = nil

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

local FLAG_HAS_TRANSFORM = 4

-- ---------------------------------------------------------------------------
--  Sample-based hash (reads first/last N uint32 values — avoids byte loops)
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
--  Header writing
-- ---------------------------------------------------------------------------

local function writeBmcHeader(f, vc, ic, pc, mc, flags, frameSize, staticSize)
    f:write('BMC1')
    writeU32(f, 1)
    writeU32(f, vc)
    writeU32(f, ic)
    writeU32(f, pc)
    writeU32(f, mc)
    writeU32(f, flags)
    writeU32(f, frameSize)
    writeU32(f, staticSize % 4294967296)
    writeU32(f, math.floor(staticSize / 4294967296))
end

-- ---------------------------------------------------------------------------
--  Primitive / material table entries
-- ---------------------------------------------------------------------------

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
--  Main capture logic
-- ---------------------------------------------------------------------------

local function requestMesh(veh)
    if currentMeshInfo then
        currentMeshInfo:free()
        currentMeshInfo = nil
    end
    currentMeshInfo = GPUMesh.bng_getGPUMesh(veh:getId())
    state = CAPTURE_WAIT_MESH
end

local function finishCapture(reason)
    if binFile then
        binFile:close()
        binFile = nil
    end
    log('I', logTag, string.format('Capture %s: %d frames, %d primitives',
        tostring(reason), currentFrame, #primitives))
    if currentMeshInfo then
        currentMeshInfo:free()
        currentMeshInfo = nil
    end
    state = CAPTURE_IDLE
end

-- ---------------------------------------------------------------------------
--  Per-frame vertex capture
-- ---------------------------------------------------------------------------

local function captureFrame(meshInfo)
    local vc = meshInfo.verticesCount
    local vertices = ffi.new('float[?]', vc * 3)
    if not meshInfo:verticesGet(vertices) then
        return nil
    end
    return ffi.string(vertices, vc * 12)
end

-- ---------------------------------------------------------------------------
--  onCaptureUpdate — called from onUpdate when dataIsReady
-- ---------------------------------------------------------------------------

local function onCaptureUpdate()
    if not currentMeshInfo then return end
    if not currentMeshInfo.dataIsReady then return end

    local veh = getPlayerVehicle(0)
    if not veh then
        finishCapture('vehicle_gone')
        return
    end

    if currentFrame == 0 then
        -- ====================================================================
        --  FRAME 0: capture all static data + first set of positions
        -- ====================================================================

        totalVerts   = currentMeshInfo.verticesCount
        totalIndices = currentMeshInfo.indicesCount
        local uvCount = currentMeshInfo.uv1Count or 0
        hasUvs = uvCount > 0

        log('I', logTag, string.format(
            'Frame 0: %d vertices, %d indices, %d flexmeshes, %d UVs',
            totalVerts, totalIndices,
            currentMeshInfo.flexmeshesCount,
            uvCount))

        -- Index buffer (read once, shared across all primitives)
        staticIndices = ffi.new('unsigned int[?]', totalIndices)
        if not currentMeshInfo:indicesGet(staticIndices) then
            finishCapture('read_error_idx')
            return
        end

        -- UVs (read once, shared)
        if hasUvs then
            staticUvs = ffi.new('float[?]', uvCount * 2)
            if not currentMeshInfo:uv1Get(staticUvs) then
                staticUvs = nil
                hasUvs = false
                log('W', logTag, 'uv1Get failed — continuing without UVs')
            end
        end

        -- Build primitive table from all flexmeshes
        primitives = {}
        local fmCount = currentMeshInfo.flexmeshesCount
        for i = 0, fmCount - 1 do
            local fm = currentMeshInfo:flexmeshes(i)
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

        log('I', logTag, string.format('  %d primitives total', #primitives))

        -- Build material table
        materials = {}
        if veh and veh.getMaterialNames then
            local names = veh:getMaterialNames()
            for _, n in ipairs(names) do
                table.insert(materials, n)
            end
        end
        log('I', logTag, string.format('  %d materials', #materials))

        -- Compute sizes
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

        local frameSize = 8 + totalVerts * 12 + 28

        -- Write header + static section
        local binPath = captureDir .. '/capture.bmc'
        local err
        binFile, err = io.open(binPath, 'wb')
        if not binFile then
            finishCapture('open_error')
            return
        end

        writeBmcHeader(binFile, totalVerts, totalIndices, #primitives, #materials, flags, frameSize, staticSize)

        -- Index data
        binFile:write(ffi.string(staticIndices, totalIndices * 4))

        -- UV data
        if hasUvs and staticUvs then
            binFile:write(ffi.string(staticUvs, uvCount * 8))
        end

        -- Primitive table
        for _, p in ipairs(primitives) do
            writePrimitiveEntry(binFile, p.name, p.startIndex, p.indexCount, p.materialId, p.flexmeshIndex)
        end

        -- Material table
        for _, m in ipairs(materials) do
            writeMaterialEntry(binFile, m)
        end

        -- Frame 0: timestamp + positions + vehicle transform
        local posBytes = captureFrame(currentMeshInfo)
        if not posBytes then
            finishCapture('read_error')
            return
        end
        writeFloat64(binFile, 0.0)
        binFile:write(posBytes)
        local p = veh:getPosition()
        local dir = veh:getDirectionVector()
        local up = veh:getDirectionVectorUp()
        local q = quatFromDir(vec3(-dir.x, -dir.y, -dir.z), up)
        writeFloat32(binFile, p.x)
        writeFloat32(binFile, p.y)
        writeFloat32(binFile, p.z)
        writeFloat32(binFile, q.x)
        writeFloat32(binFile, q.y)
        writeFloat32(binFile, q.z)
        writeFloat32(binFile, q.w)
        binFile:flush()

        log('I', logTag, string.format(
            '  Static section: header + %d indices + %d UVs + %d prims + %d mats',
            totalIndices, uvCount, #primitives, #materials))
        log('I', logTag, string.format('  Frame 0 captured: %d verts, %d bytes', totalVerts, #posBytes))

        -- Reference hashes for validation
        refIdxHash = hashSample(staticIndices, totalIndices * 4)
        if hasUvs and staticUvs then
            refUvHash = hashSample(staticUvs, uvCount * 8)
        end
        log('I', logTag, string.format('  Hashes: idx=%d uv=%s', refIdxHash,
            refUvHash and tostring(refUvHash) or '(none)'))

        currentFrame = 1
        requestMesh(veh)

    elseif currentFrame < maxFrames then
        -- ====================================================================
        --  FRAME N: validate topology, capture positions
        -- ====================================================================

        -- Quick topology check via sample hash
        local ic = currentMeshInfo.indicesCount
        local idxBuf = ffi.new('unsigned int[?]', ic)
        if not currentMeshInfo:indicesGet(idxBuf) then
            finishCapture('validate_fail')
            return
        end
        if hashSample(idxBuf, ic * 4) ~= refIdxHash then
            log('E', logTag, string.format(
                'TOPOLOGY CHANGE at frame %d — stopping capture',
                currentFrame))
            finishCapture('topology_changed')
            return
        end

        if hasUvs and staticUvs then
            local uc = currentMeshInfo.uv1Count
            local uvBuf = ffi.new('float[?]', uc * 2)
            if currentMeshInfo:uv1Get(uvBuf) then
                if hashSample(uvBuf, uc * 8) ~= refUvHash then
                    log('W', logTag, string.format(
                        'UV hash mismatch at frame %d', currentFrame))
                end
            end
        end

        -- Capture positions
        local posBytes = captureFrame(currentMeshInfo)
        if not posBytes then
            finishCapture('read_error')
            return
        end

        writeFloat64(binFile, currentFrame / 60.0)
        binFile:write(posBytes)
        local p = veh:getPosition()
        local dir = veh:getDirectionVector()
        local up = veh:getDirectionVectorUp()
        local q = quatFromDir(vec3(-dir.x, -dir.y, -dir.z), up)
        writeFloat32(binFile, p.x)
        writeFloat32(binFile, p.y)
        writeFloat32(binFile, p.z)
        writeFloat32(binFile, q.x)
        writeFloat32(binFile, q.y)
        writeFloat32(binFile, q.z)
        writeFloat32(binFile, q.w)
        binFile:flush()

        currentFrame = currentFrame + 1
        if currentFrame % 100 == 0 then
            log('I', logTag, string.format('  Frame %d/%d captured', currentFrame, maxFrames))
        end

        if currentFrame >= maxFrames then
            log('I', logTag, string.format('All %d frames captured', maxFrames))
            finishCapture('complete')
            return
        end

        requestMesh(veh)
    end
end

-- =============================================================================
--  PUBLIC API
-- =============================================================================

function M.startCapture(dir, frameCount)
    log('I', logTag, string.format('========== START BMC CAPTURE =========='))
    log('I', logTag, string.format('Dir: %s  Frames: %d', dir, frameCount))

    captureDir   = dir
    maxFrames    = frameCount
    currentFrame = 0
    primitives   = {}
    materials    = {}
    staticIndices = nil
    staticUvs    = nil
    refIdxHash   = nil
    refUvHash    = nil

    if FS then
        FS:directoryCreate(dir, true)
    end

    local veh = getPlayerVehicle(0)
    if not veh then
        log('E', logTag, 'No player vehicle')
        return false
    end
    log('I', logTag, 'Vehicle ID: ' .. tostring(veh:getId()))

    requestMesh(veh)
    return true
end

-- =============================================================================
--  LIFECYCLE
-- =============================================================================

function M.onInit()
    log('I', logTag, 'BeamNG BMC Capture loaded (shared-pool backend, BMC v1)')
    setExtensionUnloadMode(M, 'manual')
end

function M.onReset()
    if currentMeshInfo then
        currentMeshInfo:free()
        currentMeshInfo = nil
    end
    state = CAPTURE_IDLE
end

function M.onUpdate()
    if state == CAPTURE_WAIT_MESH and currentMeshInfo and currentMeshInfo.dataIsReady then
        onCaptureUpdate()
    end
end

return M
