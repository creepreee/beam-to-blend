import os

import bpy
from bpy.types import Panel, PropertyGroup
from bpy.props import BoolProperty, FloatProperty, IntProperty, StringProperty


def _default_workers() -> int:
    """A safe default worker count: leave 2 cores for Blender/OS, cap at 8."""
    cpu = os.cpu_count() or 1
    return max(1, min(8, cpu - 2))


print("[BeamNG] add-on loading...")


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
    start_second: FloatProperty(
        name="Start at Second",
        description="Animation starts at this Blender timeline second. "
                    "Cache frame 0 maps to this time on the timeline.",
        default=0.0,
        min=0.0,
        soft_max=60.0,
        step=10,
        precision=2,
    )
    playback_fps: IntProperty(
        name="Playback Speed (src fps)",
        description="Animation SPEED: how many CAPTURED (source) frames advance "
                    "per real second. This is the '15 that felt right' clock. "
                    "The capture is recorded at 60 fps (realtime); lower values "
                    "give slow motion (24 = 2.5x slow-mo, 15 = 4x slow-mo). "
                    "This is INDEPENDENT of Output FPS, so the render plays at "
                    "exactly the speed you tuned in the viewport.",
        default=24,
        min=1,
        max=240,
        soft_max=60,
    )
    output_fps: IntProperty(
        name="Output FPS (smoothness)",
        description="Scene render frame rate (scene.render.fps) — how SMOOTH the "
                    "playback/render is, NOT how fast. Higher = smoother motion "
                    "with in-between Blender frames; the animation still lasts "
                    "the same wall-clock time set by Playback Speed. Set to 60 "
                    "for smooth 60fps renders that play at the tuned speed.",
        default=60,
        min=1,
        max=240,
        soft_max=120,
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
        box.prop(props, "start_second")
        box.prop(props, "playback_fps")
        box.prop(props, "output_fps")

        col = layout.column(align=True)
        col.operator("beamng.build_cache", text="1. Build Cache", icon="EXPORT")
        col.operator("beamng.import_cache", text="2. Import Cache", icon="IMPORT")

        col.separator()
        col.prop(props, "vehicle_dir")
        col.operator("beamng.assign_textures", text="3. Assign Textures", icon="TEXTURE")

        col.separator()
        col.operator("beamng.export_alembic", text="4. Export to Alembic", icon="EXPORT")


_classes = [
    BeamNGSceneProperties,
    BEAMNG_PT_main,
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
