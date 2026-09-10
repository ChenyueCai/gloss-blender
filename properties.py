from bpy.props import StringProperty, IntProperty,PointerProperty
from bpy.types import PropertyGroup, Panel, Operator
import bpy.utils.previews
import bpy

class GlazeConfig(bpy.types.PropertyGroup):
    """Scene-level configuration paths and server settings for the add-on."""
    server_url: bpy.props.StringProperty(name="Server URL", default="ws://localhost:10017/websocket")
    mesh_folder: bpy.props.StringProperty(name="Mesh Folder", subtype='DIR_PATH', default="")
    single_views_folder: bpy.props.StringProperty(name="Reference Images", subtype='DIR_PATH', default="")
    single_views_texture_folder: bpy.props.StringProperty(name="Reference Textures", subtype='DIR_PATH', default="")
    brushes_folder: bpy.props.StringProperty(name="Brushes Folder", subtype='DIR_PATH', default="")

class GlazeSingleView(bpy.types.PropertyGroup):
    """Selected reference image metadata for the current session."""
    mesh: bpy.props.StringProperty()
    sv_id: bpy.props.IntProperty()
    image_path: bpy.props.StringProperty(name="Image", description="Select an image file", subtype='FILE_PATH')
    
    
class GlazeBrush(bpy.types.PropertyGroup):
    """Brush metadata stored in ``Scene.glaze_brushes``."""
    name: bpy.props.StringProperty()
    mesh: bpy.props.StringProperty()
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
    """Container for a single mesh face index in Blender collections."""
    index: bpy.props.IntProperty(name="Face Index")
    

class InferenceViewSettings(bpy.types.PropertyGroup):
    """Controls for choosing faces or cameras during inference workflows."""
    selection_mode: bpy.props.EnumProperty(
        name="Mode",
        items=[
            ('FACE', "FACE", ""),
            ('CAMERA', "CAMERA", "")
        ],
        default='FACE'
    )


class GlazeSessionState(bpy.types.PropertyGroup):
    """Transient session state for the active paint texture workflow."""
    loaded_paint_texture: bpy.props.StringProperty(
        name="Paint Texture",
        subtype='FILE_PATH',
        default="",
    )

    # Progress mirrors for long-running server work. Written by the router
    # handlers in backend.py; read by ui_panel.py so the panel shows what is
    # happening instead of appearing frozen.
    fill_state: bpy.props.EnumProperty(
        name="Fill State",
        items=[
            ('idle', "Idle", "No fill in flight"),
            ('preparing', "Working", "Server is generating a texture"),
            ('ready', "Ready", "Last fill completed"),
            ('error', "Error", "Last fill failed"),
        ],
        default='idle',
    )
    fill_message: bpy.props.StringProperty(name="Fill Message", default="")
    brush_state: bpy.props.EnumProperty(
        name="Brush State",
        items=[
            ('idle', "Idle", "No brush being created"),
            ('preparing', "Working", "Server is preparing a brush"),
            ('ready', "Ready", "Last brush completed"),
            ('error', "Error", "Last brush failed"),
        ],
        default='idle',
    )
    brush_message: bpy.props.StringProperty(name="Brush Message", default="")
    sync_state: bpy.props.EnumProperty(
        name="Sync State",
        description="Whether the server holds the same texture as Blender",
        items=[
            ('idle', "Idle", "Nothing painted yet"),
            ('syncing', "Syncing", "Pushing the texture to the server"),
            ('synced', "Synced", "Server holds the same texture"),
            ('error', "Error", "Server copy may be stale"),
        ],
        default='idle',
    )
    sync_message: bpy.props.StringProperty(name="Sync Message", default="")
