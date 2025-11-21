import os

import bpy
import bmesh

from .utils.config import load_glaze_config_from_yaml
from .utils.mesh import load_mesh, duplicate_mesh, apply_texture
from .utils.image import list_images

from .client import ws_client


REF_MESH_NAME = "GlazeMesh_Ref"
PAINT_MESH_NAME = "GlazeMesh_Paint"


def set_view_center(obj, area):
    """Center a 3D View area on an object."""
    for space in area.spaces:
        if space.type == 'VIEW_3D':
            space.region_3d.view_location = obj.location
            space.region_3d.view_rotation = obj.matrix_world.to_quaternion()
            break


class GLAZE_OT_LoadConfig(bpy.types.Operator):
    bl_idname = "glaze.load_config"
    bl_label = "Load Glaze Config"

    filepath: bpy.props.StringProperty(subtype='FILE_PATH')

    def execute(self, context):
        scene = context.scene
        cfg = scene.glaze_config

        load_glaze_config_from_yaml(self.filepath)

        self.report({'INFO'}, f"Config loaded from {self.filepath}")
        return {'FINISHED'}

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}


class GLAZE_OT_LoadDualMesh(bpy.types.Operator):
    bl_idname = "glaze.load_dual_mesh"
    bl_label = "Load Dual Mesh"

    def execute(self, context):
        ref_obj = load_mesh(context.scene.glaze_config.mesh_file_path, REF_MESH_NAME)
        ref_obj.location = (-1.5, 0, 0)
        paint_obj = duplicate_mesh(ref_obj, PAINT_MESH_NAME, location=(1.5, 0, 0))
        self.report({"INFO"}, "Dual mesh loaded")
        return {'FINISHED'}


class GLAZE_OT_SetReference(bpy.types.Operator):
    bl_idname = "glaze.set_reference"
    bl_label = "Set Reference"

    def execute(self, context):
        curr_view = context.scene.current_view
        single_view_path = curr_view.image_path
        curr_view.sv_id = int(os.path.basename(single_view_path).split('.')[0][4:])
        single_view_texture_path = os.path.join(context.scene.glaze_config.single_views_texture_folder, "view%04d.png" % curr_view.sv_id)
        # apply the paritial texture to meshes 
        apply_texture(bpy.data.objects.get(REF_MESH_NAME), single_view_texture_path, suffix='ref')
        apply_texture(bpy.data.objects.get(PAINT_MESH_NAME), single_view_texture_path, suffix='paint')
        self.report({'INFO'}, f"Selected Single View index: {curr_view.sv_id}")
        return {'FINISHED'}


class GLAZE_OT_split_screen(bpy.types.Operator):
    bl_idname = "glaze.split_screen"
    bl_label = "Split Screen Glaze"
    
    def execute(self, context):
        wm = context.window_manager
        window = context.window
        screen = window.screen
        
        # Find the objects
        ref_obj = bpy.data.objects.get("GlazeMesh_Ref")
        paint_obj = bpy.data.objects.get("GlazeMesh_Paint")
        
        if not ref_obj or not paint_obj:
            self.report({'ERROR'}, "Both GlazeMeshRef and GlazeMeshPaint must exist")
            return {'CANCELLED'}
        
        # Split the first 3D view vertically
        for area in screen.areas:
            if area.type == 'VIEW_3D':
                bpy.ops.screen.area_split(direction='VERTICAL', factor=0.5)
                break
        
        # Re-fetch areas after split
        areas = [a for a in screen.areas if a.type == 'VIEW_3D']
        if len(areas) < 2:
            self.report({'ERROR'}, "Failed to split 3D view")
            return {'CANCELLED'}
        
        # Left = Ref, Right = Paint
        left_area, right_area = areas[0], areas[1]
        set_view_center(ref_obj, left_area)
        set_view_center(paint_obj, right_area)
        
        return {'FINISHED'}


#class GLAZE_OT_load_brush_library(bpy.types.Operator):
    
