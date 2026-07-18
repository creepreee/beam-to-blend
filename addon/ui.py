import os

import bpy
from bpy.types import Panel, PropertyGroup
from bpy.props import BoolProperty, IntProperty, StringProperty


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

        col = layout.column(align=True)
        col.operator("beamng.scan_sequence", text="1. Scan Sequence", icon="FILE_REFRESH")
        col.operator("beamng.build_cache", text="2. Build Cache", icon="EXPORT")
        col.operator("beamng.import_cache", text="3. Import Cache", icon="IMPORT")

        col.separator()
        col.prop(props, "vehicle_dir")
        col.operator("beamng.assign_textures", text="5. Assign Textures", icon="TEXTURE")

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
