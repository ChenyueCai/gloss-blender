import bpy
import bmesh

from .utils.config import load_glaze_config_from_yaml
from .utils.mesh import load_mesh, apply_texture, update_texture, get_current_texture, undo_texture, \
    is_normal_connected, disconnect_normal, connect_normal, clear_texture
from .utils.io import to_binary, from_binary, send_large_image
from .client import ws_client

import re
import os
import torchvision, torch
import numpy as np
import time

from queue import Empty


REF_MESH_NAME = "GlazeMesh_Ref"
PAINT_MESH_NAME = "GlazeMesh_Paint"


def set_view_center(obj, area):
    """Center a 3D View area on an object."""
    for space in area.spaces:
        if space.type == 'VIEW_3D':
            space.region_3d.view_location = obj.location
            space.region_3d.view_rotation = obj.matrix_world.to_quaternion()
            break
        
def base_name(name):
    # Matches "thing", "thing.001", "thing.123", even "thing.something.001"
    m = re.match(r"^(.*?)(?:\.\d+)?$", name)
    return m.group(1)

class GLAZE_OT_LoadReferenceMesh(bpy.types.Operator):
    """Load Reference Mesh"""
    bl_idname = "glaze.load_reference_mesh"
    bl_label = "Load Reference Mesh"
    bl_options = {'UNDO'}
    
    filepath: bpy.props.StringProperty(subtype="FILE_PATH")

    def execute(self, context):
        print("Loading reference mesh:", self.filepath)
        mesh_name = os.path.splitext(os.path.basename(self.filepath))[0] + "_ref"
        ref_mesh = load_mesh(mesh_path=self.filepath, name=mesh_name)  # You can change importer
        ref_mesh.location = (-1.5, 0, 0)
        context.scene.current_reference_mesh = ref_mesh
        screen = context.window.screen
        areas = [a for a in screen.areas if a.type == 'VIEW_3D']
        left_area, right_area = areas[0], areas[1]
        set_view_center(ref_mesh, right_area)
        mesh_info = {"mesh_name": base_name(ref_mesh.name)[:-4]}
        message = {"type": "add_ref_mesh", 
                   "data": mesh_info}
        ws_client.send(message)
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
        # remove the basecolor slots
        apply_texture(paint_mesh, None, suffix='paint')
        mesh_info = {"mesh_name": base_name(paint_mesh.name)[:-4], "paint_mesh_name": paint_mesh.name}
        message = {"type": "add_pnt_mesh", 
                   "data": mesh_info}
        ws_client.send(message)
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
        curr_view.mesh = base_name(context.scene.current_reference_mesh.name)
        single_view_texture_path = os.path.join(context.scene.glaze_config.single_views_texture_folder, curr_view.mesh[:-4], "view%04d.png" % curr_view.sv_id)
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


def brush_exists(scene, name):
    for b in scene.glaze_brushes:
        if b.name == name:
            return True
    return False

    
class GLAZE_OT_create_auto_brushes(bpy.types.Operator):
    """Create New Auto Type Brush"""
    bl_idname = "glaze.create_auto_brush"
    bl_label = "Add Brush"
    
    brush_name: bpy.props.StringProperty(name="Name")

    _timer = None
    def execute(self, context):
        self.brush_type = "AutoSampledReferenceBrush"

        if not self.brush_name:
            self.report({'ERROR'}, "Brush name cannot be empty")
            return {'CANCELLED'}

        scene = context.scene

        if brush_exists(scene, self.brush_name):
            self.report(
                {'INFO'},
                f"[CREATE BRUSH] already created: {self.brush_name}",
            )
            return {'FINISHED'}

        if ws_client.ws is None:
            self.report({'ERROR'}, "Server not connected")
            return {'CANCELLED'}

        self.brush = scene.glaze_brushes.add()
        self.brush.name = self.brush_name
        self.brush.mesh = context.scene.current_view.mesh
        self.brush.brush_type = self.brush_type
        self.brush.sv_id = context.scene.current_view.sv_id

        brush_info = {
            "brush_name": self.brush_name,
            "brush_type": self.brush_type,
            "brush_mesh": self.brush.mesh[:-4],
            "sv_id": self.brush.sv_id,
        }

        self.message = {"type": "add_brush", "data": brush_info}

        self.report(
            {'INFO'},
            f"[CREATE BRUSH] creating brush: {self.brush_name}",
        )

        ws_client.send(self.message)

        self.start_time = time.time()
        self.timeout = 30.0

        wm = context.window_manager
        self._timer = wm.event_timer_add(0.1, window=context.window)
        wm.modal_handler_add(self)

        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type != 'TIMER':
            return {'PASS_THROUGH'}

        # Timeout guard
        if time.time() - self.start_time > self.timeout:
            self.report({'ERROR'}, "Auto brush creation timed out")
            self._cleanup(context)
            return {'CANCELLED'}

        rec_message = ws_client.poll_bin_messages()
        if rec_message is None:
            return {'RUNNING_MODAL'}

        msg = from_binary(rec_message)

        if msg.get("brush icon") is not None:
            brush_icon = msg["brush icon"]

            brush_dir = os.path.join(
                context.scene.glaze_config.brushes_folder,
                self.brush.name,
            )
            os.makedirs(brush_dir, exist_ok=True)

            torchvision.utils.save_image(
                brush_icon.permute(2, 0, 1),
                os.path.join(brush_dir, "icon.png"),
            )

            self.report(
                {'INFO'},
                f"[CREATE BRUSH] done creating brush: {self.brush_name}",
            )

            self._cleanup(context)
            return {'FINISHED'}

        return {'RUNNING_MODAL'}

    def _cleanup(self, context):
        wm = context.window_manager
        if self._timer:
            wm.event_timer_remove(self._timer)
        self._timer = None


