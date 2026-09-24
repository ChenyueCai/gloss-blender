import bpy
import importlib

bl_info = {
    "name": "gloss-blender",
    "version": (0, 1),
    "blender": (4, 0, 0),
    "location": "View3D > Sidebar > Gloss",
    "description": "Reference-based interactive texture fill",
    "category": "Paint",
}

from . import client
from . import backend
from . import operators
from . import properties
from . import ui_panel
from .utils import io, config, image, mesh

importlib.reload(client)
importlib.reload(backend)
importlib.reload(io)
importlib.reload(mesh)
importlib.reload(operators)
importlib.reload(properties)
importlib.reload(ui_panel)
importlib.reload(config)
importlib.reload(image)


from .client import register_client, unregister_client, ws_client
from .backend import register_backend_handlers, unregister_backend_handlers
from .ui_panel import *
from .operators import *
from .properties import *

_BASE_TYPES = (bpy.types.PropertyGroup, bpy.types.Operator, bpy.types.Panel)

#: Every add-on class to register: the PropertyGroups, Operators and Panels
#: pulled in by the star imports above, excluding Blender's own base classes.
classes = [
    cls for cls in globals().values()
    if isinstance(cls, type) and issubclass(cls, _BASE_TYPES) and cls not in _BASE_TYPES
]

def register():
    """Register the add-on classes, scene properties, overlay, and client."""
    for cls in classes:
        try:
            bpy.utils.register_class(cls)
        except ValueError:
            # Already registered
            print(f"{cls.__name__} already registered, skipping.")
    bpy.types.Scene.gloss_config = bpy.props.PointerProperty(type=GlossConfig)
    bpy.types.Scene.current_paint_mesh = bpy.props.PointerProperty(name="Mesh", type=bpy.types.Object)
    bpy.types.Scene.current_reference_mesh = bpy.props.PointerProperty(name="Mesh", type=bpy.types.Object)
    bpy.types.Scene.current_view = bpy.props.PointerProperty(type=GlossSingleView)  
    bpy.types.Scene.gloss_session = bpy.props.PointerProperty(type=GlossSessionState)
    bpy.types.Scene.current_brush = bpy.props.StringProperty(name="Current Brush Name", default="")
    bpy.types.Scene.gloss_brushes = bpy.props.CollectionProperty(type=GlossBrush)
    bpy.types.Scene.new_brush_name = bpy.props.StringProperty(name="New Brush Name", default="MyBrush")
    bpy.types.Scene.update_texture_4k = bpy.props.BoolProperty(
        name="Paint on 4K Texture",
        description="Toggle between 4K texture or lower resolution",
        default=True,
    )
    bpy.types.Scene.clip_fill_to_faces = bpy.props.BoolProperty(
        name="Clip to Faces",
        description="If true, will clip texture fill to faces, if provided",
        default=True)
    bpy.types.Scene.syncmvd = bpy.props.BoolProperty(
        name="SyncMVD",
        description="If true, use multi-view-diffusion synchronization on the server side",
        default=False)
    bpy.types.Scene.soft_add = bpy.props.BoolProperty(
        name="Soft Add",
        description="If true, updates are soft-added",
        default=True)
    bpy.types.Scene.dilate = bpy.props.BoolProperty(
        name="Dilate",
        description="If true, will dilate on the server side",
        default=True)
    bpy.types.Scene.cam_dist = bpy.props.FloatProperty(
        name="Dist",
        description="How far should local cameras be placed for filling",
        default=0.75,
        min=0.1,
        max=1.0,
        subtype='FACTOR'  # Subtype can change how it's displayed, e.g., as a percentage or distance
    )
    bpy.types.Scene.brush_cam_dist = bpy.props.FloatProperty(
        name="Brush Dist",
        description="Camera distance baked into a new brush at creation time",
        default=0.75,
        min=0.1,
        max=1.0,
        subtype='FACTOR'
    )
    bpy.types.Scene.max_cameras = bpy.props.IntProperty(
        name="Max Cam",
        description="Max fill patches to use",
        default=3,
        min=1,  # Hard minimum value
        max=200  # Hard maximum value
    )
    bpy.types.Scene.cam_fov = bpy.props.FloatProperty(
        name="FOV",
        description="Fill camera field of view in radians",
        default=0.4,
        min=0.1,
        max=3.0,
    )
    bpy.types.Scene.brush_cam_fov = bpy.props.FloatProperty(
        name="Brush FOV",
        description="Camera field of view in radians baked into a new brush at creation time",
        default=0.4,
        min=0.1,
        max=3.0,
    )
    register_client()
    register_backend_handlers()
    config.register_config_handlers()
    # Scenes have no folders until a config is loaded. Give the current one
    # the shipped data/config.yaml once the UI is up; load_post covers files
    # opened later.
    bpy.app.timers.register(_apply_default_config_once, first_interval=0)


def _apply_default_config_once():
    """Timer body: load the shipped config into unconfigured scenes, once."""
    try:
        if config.apply_default_config():
            url = bpy.context.scene.gloss_config.server_url
            if url and url != ws_client.uri:
                ws_client.restart(url)
    except Exception as exc:  # noqa: BLE001 - never leave a timer raising
        print(f"[gloss] default config not applied: {exc}")
    return None


def unregister():
    """Unregister the add-on classes, scene properties, overlay, and client."""
    config.unregister_config_handlers()
    unregister_backend_handlers()
    unregister_client()
    del bpy.types.Scene.current_brush
    del bpy.types.Scene.gloss_brushes
    del bpy.types.Scene.gloss_config
    del bpy.types.Scene.new_brush_name
    del bpy.types.Scene.current_view
    del bpy.types.Scene.gloss_session
    del bpy.types.Scene.current_paint_mesh
    del bpy.types.Scene.current_reference_mesh
    del bpy.types.Scene.update_texture_4k
    del bpy.types.Scene.clip_fill_to_faces
    del bpy.types.Scene.syncmvd
    del bpy.types.Scene.dilate
    del bpy.types.Scene.soft_add
    del bpy.types.Scene.cam_dist
    del bpy.types.Scene.brush_cam_dist
    del bpy.types.Scene.max_cameras
    del bpy.types.Scene.cam_fov
    del bpy.types.Scene.brush_cam_fov
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    
