import hashlib
import json
import os
import sys
import tempfile

import bpy
from bpy.types import Operator
from bpy.props import StringProperty

# Path setup for the bundled `importer`/`runtime` packages is done in the
# add-on package __init__.py (works for both dev and packaged layouts).


def _manifest_stash(directory: str) -> str:
    """Deterministic temp-file path so scan writes it and build reads it."""
    h = hashlib.md5(os.path.abspath(directory).encode()).hexdigest()[:12]
    return os.path.join(tempfile.gettempdir(), f"beamng_manifest_{h}.json")


def _tyre_settings(context):
    """Build a TyreSettings from the scene's tyre UI properties."""
    from runtime.tyre_deform import TyreSettings

    props = context.scene.beamng
    return TyreSettings(
        amount=float(getattr(props, "tyre_flatten", 0.0)),
        extra=float(getattr(props, "tyre_deflection", 0.02)),
        bulge=float(getattr(props, "tyre_bulge", 0.6)),
        release=float(getattr(props, "tyre_release", 0.03)),
        ground_z=float(getattr(props, "tyre_ground_z", 0.0)),
    ).update(names=getattr(props, "tyre_names", "tire,tyre"))


def _iter_layer_collections(layer_coll):
    """Yield a LayerCollection and all of its descendants."""
    yield layer_coll
    for child in layer_coll.children:
        yield from _iter_layer_collections(child)


def _reveal_for_export(bpy, object_names):
    """Temporarily reveal collections/objects so Alembic export can see them.

    Objects in a viewport-hidden or excluded collection are absent from the
    view-layer depsgraph, so their MESH_CACHE modifiers never evaluate and the
    Alembic export is static.  This reveals every layer-collection plus each
    named object, and returns a token used by :func:`_restore_after_export`
    to put everything back exactly as it was.
    """
    saved_colls = []
    saved_data_colls = []
    saved_objs = []

    view_layer = bpy.context.view_layer
    for lc in _iter_layer_collections(view_layer.layer_collection):
        saved_colls.append((lc, lc.exclude, lc.hide_viewport))
        if lc.exclude:
            lc.exclude = False
        if lc.hide_viewport:
            lc.hide_viewport = False

    # Data-level collection visibility flags (separate from LayerCollection).
    for coll in bpy.data.collections:
        saved_data_colls.append((coll, coll.hide_viewport, coll.hide_render))
        if coll.hide_viewport:
            coll.hide_viewport = False
        if coll.hide_render:
            coll.hide_render = False

    for name in object_names:
        obj = bpy.data.objects.get(name)
        if obj is None:
            continue
        saved_objs.append((obj, obj.hide_viewport, obj.hide_render, obj.hide_get()))
        obj.hide_viewport = False
        obj.hide_render = False
        obj.hide_set(False)

    bpy.context.view_layer.update()
    return (saved_colls, saved_data_colls, saved_objs)


def _restore_after_export(bpy, token):
    """Undo the visibility changes made by :func:`_reveal_for_export`."""
    saved_colls, saved_data_colls, saved_objs = token
    for obj, hv, hr, hg in saved_objs:
        try:
            obj.hide_viewport = hv
            obj.hide_render = hr
            obj.hide_set(hg)
        except (ReferenceError, RuntimeError):
            pass
    for coll, hv, hr in saved_data_colls:
        try:
            coll.hide_viewport = hv
            coll.hide_render = hr
        except (ReferenceError, RuntimeError):
            pass
    for lc, excl, hv in saved_colls:
        try:
            lc.exclude = excl
            lc.hide_viewport = hv
        except (ReferenceError, RuntimeError):
            pass
    bpy.context.view_layer.update()


