import os

import bpy
import bmesh

from .utils.config import load_glaze_config_from_yaml
from .utils.mesh import load_mesh, duplicate_mesh, apply_texture
from .utils.image import list_images

import time
from .client import ws_client
import torchvision


REF_MESH_NAME = "GlazeMesh_Ref"
PAINT_MESH_NAME = "GlazeMesh_Paint"


def set_view_center(obj, area):
    """Center a 3D View area on an object."""
    for space in area.spaces:
        if space.type == 'VIEW_3D':
            space.region_3d.view_location = obj.location
            space.region_3d.view_rotation = obj.matrix_world.to_quaternion()
            break

class GLAZE_OT_LoadReferenceMesh(bpy.types.Operator):
    """Load Reference Mesh"""
    bl_idname = "glaze.load_reference_mesh"
    bl_label = "Load Reference Mesh"
    bl_options = {'UNDO'}
    
    filepath: bpy.props.StringProperty(subtype="FILE_PATH")

    def execute(self, context):
        print("Loading reference mesh:", self.filepath)
        mesh_name = os.path.splitext(os.path.basename(self.filepath))[0] + "ref"
        ref_mesh = load_mesh(mesh_path=self.filepath, name=mesh_name)  # You can change importer
        ref_mesh.location = (-1.5, 0, 0)
        context.scene.current_reference_mesh = ref_mesh
        screen = context.window.screen
        areas = [a for a in screen.areas if a.type == 'VIEW_3D']
        left_area, right_area = areas[0], areas[1]
        set_view_center(ref_mesh, right_area)
        return {'FINISHED'}

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}


class GLAZE_OT_LoadPaintMesh(bpy.types.Operator):
    """Load Paint Mesh"""
    bl_idname = "glaze.load_paint_mesh"
    bl_label = "Load Paint Mesh"
    bl_options = {'UNDO'}
    
    filepath: bpy.props.StringProperty(subtype="FILE_PATH")

    def execute(self, context):
        print("Loading paint mesh:", self.filepath)
        mesh_name = os.path.splitext(os.path.basename(self.filepath))[0]  + "_pnt"
        paint_mesh = load_mesh(mesh_path=self.filepath, name=mesh_name)
        paint_mesh.location = (1.5, 0, 0)
        context.scene.current_paint_mesh = paint_mesh
        screen = context.window.screen
        areas = [a for a in screen.areas if a.type == 'VIEW_3D']
        left_area, right_area = areas[0], areas[1]
        set_view_center(paint_mesh, left_area)
        return {'FINISHED'}

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}


class GLAZE_OT_LoadConfig(bpy.types.Operator):
    """Load Glaze Session Config"""
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


class GLAZE_OT_SetReference(bpy.types.Operator):
    """Set Reference Image for Reference Mesh"""
    bl_idname = "glaze.set_reference"
    bl_label = "Set Reference"

    def execute(self, context):
        curr_view = context.scene.current_view
        single_view_path = curr_view.image_path
        curr_view.sv_id = int(os.path.basename(single_view_path).split('.')[0][4:])
        curr_view.mesh = context.scene.current_reference_mesh.name
        single_view_texture_path = os.path.join(context.scene.glaze_config.single_views_texture_folder, "view%04d.png" % curr_view.sv_id)
        # apply the paritial texture to meshes 
        apply_texture(bpy.data.objects.get(curr_view.mesh), single_view_texture_path, suffix='ref')
        self.report({'INFO'}, f"Selected Single View index: {curr_view.sv_id} - Mesh {curr_view.mesh}")
        return {'FINISHED'}


class GLAZE_OT_LoadReferenceView(bpy.types.Operator):
    """Load Reference View"""
    bl_idname = "glaze.load_reference_view"
    bl_label = "Select Reference View"

    filepath: bpy.props.StringProperty(subtype="FILE_PATH")

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        context.scene.current_view.image_path = self.filepath
        return {'FINISHED'}


def brush_exists(scene, name, sv_id, brush_type):
    for b in scene.glaze_brushes:
        if b.name == name:
            return True
    return False
    
