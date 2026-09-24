import os
import bpy


def get_brush_preview(brush_cache_fp):
    """Placeholder for brush preview loading.

    The helper is currently unimplemented and returns ``None``.
    """
    pass

def add_brush(name, brush_class, intensity=1):
    """Add a brush entry to the current scene collection.

    Args:
        name: Display name stored on the new brush entry.
        brush_class: Value assigned to ``brush.brush_class`` by the current
            implementation.
        intensity: Currently unused.

    Returns:
        bpy.types.PropertyGroup: The newly created brush entry.
    """
    scene = bpy.context.scene
    brush = scene.gloss_brushes.add()
    brush.name = name
    brush.sv_id = bpy.context.scene.current_view.sv_id
    brush.brush_class = brush_class
    return brush
