import os
import bpy


def get_brush_preview(brush_cache_fp):
    pass

def add_brush(name, brush_class, intensity=1):
    scene = bpy.context.scene
    brush = scene.glaze_brushes.add()
    brush.name = name
    brush.sv_id = bpy.context.scene.current_view.sv_id
    brush.brush_class = brush_class
    return brush
