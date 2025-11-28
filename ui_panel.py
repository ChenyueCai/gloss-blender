import bpy
import os
import bpy.utils.previews
from .utils.image import get_preview
#from .utils.brush import load_brushes_from_folder, get_brush_preview



class GLAZE_PT_DualMeshPanel(bpy.types.Panel):
    bl_label = "GlazePanel"
    bl_idname = "GLAZE_PT_DualMeshPanel"
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
        col_left.label(text="Reference View", icon="FILE_IMAGE")
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
            icon_id = 0 #brush.preview.icon_id if brush.preview else 0

            op = col.operator(
                "glaze.show_brush",
                text="",                  # icon-only
                icon_value=icon_id
            )
            op.brush_name = brush.name
            # Set tooltip dynamically — Blender reads from bl_description
            #op.bl_description = f"Brush: {brush.name}"
        
        box = layout.box()
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
        
        
        # op = layout.operator("glaze.create_brush", text="Create Brush")
        # op.brush_name = context.scene.new_brush_name
        # op.brush_type = context.scene.new_brush_type

        # layout.separator()
        # layout.label(text="Existing Brushes:")
        # for brush in context.scene.glaze_brushes:
        #     if context.scene.current_brush == brush.name:
        #         box = layout.box()
        #         col = box.column()
        #     else:
        #         col = layout.column()
        #     row = col.row()
        #     row.label(text=f"{brush.name} type={brush.brush_type} sv_id={brush.sv_id}")
        #     row = layout.row()
        #     op = row.operator("glaze.set_brush", text="Set as Current")
        #     op.brush_name = brush.name
        #     op = row.operator("glaze.prepare_brush", text="Prepare")
        #     op.brush_name = brush.name

        # layout.separator()
        # row = layout.row()
        # row.operator("glaze.select_target_face", text="Set Target Face")
        # row.operator("glaze.select_reference_face", text="Set Reference Face")
        # # # TODO: update the mesh texture once the server send information
        # layout.operator("glaze.update_texture", text="Update Texture")
        
       
        

