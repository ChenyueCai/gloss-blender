import os
import bpy


# def load_brushes_from_folder(brush_folder):
#     brush_ymls = [os.path.join(brush_folder, fn) for fn in os.listdir(brush_folder) if fn.endswith("yaml")]
#     brushes = []
#     if len(brush_ymls) == 0:
#         return brushes
#     for brush_yml in brush_ymls:
#         brush_config = load_config(brush_yml)
#         brushes.append(brush_config)
#     return brushes


def get_brush_preview(brush_cache_fp):
    pass


def add_brush(name, brush_class, intensity=1):
    scene = bpy.context.scene
    brush = scene.glaze_brushes.add()
    brush.name = name
    brush.sv_id = bpy.context.scene.current_view.sv_id
    brush.brush_class = brush_class
    return brush
