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

    Brush previews are cached by the parent folder name so each brush keeps a
    stable icon entry across UI redraws.
    """
    global brush_preview_collection

    if brush_preview_collection is None:
        brush_preview_collection = bpy.utils.previews.new()

    name = path.split('/')[-2]

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


def get_single_view_files(self, context):
    """Build EnumProperty items from ``glaze_config.single_views_folder``.

    Returns:
        list[tuple[str, str, str]]: File choices for Blender UI enums. When no
        image files are available, returns a single ``("NONE", ...)`` fallback
        entry.
    """
    folder = context.scene.glaze_config.single_views_folder
    items = []

    if folder and os.path.isdir(folder):
        for f in sorted(os.listdir(folder)):
            if f.lower().endswith((".png", ".jpg", ".jpeg")):
                # (identifier, name, description)
                items.append((f, f, f))
    
    if not items:
        items.append(('NONE', 'No files found', ''))

    return items