class GLAZE_OT_create_ref_brushes(bpy.types.Operator):
    """Create New Reference Type Brush"""
    bl_idname = "glaze.create_ref_brush"
    bl_label = "Add Brush"
    
    brush_name: bpy.props.StringProperty(name="Name")

    def execute(self, context):
        self.brush_type = "PreSampledReferenceBrush"

        if not self.brush_name:
            self.report({'ERROR'}, "Brush name cannot be empty")
            return {'CANCELLED'}

        obj = context.scene.current_reference_mesh

        if obj.mode != 'EDIT':
            self.report({'ERROR'}, "Reference mesh must be in Edit Mode")
            return {'CANCELLED'}

        context.scene.reference_faces.clear()
        bm = bmesh.from_edit_mesh(obj.data)
        bm.faces.ensure_lookup_table()

        self.reference_faces = []
        for f in bm.faces:
            if f.select:
                entry = context.scene.reference_faces.add()
                entry.index = f.index
                self.reference_faces.append(f.index)

        if not self.reference_faces:
            self.report(
                {'ERROR'},
                "Reference Brush requires at least one selected face"
            )
            return {'CANCELLED'}

        scene = context.scene

        if brush_exists(scene, self.brush_name):
            self.report({'INFO'}, f"Already created: {self.brush_name}")
            return {'FINISHED'}

        if ws_client.ws is None:
            self.report({'ERROR'}, "Server not connected")
            return {'CANCELLED'}

        self.brush = scene.glaze_brushes.add()
        self.brush.name = self.brush_name
        self.brush.mesh = context.scene.current_view.mesh
        self.brush.brush_type = self.brush_type
        self.brush.sv_id = context.scene.current_view.sv_id

        brush_info = {
            "brush_name": self.brush_name,
            "brush_type": self.brush_type,
            "brush_mesh": self.brush.mesh[:-4],
            "sv_id": self.brush.sv_id,
            "reference_faces": self.reference_faces,
        }

        self.message = {"type": "add_brush", "data": brush_info}

        ws_client.send(self.message)

        self.report({'INFO'}, f"[CREATE BRUSH] Creating: {self.brush_name}")

        self.start_time = time.time()
        self.timeout = 30.0
        self.received = False

        wm = context.window_manager
        self._timer = wm.event_timer_add(0.1, window=context.window)
        wm.modal_handler_add(self)

        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type != 'TIMER':
            return {'PASS_THROUGH'}

        if time.time() - self.start_time > self.timeout:
            self.report({'ERROR'}, "Brush creation timed out")
            self._cleanup(context)
            return {'CANCELLED'}

        rec_message = ws_client.poll_bin_messages()
        if rec_message is None:
            return {'RUNNING_MODAL'}

        msg = from_binary(rec_message)

        if msg.get("brush icon") is not None:
            brush_icon = msg["brush icon"]

            brush_dir = os.path.join(
                context.scene.glaze_config.brushes_folder,
                self.brush.name,
            )
            os.makedirs(brush_dir, exist_ok=True)

            torchvision.utils.save_image(
                brush_icon.permute(2, 0, 1),
                os.path.join(brush_dir, "icon.png"),
            )

            self.report(
                {'INFO'},
                f"[CREATE BRUSH] Brush created: {self.brush_name}",
            )

            self._cleanup(context)
            return {'FINISHED'}

        return {'RUNNING_MODAL'}

    def _cleanup(self, context):
        wm = context.window_manager
        if self._timer:
            wm.event_timer_remove(self._timer)
        self._timer = None


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

    
class GLAZE_OT_save_brush(bpy.types.Operator):
    bl_idname = "glaze.save_brush"
    bl_label = "Save Brush"

    brush_name: bpy.props.StringProperty()

    def execute(self, context):
        brush_info = {"brush_name": self.brush_name}
        message = {"type": "save_brush",
                    "data": brush_info}
        ws_client.send(message)
        self.report({'INFO'}, f"Saved {self.brush_name}")
        return {'FINISHED'}


