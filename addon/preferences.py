import bpy
from bpy.types import AddonPreferences
from bpy.props import StringProperty


class BeamNGCacheImporterPreferences(AddonPreferences):
    bl_idname = __package__ or __name__

    cache_dir: StringProperty(
        name="Cache Directory",
        subtype="DIR_PATH",
        default="",
    )

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "cache_dir")


def register():
    bpy.utils.register_class(BeamNGCacheImporterPreferences)


def unregister():
    bpy.utils.unregister_class(BeamNGCacheImporterPreferences)
