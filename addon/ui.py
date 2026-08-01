import os

import bpy
from bpy.types import Panel, PropertyGroup
from bpy.props import BoolProperty, FloatProperty, IntProperty, StringProperty


def _default_workers() -> int:
    """A safe default worker count: leave 2 cores for Blender/OS, cap at 8."""
    cpu = os.cpu_count() or 1
    return max(1, min(8, cpu - 2))


print("[BeamNG] add-on loading...")


def _on_start_frame_update(self, context):
    """Slide the whole animation along the timeline on the LIVE handler.

    The cache-frame mapping is pure arithmetic on the start offset, so this
    needs no re-import — the handler re-derives frame_start/frame_end and
    re-runs the current frame.
    """
    try:
        from runtime import frame_handler
        frame_handler.update_start_frame(int(self.start_frame))
    except Exception:
        pass


def _on_playback_fps_update(self, context):
    """Push the new playback speed to the live handler (no re-import needed)."""
    try:
        from runtime import frame_handler
        frame_handler.update_fps(playback_fps=int(self.playback_fps))
    except Exception:
        pass


def _on_output_fps_update(self, context):
    """Push the new output/render fps to the live handler (no re-import needed)."""
    try:
        from runtime import frame_handler
        frame_handler.update_fps(output_fps=int(self.output_fps))
    except Exception:
        pass


def _on_tyre_update(self, context):
    """Push tyre ground-contact settings to the live handler and redraw.

    Fires on every slider drag so the flattening is tunable interactively — the
    handler re-runs the current frame, so there is no need to scrub or re-import.
    """
    try:
        from runtime import frame_handler
        frame_handler.update_tyre(
            amount=self.tyre_flatten,
            extra=self.tyre_deflection,
            bulge=self.tyre_bulge,
            release=self.tyre_release,
            ground_z=self.tyre_ground_z,
            names=self.tyre_names,
        )
    except Exception:
        pass