def remove_item_by_name(collection, name):
    for i, item in enumerate(collection):
        if item.name == name:
            collection.remove(i)
            return True
    return False

class GLAZE_OT_remove_brush(bpy.types.Operator):
    bl_idname = "glaze.remove_brush"
    bl_label = "Remove Brush"
    bl_options = {'UNDO'}

    brush_name: bpy.props.StringProperty()

    def execute(self, context):
        remove_item_by_name(context.scene.glaze_brushes, self.brush_name)
        return {'FINISHED'}

class GLAZE_OT_FillTexture(bpy.types.Operator):
    bl_idname = "glaze.fill"
    bl_label = "Fill Texture"
    bl_options = {'REGISTER', 'UNDO'} 

    def execute(self, context):
        obj = context.scene.current_paint_mesh
        # support two modes of filling: face mode and view mode
        # face mode
        if context.scene.inference_view_settings.selection_mode == 'FACE':
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
            
            fill_info = {"target_faces": target_faces,
                         "mesh_name": base_name(context.scene.current_paint_mesh.name),
                        "brush_name": context.scene.current_brush, "high_res": context.scene.update_texture_4k}
            message = {"type": "fill_face",
                    "data": fill_info}
        if context.scene.inference_view_settings.selection_mode == 'ALL':
            fill_info = {"mesh_name": base_name(context.scene.current_paint_mesh.name),
                        "brush_name": context.scene.current_brush, "high_res": context.scene.update_texture_4k, "all": True}
            message = {"type": "fill_face",
                    "data": fill_info}
        if ws_client.ws is None:
            self.report({'ERROR'}, f"Server not connected")
            return {'FINISHED'}
        
        ws_client.send(message)

        
        self.start_time = time.time()
        self.timeout = 30.0

        self.textures_meta = {}
        self.num_completed_views = 0
        self.expected_views = None

        # ------------------------------
        # Start modal timer
        # ------------------------------
        wm = context.window_manager
        self._timer = wm.event_timer_add(0.1, window=context.window)
        wm.modal_handler_add(self)

        return {'RUNNING_MODAL'}
    # ------------------------------
    
    def modal(self, context, event):
        obj = context.scene.current_paint_mesh
        if event.type != 'TIMER':
            return {'PASS_THROUGH'}

        # Timeout guard
        if time.time() - self.start_time > self.timeout:
            self.report({'ERROR'}, "Texture fill timed out")
            self._cleanup(context)
            return {'CANCELLED'}

        while True:
            try:
                rec_message = ws_client.ws_chunks.get_nowait()
            except Empty:
                break

            if not isinstance(rec_message, bytes):
                continue

            msg = from_binary(rec_message)

            if msg.get("chunk_total") is None:
                continue

            image_name = msg.get("name")
            chunk_index = msg.get("chunk_index")
            chunk_total = msg.get("chunk_total")
            view_id = msg.get("view_id")
            num_views = msg.get("num_views")
            image_chunk = msg.get("image")

            self.expected_views = num_views

            print(f"[LOG] {image_name}: {chunk_index + 1} / {chunk_total}")

            if view_id not in self.textures_meta:
                self.textures_meta[view_id] = {}

            if chunk_index not in self.textures_meta[view_id]:
                self.textures_meta[view_id][chunk_index] = image_chunk

            # View complete
            if len(self.textures_meta[view_id]) == chunk_total:
                ordered = [
                    self.textures_meta[view_id][k]
                    for k in sorted(self.textures_meta[view_id].keys())
                ]
                image = torch.cat(ordered)
                if context.scene.update_texture_4k:
                    image = image.reshape((4096, 4096, 4))
                else:
                    image = image.reshape((1024, 1024, 4))

                image = image.cpu()
                update_texture(obj, image)

                self.num_completed_views += 1

        # All views complete
        if (
            self.expected_views is not None
            and self.num_completed_views >= self.expected_views
        ):
            self._cleanup(context)
            self.report({'INFO'}, "[FILL TEXTURE] Completed all views")
            return {'FINISHED'}

        return {'RUNNING_MODAL'}


    def _cleanup(self, context):
        wm = context.window_manager
        if self._timer:
            wm.event_timer_remove(self._timer)
        self._timer = None

