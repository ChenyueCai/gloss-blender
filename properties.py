from bpy.props import StringProperty, IntProperty,PointerProperty
from bpy.types import PropertyGroup, Panel, Operator
import bpy.utils.previews

# global variables 
# all brushes
# ---- Property group to hold selected image ---- #TODO:
import bpy

class GlazeConfig(bpy.types.PropertyGroup):
    mesh_file_path: bpy.props.StringProperty(name="Mesh File Path", subtype='FILE_PATH', default="")
    single_views_folder: bpy.props.StringProperty(name="Single Views Folder", subtype='DIR_PATH', default="")
    single_views_cam_folder: bpy.props.StringProperty(name="Single Views Camera Folder", subtype='DIR_PATH', default="")
    single_views_texture_folder: bpy.props.StringProperty(name="Single Views Texture Folder", subtype='DIR_PATH', default="")
    brushes_folder: bpy.props.StringProperty(name="Brushes Folder", subtype='DIR_PATH', default="")
    preload_mode: bpy.props.EnumProperty(name="Preload Mode", items=[('train', "Train", ""), ('test', "Test", "")], default='test')
    cache_folder: bpy.props.StringProperty(name="Cache Folder", subtype='DIR_PATH', default="")

class GlazeSingleView(bpy.types.PropertyGroup):
    sv_id: bpy.props.IntProperty()
    image_path: bpy.props.StringProperty(name="Image", description="Select an image file", subtype='FILE_PATH')
    
    
class GlazeBrush(bpy.types.PropertyGroup):
    name: bpy.props.StringProperty()
    sv_id: bpy.props.IntProperty()
    brush_type: bpy.props.EnumProperty(
        name="Brush Type",
        description="Type of brush",
        items=[
            ('AutoSampledReferenceBrush', "AutoSampledReferenceBrush", "Automatically sampled reference brush"),
            ('PreSampledReferenceBrush', "PreSampledReferenceBrush", "Pre-sampled reference brush")
        ],
        default='AutoSampledReferenceBrush'
    )


class FaceIndexItem(bpy.types.PropertyGroup):
    index: bpy.props.IntProperty(name="Face Index")
