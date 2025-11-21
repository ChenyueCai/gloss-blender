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
        layout.operator("glaze.glaze_reload_addon", icon="FILE_REFRESH")
        layout.operator("glaze.load_config", text="Load Glaze Config")
        # reference image previews, mesh texture previews 
        # (can use a custmo OT file selector to load from specific folders):
        layout.prop(context.scene.current_view, "image_path", text="Current View")
        icon_id = get_preview(context.scene.current_view.image_path)
        layout.template_icon(icon_id, scale=8)
        layout.operator("glaze.load_dual_mesh", text="Load Ref / Paint Mesh")
        layout.operator("glaze.split_screen", text="Split Screen Ref / Paint")
        layout.operator("glaze.set_reference", text="Set Single View Reference")

        layout.prop(context.scene, "new_brush_name", text="Brush Name")
        layout.prop(context.scene, "new_brush_type", text="Brush Type")

        op = layout.operator("glaze.create_brush", text="Create Brush")
        op.brush_name = context.scene.new_brush_name
        op.brush_type = context.scene.new_brush_type

        layout.separator()
        layout.label(text="Existing Brushes:")
        for brush in context.scene.glaze_brushes:
            row = layout.row()
            row.label(text=f"{brush.name} type={brush.brush_type} sv_id={brush.sv_id}")
            row = layout.row()
            op = row.operator("glaze.set_brush", text="Set as Current")
            op.brush_name = brush.name
            op = row.operator("glaze.prepare_brush", text="Prepare")
            op.brush_name = brush.name

        layout.separator()
        row = layout.row()
        row.operator("glaze.select_target_face", text="Set Target Face")
        row.operator("glaze.select_reference_face", text="Set Reference Face")
        # # TODO: update the mesh texture once the server send information
        layout.operator("glaze.update_texture", text="Update Texture")
        
       
        