class GLAZE_OT_FillALLTexture(bpy.types.Operator):
    # TODO: BUGGY
    bl_idname = "glaze.fill_all"
    bl_label = "Fill All Texture"
    bl_options = {'REGISTER', 'UNDO'} 

    _timer = None

    # -----------------------------------
    # EXECUTE: send request + init state
    # -----------------------------------
    def execute(self, context):
        self.obj = context.scene.current_paint_mesh

        if ws_client.ws is None:
            self.report({'ERROR'}, "Server not connected")
            return {'CANCELLED'}

        fill_info = {
            "mesh_name": base_name(self.obj.name),
            "brush_name": context.scene.current_brush,
            "high_res": context.scene.update_texture_4k,
        }

        self.message = {"type": "fill_all", "data": fill_info}
        ws_client.send(self.message)

        # -----------------------------------
        # Modal state
        # -----------------------------------
        self.textures_meta = {}
        self.num_completed_views = 0
        self.expected_views = None

        self.start_time = time.time()
        

        wm = context.window_manager
        self._timer = wm.event_timer_add(0.1, window=context.window)
        wm.modal_handler_add(self)

        self.report({'INFO'}, "[FILL TEXTURE] Filling all views…")

        return {'RUNNING_MODAL'}

    # -----------------------------------
    # MODAL: poll websocket chunks
    # -----------------------------------
    def modal(self, context, event):
        if event.type != 'TIMER':
            return {'PASS_THROUGH'}

        # Timeout guard
        # if time.time() - self.start_time > self.timeout:
        #     self.report({'ERROR'}, "Fill all texture timed out")
        #     self._cleanup(context)
        #     return {'CANCELLED'}

        # Drain websocket queue non-blocking
        while True:
            try:
                rec_message = ws_client.ws_chunks.get_nowait()
            except Empty:
                break

            if not isinstance(rec_message, bytes):
                continue

            msg = from_binary(rec_message)

            if msg.get("chunk_total") is None:
                continue

            image_name = msg.get("name")
            chunk_index = msg.get("chunk_index")
            chunk_total = msg.get("chunk_total")
            view_id = msg.get("view_id")
            num_views = msg.get("num_views")
            image_chunk = msg.get("image")

            self.expected_views = num_views

            print(f"[LOG] {image_name}: {chunk_index + 1} / {chunk_total}")

            if view_id not in self.textures_meta:
                self.textures_meta[view_id] = {}

            if chunk_index not in self.textures_meta[view_id]:
                self.textures_meta[view_id][chunk_index] = image_chunk

            # View complete
            if len(self.textures_meta[view_id]) == chunk_total:
                ordered = [
                    self.textures_meta[view_id][k]
                    for k in sorted(self.textures_meta[view_id].keys())
                ]

                image = torch.cat(ordered).reshape((4096, 4096, 4))
                image = image.cpu()

                self.report(
                    {'INFO'},
                    f"[FILL TEXTURE] Filling texture {view_id + 1}/{num_views}",
                )

                update_texture(self.obj, image)
                self.num_completed_views += 1

        # All views complete
        if (
            self.expected_views is not None
            and self.num_completed_views >= self.expected_views
        ):
            self._cleanup(context)
            self.report({'INFO'}, "[FILL TEXTURE] Completed all views")
            return {'FINISHED'}

        return {'RUNNING_MODAL'}

    # -----------------------------------
    # CLEANUP
    # -----------------------------------
    def _cleanup(self, context):
        wm = context.window_manager
        if self._timer:
            wm.event_timer_remove(self._timer)
        self._timer = None
    



