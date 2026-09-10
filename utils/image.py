import bpy
import os
from pathlib import Path
import numpy as np


def list_images(folder_path):
    """Return supported image files in ``folder_path`` sorted by name."""
    folder_path = Path(folder_path)
    return sorted([p for p in folder_path.iterdir() if p.suffix.lower() in [".png", ".jpg", ".jpeg"]])


preview_collection = None
brush_preview_collection = None

def get_preview(path):
    """Load or reuse a preview icon for an image file.

    Returns:
        int: Blender preview icon id, or ``0`` when the preview cannot be
        loaded.
    """
    global preview_collection

    if preview_collection is None:
        preview_collection = bpy.utils.previews.new()

    name = os.path.basename(path)
    
    # Avoid caching stale previews
    if name in preview_collection:
        return preview_collection[name].icon_id

    # Load the preview synchronously
    try:
        preview = preview_collection.load(name, path, "IMAGE")
        return preview.icon_id
    except Exception as e:
        print("Preview load error:", e)
        return 0

def get_brush_preview(path):
    """Load or reuse a preview icon for a brush icon image.

    Brush previews are cached by full path so changing the brushes folder
    loads the icon from the new location.
    """
    global brush_preview_collection

    if brush_preview_collection is None:
        brush_preview_collection = bpy.utils.previews.new()

    name = os.path.normpath(bpy.path.abspath(path))

    # Avoid caching stale previews
    if name in brush_preview_collection:
        return brush_preview_collection[name].icon_id

    # Load the preview synchronously
    try:
        preview = brush_preview_collection.load(name, path, "IMAGE")
        return preview.icon_id
    except Exception as e:
        print("Preview load error:", e)
        return 0