class BEAMNG_OT_scan_sequence(Operator):
    bl_idname = "beamng.scan_sequence"
    bl_label = "Scan BeamNG Sequence"
    bl_options = {"REGISTER"}

    def execute(self, context):
        directory = context.scene.beamng.sequence_dir
        if not directory:
            self.report({"ERROR"}, "Set the sequence folder first")
            return {"CANCELLED"}
        from importer.scanner import SequenceScanner

        workers = int(getattr(context.scene.beamng, "workers", 1))
        try:
            manifest = SequenceScanner(directory).scan(workers=workers)
        except Exception as exc:
            self.report({"ERROR"}, f"Scan failed: {exc}")
            return {"CANCELLED"}

        path = _manifest_stash(directory)
        with open(path, "w") as f:
            f.write(manifest.to_json())
        sys.stderr.write(f"[BeamNG] manifest saved ({manifest.frame_count} frames)\n")
        sys.stderr.flush()

        self.report(
            {"INFO"},
            f"{manifest.frame_count} frames, "
            f"{len(manifest.stable_objects)} stable, "
            f"{len(manifest.dynamic_objects)} dynamic",
        )
        return {"FINISHED"}


class BEAMNG_OT_build_cache(Operator):
    bl_idname = "beamng.build_cache"
    bl_label = "Build BeamNG Cache"
    bl_options = {"REGISTER"}

    def execute(self, context):
        import traceback
        sys.stderr.write("[BeamNG] BUILD CACHE\n")
        sys.stderr.flush()

        directory = context.scene.beamng.sequence_dir.strip().rstrip("\\/")
        if not directory:
            self.report({"ERROR"}, "Set the sequence folder first")
            return {"CANCELLED"}

        name = os.path.basename(os.path.normpath(directory)) or "sequence"
        out = os.path.join(directory, f"{name}.bvc")
        # Normalize to avoid trailing-space / encoding issues on Windows.
        out = os.path.abspath(out)
        context.scene.beamng.cache_path = out

        from importer.scanner import SequenceManifest, SequenceScanner
        from importer.cache_builder import CacheBuilder

        weld = bool(context.scene.beamng.weld_cache)

        # --- BMC pipeline routing ------------------------------------------
        # If the folder holds a .bmc capture (not a .glb sequence), build via
        # build_from_capture so the cross-frame-safe weld actually runs.  The
        # GLB path (below) never reaches build_from_capture, so without this a
        # BMC user's "Weld duplicate vertices" checkbox is a no-op.
        bmc_files = [f for f in os.listdir(directory) if f.lower().endswith(".bmc")]
        if bmc_files and not any(
            f.lower().endswith(".glb") for f in os.listdir(directory)
        ):
            bmc_path = os.path.join(directory, sorted(bmc_files)[0])
            sys.stderr.write(
                f"[BeamNG] BMC capture found ({bmc_files[0]}), "
                f"building via build_from_capture (weld={weld})\n"
            )
            sys.stderr.flush()
            try:
                manifest = CacheBuilder(directory, out).build_from_capture(
                    bmc_path, weld=weld)
            except Exception as exc:
                details = traceback.format_exc()
                sys.stderr.write(f"[BeamNG] BUILD CACHE ERROR:\n{details}\n")
                sys.stderr.flush()
                self.report({"ERROR"}, f"Cache build failed: {exc}")
                return {"CANCELLED"}
            sys.stderr.write("[BeamNG] BUILD CACHE DONE\n")
            sys.stderr.flush()
            self.report(
                {"INFO"},
                f"Built BMC cache: {out} "
                f"({manifest.frame_count} frames, "
                f"{len(manifest.stable_objects)} objects)",
            )
            return {"FINISHED"}

        # Try loading stashed manifest; fall back to scan.
        manifest = None
        stash = _manifest_stash(directory)
        if os.path.exists(stash):
            try:
                with open(stash) as f:
                    manifest = SequenceManifest.from_json(f.read())
                sys.stderr.write(f"[BeamNG] using stashed manifest ({manifest.frame_count} frames)\n")
                sys.stderr.flush()
            except Exception as exc:
                sys.stderr.write(f"[BeamNG] stash broken ({exc}), re-scanning...\n")
                sys.stderr.flush()

        workers = int(getattr(context.scene.beamng, "workers", 1))
        if manifest is None:
            sys.stderr.write("[BeamNG] no manifest found, scanning...\n")
            sys.stderr.flush()
            manifest = SequenceScanner(directory).scan(workers=workers)

        try:
            manifest = CacheBuilder(directory, out).build(
                manifest=manifest, weld=weld, workers=workers)
        except Exception as exc:
            details = traceback.format_exc()
            sys.stderr.write(f"[BeamNG] BUILD CACHE ERROR:\n{details}\n")
            sys.stderr.flush()
            self.report({"ERROR"}, f"Cache build failed: {exc}")
            return {"CANCELLED"}

        sys.stderr.write("[BeamNG] BUILD CACHE DONE\n")
        sys.stderr.flush()
        self.report(
            {"INFO"},
            f"Built cache: {out} ({manifest.frame_count} frames)",
        )
        return {"FINISHED"}