class GLAZE_OT_create_brushes(bpy.types.Operator):
    bl_idname = "glaze.create_brush"
    bl_label = "Add Brush"
    
    brush_name: bpy.props.StringProperty(name="Name")
    brush_type: bpy.props.EnumProperty(
        name="Type",
        items=[
            ('AutoSampledReferenceBrush', "AutoSampledReferenceBrush", ""),
            ('PreSampledReferenceBrush', "PreSampledReferenceBrush", "")
        ]
    )
    def execute(self, context):
        if not self.brush_name:
            self.report({'ERROR'}, "Brush name cannot be empty")
            return {'CANCELLED'}
        scene = context.scene
        brush = scene.glaze_brushes.add()
        brush.name = self.brush_name
        brush.brush_type = self.brush_type
        brush.sv_id = context.scene.current_view.sv_id
        brush_info = {"brush_name": self.brush_name, 
                      "brush_type":self.brush_type,
                      "sv_id":brush.sv_id}
        message = {"type": "add_brush",
                   "data": brush_info}
        ws_client.send(message)
        return {'FINISHED'}
        
    
class GLAZE_OT_SetBrush(bpy.types.Operator):
    bl_idname = "glaze.set_brush"
    bl_label = "Set Brush"

    brush_name: bpy.props.StringProperty(name="Name")

    def execute(self, context):
        self.report({'INFO'}, f"Setting the current brush as: {self.brush_name}")
        context.scene.current_brush = self.brush_name
        # TODO: any texture chnage in the scene?
        return {"FINISHED"}


class GLAZE_OT_PrepareBrush(bpy.types.Operator):
    bl_idname = "glaze.prepare_brush"
    bl_label = "Prepare Brush"

    brush_name: bpy.props.StringProperty(name="Name")

    def execute(self, context):
        print(f"Preparing brush: {self.brush_name}")
        # TODO: send msg to server to prepare the brush
        brush_info = {"brush_name": self.brush_name}
        message = {"type": "prepare_brush",
                   "data": brush_info}
        ws_client.send(message)
        return {"FINISHED"}
    
class GLAZE_OT_SaveBrush():
    #TODO:
    pass
    
class GLAZE_OT_RemoveBrush():
    #TODO:
    pass

class GLAZE_OT_SelectTargetFace(bpy.types.Operator):
    bl_idname = "glaze.select_target_face"
    bl_label = "Select Target Face"

    def execute(self, context):
        obj = bpy.data.objects.get(REF_MESH_NAME)
        context.scene.target_faces.clear()
        bm = bmesh.from_edit_mesh(obj.data)
        bm.faces.ensure_lookup_table()
        target_faces = []
        for f in bm.faces:
            if f.select:
                entry = context.scene.target_faces.add()
                entry.index = f.index
                target_faces.append(f.index)
                self.report({'INFO'}, f"Adding {f.index} as a target face")
        
        face_info = {"target_faces": target_faces,
                     "brush_name": context.scene.current_brush}
        message = {"type": "set_target_face",
                   "data": face_info}
        ws_client.send(message)
        
        return {"FINISHED"}

    
class GLAZE_OT_SelectReferenceFace(bpy.types.Operator):
    bl_idname = "glaze.select_reference_face"
    bl_label = "Select Reference Face"

    def execute(self, context):
        obj = bpy.data.objects.get(REF_MESH_NAME)
        context.scene.reference_faces.clear()
        bm = bmesh.from_edit_mesh(obj.data)
        bm.faces.ensure_lookup_table()
        reference_faces = []
        for f in bm.faces:
            if f.select:
                entry = context.scene.reference_faces.add()
                entry.index = f.index
                reference_faces.append(f.index)
                self.report({'INFO'}, f"Adding {f.index} as a reference face")
        face_info = {"reference_faces": reference_faces,
                     "brush_name": context.scene.current_brush}
        payload = {"type": "set_reference_face",
                   "data": face_info}
        ws_client.send(payload)
        return {"FINISHED"}


class GLAZE_OT_UpdateTexture(bpy.types.Operator):
    bl_idname = "glaze.update_texture"
    bl_label = "Update Texture"

    brush: bpy.props.StringProperty()

    def execute(self, context):
        print(f"Setting current brush: {self.brush}")
        # TODO: update the texture with new texture
        
        # receive updated texture through client
        
        # update the texture
        
        
        return {"FINISHED"}


class GLAZE_OT_ReloadAddon(bpy.types.Operator):
    """Reload all Blender scripts (useful during development)"""
    bl_idname = "glaze.glaze_reload_addon"
    bl_label = "🔄 Reload Add-on"

    def execute(self, context):
        bpy.ops.script.reload()
        self.report({'INFO'}, "Scripts reloaded successfully!")
        return {'FINISHED'}
    