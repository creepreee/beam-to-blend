-- BeamNG Motion Capture (BMC) exporter
-- version: 7 (modded — single extension file, no per-frame debug logging)
--
-- v4 adds rigid-prop capture: BeamNG exposes steering wheel / undertray / wipers
-- and other rigid, non-deformable parts in a SEPARATE list from flexmeshes
-- (meshInfo:propmeshes(i) / propmeshesCount), which earlier versions never
-- iterated — so those parts were missing from the import.  Props are
-- self-contained (own verticesGet/indicesGet/uv1Get) with a per-frame
-- position + rotation; we bake local verts through that transform so their
-- output positions live in the same vehicle space as flexmesh verts.  No
-- capture.bin/meta format change: props are just extra objects in meta order.
--
-- Writes capture.bin + capture.meta directly from GPU mesh data, bypassing GLTF.
--
-- Usage:
--   1. Load as extension "beamng_capture" (mod installs to mods/ or place directly in lua/ge/extensions/)
--   2. Call M.startCapture(dir, frameCount) from the console.
--
-- capture.bin layout (must match importer/capture_reader.py):
--   [STATIC]  per object: indices (uint32[]), UVs (float32[])
--   [FRAMES]  per frame: per object positions (float32[N*3]) + vehicle world
--             transform (7 float32 = px,py,pz, qx,qy,qz,qw)
--   [FOOTER]  frame_count (uint64), frame_offsets[] (uint64), footer_offset (uint64)
-- capture.meta version 2 includes the vehicle transform block (version 1 omitted it).
--
-- DEBUG VERSION: logs everything possible to diagnose vertex corruption.

local M = {}
local logTag = 'beamngCapture'

local ffi = require('ffi')

-- State machine
local CAPTURE_IDLE = 0
local CAPTURE_WAIT_MESH = 1
local CAPTURE_WAIT_NEXT = 2
local state = CAPTURE_IDLE

local captureDir = nil
local maxFrames = 0
local currentFrame = 0
local frameOffsets = {}
local metaObjects = {}
local binFile = nil
local currentMeshInfo = nil
local vehId = nil

-- Cached flexmesh info (indices, UVs, material names) — frame 0 only
local staticBins = {}      -- ordered list of {indices, uvs}

-- Iteration state
local flexmeshCache = {}   -- {name: {flexmesh_ptr, primitives_ptr, minIdx, vc}}
local propCache = {}       -- {name: {propIndex, minIdx, vertexCount}} for rigid props
local bksPlayed = false    -- whether we've triggered BKS animation already

-- Debug: collect per-frame timing
local debugTiming = {}

-- ============================================================================
-- DEBUG STATE — everything here is for diagnostic logging only
-- ============================================================================

-- Frame-0 reference data for cross-frame comparison
local dbg_frame0_refs = {}  -- {objName -> {positions = float[N*3], minIdx, maxIdx, vc, firstIdx, lastIdx}}
local dbg_spot_indices = {} -- indices of primitives to track every frame
local dbg_total_verts = {}  -- per-frame total vertex count from GPU
local dbg_total_indices = {} -- per-frame total index count from GPU
local dbg_total_flexmeshes = {} -- per-frame flexmesh count
local dbg_frame_pos_deltas = {} -- {frame -> {objName -> maxDeltaFromFrame0}}
local dbg_index_overflow = {} -- {frame -> list of objects where minIdx+vc > totalVerts}
local dbg_vert_pool_size = {} -- {frame -> totalVerts}
local dbg_raw_indices_sample = {} -- frame-0 raw index values for spot objects
local diagFile = nil  -- file handle for debug dump


-- --------------------------------------------------------------------------
-- Binary writing helpers
-- --------------------------------------------------------------------------

local function writeU32(f, val)
    f:write(string.char(
        bit.band(val, 0xFF),
        bit.band(bit.rshift(val, 8), 0xFF),
        bit.band(bit.rshift(val, 16), 0xFF),
        bit.band(bit.rshift(val, 24), 0xFF)
    ))
end

local function writeU64(f, val)
    local lo = val % 4294967296  -- 2^32
    local hi = math.floor(val / 4294967296)
    writeU32(f, lo)
    writeU32(f, hi)
end


-- --------------------------------------------------------------------------
-- Prop transform helper — rotate a local-space vertex by a quaternion and
-- translate by the prop position, producing a vehicle-space position that
-- matches the flexmesh vertex space.  q = (x,y,z,w), v = (x,y,z), p = position.
-- Standard quaternion-vector rotation: v' = v + 2*cross(q.xyz, cross(q.xyz, v) + w*v)
-- --------------------------------------------------------------------------

local function transformPropVertex(vx, vy, vz, qx, qy, qz, qw, px, py, pz)
    -- t = 2 * cross(q.xyz, v)
    local tx = 2 * (qy * vz - qz * vy)
    local ty = 2 * (qz * vx - qx * vz)
    local tz = 2 * (qx * vy - qy * vx)
    -- v' = v + qw * t + cross(q.xyz, t)
    local rx = vx + qw * tx + (qy * tz - qz * ty)
    local ry = vy + qw * ty + (qz * tx - qx * tz)
    local rz = vz + qw * tz + (qx * ty - qy * tx)
    return rx + px, ry + py, rz + pz
end


-- --------------------------------------------------------------------------
-- DEBUG: write diagnostic dump file
-- --------------------------------------------------------------------------

