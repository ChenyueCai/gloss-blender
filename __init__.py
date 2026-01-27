import bpy
import blf
import importlib

bl_info = {
    "version": (0, 1),
    "blender": (4, 0, 0),
    "location": "View3D > Sidebar > glaze-ui",
    "description": "GLAZE Painter",
    "category": "Development",
}

# Step towards the final prototype [UI design, functionality]
# 1. load in all the configs
# 2. initial displayer setting, two panels, left is for reference, right is for referening 
# 3. client server testing 

from . import client
from . import operators
from . import properties
from . import ui_panel
from .utils import io, config, image, mesh, brush

importlib.reload(client)
importlib.reload(io)
importlib.reload(mesh)
importlib.reload(operators)
importlib.reload(properties)
importlib.reload(ui_panel)
importlib.reload(config)
importlib.reload(image)
importlib.reload(brush)


from .client import register_client, unregister_client
from .ui_panel import *
from .operators import *
from .properties import *

classes = [cls for name, cls in globals().items() if isinstance(cls, type) 
           and (issubclass(cls, bpy.types.PropertyGroup) or 
                issubclass(cls, bpy.types.Operator) or 
                issubclass(cls, bpy.types.Panel))]

classes.remove(bpy.types.Operator)
classes.remove(bpy.types.PropertyGroup)
classes.remove(bpy.types.Panel)

handlers = []

def draw_text(region, x=50, y=125):
    def callback():
        line_height = 25
        blf.position(0, x, y, 0)
        blf.size(0, 18)
        blf.draw(0, "------------------------")

        # Draw the layout lines
        layout_lines = [
            "GLAZE add-on Layout",
            "LEFT: Paint Playground ",
            "TOP-RIGHT: Palette 🎨",
            "BOTTOM-RIGHT: Control 🔧",
        ]

        for i, line in enumerate(layout_lines):
            blf.position(0, x, y - (i + 1) * line_height, 0)
            blf.draw(0, line)

        # Bottom border
        blf.position(0, x, y - (len(layout_lines) + 1) * line_height, 0)
        blf.draw(0, "------------------------")
    handler = bpy.types.SpaceView3D.draw_handler_add(callback, (), 'WINDOW', 'POST_PIXEL')
    handlers.append(handler)
    
def setup_text():
    areas = bpy.context.screen.areas
    for i, area in enumerate(areas):
        for region in area.regions:
            if region.type == 'WINDOW':
                draw_text(region)
                break


def register():
    bpy.app.timers.register(setup_text)
    for cls in classes:
        try:
            bpy.utils.register_class(cls)
        except ValueError:
            # Already registered
            print(f"{cls.__name__} already registered, skipping.")
    bpy.types.Scene.glaze_config = bpy.props.PointerProperty(type=GlazeConfig)
    bpy.types.Scene.current_paint_mesh = bpy.props.PointerProperty(name="Mesh", type=bpy.types.Object)
    bpy.types.Scene.current_reference_mesh = bpy.props.PointerProperty(name="Mesh", type=bpy.types.Object)
    bpy.types.Scene.current_view = bpy.props.PointerProperty(type=GlazeSingleView)  
    bpy.types.Scene.current_brush = bpy.props.StringProperty(name="Current Brush Name", default="")
    bpy.types.Scene.glaze_brushes = bpy.props.CollectionProperty(type=GlazeBrush)
    bpy.types.Scene.new_brush_name = bpy.props.StringProperty(name="New Brush Name", default="MyBrush")
    bpy.types.Scene.target_faces = bpy.props.CollectionProperty(type=FaceIndexItem)
    bpy.types.Scene.reference_faces = bpy.props.CollectionProperty(type=FaceIndexItem)
    bpy.types.Scene.inference_view_settings = bpy.props.PointerProperty(type=InferenceViewSettings)
    bpy.types.Scene.update_texture_4k = bpy.props.BoolProperty(
        name="Update with 4K Texture",
        description="Toggle between 4K texture or lower resolution",
        default=True,
    )
    bpy.types.Scene.server_debug = bpy.props.BoolProperty(
        name="Server Debug",
        description="If true, server will write debug data to disk",
        default=True)
    bpy.types.Scene.clip_fill_to_faces = bpy.props.BoolProperty(
        name="Clip to Faces",
        description="If true, will clip texture fill to faces, if provided",
        default=True)
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
    bpy.types.Scene.max_cameras = bpy.props.IntProperty(
        name="Max Cam",
        description="Max fill patches to use",
        default=5,
        min=1,  # Hard minimum value
        max=15  # Hard maximum value
    )

    register_client()
    
def unregister():
    unregister_client()
    for h in handlers:
        bpy.types.SpaceView3D.draw_handler_remove(h, 'WINDOW')
        handlers.clear()
    del bpy.types.Scene.current_brush
    del bpy.types.Scene.glaze_brushes
    del bpy.types.Scene.glaze_config
    del bpy.types.Scene.new_brush_name
    del bpy.types.Scene.current_view
    del bpy.types.Scene.current_paint_mesh
    del bpy.types.Scene.current_reference_mesh
    del bpy.types.Scene.target_faces
    del bpy.types.Scene.reference_faces
    del bpy.types.Scene.inference_view_settings
    del bpy.types.Scene.update_texture_4k
    del bpy.types.Scene.server_debug
    del bpy.types.Scene.clip_fill_to_faces
    del bpy.types.Scene.dilate
    del bpy.types.Scene.soft_add
    del bpy.types.Scene.cam_dist
    del bpy.types.Scene.max_cameras
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    