class BEAMNG_OT_import_cache(Operator):
    bl_idname = "beamng.import_cache"
    bl_label = "Import BeamNG Cache"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        cache_path = context.scene.beamng.cache_path
        if not cache_path or not os.path.exists(cache_path):
            self.report({"ERROR"}, "Cache file not found — build it first, or set the path")
            return {"CANCELLED"}

        from runtime.cache_reader import CacheReader
        from runtime.mesh_update import CachePlayback, CHUNK_MAP_E180
        from runtime import frame_handler

        try:
            reader = CacheReader(cache_path)
            log_path = os.path.splitext(cache_path)[0] + "_debug.log"
            chunk_map = CHUNK_MAP_E180 if context.scene.beamng.use_chunked else None
            playback = CachePlayback(reader, log_path=log_path, chunk_map=chunk_map,
                                     tyre=_tyre_settings(context))
            playback.build_scene()

            # Two INDEPENDENT knobs (see runtime.frame_handler for the mapping):
            #   * playback_fps = animation SPEED (captured/source frames per real
            #     second — the "15 that felt right").
            #   * output_fps   = scene.render.fps (render/viewport smoothness).
            # The handler maps cache frames by TIME, so the sequence lasts
            # frame_count/playback_fps seconds regardless of output_fps, and the
            # viewport preview matches the final render exactly (no more "60fps
            # render is 4x too fast").
            playback_fps = max(1, int(getattr(context.scene.beamng, "playback_fps", 24)))
            output_fps = max(1, int(getattr(context.scene.beamng, "output_fps", 60)))
            context.scene.render.fps = output_fps
            context.scene.render.fps_base = 1.0
            # Pass the offset in FRAMES — that is what the "Start at Frame"
            # field means and what frame_handler stores, so the slider can
            # retune it live (frame_handler.update_start_frame) with no
            # re-import and no seconds↔frames round-trip.
            start_frame = int(getattr(context.scene.beamng, "start_frame", 0))
            frame_handler.attach(
                playback,
                frame_start=start_frame,
                playback_fps=playback_fps,
                output_fps=output_fps,
            )
            playback.close_log()

            if chunk_map:
                n_chunks = len(chunk_map)
                n_source = len(reader.stable_objects())
                sys.stderr.write(
                    f"[BeamNG] chunked mode: {n_source} source → {n_chunks} chunks\n"
                )
                sys.stderr.flush()

            # --- auto-ground: shift entire car so it sits on Z=0 ---
            coll = bpy.data.collections.get("BeamNG Cache")
            if coll:
                import numpy as np
                min_z = float("inf")
                for obj in coll.objects:
                    if obj.type == "MESH" and obj.data.vertices:
                        pts = np.empty(len(obj.data.vertices) * 3, dtype=np.float32)
                        obj.data.vertices.foreach_get("co", pts)
                        min_z = min(min_z, float(pts[2::3].min()))
                if min_z < float("inf"):
                    dz = -min_z
                    for obj in coll.objects:
                        if obj.type == "MESH":
                            obj.location.z += dz
                    sys.stderr.write(f"[BeamNG] auto-ground: shifted up {dz:.4f} Z\n")
                    sys.stderr.flush()
                    # Remember the shift: it lives in obj.location, outside the
                    # cache data, so the Alembic bake has to be told about it to
                    # measure tyre-to-ground distance the same way playback does.
                    context.scene["_beamng_ground_shift"] = float(dz)
                    # Auto-ground runs AFTER build_scene's set_frame(0), so the
                    # frame-0 tyre deform measured heights before this shift.
                    # Redo frame 0 now that the objects sit on Z=0.
                    if playback.tyre.enabled:
                        playback.set_tyre_settings(playback.tyre)
                        playback.set_frame(0)

        except Exception as exc:
            self.report({"ERROR"}, f"Import failed: {exc}")
            return {"CANCELLED"}

        self.report(
            {"INFO"},
            f"Imported {len(reader.stable_objects())} objects, "
            f"{reader.frame_count} frames",
        )
        return {"FINISHED"}