class BeamNGSceneProperties(PropertyGroup):
    sequence_dir: StringProperty(
        name="Sequence Folder",
        description="Folder containing the .glb frame files",
        subtype="DIR_PATH",
        default="",
    )
    cache_path: StringProperty(
        name="Cache File",
        description="Path to the .bvc cache file",
        subtype="FILE_PATH",
        default="",
    )
    use_chunked: BoolProperty(
        name="Chunked Playback (faster)",
        description="Merge small parts into groups for fewer GPU uploads. "
                    "Source objects stay available in a hidden collection.",
        default=False,
    )
    weld_cache: BoolProperty(
        name="Weld duplicate vertices",
        description="Collapse coincident (seam-split) vertices when building the "
                    "cache. Smaller cache + lets Shade Smooth / Weighted Normal work. "
                    "The build verifies per frame that welded vertices never "
                    "separate, and aborts if any do.",
        default=False,
    )
    vehicle_dir: StringProperty(
        name="Vehicle Folder",
        description="Folder containing extracted BeamNG vehicle files "
                    "(with .materials.json and textures). "
                    "Typically the vehicle's root folder (e.g. flanje_e180).",
        subtype="DIR_PATH",
        default="",
    )
    use_game_textures: BoolProperty(
        name="Include Base Game Textures",
        description="Also resolve materials from the game's content/vehicles "
                    "zips. Mods inherit shared materials (tyres, brake discs, "
                    "mirrors, licence plates) from the base game, so without "
                    "this those parts import untextured",
        default=True,
    )
    game_dir: StringProperty(
        name="Game Folder",
        description="BeamNG.drive install folder (the one containing "
                    "content\\vehicles). Leave empty to auto-detect",
        subtype="DIR_PATH",
        default="",
    )
    workers: IntProperty(
        name="Parallel Workers",
        description="Number of worker processes for scanning and cache building. "
                    "Frames are read in parallel across CPU cores (each frame is "
                    "independent), giving a large speedup on long sequences. "
                    "1 = sequential. Falls back to sequential automatically if the "
                    "process pool can't start.",
        default=_default_workers(),
        min=1,
        max=64,
        soft_max=32,
    )
    start_frame: IntProperty(
        name="Start at Frame",
        description="Animation starts at this Blender timeline FRAME — cache "
                    "frame 0 lands on this frame number, the same number the "
                    "timeline shows. LIVE: drag it after importing and the "
                    "whole animation slides, no re-import needed. Note this is "
                    "a frame, not a second, so it does not follow Output FPS — "
                    "set Output FPS first, then pick the start frame.",
        default=0,
        min=0,
        soft_max=2000,
        update=_on_start_frame_update,
    )
    playback_fps: IntProperty(
        name="Playback Speed (src fps)",
        description="Animation SPEED: how many CAPTURED (source) frames advance "
                    "per real second. This is the '15 that felt right' clock. "
                    "The capture is recorded at 60 fps (realtime); lower values "
                    "give slow motion (24 = 2.5x slow-mo, 15 = 4x slow-mo). "
                    "This is INDEPENDENT of Output FPS, so the render plays at "
                    "exactly the speed you tuned in the viewport. LIVE: drag it "
                    "after importing and the speed changes in place, no "
                    "re-import (and no re-linking materials) needed.",
        default=24,
        min=1,
        max=240,
        soft_max=60,
        update=_on_playback_fps_update,
    )
    output_fps: IntProperty(
        name="Output FPS (smoothness)",
        description="Scene render frame rate (scene.render.fps) — how SMOOTH the "
                    "playback/render is, NOT how fast. Higher = smoother motion "
                    "with in-between Blender frames; the animation still lasts "
                    "the same wall-clock time set by Playback Speed. Set to 60 "
                    "for smooth 60fps renders that play at the tuned speed. "
                    "LIVE: takes effect immediately, no re-import needed.",
        default=60,
        min=1,
        max=240,
        soft_max=120,
        update=_on_output_fps_update,
    )

    # --- tyre ground-contact deformation -------------------------------
    # BeamNG's tyre mesh is rigid, so a loaded tyre never shows a contact
    # patch — it just sinks into the ground.  These fake the rubber squash at
    # playback time, entirely from the cached geometry's distance to the ground
    # plane, so a tyre lifted off the ground goes perfectly round again.
    tyre_flatten: FloatProperty(
        name="Tyre Flatten",
        description="Strength of fake tyre ground-contact flattening. "
                    "0 = off (rigid BeamNG tyres, zero cost), 1 = tyre sits "
                    "flat on the ground plane. The contact patch grows and "
                    "shrinks with the real physics load, and returns to fully "
                    "round when the wheel leaves the ground",
        default=0.0,
        min=0.0,
        max=1.0,
        step=2,
        precision=2,
        subtype="FACTOR",
        update=_on_tyre_update,
    )
    tyre_deflection: FloatProperty(
        name="Static Deflection (m)",
        description="Extra squash of the lower carcass while in contact, in "
                    "metres. This is what gives a resting tyre a visible patch "
                    "even when the hub has not sunk into the ground. Weighted "
                    "by height below the axle, so the tread flattens and the "
                    "bead near the rim stays put",
        default=0.02,
        min=0.0,
        max=0.2,
        soft_max=0.06,
        step=1,
        precision=3,
        update=_on_tyre_update,
    )
    tyre_bulge: FloatProperty(
        name="Sidewall Bulge",
        description="How much of the squashed rubber bulges out sideways along "
                    "the wheel's axle, as a fraction of the squash depth. "
                    "0 = no bulge (pure flattening)",
        default=0.6,
        min=0.0,
        max=2.0,
        soft_max=1.0,
        step=5,
        precision=2,
        update=_on_tyre_update,
    )
    tyre_release: FloatProperty(
        name="Lift-off Release (m)",
        description="Height above the ground at which flattening reaches zero. "
                    "The effect ramps off over this distance, so a wheel that "
                    "leaves the ground (car lifted, jump, rollover) smoothly "
                    "recovers its round shape instead of holding a flat spot",
        default=0.03,
        min=0.001,
        max=0.5,
        soft_max=0.1,
        step=1,
        precision=3,
        update=_on_tyre_update,
    )
    tyre_ground_z: FloatProperty(
        name="Ground Z",
        description="World Z height of the ground plane the tyres flatten "
                    "against. Leave at 0 when using the importer's auto-ground",
        default=0.0,
        min=-1000.0,
        max=1000.0,
        step=1,
        precision=3,
        update=_on_tyre_update,
    )
    tyre_names: StringProperty(
        name="Tyre Name Match",
        description="Comma-separated substrings identifying tyre objects "
                    "(case-insensitive). Only matching objects are deformed, so "
                    "rims, hubs and brakes stay rigid",
        default="tire,tyre",
        update=_on_tyre_update,
    )


