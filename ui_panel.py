import bpy
import os
import bpy.utils.previews
from .backend import server_status_icon, server_status_text
from .utils.image import get_preview, get_brush_preview
from .utils.mesh import is_normal_connected


class GLAZE_PT_Panel(bpy.types.Panel):
    """Sidebar UI for configuring assets, textures, and brush actions."""
    bl_label = "GlazePanel"
    bl_idname = "GLAZE_PT_Panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Glaze"

    def draw(self, context):
        """Render the add-on controls in the 3D View sidebar."""
        layout = self.layout
        scene = context.scene
        current_view = scene.current_view
        session = scene.glaze_session

        server_box = layout.box()
        server_box.label(text="Server", icon="URL")
        server_box.prop(scene.glaze_config, "server_url", text="")
        status_row = server_box.row(align=True)
        status_row.label(text=server_status_text(), icon=server_status_icon())
        status_row.operator("glaze.reconnect_server", text="Reconnect", icon="FILE_REFRESH")

        util_row = server_box.row(align=True)
        util_row.operator("glaze.load_config", text="Load Config", icon="FILE_FOLDER")
        util_row.operator("glaze.glaze_reload_addon", text="Reload Add-on", icon="FILE_REFRESH")

        asset_box = layout.box()
        asset_box.label(text="Assets", icon="MESH_DATA")
        mesh_row = asset_box.row(align=True)
        mesh_row.operator("glaze.load_reference_mesh", text="Load Reference Mesh")
        mesh_row.operator("glaze.load_paint_mesh", text="Load Paint Mesh")

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
        ref_left.operator("glaze.load_reference_view", text="Choose Reference Image")
        ref_left.operator("glaze.set_reference", text="Apply to Reference Mesh")

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
        tex_row.operator("glaze.load_paint_texture", text="Load Texture")
        tex_row.prop(session, "auto_sync_texture", text="Auto Sync")
        tex_box.label(
            text=f"Resolution: {'4K' if scene.update_texture_4k else '1K'} (from config)"
        )

        gen_box = layout.box()
        gen_box.label(text="Generation", icon="BRUSH_DATA")
        top_row = gen_box.row(align=True)
        top_row.operator("glaze.fill", text="Generate Texture", icon="PLAY")
        top_row.operator("glaze.clear_texture", text="Clear Faces", icon="X")

        settings_row = gen_box.row(align=True)
        settings_row.prop(scene, "cam_dist")
        settings_row.prop(scene, "max_cameras")

        flags_row = gen_box.row(align=True)
        flags_row.prop(scene, "clip_fill_to_faces")
        flags_row.prop(scene, "dilate")
        flags_row.prop(scene, "soft_add")

        brush_name = scene.current_brush if scene.current_brush else "None"
        gen_box.label(text=f"Active Brush: {brush_name}")

        action_row = gen_box.row(align=True)
        action_row.operator("glaze.undo_texture", text="Undo")
        action_row.operator("glaze.set_texture", text="Sync to Server")
        action_row.operator("glaze.clear_all_texture", text="Clear All")

        if context.object is not None:
            mat = context.object.active_material
            normal_row = gen_box.row(align=True)
            normal_row.operator("material.toggle_normal_map",
                        text=("Disconnect Normal" if mat and is_normal_connected(mat)
                            else "Connect Normal"),
                        icon="NORMALS_VERTEX")
        
        brush_box = layout.box()
        brush_box.label(text="Brush Library", icon="BRUSHES_ALL")
        
        columns = 3
        grid = brush_box.grid_flow(
            row_major=True,
            columns=columns,
            even_columns=True,
            even_rows=True,
            align=True
        )
        for brush in context.scene.glaze_brushes:
            if context.scene.current_brush == brush.name:
                col = grid.box().column(align=True)
            else:
                col = grid.column(align=True)
            icon_path = os.path.join(context.scene.glaze_config.brushes_folder, f"{brush.name}", "icon.png")  
            icon_id = get_brush_preview(icon_path)
            op = col.operator(
                "glaze.show_brush_menu",
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
        col_right = split.column()
        row = col_right.row(align=True)
        op = row.operator("glaze.create_auto_brush", text="Auto Brush")
        op.brush_name = context.scene.new_brush_name
        op = row.operator("glaze.create_ref_brush", text="Ref Brush")
        op.brush_name = context.scene.new_brush_name
        
        split_layout.column().operator("glaze.clear_brush_lib", text="Clear All")
     
