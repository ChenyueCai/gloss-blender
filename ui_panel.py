import bpy
import os
import bpy.utils.previews
from .utils.image import get_preview, get_brush_preview
from .client import ws_client
from .utils.mesh import is_normal_connected


class GLAZE_PT_Panel(bpy.types.Panel):
    bl_label = "GlazePanel"
    bl_idname = "GLAZE_PT_Panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Glaze"

    def draw(self, context):
        layout = self.layout
        row = layout.row()
        row.label(text="Utilities", icon="MESH_CUBE")
    
        layout.operator("glaze.load_config", text="Load Glaze Config")
        layout.operator("glaze.glaze_reload_addon", icon="FILE_REFRESH")
        
        # LOAD MESH
        row = layout.row(align=True)
        row.label(text="Meshes:", icon="MESH_CUBE")
        row = layout.row(align=True)
        row.operator("glaze.load_reference_mesh", text="Load Reference")
        row.operator("glaze.load_paint_mesh", text="Load Paint")

        # LOAD REF IMAGE
        split = layout.split(factor=0.6)
        col_left = split.column(align=True)
        col_left.label(text="Reference View", icon="IMAGE_REFERENCE")
        # Display filename if loaded
        if context.scene.current_view:
            filename = bpy.path.basename(context.scene.current_view.image_path)
            col_left.label(text=f"File: {filename}")
        else:
            col_left.label(text="No file selected")
        row = col_left.row(align=True)
        row.operator("glaze.load_reference_view", text="Select File")
        
        # SHOW MESH REFERENCE
        row = col_left.row(align=True)
        row.operator("glaze.set_reference", text="Show on Mesh")

        col_right = split.column(align=True)
        col_right.label(text="Preview", icon="VIEW_ZOOM")
        if context.scene.current_view.image_path != "":
            icon_id = get_preview(context.scene.current_view.image_path)
            col_right.template_icon(icon_id, scale=4)
        else:
            col_right.label(text="(No Image)")
            
        # PAINT MODE
        split = layout.split(factor=0.2)
        col_left = split.column(align=True)
        col_left.label(text="Paint Mode", icon="USER")
        # Right button region
        col_right = split.row(align=True)

        # Column 1: FILL
        col_fill = col_right.column(align=True)
        col_fill.operator("glaze.fill", text="FILL FACE")

        # Column 2: FILL_ALL #TODO: BUGGY!
        col_clear = col_right.column(align=True)
        col_clear.operator("glaze.fill_all", text="FILL ALL")
        
        # Column 3: CLEAR TEXTURE
        col_clear = col_right.column(align=True)
        col_clear.operator("glaze.clear_texture", text="CLEAR FACE")
        
        # INFERENCE FACE MODE
        layout.prop(context.scene, "update_texture_4k")
        
        row = layout.row()
        col1 = row.column()
        col2 = row.column()
        col3 = row.column()
        col1.operator("glaze.undo_texture", text="Undo")
        col2.operator("glaze.set_texture", text="Set Texture")
        col3.operator("glaze.clear_all_texture", text="Clear All Texture")

        try:
            mat = context.object.active_material
            row = layout.row(align=True)
            row.operator("material.toggle_normal_map",
                        text=("Disconnect Normal" if mat and is_normal_connected(mat)
                            else "Connect Normal"),
                        icon="NORMALS_VERTEX")
        except Exception as e:
            print(f'could not set up material toggle {e}')
        
        # CREATE BRUSHES
        row = layout.row(align=True)
        row.label(text="Brushes 🎨", icon="BRUSHES_ALL")
        
        columns = 3
        scale = 4
        grid = layout.grid_flow(
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
            
        split_layout = layout.split(factor=0.8)
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
     