class BEAMNG_PT_main(Panel):
    bl_label = "BeamNG"
    bl_idname = "BEAMNG_PT_main"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "BeamNG"

    @classmethod
    def poll(cls, context):
        return True

    def draw(self, context):
        props = context.scene.beamng
        layout = self.layout

        layout.label(text="BeamNG Cache Importer")

        box = layout.box()
        box.prop(props, "sequence_dir")
        box.prop(props, "cache_path")
        box.prop(props, "use_chunked")
        box.prop(props, "weld_cache")
        box.prop(props, "workers")
        box.prop(props, "start_frame")
        box.prop(props, "playback_fps")
        box.prop(props, "output_fps")
        # These three retune the imported cache in place (see
        # runtime.frame_handler.update_start_frame / update_fps), so say so —
        # otherwise the natural assumption is that they need a re-import.
        note = box.column(align=True)
        note.scale_y = 0.7
        note.label(text="Timing is live — no re-import needed.", icon="INFO")

        col = layout.column(align=True)
        col.operator("beamng.build_cache", text="1. Build Cache", icon="EXPORT")
        col.operator("beamng.import_cache", text="2. Import Cache", icon="IMPORT")

        col.separator()
        col.prop(props, "vehicle_dir")
        col.prop(props, "use_game_textures")
        sub = col.column(align=True)
        sub.enabled = props.use_game_textures
        sub.prop(props, "game_dir")
        col.operator("beamng.assign_textures", text="3. Assign Textures", icon="TEXTURE")

        col.separator()
        col.operator("beamng.export_alembic", text="4. Export to Alembic", icon="EXPORT")


class BEAMNG_PT_tyres(Panel):
    """Fake tyre ground-contact deformation (BeamNG tyre meshes are rigid)."""
    bl_label = "Tyre Contact"
    bl_idname = "BEAMNG_PT_tyres"
    bl_parent_id = "BEAMNG_PT_main"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "BeamNG"
    bl_options = {"DEFAULT_CLOSED"}

    def draw_header(self, context):
        # Mirror the master strength in the header so it reads as on/off at a
        # glance even when the panel is collapsed.
        props = context.scene.beamng
        self.layout.label(
            text="", icon="CHECKBOX_HLT" if props.tyre_flatten > 0.0
            else "CHECKBOX_DEHLT")

    def draw(self, context):
        props = context.scene.beamng
        layout = self.layout
        layout.use_property_split = True

        layout.prop(props, "tyre_flatten")

        col = layout.column()
        col.active = props.tyre_flatten > 0.0
        col.prop(props, "tyre_deflection")
        col.prop(props, "tyre_bulge")
        col.prop(props, "tyre_release")
        col.prop(props, "tyre_ground_z")
        col.prop(props, "tyre_names")

        if props.tyre_flatten > 0.0:
            note = layout.column(align=True)
            note.scale_y = 0.7
            note.label(text="Live — updates as you drag.", icon="INFO")


_classes = [
    BeamNGSceneProperties,
    BEAMNG_PT_main,
    BEAMNG_PT_tyres,
]


def register():
    print("[BeamNG] registering panel and properties...")
    for cls in _classes:
        bpy.utils.register_class(cls)
        print(f"[BeamNG] registered {cls.__name__}")
    bpy.types.Scene.beamng = bpy.props.PointerProperty(type=BeamNGSceneProperties)
    print("[BeamNG] Scene.beamng property set")


def unregister():
    print("[BeamNG] unregistering...")
    del bpy.types.Scene.beamng
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
    print("[BeamNG] done")