class BEAMNG_OT_export_alembic(Operator):
    """Bake cache to MDD, attach MESH_CACHE modifiers, export Alembic.

    Blender's multi-frame Alembic exporter evaluates the depsgraph once and
    reuses it — Python frame handlers don't fire for frames 2..N.  This
    operator uses the proven **bake-to-MDD approach**::

        for each stable object:
            write per-frame positions as .mdd (point cache)
        attach MESH_CACHE modifier to each object
        bpy.ops.wm.alembic_export(start=..., end=...)  # single call
        remove MESH_CACHE modifiers, delete temp .mdd files

    The MESH_CACHE modifier lives in the depsgraph, so Alembic export
    evaluates it on every frame natively — no Python handler dependency.
    """
    bl_idname = "beamng.export_alembic"
    bl_label = "Export BeamNG Cache to Alembic"
    bl_options = {"REGISTER", "UNDO"}

    filter_glob: StringProperty(default="*.abc", options={"HIDDEN"})

    filepath: StringProperty(
        name="Alembic File",
        description="Output .abc path",
        subtype="FILE_PATH",
        default="",
    )

    def invoke(self, context, event):
        cache_path = context.scene.beamng.cache_path
        if cache_path:
            self.filepath = os.path.splitext(cache_path)[0] + ".abc"
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        import shutil
        import traceback

        filepath = self.filepath
        if not filepath:
            self.report({"ERROR"}, "Output path not set")
            return {"CANCELLED"}
        if not filepath.endswith(".abc"):
            filepath += ".abc"

        cache_path = context.scene.beamng.cache_path
        if not cache_path or not os.path.exists(cache_path):
            self.report({"ERROR"}, "Cache file not found")
            return {"CANCELLED"}

        from runtime.cache_reader import CacheReader
        from runtime.baker import bake_to_mdd, apply_mdd_modifiers
        from runtime import frame_handler

        reader = None
        mdd_dir = None
        baked_names = []

        try:
            reader = CacheReader(cache_path)
            n_frames = reader.frame_count

            scene = context.scene
            start = scene.frame_start
            end = start + n_frames - 1

            # --- detach Python frame handler if active ---
            frame_handler.detach()

            # --- bake stable objects to .mdd in temp dir ---
            mdd_dir = tempfile.mkdtemp(prefix="beamng_mdd_")
            # Bake the SAME tyre deformation the viewport shows, including the
            # auto-ground shift that the import operator put in obj.location —
            # otherwise the export would come out with round tyres.
            baked_names = bake_to_mdd(
                reader, mdd_dir,
                frame_start=0, frame_end=n_frames - 1,
                tyre=_tyre_settings(context),
                height_bias=float(scene.get("_beamng_ground_shift", 0.0)),
            )
            if not baked_names:
                self.report({"ERROR"}, "No stable objects to bake — nothing to export")
                return {"CANCELLED"}

            abs_mdd = os.path.abspath(mdd_dir)
            sys.stderr.write(
                "[BeamNG]  baked {0} objects, {1} frames -> {2}\n".format(
                    len(baked_names), n_frames, abs_mdd))
            sys.stderr.flush()

            # --- attach MESH_CACHE modifiers (absolute path — works without saved .blend) ---
            abs_mdd = os.path.abspath(mdd_dir)
            if not abs_mdd.endswith(os.sep):
                abs_mdd += os.sep
            apply_mdd_modifiers(bpy, baked_names, abs_mdd, start)

            # --- make the MESH_CACHE-carrying objects exportable ---
            # In chunked mode the source objects (which hold the MESH_CACHE
            # modifiers) live in a collection with hide_viewport / exclude set.
            # A viewport-hidden / excluded collection is NOT in the view-layer
            # depsgraph, so the MESH_CACHE modifier never evaluates per-frame
            # and Alembic exports a *static* mesh (or nothing, if unselectable).
            # We temporarily reveal the collections + objects, export, then
            # restore the exact prior visibility state.
            restore = _reveal_for_export(bpy, baked_names)

            # --- select only objects with MESH_CACHE modifiers ---
            for obj in bpy.data.objects:
                obj.select_set(False)
            n_selected = 0
            for name in baked_names:
                obj = bpy.data.objects.get(name)
                if obj and obj.type == 'MESH':
                    obj.select_set(True)
                    if obj.select_get():
                        n_selected += 1
            sys.stderr.write(
                "[BeamNG]  selected {0}/{1} baked objects for export\n".format(
                    n_selected, len(baked_names)))
            sys.stderr.flush()
            if n_selected == 0:
                _restore_after_export(bpy, restore)
                self.report({"ERROR"},
                            "No exportable objects — cache import may have failed")
                return {"CANCELLED"}

            # --- single multi-frame Alembic export ---
            sys.stderr.write("[BeamNG]  exporting Alembic: {0} (frames {1}..{2})\n".format(
                filepath, start, end))
            sys.stderr.flush()

            # face_sets=True is REQUIRED for materials to survive: Alembic
            # carries per-material face assignments as face sets, and the
            # operator default is False — without it a re-imported .abc has
            # ZERO material slots (measured: 235 slots -> 0) and every part
            # renders with the default grey.  uvs/normals default to True but
            # are passed explicitly so a Blender default change can't silently
            # strip them.
            bpy.ops.wm.alembic_export(
                filepath=filepath,
                start=start,
                end=end,
                selected=True,
                flatten=False,
                face_sets=True,
                uvs=True,
                packuv=True,
                normals=True,
            )

            # deselect + restore prior visibility state
            for obj in bpy.data.objects:
                obj.select_set(False)
            _restore_after_export(bpy, restore)

            # --- remove MESH_CACHE modifiers ---
            for name in baked_names:
                obj = bpy.data.objects.get(name)
                if obj and obj.type == 'MESH':
                    for mod in list(obj.modifiers):
                        if mod.type == 'MESH_CACHE':
                            obj.modifiers.remove(mod)

            # --- clean up temp .mdd directory ---
            if mdd_dir and os.path.exists(mdd_dir):
                shutil.rmtree(mdd_dir, ignore_errors=True)

            reader.close()
            reader = None

        except Exception as exc:
            sys.stderr.write("[BeamNG] ABC export error: {0}\n{1}\n".format(
                exc, traceback.format_exc()))
            sys.stderr.flush()
            # best-effort cleanup
            if baked_names:
                for name in baked_names:
                    obj = bpy.data.objects.get(name)
                    if obj and obj.type == 'MESH':
                        for mod in list(obj.modifiers):
                            if mod.type == 'MESH_CACHE':
                                obj.modifiers.remove(mod)
            if mdd_dir and os.path.exists(mdd_dir):
                shutil.rmtree(mdd_dir, ignore_errors=True)
            if reader:
                reader.close()
            self.report({"ERROR"}, "ABC export failed: {0}".format(exc))
            return {"CANCELLED"}

        self.report(
            {"INFO"},
            "Exported {0} frames to {1}".format(n_frames, os.path.basename(filepath)),
        )
        return {"FINISHED"}