local function writeDiagDump()
    if not diagFile then return end

    diagFile:write('\n========== CAPTURE DIAGNOSTIC DUMP ==========\n')
    diagFile:write(string.format('Total objects: %d\n', #metaObjects))
    diagFile:write(string.format('Total frames captured: %d\n', #frameOffsets))

    -- Frame-0 reference data
    diagFile:write('\n--- FRAME 0 REFERENCE DATA ---\n')
    for name, ref in pairs(dbg_frame0_refs) do
        diagFile:write(string.format(
            '  %s: minIdx=%d maxIdx=%d vc=%d firstIdx=%s lastIdx=%s\n',
            name, ref.minIdx, ref.maxIdx, ref.vc,
            tostring(ref.firstIdx), tostring(ref.lastIdx)))
        -- Log first3 and last3 positions
        local p = ref.positions
        local n = ref.vc
        if n > 0 then
            diagFile:write(string.format(
                '    pos[0]=(%.4f, %.4f, %.4f) pos[1]=(%.4f, %.4f, %.4f) pos[2]=(%.4f, %.4f, %.4f)\n',
                p[0], p[1], p[2], p[3], p[4], p[5], p[6], p[7], p[8]))
            if n > 3 then
                local li = (n - 1) * 3
                diagFile:write(string.format(
                    '    pos[%d]=(%.4f, %.4f, %.4f) pos[%d]=(%.4f, %.4f, %.4f)\n',
                    n-1, p[li], p[li+1], p[li+2],
                    n-2, p[li-3], p[li-2], p[li-1]))
            end
        end
        -- Log first few raw index values
        if dbg_raw_indices_sample[name] then
            diagFile:write('    raw_idx[0..5] = ')
            local ridx = dbg_raw_indices_sample[name]
            for k = 0, math.min(5, #ridx - 1) do
                diagFile:write(string.format('%d ', ridx[k]))
            end
            diagFile:write('\n')
        end
    end

    -- Per-frame vertex pool stability
    diagFile:write('\n--- VERTEX POOL STABILITY PER FRAME ---\n')
    for frame, tv in pairs(dbg_vert_pool_size) do
        diagFile:write(string.format(
            '  frame %d: totalVerts=%d totalIndices=%s totalFlexmeshes=%s\n',
            frame, tv,
            tostring(dbg_total_indices[frame] or '?'),
            tostring(dbg_total_flexmeshes[frame] or '?')))
    end

    -- Per-frame position deltas
    diagFile:write('\n--- POSITION DELTAS FROM FRAME 0 (max per object, sampled frames) ---\n')
    local sortedFrames = {}
    for f, _ in pairs(dbg_frame_pos_deltas) do table.insert(sortedFrames, f) end
    table.sort(sortedFrames)
    for _, frame in ipairs(sortedFrames) do
        local deltas = dbg_frame_pos_deltas[frame]
        local maxDelta = 0
        local worstObj = ''
        for name, d in pairs(deltas) do
            if d > maxDelta then
                maxDelta = d
                worstObj = name
            end
        end
        if maxDelta > 0.001 then  -- only log frames with actual motion
            diagFile:write(string.format('  frame %d: maxDelta=%.4f (%s)\n', frame, maxDelta, worstObj))
        end
    end

    -- Index overflow events
    diagFile:write('\n--- INDEX OVERFLOW EVENTS ---\n')
    for frame, objs in pairs(dbg_index_overflow) do
        for _, info in ipairs(objs) do
            diagFile:write(string.format(
                '  frame %d: %s minIdx=%d vc=%d totalVerts=%d OVERFLOW=%d\n',
                frame, info.name, info.minIdx, info.vc, info.totalVerts,
                info.minIdx + info.vc - info.totalVerts))
        end
    end
    if next(dbg_index_overflow) == nil then
        diagFile:write('  (none)\n')
    end

    diagFile:write('\n========== END DUMP ==========\n')
    diagFile:flush()
    diagFile:close()
    diagFile = nil
end


-- --------------------------------------------------------------------------
-- Mesh extraction — fixed to iterate ALL primitives per flexmesh
-- --------------------------------------------------------------------------

local function extractFlexmeshes(meshInfo, veh)
    -- Returns: ordered list of {name, matName, vc, ic, indices_ffi, uvs_ffi, minIdx}

    local totalIndices = meshInfo.indicesCount
    local indices = ffi.new('unsigned int[?]', totalIndices)
    if not meshInfo:indicesGet(indices) then
        log('E', logTag, 'Cannot get indices')
        return nil
    end

    local uvs = nil
    local uvCount = meshInfo.uv1Count
    if uvCount > 0 then
        uvs = ffi.new('float[?]', uvCount * 2)
        if not meshInfo:uv1Get(uvs) then
            uvs = nil
        end
    end

    local matNames = {}
    if veh and veh.getMaterialNames then
        local names = veh:getMaterialNames()
        for i, n in ipairs(names) do
            matNames[i - 1] = n
        end
        log('I', logTag, string.format('  Material names count: %d', #names))
        if #names <= 20 then
            for ni, nn in ipairs(names) do
                log('I', logTag, string.format('    mat[%d] = "%s"', ni - 1, nn))
            end
        end
    else
        log('W', logTag, '  No material names from vehicle')
    end

    log('I', logTag, string.format(
        '=== extractFlexmeshes START: %d flexmeshes, %d total indices, %d UVs ===',
        meshInfo.flexmeshesCount, totalIndices, uvCount))

    local result = {}
    local seenNames = {}
    local totalPrimsBefore = 0
    local totalPrimsAfter = 0
    local primCountsLog = {}

    for i = 0, meshInfo.flexmeshesCount - 1 do
        local flexmesh = meshInfo:flexmeshes(i)
        local meshName = flexmesh.meshName or ('object_' .. i)
        local primCount = flexmesh.primitivesCount
        totalPrimsBefore = totalPrimsBefore + primCount

        table.insert(primCountsLog, string.format('  flexmesh[%d] "%s": %d prims', i, meshName, primCount))

        if primCount == 0 then
            log('W', logTag, 'No primitives for ' .. meshName .. ' — skipping')
            goto continueFm
        end

        local primitives = ffi.new('gpuPrimitive_t[?]', primCount)
        if not flexmesh:primitivesGet(primitives) then
            log('W', logTag, 'Cannot get primitives for ' .. meshName .. ' — skipping')
            goto continueFm
        end

        -- *** FIX: iterate ALL primitives, not just primitives[0] ***
        for p = 0, primCount - 1 do
            local prim = primitives[p]
            local si = prim.startIndex
            local ic = prim.indexCount
            local minIdx, maxIdx = meshInfo:indicesMinMax(si, si + ic)
            local vc = maxIdx - minIdx + 1

            -- ================================================================
            -- DEBUG: also check with OLD range to detect off-by-one
            -- ================================================================
            local minIdx_old, maxIdx_old = meshInfo:indicesMinMax(si, si + ic - 1)
            if minIdx ~= minIdx_old or maxIdx ~= maxIdx_old then
                log('I', logTag, string.format(
                    '    RANGE FIX: old(minIdx=%d,maxIdx=%d,vc=%d) -> new(minIdx=%d,maxIdx=%d,vc=%d)',
                    minIdx_old, maxIdx_old, maxIdx_old - minIdx_old + 1,
                    minIdx, maxIdx, vc))
            end

            -- ================================================================
            -- DEBUG: log EVERY primitive, not just first
            -- ================================================================
            log('I', logTag, string.format(
                '  PRIM[%d/%d] "%s" p=%d: si=%d ic=%d minIdx=%d maxIdx=%d vc=%d matId=%d mat="%s"',
                p + 1, primCount, meshName, p,
                si, ic, minIdx, maxIdx, vc,
                prim.materialId,
                tostring(matNames[prim.materialId] or '?')))

            if vc <= 0 or ic <= 0 then
                log('W', logTag, string.format(
                    '  SKIP: vc=%d ic=%d (degenerate)', vc, ic))
                goto continuePrim
            end

            -- ================================================================
            -- DEBUG: check if minIdx+vc exceeds total vertex count
            -- ================================================================
            if minIdx + vc > meshInfo.verticesCount then
                log('E', logTag, string.format(
                    '  *** INDEX OVERFLOW *** minIdx(%d) + vc(%d) = %d > totalVerts(%d)',
                    minIdx, vc, minIdx + vc, meshInfo.verticesCount))
            end

            -- ================================================================
            -- DEBUG: log raw index values (first 5, last 5, any duplicates)
            -- ================================================================
            local rawIdxSample = {}
            local idxSet = {}
            local duplicateCount = 0
            for j = 0, math.min(ic - 1, 19) do
                local rawVal = indices[si + j]
                local localVal = rawVal - minIdx
                rawIdxSample[j] = rawVal
                if j < 5 then
                    log('I', logTag, string.format(
                        '    idx[%d]: raw=%d local=%d (minIdx=%d)', j, rawVal, localVal, minIdx))
                end
                if idxSet[rawVal] then
                    duplicateCount = duplicateCount + 1
                else
                    idxSet[rawVal] = true
                end
            end
            if ic > 20 then
                for j = ic - 5, ic - 1 do
                    local rawVal = indices[si + j]
                    rawIdxSample[j] = rawVal
                    log('I', logTag, string.format(
                        '    idx[%d/%d]: raw=%d local=%d', j, ic, rawVal, rawVal - minIdx))
                end
            end

            local uniqueIdxCount = 0
            for _ in pairs(idxSet) do uniqueIdxCount = uniqueIdxCount + 1 end
            log('I', logTag, string.format(
                '    UNIQUE: %d unique indices out of %d total (duplicates=%d) vc=%d',
                uniqueIdxCount, ic, duplicateCount, vc))

            if uniqueIdxCount > vc then
                log('E', logTag, string.format(
                    '  *** BAD MESH *** %d unique indices > %d vertices — '
                    .. 'IMPOSSIBLE: index buffer references vertices outside minIdx..maxIdx range!',
                    uniqueIdxCount, vc))
            end

            -- ================================================================
            -- DEBUG: check the first few vertex positions at minIdx
            -- ================================================================
            local totalVerts = meshInfo.verticesCount
            local checkVerts = ffi.new('float[?]', math.min(vc, 10) * 3)
            -- We can't get individual vertices yet, but we record the bounds
            log('I', logTag, string.format(
                '    BOUNDS: minIdx=%d maxIdx=%d vc=%d totalGPUVerts=%d range_fraction=%.4f',
                minIdx, maxIdx, vc, totalVerts,
                vc / math.max(totalVerts, 1)))

            -- Extract local indices (shifted to 0-based for this primitive's vertex range)
            local localIdx = ffi.new('unsigned int[?]', ic)
            for j = 0, ic - 1 do
                localIdx[j] = indices[si + j] - minIdx
            end

            -- ================================================================
            -- DEBUG: verify local indices are all in [0, vc-1]
            -- ================================================================
            local localMin = vc
            local localMax = -1
            local outOfBounds = 0
            for j = 0, ic - 1 do
                local li = localIdx[j]
                if li < 0 or li >= vc then
                    outOfBounds = outOfBounds + 1
                    if outOfBounds <= 3 then
                        log('E', logTag, string.format(
                            '    LOCAL OOB idx[%d]=%d (vc=%d, raw=%d)',
                            j, li, vc, indices[si + j]))
                    end
                end
                if li < localMin then localMin = li end
                if li > localMax then localMax = li end
            end
            if outOfBounds > 0 then
                log('E', logTag, string.format(
                    '    TOTAL LOCAL OOB: %d/%d indices out of range [0, %d]',
                    outOfBounds, ic, vc - 1))
            else
                log('I', logTag, string.format(
                    '    LOCAL INDEX CHECK: OK (range [%d, %d], vc=%d)', localMin, localMax, vc))
            end

            -- Extract local UVs (one per vertex in this primitive's range)
            local localUvs = nil
            if uvs then
                localUvs = ffi.new('float[?]', vc * 2)
                for j = 0, vc - 1 do
                    localUvs[j * 2]     = uvs[(minIdx + j) * 2]
                    localUvs[j * 2 + 1] = uvs[(minIdx + j) * 2 + 1]
                end
            end

            local matName = matNames[prim.materialId] or tostring(prim.materialId)

            -- Determine unique object name.
            local primName = meshName
            if p > 0 then
                local mn = matNames[prim.materialId]
                if mn and mn ~= "" then
                    local safe = mn:gsub('[^%w_]+', '_')
                    safe = safe:gsub('^_+', ''):gsub('_+$', '')
                    if safe and safe ~= "" then
                        primName = meshName .. '_' .. safe
                    else
                        primName = meshName .. '_p' .. p
                    end
                else
                    primName = meshName .. '_p' .. p
                end
            end

            -- Deduplicate names
            if seenNames[primName] then
                local counter = 2
                local candidate
                repeat
                    candidate = primName .. '_' .. counter
                    counter = counter + 1
                until not seenNames[candidate]
                primName = candidate
            end
            seenNames[primName] = true

            table.insert(result, {
                name = primName,
                materialName = matName,
                vertexCount = vc,
                indexCount = ic,
                indices = localIdx,
                uvs = localUvs,
                minIdx = minIdx,
                flexmeshIndex = i,
            })

            -- ================================================================
            -- DEBUG: store raw index sample for this primitive
            -- ================================================================
            dbg_raw_indices_sample[primName] = rawIdxSample

            totalPrimsAfter = totalPrimsAfter + 1

            ::continuePrim::
        end -- for p

        ::continueFm::
    end -- for i

    -- Log summary
    for _, l in ipairs(primCountsLog) do
        log('I', logTag, l)
    end
    log('I', logTag, string.format(
        '=== extractFlexmeshes DONE: %d flexmeshes → %d total objects (was %d prims before fix) ===',
        meshInfo.flexmeshesCount, #result, totalPrimsBefore))

    return result
end


local function extractProps(meshInfo)
    -- Rigid props (steering wheel, undertray, wipers, badges, interior trim)
    -- live in a SEPARATE list from flexmeshes: meshInfo:propmeshes(i).  BeamNG's
    -- own GLB exporter (util/export.lua) reads them this way.  Unlike flexmeshes
    -- they do NOT share the vehicle vertex pool — each prop is self-contained
    -- with its own verticesGet / indicesGet / uv1Get and a per-prop world
    -- position + rotation.  We bake local verts through that transform at
    -- capture time so the output positions live in the same vehicle space as
    -- the flexmesh verts (no format change needed downstream).
    --
    -- Returns: ordered list of {name, materialName, vertexCount, indexCount,
    --   indices (localIdx ffi), uvs (ffi), propIndex, isProp=true}

    local propCount = meshInfo.propmeshesCount or 0
    if propCount == 0 then
        log('I', logTag, 'No propmeshes on this vehicle (0 rigid props)')
        return {}
    end

    log('I', logTag, string.format('=== extractProps START: %d propmeshes ===', propCount))

    local result = {}
    local seenNames = {}

    for i = 0, propCount - 1 do
        local prop = meshInfo:propmeshes(i)
        local meshName = prop.meshName or ('prop_' .. i)
        local vc = prop.verticesCount
        local ic = prop.indicesCount

        if vc <= 0 or ic <= 0 then
            log('W', logTag, string.format('  PROP[%d] "%s": empty (vc=%d ic=%d) — skipping',
                i, meshName, vc, ic))
            goto continueProp
        end

        -- Indices: props have their own local index buffer already 0-based.
        local idxBuf = ffi.new('unsigned int[?]', ic)
        if not prop:indicesGet(idxBuf) then
            log('W', logTag, '  Cannot get prop indices for ' .. meshName .. ' — skipping')
            goto continueProp
        end
        local localIdx = ffi.new('unsigned int[?]', ic)
        for j = 0, ic - 1 do
            local v = idxBuf[j]
            if v >= vc then v = vc - 1 end  -- clamp defensively
            localIdx[j] = v
        end

        -- UVs (optional)
        local localUvs = nil
        if prop.uv1Count and prop.uv1Count > 0 then
            local uvBuf = ffi.new('float[?]', prop.uv1Count * 2)
            if prop:uv1Get(uvBuf) then
                localUvs = ffi.new('float[?]', vc * 2)
                for j = 0, vc * 2 - 1 do
                    localUvs[j] = uvBuf[j]
                end
            end
        end

        -- Material name: take the first primitive's material if available.
        local matName = meshName
        if prop.primitivesCount and prop.primitivesCount > 0 then
            local prims = ffi.new('gpuPrimitive_t[?]', prop.primitivesCount)
            if prop:primitivesGet(prims) then
                matName = tostring(prims[0].materialId)
            end
        end

        -- Prefix props so the builder groups them separately and they are
        -- visually identifiable.  Keeps the flexmesh naming untouched.
        local primName = 'prop_' .. meshName
        if seenNames[primName] then
            local counter = 2
            local candidate
            repeat
                candidate = primName .. '_' .. counter
                counter = counter + 1
            until not seenNames[candidate]
            primName = candidate
        end
        seenNames[primName] = true

        table.insert(result, {
            name = primName,
            materialName = matName,
            vertexCount = vc,
            indexCount = ic,
            indices = localIdx,
            uvs = localUvs,
            propIndex = i,
            isProp = true,
        })

        log('I', logTag, string.format('  PROP[%d] "%s" -> "%s": vc=%d ic=%d',
            i, meshName, primName, vc, ic))

        ::continueProp::
    end

    log('I', logTag, string.format('=== extractProps DONE: %d props captured ===', #result))
    return result
end


local function captureFrame(meshInfo, metaObjs, veh)
    -- Returns flat binary: per-object positions (in stabilized GPU-pool space,
    -- meta order) followed by the v3 vehicle transform (10 float32:
    -- refnode-world-pos 3 + cluster-quat 4 + centre-of-mass 3).
    -- Built into one contiguous ffi float buffer, then ffi.string'd once.
    --
    -- FAST PATH: no per-frame logging, no per-vertex delta comparison, no
    -- string.format in the hot loop.  Those debug costs (once per frame over
    -- hundreds of frames) were a major source of the capture FPS drop.

    local totalVerts = meshInfo.verticesCount
    local vertices = ffi.new('float[?]', totalVerts * 3)
    if not meshInfo:verticesGet(vertices) then
        log('E', logTag, 'Cannot get vertices')
        return nil
    end

    -- Total output vertex count = sum of per-object vertexCount (meta order).
    local outVerts = 0
    for _, obj in ipairs(metaObjs) do
        outVerts = outVerts + obj.vertexCount
    end

    -- Allocate buffer: positions + 10 floats for vehicle transform (v3).
    local buf = ffi.new('float[?]', outVerts * 3 + 10)
    local w = 0

    -- --- Vehicle world transform (computed ONCE, up front) ---------------
    -- We need the ref-node world position + cluster rotation here (not just at
    -- the end) so rigid props can be STABILIZED into the same pool space as
    -- flexmeshes.  Flexmesh pool verts already have vehicle tumble removed; a
    -- prop's own transform (pm.position/pm.rotation) is full physics world
    -- space WITH tumble.  If we stored props in world space and let the offline
    -- builder re-apply the vehicle rotation (as it does for every object), the
    -- prop tumble would be applied TWICE -> props fly "here and there beneath
    -- the car".  So we remove the tumble here: pool_prop = swapYZ( R_f^-1 @
    -- (world_prop - T_ref) ).  Downstream then treats props identically to
    -- flexmeshes — no special-casing, which is the correct design.
    local Trx, Try, Trz = 0, 0, 0   -- ref-node world position (physics Z-up)
    local qfx, qfy, qfz, qfw = 0, 0, 0, 1  -- cluster rotation
    local Tcx, Tcy, Tcz = 0, 0, 0   -- centre-of-mass world position
    if veh then
        local base = veh:getPosition()
        Tcx, Tcy, Tcz = base.x, base.y, base.z
        Trx, Try, Trz = base.x, base.y, base.z
        local okN, refId = pcall(function() return veh:getRefNodeId() end)
        if okN and refId then
            local okP, rp = pcall(function() return veh:getNodePosition(refId) end)
            if okP and rp then
                Trx, Try, Trz = base.x + rp.x, base.y + rp.y, base.z + rp.z
            end
        end
        local okR, q = pcall(function()
            return veh:getClusterRotationSlow(veh:getRefNodeId())
        end)
        if okR and q then
            local len = math.sqrt(q.x*q.x + q.y*q.y + q.z*q.z + q.w*q.w)
            if len > 1e-4 then
                qfx, qfy, qfz, qfw = q.x/len, q.y/len, q.z/len, q.w/len
            end
        end
    end

    for objIdx, obj in ipairs(metaObjs) do
        local prop = propCache[obj.name]
        if prop then
            -- --- Rigid prop: own vertex buffer + per-frame world transform --
            local vc = obj.vertexCount
            local pm = meshInfo:propmeshes(prop.propIndex)
            local pverts = ffi.new('float[?]', vc * 3)
            -- Prop world transform in PHYSICS space (Z-up), no cyclic shift.
            local px, py, pz = 0, 0, 0
            local pqx, pqy, pqz, pqw = 0, 0, 0, 1
            if pm and pm:verticesGet(pverts) then
                if pm.position then px, py, pz = pm.position.x, pm.position.y, pm.position.z end
                if pm.rotation then pqx, pqy, pqz, pqw = pm.rotation.x, pm.rotation.y, pm.rotation.z, pm.rotation.w end
            else
                log('E', logTag, 'prop read failed: ' .. tostring(obj.name))
            end
            for j = 0, vc - 1 do
                local s = j * 3
                -- 1) local vert -> physics world:  world = R_prop @ v + T_prop
                local wx, wy, wz = transformPropVertex(
                    pverts[s], pverts[s + 1], pverts[s + 2],
                    pqx, pqy, pqz, pqw, px, py, pz)
                -- 2) remove vehicle tumble -> stabilized (Z-up):
                --    stab = R_f^-1 @ (world - T_ref)   (conjugate quat, no trans)
                local sx, sy, sz = transformPropVertex(
                    wx - Trx, wy - Try, wz - Trz,
                    -qfx, -qfy, -qfz, qfw, 0, 0, 0)
                -- 3) Z-up stabilized -> Y-up pool space (swap Y<->Z) so the
                --    offline _pool_to_blender + _place path applies uniformly.
                buf[w]     = sx
                buf[w + 1] = sz
                buf[w + 2] = sy
                w = w + 3
            end
        else
            local cache = flexmeshCache[obj.name]
            if not cache then
                log('E', logTag, 'No cached minIdx for ' .. tostring(obj.name))
                return nil
            end
            local mi = cache.minIdx
            local vc = obj.vertexCount
            for j = 0, vc - 1 do
                local src = (mi + j) * 3
                buf[w]     = vertices[src]
                buf[w + 1] = vertices[src + 1]
                buf[w + 2] = vertices[src + 2]
                w = w + 3
            end
        end
    end

    -- Append the v3 vehicle transform (10 floats), reusing the values already
    -- computed at the top of this function:
    --   [0..2] ref-node world position  = the rotation pivot (matched to R_f)
    --   [3..6] cluster rotation quat    = softbody tumble (pitch+roll+yaw)
    --   [7..9] centre-of-mass world pos = fallback pivot (offline correction)
    buf[w]     = Trx
    buf[w + 1] = Try
    buf[w + 2] = Trz
    buf[w + 3] = qfx
    buf[w + 4] = qfy
    buf[w + 5] = qfz
    buf[w + 6] = qfw
    buf[w + 7] = Tcx
    buf[w + 8] = Tcy
    buf[w + 9] = Tcz

    return ffi.string(buf, (outVerts * 3 + 10) * 4)
end


-- --------------------------------------------------------------------------
-- Capture state machine
-- --------------------------------------------------------------------------

local function requestMesh(veh)
    if currentMeshInfo then
        currentMeshInfo:free()
        currentMeshInfo = nil
    end
    currentMeshInfo = GPUMesh.bng_getGPUMesh(veh:getId())
    state = CAPTURE_WAIT_MESH
end


local function writeFooter()
    if not binFile then return end
    local footerOffset = binFile:seek('end')
    writeU64(binFile, #frameOffsets)
    for _, off in ipairs(frameOffsets) do
        writeU64(binFile, off)
    end
    writeU64(binFile, footerOffset)
    binFile:close()
    binFile = nil
end


local function writeMeta()
    local metaPath = captureDir .. '/capture.meta'
    local f = io.open(metaPath, 'w')
    if f then
        f:write(jsonEncodePretty({
            version = 3,
            vehicleName = 'unknown',
            frameCount = #frameOffsets,
            objects = metaObjects,
        }))
        f:close()
    end
end


-- Finalize the capture: write footer + meta so that whatever frames have been
-- captured so far are a valid, importable file, then reset state.  Called on
-- normal completion AND on any mid-run failure (so a crash still salvages what
-- was recorded instead of leaving a truncated bin with no footer).
local function finishCapture(reason)
    if state == CAPTURE_IDLE then return end
    writeFooter()
    writeMeta()
    writeDiagDump()
    log('I', logTag, string.format('Capture %s: %d frames, %d objects',
        tostring(reason), #frameOffsets, #metaObjects))
    if currentMeshInfo then
        currentMeshInfo:free()
        currentMeshInfo = nil
    end
    state = CAPTURE_IDLE
end


-- Real-time capture: no be:step calls.  The simulation advances naturally
-- every frame (driven by the game engine + BKS onUpdate).  We just grab the
-- GPU mesh whenever the async readback finishes.  The sample rate may not
-- match the sim rate exactly (GPU readback takes ~1-2 frames), but every
-- captured frame has valid positions at that moment in time.


function M.startCapture(dir, frameCount)
    log('I', logTag, '========== START CAPTURE (v7) ==========')
    log('I', logTag, 'Capture dir: ' .. dir .. '  frames: ' .. frameCount)

    -- BeamNG's Lua io is sandboxed to the user folder; absolute paths like
    -- C:/temp are not writable and os.execute('mkdir') is blocked.  Use the
    -- FS API with a path RELATIVE to the user folder.  The file then lands at
    -- <userfolder>/<dir>/capture.bin (e.g. .../BeamNG.drive/current/<dir>/).
    if dir:match('^%a:[/\\]') or dir:sub(1, 1) == '/' then
        log('W', logTag, 'Absolute path given; sandbox may reject it. '
            .. 'Prefer a relative dir like "captures/mycap".')
    end

    captureDir = dir
    maxFrames = frameCount
    currentFrame = 0
    frameOffsets = {}
    metaObjects = {}
    staticBins = {}
    flexmeshCache = {}
    propCache = {}
    bksPlayed = false
    debugTiming = {}

    -- Reset debug state
    dbg_frame0_refs = {}
    dbg_spot_indices = {}
    dbg_total_verts = {}
    dbg_total_indices = {}
    dbg_total_flexmeshes = {}
    dbg_frame_pos_deltas = {}
    dbg_index_overflow = {}
    dbg_vert_pool_size = {}
    dbg_raw_indices_sample = {}

    -- Open debug dump file
    local diagPath = dir .. '/capture_diag.log'
    local diagErr
    diagFile, diagErr = io.open(diagPath, 'w')
    if diagFile then
        diagFile:write('BeamNG Capture Debug Log\n')
        diagFile:write(string.format('Started: %s\n', os.date()))
        diagFile:write(string.format('Frames requested: %d\n', frameCount))
        diagFile:flush()
    else
        log('W', logTag, 'Cannot open diag file: ' .. tostring(diagErr))
    end

    if FS then
        FS:directoryCreate(dir, true)
    end

    local veh = getPlayerVehicle(0)
    if not veh then
        log('E', logTag, 'No player vehicle')
        return false
    end
    vehId = veh:getId()
    log('I', logTag, 'Vehicle ID: ' .. tostring(vehId))

    requestMesh(veh)
    return true
end


-- Called every frame via onUpdate
local function onCaptureUpdate()
    if not currentMeshInfo then
        return  -- wait
    end

    if not currentMeshInfo.dataIsReady then
        return  -- data not yet available from GPU readback
    end

    local veh = getPlayerVehicle(0)
    if not veh then
        log('E', logTag, 'Vehicle gone')
        state = CAPTURE_IDLE
        return
    end

    if currentFrame == 0 then
        -- --- Frame 0: extract static data (indices, UVs, materials) ---
        local flexmeshes = extractFlexmeshes(currentMeshInfo, veh)
        if not flexmeshes or #flexmeshes == 0 then
            log('E', logTag, 'No flexmeshes found — aborting')
            state = CAPTURE_IDLE
            return
        end

        -- Store meta objects and static bytes for writing
        metaObjects = {}
        staticBins = {}
        local totalVerts = 0
        local totalTris = 0
        for _, fm in ipairs(flexmeshes) do
            local name = fm.name
            local idxBin = ffi.string(fm.indices, fm.indexCount * 4)
            local uvBin = ffi.string(fm.uvs or ffi.new('float[?]', fm.vertexCount * 2),
                                     fm.vertexCount * 2 * 4)
            local sbs = fm.indexCount * 4 + fm.vertexCount * 2 * 4
            local tris = math.floor(fm.indexCount / 3)

            table.insert(metaObjects, {
                name = name,
                materialName = fm.materialName,
                vertexCount = fm.vertexCount,
                indexCount = fm.indexCount,
                staticByteSize = sbs,
                flexmeshIndex = fm.flexmeshIndex,
            })
            table.insert(staticBins, {
                indices = idxBin,
                uvs = uvBin,
            })

            -- Cache for per-frame extraction
            flexmeshCache[name] = {
                minIdx = fm.minIdx,
                vertexCount = fm.vertexCount,
            }

            totalVerts = totalVerts + fm.vertexCount
            totalTris = totalTris + tris
        end

        -- --- Rigid props (steering wheel, undertray, etc.) ---
        -- These are a SEPARATE list from flexmeshes (meshInfo:propmeshes) and
        -- are missed by the flexmesh loop above.  Append them as extra objects.
        -- Their flexmeshIndex continues past the flexmesh range so the builder
        -- never groups a prop with a flexmesh.
        local props = extractProps(currentMeshInfo)
        local propFmBase = currentMeshInfo.flexmeshesCount + 1000
        for pi, pm in ipairs(props) do
            local idxBin = ffi.string(pm.indices, pm.indexCount * 4)
            local uvBin = ffi.string(pm.uvs or ffi.new('float[?]', pm.vertexCount * 2),
                                     pm.vertexCount * 2 * 4)
            local sbs = pm.indexCount * 4 + pm.vertexCount * 2 * 4
            local tris = math.floor(pm.indexCount / 3)

            table.insert(metaObjects, {
                name = pm.name,
                materialName = pm.materialName,
                vertexCount = pm.vertexCount,
                indexCount = pm.indexCount,
                staticByteSize = sbs,
                flexmeshIndex = propFmBase + pi,
            })
            table.insert(staticBins, {
                indices = idxBin,
                uvs = uvBin,
            })

            propCache[pm.name] = {
                propIndex = pm.propIndex,
                vertexCount = pm.vertexCount,
            }

            totalVerts = totalVerts + pm.vertexCount
            totalTris = totalTris + tris
        end

        log('I', logTag, string.format(
            '  STATIC DATA: %d objects (%d flexmesh + %d prop), %d total verts, %d total tris',
            #metaObjects, #flexmeshes, #props, totalVerts, totalTris))

        -- Log first 5 and last 5 objects
        local maxLogObjs = math.min(5, #metaObjects)
        log('I', logTag, '  First objects:')
        for k = 1, maxLogObjs do
            local o = metaObjects[k]
            log('I', logTag, string.format(
                '    [%d] %40s  vc=%-6d ic=%-6d mat="%s"',
                k - 1, o.name, o.vertexCount, o.indexCount, o.materialName))
        end
        if #metaObjects > 10 then
            log('I', logTag, '    ...')
            for k = #metaObjects - 4, #metaObjects do
                local o = metaObjects[k]
                log('I', logTag, string.format(
                    '    [%d] %40s  vc=%-6d ic=%-6d mat="%s"',
                    k - 1, o.name, o.vertexCount, o.indexCount, o.materialName))
            end
        end

        -- ================================================================
        -- DEBUG: store frame-0 reference positions for cross-frame check
        -- We need to read the positions at this point.
        -- ================================================================
        local totalGPUMeshVerts = currentMeshInfo.verticesCount
        local frame0_verts = ffi.new('float[?]', totalGPUMeshVerts * 3)
        if currentMeshInfo:verticesGet(frame0_verts) then
            for _, fm in ipairs(flexmeshes) do
                local mi = fm.minIdx
                local vc = fm.vertexCount
                local refPositions = ffi.new('float[?]', vc * 3)
                for j = 0, vc * 3 - 1 do
                    refPositions[j] = frame0_verts[(mi * 3) + j]
                end
                dbg_frame0_refs[fm.name] = {
                    positions = refPositions,
                    minIdx = mi,
                    maxIdx = mi + vc - 1,
                    vc = vc,
                    firstIdx = dbg_raw_indices_sample[fm.name] and dbg_raw_indices_sample[fm.name][0] or nil,
                    lastIdx = nil,
                }
                log('I', logTag, string.format(
                    '  F0REF "%s": minIdx=%d vc=%d firstPos=(%.4f,%.4f,%.4f)',
                    fm.name, mi, vc,
                    refPositions[0], refPositions[1], refPositions[2]))
            end
            log('I', logTag, string.format(
                '  FRAME 0 GPU mesh: %d total verts, %d indices, %d flexmeshes',
                totalGPUMeshVerts, currentMeshInfo.indicesCount, currentMeshInfo.flexmeshesCount))
        end

        -- Frame-0 refs for props (baked into vehicle space, same as captureFrame)
        for name, pc in pairs(propCache) do
            local vc = pc.vertexCount
            local pm = currentMeshInfo:propmeshes(pc.propIndex)
            local pverts = ffi.new('float[?]', vc * 3)
            if pm and pm:verticesGet(pverts) then
                local px, py, pz = 0, 0, 0
                local qx, qy, qz, qw = 0, 0, 0, 1
                if pm.position then px, py, pz = pm.position.y, pm.position.z, pm.position.x end
                if pm.rotation then qx, qy, qz, qw = pm.rotation.y, pm.rotation.z, pm.rotation.x, pm.rotation.w end
                local refPositions = ffi.new('float[?]', vc * 3)
                for j = 0, vc - 1 do
                    local s = j * 3
                    local wx, wy, wz = transformPropVertex(
                        pverts[s], pverts[s + 1], pverts[s + 2],
                        qx, qy, qz, qw, px, py, pz)
                    refPositions[s] = wx
                    refPositions[s + 1] = wy
                    refPositions[s + 2] = wz
                end
                dbg_frame0_refs[name] = {
                    positions = refPositions, minIdx = -1, maxIdx = -1, vc = vc,
                    firstIdx = nil, lastIdx = nil,
                }
                log('I', logTag, string.format(
                    '  F0REF PROP "%s": vc=%d pos=(%.4f,%.4f,%.4f) firstVert=(%.4f,%.4f,%.4f)',
                    name, vc, px, py, pz,
                    refPositions[0], refPositions[1], refPositions[2]))
            end
        end

        -- Write static section to bin file
        local binPath = captureDir .. '/capture.bin'
        local openErr
        binFile, openErr = io.open(binPath, 'wb')
        if not binFile then
            log('E', logTag, 'Cannot open ' .. binPath .. ' : '
                .. tostring(openErr) .. ' (sandbox writes only into the user folder; '
                .. 'use a relative dir such as "captures/mycap")')
            state = CAPTURE_IDLE
            return
        end
        log('I', logTag, '  Written ' .. #staticBins .. ' static objects')
        for _, sb in ipairs(staticBins) do
            binFile:write(sb.indices)
            binFile:write(sb.uvs)
        end

        -- Write meta
        writeMeta()

        -- Capture frame 0 positions (includes vehicle transform)
        local pos = captureFrame(currentMeshInfo, metaObjects, veh)
        if pos then
            table.insert(frameOffsets, binFile:seek('end'))
            binFile:write(pos)
            log('I', logTag, string.format(
                '  Frame 0 positions: %d bytes (%d verts + transform)',
                #pos, totalVerts))
        end

        log('I', logTag, string.format(
            '  Frame 0/%d captured (%d objects)', maxFrames, #metaObjects))

        -- Auto-start BKS Controller animation after frame 0.
        if not bksPlayed then
            local bks = extensions and extensions.telekinesis_main
            if bks and bks.play then
                pcall(bks.play)
                log('I', logTag, 'BKS animation triggered')
            else
                log('W', logTag, 'BKS telekinesis_main not found — animation not started')
            end
            bksPlayed = true
        end

        -- Advance to frame 1 and request next mesh
        currentFrame = 1
        requestMesh(veh)

    elseif currentFrame < maxFrames then
        -- --- Subsequent frames: capture positions + vehicle transform ---
        local t0 = os.clock()
        local pos = captureFrame(currentMeshInfo, metaObjects, veh)
        local elapsed = os.clock() - t0
        if pos then
            table.insert(frameOffsets, binFile:seek('end'))
            binFile:write(pos)
        end

        currentFrame = currentFrame + 1
        if currentFrame % 50 == 0 then
            log('I', logTag, string.format(
                '  Frame %d/%d captured  (%.2f ms)', currentFrame, maxFrames, elapsed * 1000))
        end

        if currentFrame >= maxFrames then
        log('I', logTag, string.format(
            'All %d frames captured. Average: %.2f sec/frame',
            maxFrames, os.clock() / maxFrames))
            finishCapture('complete')
            return
        end

        -- Request next mesh
        requestMesh(veh)
    end
end


-- --------------------------------------------------------------------------
-- Extension lifecycle
-- --------------------------------------------------------------------------

function M.onInit()
    log('I', logTag, 'BeamNG Capture extension loaded  (version 7 — modded, no per-frame debug logging)')
    setExtensionUnloadMode(M, 'manual')
end

function M.onReset()
    log('I', logTag, 'Reset')
    if currentMeshInfo then
        currentMeshInfo:free()
        currentMeshInfo = nil
    end
    state = CAPTURE_IDLE
end

function M.onUpdate()
    if state == CAPTURE_WAIT_MESH then
        onCaptureUpdate()
    end
end


return M
