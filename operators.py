import bpy
import bmesh

from .backend import clear_all_texture, load_paint_texture, send_clear_request, send_fill_request, sync_paint_texture
from .utils.config import load_glaze_config_from_yaml
from .utils.mesh import load_mesh, apply_texture, undo_texture, \
    is_normal_connected, disconnect_normal, connect_normal, set_view_center, base_name
from .utils.io import from_binary
from .client import ws_client

import os
import re
import torchvision


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
        context.scene.glaze_session.loaded_paint_texture = ""
        mesh_info = {"mesh_name": base_name(paint_mesh.name)[:-4], "paint_mesh_name": paint_mesh.name,
                     "high_res": context.scene.update_texture_4k}
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
        ws_client.configure(cfg.server_url)

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
        if context.scene.current_reference_mesh is None:
            self.report({'ERROR'}, "Load a reference mesh before applying a reference image")
            return {'CANCELLED'}

        curr_view = context.scene.current_view
        single_view_path = curr_view.image_path
        if not single_view_path:
            self.report({'ERROR'}, "Choose a reference image first")
            return {'CANCELLED'}

        curr_view.mesh = base_name(context.scene.current_reference_mesh.name)
        texture_path = single_view_path
        match = re.match(r"view(\d+)\.", os.path.basename(single_view_path))

        if match:
            curr_view.sv_id = int(match.group(1))
            single_view_texture_path = os.path.join(
                context.scene.glaze_config.single_views_texture_folder,
                curr_view.mesh[:-4],
                f"view{curr_view.sv_id:04d}.png",
            )
            if os.path.exists(single_view_texture_path):
                texture_path = single_view_texture_path
        else:
            curr_view.sv_id = -1

        apply_texture(context.scene.current_reference_mesh, texture_path, suffix='ref')
        self.report({'INFO'}, f"Applied reference image to {context.scene.current_reference_mesh.name}")
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


class GLAZE_OT_LoadPaintTexture(bpy.types.Operator):
    """Load a texture onto the current paint mesh"""
    bl_idname = "glaze.load_paint_texture"
    bl_label = "Load Paint Texture"

    filepath: bpy.props.StringProperty(subtype="FILE_PATH")

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        try:
            load_paint_texture(
                context,
                self.filepath,
                sync_to_server=context.scene.glaze_session.auto_sync_texture,
            )
        except RuntimeError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}

        self.report({'INFO'}, f"Loaded paint texture {os.path.basename(self.filepath)}")
        return {'FINISHED'}


class GLAZE_OT_ReconnectServer(bpy.types.Operator):
    """Reconnect to the websocket server"""
    bl_idname = "glaze.reconnect_server"
    bl_label = "Reconnect Server"

    def execute(self, context):
        ws_client.restart(context.scene.glaze_config.server_url)
        self.report({'INFO'}, f"Reconnecting to {context.scene.glaze_config.server_url}")
        return {'FINISHED'}


################################## Brush Operators Utils ####################################

def brush_exists(scene, name):
    """Return ``True`` when ``scene.glaze_brushes`` already contains ``name``."""
    for b in scene.glaze_brushes:
        if b.name == name:
            return True
    return False


def start_brush_listener(context, brush_name, brush_type, brush_sv_id):
    """Poll for a generated brush icon and register the brush when it arrives.

    Side Effects:
        Creates the brush icon directory on disk, writes ``icon.png``, appends a
        brush entry to ``scene.glaze_brushes``, and registers a Blender timer.
    """
    def _poll():
        rec_message = ws_client.poll_bin_messages()
        if rec_message is None:
            return 0.1  # keep polling
        msg = from_binary(rec_message)
        if msg.get("brush icon") is not None:
            # save brush icon
            brush_icon = msg["brush icon"]
            brush_dir = os.path.join(
                context.scene.glaze_config.brushes_folder,
                brush_name,
            )
            os.makedirs(brush_dir, exist_ok=True)
            torchvision.utils.save_image(
                brush_icon.permute(2, 0, 1),
                os.path.join(brush_dir, "icon.png"),
            )
            # add brush to the scene
            brush = context.scene.glaze_brushes.add()
            brush.name = brush_name
            brush.sv_id = brush_sv_id
            brush.brush_type = brush_type
            print(f"[CREATE BRUSH] done creating brush: {brush_name}")
            return None  # stop timer
        return 0.1  # keep polling
    bpy.app.timers.register(_poll)

   