###############################
###### TEXTURE OPERATORS ######
################################
class GLAZE_OT_SetPaintTexture(bpy.types.Operator):
    bl_idname = "glaze.set_texture"
    bl_label = "Sync Texture with server"
    bl_options = {'REGISTER', 'UNDO'} 
    
    def execute(self, context): #TODO:
        # send the current texture to server
        if ws_client.ws is None:
            self.report({'ERROR'}, f"Server not connected")
            return {'FINISHED'}
        h = w = 4096
        buffer_size = h * w * 4 
        paint_obj = context.scene.current_paint_mesh
        current_paint_texture = get_current_texture(paint_obj)
        current_texture_pixels = np.empty(buffer_size, dtype=np.float32)
        current_paint_texture.pixels.foreach_get(current_texture_pixels)
        msgs = send_large_image("texture", "set_texture", current_texture_pixels)
        for msg in msgs:
            msg["mesh_name"] = context.scene.current_paint_mesh.name
            #self.report()({'INFO'}, f"Sending {msg["mesh_name"]} texture to server...")
            ws_client.send(to_binary(msg)) 
        return {"FINISHED"}


class GLAZE_OT_ClearAllPaintTexture(bpy.types.Operator):
    bl_idname = "glaze.clear_all_texture"
    bl_label = "Clear All Texture"
    bl_options = {'REGISTER', 'UNDO'} 
    
    def execute(self, context): 
        clear_texture(context.scene.current_paint_mesh)
        mesh_info = {"paint_mesh_name": context.scene.current_paint_mesh.name}
        payload = {"type": "clear_all_texture",
                   "data": mesh_info}
        ws_client.send(payload)
        
class GLAZE_OT_ClearPaintTexture(bpy.types.Operator):
    bl_idname = "glaze.clear_texture"
    bl_label = "Clear Selected Face Texture"
    bl_options = {'REGISTER', 'UNDO'} 
    
    def execute(self, context): 
        clear_texture(context.scene.current_paint_mesh)
        mesh_info = {"paint_mesh_name": context.scene.current_paint_mesh.name}
        payload = {"type": "clear_all_texture",
                   "data": mesh_info}
        ws_client.send(payload)
        
        
class GLAZE_OT_Undo_Fill(bpy.types.Operator):
    """Undo Fill Operation, you can only consecutively undo twice"""
    bl_idname = "glaze.undo_texture"
    bl_label = "Undo Fill"
    bl_options = {'REGISTER', 'UNDO'} 
    
    def execute(self, context):
        undo_texture()
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

    
class GLAZE_OT_show_brush_menu(bpy.types.Operator):
    bl_idname = "glaze.show_brush_menu"
    bl_label = "Show Brush Menu"

    brush_name: bpy.props.StringProperty()
    
    @classmethod
    def description(cls, context, properties):
        return f"{properties.brush_name}"

    def invoke(self, context, event):
        context.window_manager.popup_menu(
            self.draw_menu,
            title="Brush Actions",
            icon='BRUSH_DATA'
        )
        return {'FINISHED'}

    def draw_menu(self, menu, context):
        layout = menu.layout
        
        layout.operator(
            "glaze.set_brush",
            icon='FILE_TICK'
        ).brush_name = self.brush_name

        layout.operator(
            "glaze.save_brush",
            icon='FILE_TICK'
        ).brush_name = self.brush_name

        layout.operator(
            "glaze.remove_brush",
            icon='TRASH'
        ).brush_name = self.brush_name


class GLAZE_OT_ReloadAddon(bpy.types.Operator):
    """Reload all Blender scripts (useful during development)"""
    bl_idname = "glaze.glaze_reload_addon"
    bl_label = "🔄 Reload Add-on"

    def execute(self, context):
        bpy.ops.script.reload()
        self.report({'INFO'}, "Scripts reloaded successfully!")
        return {'FINISHED'}


class MATERIAL_OT_toggle_normal(bpy.types.Operator):
    """Toggle Normal Map Connection"""
    bl_idname = "material.toggle_normal_map"
    bl_label = "Toggle Normal Map"

    def execute(self, context):
        mat = context.object.active_material
        if not mat:
            self.report({"WARNING"}, "No active material")
            return {'CANCELLED'}

        if is_normal_connected(mat):
            disconnect_normal(mat)
        else:
            connect_normal(mat)

        return {'FINISHED'}
