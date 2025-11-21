import bpy
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
importlib.reload(operators)
importlib.reload(properties)
importlib.reload(ui_panel)
importlib.reload(config)
importlib.reload(image)
importlib.reload(mesh)
importlib.reload(brush)
importlib.reload(io)

from .client import register_client, unregister_client
from .ui_panel import *
from .operators import *
from .properties import *
from .utils.image import get_single_view_files

classes = [cls for name, cls in globals().items() if isinstance(cls, type) 
           and (issubclass(cls, bpy.types.PropertyGroup) or 
                issubclass(cls, bpy.types.Operator) or 
                issubclass(cls, bpy.types.Panel))]

def register():
    for cls in classes:
        try:
            bpy.utils.register_class(cls)
        except ValueError:
            # Already registered
            print(f"{cls.__name__} already registered, skipping.")

    bpy.types.Scene.glaze_config = bpy.props.PointerProperty(type=GlazeConfig)
    bpy.types.Scene.current_view = bpy.props.PointerProperty(type=GlazeSingleView)  
    bpy.types.Scene.current_brush = bpy.props.StringProperty(name="Current Brush Name", default="")
    bpy.types.Scene.glaze_brushes = bpy.props.CollectionProperty(type=GlazeBrush)
    bpy.types.Scene.new_brush_name = bpy.props.StringProperty(name="New Brush Name", default="MyBrush")
    bpy.types.Scene.new_brush_type = bpy.props.EnumProperty(
        name="New Brush Type",
        items=[
            ('AutoSampledReferenceBrush', "AutoSampledReferenceBrush", ""),
            ('PreSampledReferenceBrush', "PreSampledReferenceBrush", "")
        ],
        default='AutoSampledReferenceBrush'
    )
    bpy.types.Scene.target_faces = bpy.props.CollectionProperty(type=FaceIndexItem)
    bpy.types.Scene.reference_faces = bpy.props.CollectionProperty(type=FaceIndexItem)
    
    register_client()
    

    
        
def unregister():
    unregister_client()
    del bpy.types.Scene.current_brush
    del bpy.types.Scene.glaze_brushes
    del bpy.types.Scene.glaze_config
    del bpy.types.Scene.new_brush_name
    del bpy.types.Scene.new_brush_type
    del bpy.types.Scene.current_view
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    
    