class GLAZE_OT_create_auto_brushes(bpy.types.Operator):
    """Create New Auto Type Brush"""
    bl_idname = "glaze.create_auto_brush"
    bl_label = "Add Brush"
    brush_name: bpy.props.StringProperty(name="Name")
    
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

        brush_info = {
            "brush_name": self.brush_name,
            "brush_type": self.brush_type,
            "brush_mesh": context.scene.current_view.mesh[:-4],
            "sv_id": context.scene.current_view.sv_id,
        }

        self.message = {"type": "add_brush", "data": brush_info}

        self.report(
            {'INFO'},
            f"[CREATE BRUSH] creating brush: {self.brush_name}",
        )

        ws_client.send(self.message)
        start_brush_listener(context, self.brush_name, self.brush_type, context.scene.current_view.sv_id)
        return {'FINISHED'}


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

        brush_info = {
            "brush_name": self.brush_name,
            "brush_type": self.brush_type,
            "brush_mesh": context.scene.current_view.mesh[:-4],
            "sv_id": context.scene.current_view.sv_id,
            "reference_faces": self.reference_faces,
        }

        self.message = {"type": "add_brush", "data": brush_info}

        ws_client.send(self.message)
        start_brush_listener(context, self.brush_name, self.brush_type, context.scene.current_view.sv_id)
        return {'FINISHED'}


class GLAZE_OT_SetBrush(bpy.types.Operator):
    """Set the active brush used for fill requests."""
    bl_idname = "glaze.set_brush"
    bl_label = "Set Brush"

    brush_name: bpy.props.StringProperty(name="Name")

    def execute(self, context):
        self.report({'INFO'}, f"Setting the current brush as: {self.brush_name}")
        context.scene.current_brush = self.brush_name
        return {"FINISHED"}

class GLAZE_OT_ClearBrushLib(bpy.types.Operator):
    """Clear the in-memory brush library for the current scene."""
    bl_idname = "glaze.clear_brush_lib"
    bl_label = "Clear Brush Library"

    def execute(self, context):
        context.scene.current_brush = ""
        context.scene.glaze_brushes.clear()
        self.report({'INFO'}, f"Clear All Brushes")
        return {"FINISHED"}

    
class GLAZE_OT_save_brush(bpy.types.Operator):
    """Ask the server to persist a named brush."""
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
    """Remove the first item named ``name`` from a Blender collection."""
    for i, item in enumerate(collection):
        if item.name == name:
            collection.remove(i)
            return True
    return False

class GLAZE_OT_remove_brush(bpy.types.Operator):
    """Remove a brush entry from the scene collection."""
    bl_idname = "glaze.remove_brush"
    bl_label = "Remove Brush"
    bl_options = {'UNDO'}

    brush_name: bpy.props.StringProperty()

    def execute(self, context):
        remove_item_by_name(context.scene.glaze_brushes, self.brush_name)
        return {'FINISHED'}


################################## Texture Operators ####################################

class GLAZE_OT_FillTexture(bpy.types.Operator):
    """Send a texture generation request for the selected faces."""
    bl_idname = "glaze.fill"
    bl_label = "Generate Texture"
    bl_options = {'REGISTER', 'UNDO'} 

    def execute(self, context):
        try:
            target_faces = send_fill_request(context)
        except RuntimeError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}

        context.scene.target_faces.clear()
        for face_index in target_faces:
            entry = context.scene.target_faces.add()
            entry.index = face_index

        self.report({'INFO'}, "Texture generation request sent")
        return {'FINISHED'}


class GLAZE_OT_SetPaintTexture(bpy.types.Operator):
    """Push the active paint texture to the server."""
    bl_idname = "glaze.set_texture"
    bl_label = "Sync Texture with server"
    bl_options = {'REGISTER', 'UNDO'} 
    
    def execute(self, context):
        try:
            sync_paint_texture(context)
        except RuntimeError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}

        self.report({'INFO'}, "Paint texture synced to server")
        return {"FINISHED"}


class GLAZE_OT_ClearAllPaintTexture(bpy.types.Operator):
    """Clear the entire active paint texture."""
    bl_idname = "glaze.clear_all_texture"
    bl_label = "Clear All Texture"
    bl_options = {'REGISTER', 'UNDO'} 
    
    def execute(self, context): 
        try:
            clear_all_texture(context)
        except RuntimeError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}

        self.report({'INFO'}, "Cleared the active paint texture")
        return {"FINISHED"}

        
class GLAZE_OT_ClearPaintTexture(bpy.types.Operator):
    """Send a clear request for the selected faces."""
    bl_idname = "glaze.clear_texture"
    bl_label = "Clear Selected Face Texture"
    bl_options = {'REGISTER', 'UNDO'} 
    
    def execute(self, context):
        try:
            target_faces = send_clear_request(context)
        except RuntimeError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}

        context.scene.target_faces.clear()
        for face_index in target_faces:
            entry = context.scene.target_faces.add()
            entry.index = face_index

        self.report({'INFO'}, "Clear request sent to server")
        return {'FINISHED'}

        
class GLAZE_OT_Undo_Fill(bpy.types.Operator):
    """Undo Fill Operation, you can only consecutively undo twice"""
    bl_idname = "glaze.undo_texture"
    bl_label = "Undo Fill"
    bl_options = {'REGISTER', 'UNDO'} 
    
    def execute(self, context):
        undo_texture()
        return {"FINISHED"}
        

class GLAZE_OT_show_brush_menu(bpy.types.Operator):
    """Open the context menu for a brush library entry."""
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
    