class GLAZE_OT_create_auto_brushes(bpy.types.Operator):
    """Create New Auto Type Brush"""
    bl_idname = "glaze.create_auto_brush"
    bl_label = "Add Brush"
    
    brush_name: bpy.props.StringProperty(name="Name")

    def execute(self, context):
        brush_type = "AutoSampledReferenceBrush"
        if not self.brush_name:
            self.report({'ERROR'}, "Brush name cannot be empty")
            return {'CANCELLED'}
        scene = context.scene
        if not brush_exists(scene, self.brush_name, context.scene.current_view.mesh, context.scene.current_view.sv_id, brush_type):
            brush = scene.glaze_brushes.add()
            brush.name = self.brush_name
            brush.mesh = context.scene.current_view.mesh
            brush.brush_type = brush_type 
            brush.sv_id = context.scene.current_view.sv_id
            brush_info = {"brush_name": self.brush_name, 
                        "brush_type":brush_type,
                        "brush_mesh": brush.mesh[:-4],
                        "sv_id":brush.sv_id}
            message = {"type": "add_brush",
                    "data": brush_info}
            ws_client.send(message)
            received = False
            start = time.time()
            while not received and (time.time() - start) < 8:
                rec_message = ws_client.poll_bin_messages()
                if rec_message is not None:
                    if rec_message.get("brush icon") is not None:
                        brush_icon = rec_message["brush icon"]
                        brush_dir = os.path.join(bpy.context.scene.glaze_config.brushes_folder, f"{brush.name}")    
                        os.makedirs(brush_dir, exist_ok=True)
                        torchvision.utils.save_image(brush_icon.permute(2, 0, 1), os.path.join(brush_dir, "icon.png"))  
                    received = True 
        else:
            self.report({'INFO'}, f"already created : {self.brush_name}")
        return {'FINISHED'}


class GLAZE_OT_create_ref_brushes(bpy.types.Operator):
    """Create New Reference Type Brush"""
    bl_idname = "glaze.create_ref_brush"
    bl_label = "Add Brush"
    
    brush_name: bpy.props.StringProperty(name="Name")

    def execute(self, context):
        brush_type = "PreSampledReferenceBrush"
        if not self.brush_name:
            self.report({'ERROR'}, "Brush name cannot be empty")
            return {'CANCELLED'}
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
        if len(reference_faces) == 0:
            self.report({'ERROR'}, "Reference Brush Requires Reference Face Selected before Creation.")
            return {'CANCELLED'}
        scene = context.scene
        if not brush_exists(scene, self.brush_name, context.scene.current_view.mesh, context.scene.current_view.sv_id, brush_type):
            brush = scene.glaze_brushes.add()
            brush.name = self.brush_name
            brush.mesh = context.scene.current_view.mesh
            brush.brush_type = brush_type 
            brush.sv_id = context.scene.current_view.sv_id
            brush_info = {"brush_name": self.brush_name, 
                        "brush_type": brush_type,
                        "brush_mesh": brush.mesh[:-4],
                        "sv_id": brush.sv_id,
                        "reference_faces": reference_faces}
            message = {"type": "add_brush",
                    "data": brush_info}
            ws_client.send(message)
            start = time.time()
            while not received and (time.time() - start) < 8:
                rec_message = ws_client.poll_bin_messages()
                if rec_message is not None:
                    if rec_message.get("brush icon") is not None:
                        brush_icon = rec_message["brush icon"]
                        brush_dir = os.path.join(bpy.context.scene.glaze_config.brushes_folder, f"{brush.name}")    
                        os.makedirs(brush_dir, exist_ok=True)
                        torchvision.utils.save_image(brush_icon.permute(2, 0, 1), os.path.join(brush_dir, "icon.png"))  
                    received = True    
        else:
            self.report({'INFO'}, f"already created : {self.brush_name}")
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

class GLAZE_OT_ClearBrushLib(bpy.types.Operator):
    bl_idname = "glaze.clear_brush_lib"
    bl_label = "Clear Brush Library"

    def execute(self, context):
        context.scene.current_brush = ""
        context.scene.glaze_brushes.clear()
        self.report({'INFO'}, f"Clear All Brushes")
        # TODO: any texture chnage in the scene?
        return {"FINISHED"}
    
class GLAZE_OT_ShowBrush(bpy.types.Operator):
    bl_idname = "glaze.show_brush"
    bl_label = "Brush"
    brush_name: bpy.props.StringProperty()
    
    @classmethod
    def description(cls, context, properties):
        return f"{properties.brush_name}"

    def execute(self, context):
        context.scene.current_brush = self.brush_name
        return {'FINISHED'}
    
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
    