class BEAMNG_OT_assign_textures(Operator):
    """Auto-assign BeamNG PBR textures from a vehicle folder to the imported materials.

    Reads ``*.materials.json`` from the selected vehicle folder, resolves texture
    paths (DDS/PNG/JPG), and builds Principled BSDF node trees for every material
    in the scene whose name matches a ``mapTo`` entry.
    """
    bl_idname = "beamng.assign_textures"
    bl_label = "Assign BeamNG Textures"
    bl_description = "Auto-assign PBR textures to all imported car materials"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        vehicle_dir = context.scene.beamng.vehicle_dir
        if not vehicle_dir or not os.path.isdir(vehicle_dir):
            self.report({"ERROR"}, "Set a valid vehicle folder path first")
            return {"CANCELLED"}

        from importer.materials import (
            parse_materials_json,
            parse_material_shader_params,
            build_filename_lookup,
            find_by_filename,
            strip_blender_index,
            _normalize_material_key,
            _alternate_keys,
            build_material_node_tree,
            build_material_from_params,
            build_search_roots,
            find_beamng_install,
        )

        car_prefix = os.path.basename(os.path.normpath(vehicle_dir)).lower()

        # Materials a mod inherits from the base game (tyres, brake discs,
        # mirrors, licence plates) are defined in content/vehicles/*.zip, not in
        # the mod folder — search both.  See materials.build_search_roots.
        props = context.scene.beamng
        game_dir = (props.game_dir or "").strip() or None
        use_game = bool(props.use_game_textures)
        install = find_beamng_install(game_dir) if use_game else None
        if use_game and not install:
            sys.stderr.write(
                "[BeamNG] Could not locate a BeamNG install with "
                "content/vehicles — set 'Game Folder' to fix base-game "
                "materials (tyres, brakes, mirrors)\n"
            )
        roots = build_search_roots(vehicle_dir, game_dir, include_game=use_game)

        # Zip members get extracted here on demand so Blender can load them.
        extract_dir = os.path.join(vehicle_dir, "_beamng_extracted_textures")

        json_map = parse_materials_json(roots)
        params_map = parse_material_shader_params(roots)
        filename_lookup = build_filename_lookup(roots)

        assigned_json = 0
        assigned_file = 0
        assigned_params = 0
        deduped = 0
        skipped = []
        base_tex_cache = {}

        def _lookup_any(key, source_dict):
            """Try *key* then alternate-key forms in *source_dict*."""
            val = source_dict.get(key)
            if val:
                return val
            for ak in _alternate_keys(key):
                if ak != key:
                    val = source_dict.get(ak)
                    if val:
                        return val
            # Forward suffix fallback: "mirror" → try "mirror_001"
            for suf in ("_001", ".001"):
                val = source_dict.get(key + suf)
                if val:
                    return val
            # Strip common suffixes: "etk800_main" → try "etk800"
            for suf in ("_main", "_body", "_base", "_on", "_bake"):
                if key.endswith(suf):
                    val = source_dict.get(key[: -len(suf)])
                    if val:
                        return val
            return None

        for mat in bpy.data.materials:
            raw_name = mat.name
            base_name = strip_blender_index(raw_name)
            base_key = base_name.lower()

            if raw_name != base_name and base_key in base_tex_cache:
                # The cache holds two shapes — texture maps and shader params —
                # which need different builders.  Tagging the kind avoids
                # feeding a params dict to the texture builder (it would try to
                # unpack a float as a (path, is_normal) pair).
                kind, cached = base_tex_cache[base_key]
                if kind == "tex":
                    build_material_node_tree(
                        mat, cached, extract_dir=extract_dir,
                    )
                else:
                    build_material_from_params(mat, cached)
                deduped += 1
                continue

            tex_paths = _lookup_any(base_key, json_map)
            source = "json"

            if not tex_paths:
                tex_paths = find_by_filename(base_key, filename_lookup, car_prefix)
                source = "file"

            if tex_paths:
                build_material_node_tree(mat, tex_paths, extract_dir=extract_dir)
                base_tex_cache[base_key] = ("tex", tex_paths)
                if source == "json":
                    assigned_json += 1
                else:
                    assigned_file += 1
            else:
                shader_params = _lookup_any(base_key, params_map)
                if shader_params:
                    build_material_from_params(mat, shader_params)
                    base_tex_cache[base_key] = ("params", shader_params)
                    assigned_params += 1
                else:
                    skipped.append(raw_name)

        total = assigned_json + assigned_file + assigned_params + deduped
        skipped_list = ", ".join(skipped[:10])
        if len(skipped) > 10:
            skipped_list += f", ... ({len(skipped)} total)"
        msg = (
            f"Done! {total} textured  "
            f"({assigned_json} from JSON, {assigned_file} from filename, "
            f"{assigned_params} from shader params, {deduped} deduped)  "
            f"| {len(skipped)} skipped"
        )
        sys.stderr.write(
            f"[BeamNG] skipped materials: {skipped_list}\n"
        )
        sys.stderr.flush()
        self.report({"INFO"}, msg)
        return {"FINISHED"}


_CLASSES = (
    BEAMNG_OT_scan_sequence,
    BEAMNG_OT_build_cache,
    BEAMNG_OT_import_cache,
    BEAMNG_OT_export_alembic,
    BEAMNG_OT_assign_textures,
)


def register():
    for cls in _CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
