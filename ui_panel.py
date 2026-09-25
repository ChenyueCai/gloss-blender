# SPDX-FileCopyrightText: Copyright (c) 2025 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import bpy
import os
import bpy.utils.previews
from .backend import server_status_icon, server_status_text
from .utils.image import get_preview, get_brush_preview


#: Icon per progress state.
_PROGRESS_ICONS = {
    'preparing': 'SORTTIME',
    'syncing': 'SORTTIME',
    'ready': 'CHECKMARK',
    'synced': 'CHECKMARK',
    'error': 'ERROR',
}


def draw_progress(layout, state, message):
    """Draw a one-line progress row for a long-running server operation.

    Nothing is drawn when idle, so the panel is unchanged in the common case.
    """
    if state == 'idle' or not state:
        return
    icon = _PROGRESS_ICONS.get(state, 'INFO')
    row = layout.row()
    row.alert = state == 'error'
    row.label(text=message or state, icon=icon)


def session_display_name(scene):
    """The session label for the panel header.

    An explicit ``gloss_session.session_name`` wins. Otherwise the name comes
    from where the .blend lives: the user-study files are laid out as
    ``.../task/<object>/<task>/template.blend``, so the two folders above the
    file (``croissant/painting``) identify a session better than the file name
    does. An unsaved file has no location to speak of.
    """
    explicit = getattr(getattr(scene, "gloss_session", None), "session_name", "")
    if explicit:
        return explicit
    filepath = bpy.data.filepath
    if not filepath:
        return "unsaved file"
    parts = [part for part in os.path.normpath(filepath).split(os.sep) if part]
    if len(parts) >= 3:
        return f"{parts[-3]}/{parts[-2]}"
    return os.path.splitext(parts[-1])[0]


class GLOSS_PT_Panel(bpy.types.Panel):
    """Sidebar UI for configuring assets, textures, and brush actions."""
    bl_label = "GlossPanel"
    bl_idname = "GLOSS_PT_Panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Gloss"

    def draw(self, context):
        """Render the add-on controls in the 3D View sidebar."""
        layout = self.layout
        scene = context.scene
        current_view = scene.current_view
        session = scene.gloss_session

        layout.label(text=f"Session: {session_display_name(scene)}", icon="FILE_BLEND")

        server_box = layout.box()
        server_box.label(text="Configuration", icon="PREFERENCES")
        server_box.prop(scene.gloss_config, "server_url")
        status_row = server_box.row(align=True)
        status_row.label(text=server_status_text(), icon=server_status_icon())
        status_row.operator("gloss.reconnect_server", text="Reconnect", icon="FILE_REFRESH")

        util_row = server_box.row(align=True)
        util_row.operator("gloss.load_config", text="Load Config", icon="FILE_FOLDER")
        util_row.operator("gloss.gloss_reload_addon", text="Reload Add-on", icon="FILE_REFRESH")
        util_row.operator("gloss.purge", text="Purge", icon="TRASH")

        server_box.prop(scene.gloss_config, "mesh_folder")
        server_box.prop(scene.gloss_config, "single_views_folder")
        server_box.prop(scene.gloss_config, "single_views_texture_folder")
        resolution_locked = (
            scene.current_paint_mesh is not None
            or bool(session.loaded_paint_texture)
        )
        resolution_row = server_box.row()
        resolution_row.enabled = not resolution_locked
        resolution_row.prop(scene, "update_texture_4k", text="Use 4K Texture")
        if resolution_locked:
            server_box.label(text="Resolution locked for this paint session", icon="LOCKED")

        asset_box = layout.box()
        asset_box.label(text="Assets", icon="MESH_DATA")
        mesh_row = asset_box.row(align=True)
        mesh_row.operator("gloss.load_reference_mesh", text="Load Reference Mesh")
        mesh_row.operator("gloss.load_paint_mesh", text="Load Paint Mesh")

        asset_box.label(
            text=f"Reference Mesh: {scene.current_reference_mesh.name if scene.current_reference_mesh else 'None'}"
        )
        asset_box.label(
            text=f"Paint Mesh: {scene.current_paint_mesh.name if scene.current_paint_mesh else 'None'}"
        )

        ref_split = asset_box.split(factor=0.62)
        ref_left = ref_split.column(align=True)
        ref_right = ref_split.column(align=True)

        ref_left.label(text="Reference Image", icon="IMAGE_REFERENCE")
        ref_left.label(
            text=bpy.path.basename(current_view.image_path) if current_view.image_path else "No file selected"
        )
        ref_left.operator("gloss.load_reference_view", text="Choose Reference Image")
        ref_left.operator("gloss.set_reference", text="Apply to Reference Mesh")

        ref_right.label(text="Preview", icon="IMAGE_DATA")
        if current_view.image_path:
            icon_id = get_preview(current_view.image_path)
            ref_right.template_icon(icon_id, scale=4)
        else:
            ref_right.label(text="(No Image)")

        tex_box = asset_box.box()
        tex_box.label(text="Paint Texture", icon="TEXTURE")
        tex_box.label(
            text=bpy.path.basename(session.loaded_paint_texture) if session.loaded_paint_texture else "No paint texture loaded"
        )
        tex_row = tex_box.row(align=True)
        tex_row.operator("gloss.load_paint_texture", text="Load Texture")
        tex_box.label(
            text=f"Resolution: {'4K' if scene.update_texture_4k else '1K'}"
        )

        gen_box = layout.box()
        gen_box.label(text="Generation", icon="BRUSH_DATA")
        fill_busy = session.fill_state == 'preparing'
        top_row = gen_box.row(align=True)
        # Disabled while the server works, so a second request cannot be fired
        # into a busy backend; the status row below says why.
        top_row.enabled = not fill_busy
        top_row.operator("gloss.fill", text="Generate Texture", icon="PLAY")
        top_row.operator("gloss.clear_texture", text="Clear Faces", icon="X")
        draw_progress(gen_box, session.fill_state, session.fill_message)

        settings_row = gen_box.row(align=True)
        settings_row.prop(scene, "cam_dist")
        settings_row.prop(scene, "max_cameras")
        settings_row.prop(scene, "cam_fov")

        flags_row = gen_box.row(align=True)
        flags_row.prop(scene, "clip_fill_to_faces")
        flags_row.prop(scene, "dilate")
        flags_row.prop(scene, "soft_add")
        flags_row.prop(scene, "syncmvd")

        brush_name = scene.current_brush if scene.current_brush else "None"
        gen_box.label(text=f"Active Brush: {brush_name}")

        # Undo and Clear All are not exposed here; their operators remain
        # registered (gloss.undo_texture, gloss.clear_all_texture) for F3.
        draw_progress(gen_box, session.sync_state, session.sync_message)

        brush_box = layout.box()
        header = brush_box.row(align=True)
        header.label(text="Brush Library", icon="BRUSHES_ALL")
        header.operator("gloss.refresh_brush_lib", text="", icon="FILE_REFRESH")
        
        columns = 3
        grid = brush_box.grid_flow(
            row_major=True,
            columns=columns,
            even_columns=True,
            even_rows=True,
            align=True
        )
        for brush in context.scene.gloss_brushes:
            if context.scene.current_brush == brush.name:
                col = grid.box().column(align=True)
            else:
                col = grid.column(align=True)
            icon_path = os.path.join(bpy.path.abspath(scene.gloss_config.brushes_folder), brush.name, "icon.png")
            icon_id = get_brush_preview(icon_path)
            op = col.operator(
                "gloss.show_brush_menu",
                text="",
                icon_value=icon_id
            )
            op.brush_name = brush.name
            
        split_layout = brush_box.split(factor=0.8)
        box = split_layout.column().box()
        box.label(text="Create New Brushes")
        split =  box.split(factor=0.45)
        col_left = split.column()

        col_left.prop(context.scene, "new_brush_name", text="")
        col_left.prop(context.scene, "brush_cam_dist")
        col_left.prop(context.scene, "brush_cam_fov")
        col_right = split.column()
        row = col_right.row(align=True)
        row.enabled = session.brush_state != 'preparing'
        op = row.operator("gloss.create_auto_brush", text="Auto Brush")
        op.brush_name = context.scene.new_brush_name
        op = row.operator("gloss.create_ref_brush", text="Ref Brush")
        op.brush_name = context.scene.new_brush_name
        draw_progress(box, session.brush_state, session.brush_message)
        
        split_layout.column().operator("gloss.clear_brush_lib", text="Clear All")
     
