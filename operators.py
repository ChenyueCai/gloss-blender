# SPDX-FileCopyrightText: Copyright (c) 2025 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import bpy
import bmesh

from .backend import clear_all_texture, collect_selected_face_indices, load_paint_texture, purge_duplicates, send_clear_request, send_fill_request
from .utils.config import load_gloss_config_from_yaml
from .utils.mesh import load_mesh, apply_texture, undo_texture, \
    set_view_center, base_name, strip_to_diffuse_normal
from .utils.io import decode_pixels
from .client import ws_client
from .protocol import (
    DTYPE_UINT8,
    MSG_BRUSH_ICON,
    STATE_ERROR,
    STATE_PREPARING,
    STATE_READY,
)
from .backend import (
    BRUSH_TIMEOUT_S,
    push_texture_to_server,
    set_brush_state,
)

import os
import re
import time

import numpy as np


def _mesh_stem_from_path(filepath):
    """Derive a mesh stem from ``filepath``.

    glTF assets are commonly distributed as ``<asset_name>/scene.gltf`` (with
    sibling ``scene.bin`` / ``textures/``). When the picked file's stem is the
    generic ``scene``, use the parent directory name so the mesh ends up named
    after the asset rather than the literal ``scene`` file.
    """
    stem = os.path.splitext(os.path.basename(filepath))[0]
    if stem.lower() == "scene":
        parent = os.path.basename(os.path.dirname(filepath))
        if parent:
            return parent
    return stem


class GLOSS_OT_LoadReferenceMesh(bpy.types.Operator):
    """Load Reference Mesh"""
    bl_idname = "gloss.load_reference_mesh"
    bl_label = "Load Reference Mesh"
    bl_options = {'UNDO'}

    filepath: bpy.props.StringProperty(subtype="FILE_PATH", options={'SKIP_SAVE'})
    directory: bpy.props.StringProperty(subtype="DIR_PATH", options={'HIDDEN', 'SKIP_SAVE'})
    filter_glob: bpy.props.StringProperty(default="*.obj;*.gltf;*.glb", options={'HIDDEN'})

    def execute(self, context):
        print("Loading reference mesh:", self.filepath)
        mesh_name = _mesh_stem_from_path(self.filepath) + "_ref"
        purge_duplicates(context)
        ref_mesh = load_mesh(mesh_path=self.filepath, name=mesh_name, remove_existing=True)
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
        folder = context.scene.gloss_config.mesh_folder
        if folder:
            folder = os.path.abspath(bpy.path.abspath(os.path.expanduser(folder)))
            if not os.path.isdir(folder):
                self.report({'ERROR'}, f"Mesh folder does not exist: {folder}")
                return {'CANCELLED'}
            self.properties.property_unset("filepath")
            self.directory = os.path.join(folder, "")
        else:
            self.properties.property_unset("filepath")
            self.properties.property_unset("directory")
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}


class GLOSS_OT_LoadPaintMesh(bpy.types.Operator):
    """Load Paint Mesh"""
    bl_idname = "gloss.load_paint_mesh"
    bl_label = "Load Paint Mesh"
    bl_options = {'UNDO'}

    filepath: bpy.props.StringProperty(subtype="FILE_PATH", options={'SKIP_SAVE'})
    directory: bpy.props.StringProperty(subtype="DIR_PATH", options={'HIDDEN', 'SKIP_SAVE'})
    filter_glob: bpy.props.StringProperty(default="*.obj;*.gltf;*.glb", options={'HIDDEN'})

    def execute(self, context):
        print("Loading paint mesh:", self.filepath)
        mesh_name = _mesh_stem_from_path(self.filepath) + "_pnt"
        purge_duplicates(context)
        paint_mesh = load_mesh(mesh_path=self.filepath, name=mesh_name, remove_existing=True)
        paint_mesh.location = (1.5, 0, 0)
        context.scene.current_paint_mesh = paint_mesh
        screen = context.window.screen
        areas = [a for a in screen.areas if a.type == 'VIEW_3D']
        left_area, right_area = areas[0], areas[1]
        set_view_center(paint_mesh, left_area)
        strip_to_diffuse_normal(paint_mesh)
        # remove the basecolor slots
        apply_texture(paint_mesh, None, suffix='paint')
        context.scene.gloss_session.loaded_paint_texture = ""
        mesh_info = {"mesh_name": base_name(paint_mesh.name)[:-4], "paint_mesh_name": paint_mesh.name,
                     "high_res": context.scene.update_texture_4k}
        message = {"type": "add_pnt_mesh", 
                   "data": mesh_info}
        ws_client.send(message)
        return {'FINISHED'}

    def invoke(self, context, event):
        folder = context.scene.gloss_config.mesh_folder
        if folder:
            folder = os.path.abspath(bpy.path.abspath(os.path.expanduser(folder)))
            if not os.path.isdir(folder):
                self.report({'ERROR'}, f"Mesh folder does not exist: {folder}")
                return {'CANCELLED'}
            self.properties.property_unset("filepath")
            self.directory = os.path.join(folder, "")
        else:
            self.properties.property_unset("filepath")
            self.properties.property_unset("directory")
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}


class GLOSS_OT_LoadConfig(bpy.types.Operator):
    """Load Gloss Session Config"""
    bl_idname = "gloss.load_config"
    bl_label = "Load Gloss Config"

    filepath: bpy.props.StringProperty(subtype='FILE_PATH')

    def execute(self, context):
        scene = context.scene
        cfg = scene.gloss_config

        load_gloss_config_from_yaml(self.filepath)
        ws_client.configure(cfg.server_url)

        self.report({'INFO'}, f"Config loaded from {self.filepath}")
        return {'FINISHED'}

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}


class GLOSS_OT_SetReference(bpy.types.Operator):
    """Set Reference Image for Reference Mesh"""
    bl_idname = "gloss.set_reference"
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

        mesh_name = base_name(context.scene.current_reference_mesh.name)
        texture_folder = context.scene.gloss_config.single_views_texture_folder
        match = re.match(r"view(\d+)\.", os.path.basename(single_view_path))

        if not match or not texture_folder:
            self.report({'ERROR'}, "requested texture does not exist")
            return {'CANCELLED'}

        sv_id = int(match.group(1))
        texture_path = os.path.join(
            bpy.path.abspath(texture_folder),
            mesh_name[:-4],
            f"view{sv_id:04d}.png",
        )
        if not os.path.isfile(texture_path):
            self.report({'ERROR'}, "requested texture does not exist")
            return {'CANCELLED'}

        apply_texture(context.scene.current_reference_mesh, texture_path, suffix='ref')
        curr_view.mesh = mesh_name
        curr_view.sv_id = sv_id
        self.report({'INFO'}, f"Applied reference image to {context.scene.current_reference_mesh.name}")
        return {'FINISHED'}


class GLOSS_OT_LoadReferenceView(bpy.types.Operator):
    """Load Reference View"""
    bl_idname = "gloss.load_reference_view"
    bl_label = "Select Reference View"

    filepath: bpy.props.StringProperty(subtype="FILE_PATH", options={'SKIP_SAVE'})
    directory: bpy.props.StringProperty(subtype="DIR_PATH", options={'HIDDEN', 'SKIP_SAVE'})

    def invoke(self, context, event):
        folder = context.scene.gloss_config.single_views_folder
        if folder:
            folder = os.path.abspath(bpy.path.abspath(os.path.expanduser(folder)))
            if not os.path.isdir(folder):
                self.report({'ERROR'}, f"Reference images folder does not exist: {folder}")
                return {'CANCELLED'}
            # Blender prioritizes a set filepath over directory when opening the browser.
            self.properties.property_unset("filepath")
            self.directory = os.path.join(folder, "")
        else:
            self.properties.property_unset("filepath")
            self.properties.property_unset("directory")
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        context.scene.current_view.image_path = self.filepath
        return {'FINISHED'}


class GLOSS_OT_LoadPaintTexture(bpy.types.Operator):
    """Load a texture onto the current paint mesh"""
    bl_idname = "gloss.load_paint_texture"
    bl_label = "Load Paint Texture"

    filepath: bpy.props.StringProperty(subtype="FILE_PATH")

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        try:
            load_paint_texture(context, self.filepath)
        except RuntimeError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}

        self.report({'INFO'}, f"Loaded paint texture {os.path.basename(self.filepath)}")
        return {'FINISHED'}


class GLOSS_OT_ReconnectServer(bpy.types.Operator):
    """Reconnect to the websocket server"""
    bl_idname = "gloss.reconnect_server"
    bl_label = "Reconnect Server"

    def execute(self, context):
        ws_client.restart(context.scene.gloss_config.server_url)
        self.report({'INFO'}, f"Reconnecting to {context.scene.gloss_config.server_url}")
        return {'FINISHED'}


################################## Brush Operators Utils ####################################

def brush_exists(scene, name):
    """Return ``True`` when ``scene.gloss_brushes`` already contains ``name``."""
    for b in scene.gloss_brushes:
        if b.name == name:
            return True
    return False


#: brush_name -> deadline, for brushes whose icon has not arrived yet. The
#: per-request deadline below and the panel guard in backend.py must agree on
#: how long a brush may take, so BRUSH_TIMEOUT_S is defined there.
_PENDING_BRUSHES = {}


def write_icon_png(pixels, filepath):
    """Write an HxWx{3,4} array of ``[0, 1]`` floats to ``filepath`` as PNG.

    Uses Blender's own image API rather than torchvision: this runs on the UI
    thread, and importing/calling torchvision cost far more than writing a
    thumbnail. Blender's pixel buffer is bottom-up, hence the flip.
    """
    height, width = pixels.shape[0], pixels.shape[1]
    if pixels.shape[2] == 3:
        alpha = np.ones((height, width, 1), dtype=np.float32)
        pixels = np.concatenate([pixels, alpha], axis=2)

    image = bpy.data.images.new(
        name=os.path.basename(filepath), width=width, height=height, alpha=True
    )
    try:
        image.pixels.foreach_set(np.flipud(pixels).astype(np.float32).ravel())
        image.filepath_raw = filepath
        image.file_format = "PNG"
        image.save()
    finally:
        bpy.data.images.remove(image)


def _finish_brush(context, brush_name, brush_type, brush_sv_id):
    """Register a completed brush in the scene collection."""
    if brush_exists(context.scene, brush_name):
        return
    brush = context.scene.gloss_brushes.add()
    brush.name = brush_name
    brush.sv_id = brush_sv_id
    brush.brush_type = brush_type


def start_brush_listener(context, brush_name, brush_type, brush_sv_id):
    """Route the icon for ``brush_name`` to disk and register the brush.

    Replaces a polling timer that competed with the status poller for a shared
    inbox and dropped icons. This registers a handler on the router instead, so
    the icon reaches exactly one consumer, and arms a deadline so a lost reply
    surfaces as an error rather than polling forever.

    Side Effects:
        Creates the brush icon directory, writes ``icon.png``, appends to
        ``scene.gloss_brushes``, and registers a router handler plus a timer.
    """
    _PENDING_BRUSHES[brush_name] = time.time() + BRUSH_TIMEOUT_S

    def _on_icon(payload):
        # A payload naming a different brush belongs to another request.
        payload_name = payload.get("brush_name")
        if payload_name is not None and payload_name != brush_name:
            return

        icon = payload.get("image")
        if icon is None:
            icon = payload.get("brush icon")  # legacy untagged server
        if icon is None:
            return

        ws_client.unregister_handler(MSG_BRUSH_ICON, _on_icon)
        _PENDING_BRUSHES.pop(brush_name, None)

        try:
            pixels = decode_pixels(icon, payload.get("dtype", DTYPE_UINT8))
            brush_dir = os.path.join(
                bpy.path.abspath(context.scene.gloss_config.brushes_folder),
                brush_name,
            )
            os.makedirs(brush_dir, exist_ok=True)
            write_icon_png(np.asarray(pixels), os.path.join(brush_dir, "icon.png"))
            _finish_brush(context, brush_name, brush_type, brush_sv_id)
            set_brush_state(STATE_READY, f"brush ready: {brush_name}")
            print(f"[CREATE BRUSH] done creating brush: {brush_name}")
        except Exception as exc:
            set_brush_state(STATE_ERROR, f"{brush_name}: {exc}")
            print(f"[CREATE BRUSH] failed to store icon for {brush_name}: {exc}")

    ws_client.register_handler(MSG_BRUSH_ICON, _on_icon)

    def _deadline():
        deadline = _PENDING_BRUSHES.get(brush_name)
        if deadline is None:
            return None  # icon arrived; the handler already cleaned up
        if time.time() < deadline:
            return 1.0
        _PENDING_BRUSHES.pop(brush_name, None)
        ws_client.unregister_handler(MSG_BRUSH_ICON, _on_icon)
        set_brush_state(STATE_ERROR, f"timed out creating {brush_name}")
        print(f"[CREATE BRUSH] timed out waiting for icon: {brush_name}")
        return None

    bpy.app.timers.register(_deadline)
    set_brush_state(STATE_PREPARING, f"creating brush: {brush_name}")


class GLOSS_OT_create_auto_brushes(bpy.types.Operator):
    """Create New Auto Type Brush"""
    bl_idname = "gloss.create_auto_brush"
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
            "cam_dist": context.scene.brush_cam_dist,
            "cam_fov": context.scene.brush_cam_fov,
        }

        self.message = {"type": "add_brush", "data": brush_info}

        self.report(
            {'INFO'},
            f"[CREATE BRUSH] creating brush: {self.brush_name}",
        )

        ws_client.send(self.message)
        start_brush_listener(context, self.brush_name, self.brush_type, context.scene.current_view.sv_id)
        return {'FINISHED'}


class GLOSS_OT_create_ref_brushes(bpy.types.Operator):
    """Create New Reference Type Brush"""
    bl_idname = "gloss.create_ref_brush"
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

        bm = bmesh.from_edit_mesh(obj.data)
        bm.faces.ensure_lookup_table()
        self.reference_faces = [f.index for f in bm.faces if f.select]

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
            "cam_dist": context.scene.brush_cam_dist,
            "cam_fov": context.scene.brush_cam_fov,
            "reference_faces": self.reference_faces,
        }

        self.message = {"type": "add_brush", "data": brush_info}

        ws_client.send(self.message)
        start_brush_listener(context, self.brush_name, self.brush_type, context.scene.current_view.sv_id)
        return {'FINISHED'}


class GLOSS_OT_SetBrush(bpy.types.Operator):
    """Set the active brush used for fill requests."""
    bl_idname = "gloss.set_brush"
    bl_label = "Set Brush"

    brush_name: bpy.props.StringProperty(name="Name")

    def execute(self, context):
        self.report({'INFO'}, f"Setting the current brush as: {self.brush_name}")
        context.scene.current_brush = self.brush_name
        return {"FINISHED"}

class GLOSS_OT_ClearBrushLib(bpy.types.Operator):
    """Clear the in-memory brush library for the current scene."""
    bl_idname = "gloss.clear_brush_lib"
    bl_label = "Clear Brush Library"

    def execute(self, context):
        context.scene.current_brush = ""
        context.scene.gloss_brushes.clear()
        self.report({'INFO'}, f"Clear All Brushes")
        return {"FINISHED"}


class GLOSS_OT_RefreshBrushLib(bpy.types.Operator):
    """Rescan the brushes folder on disk and sync ``scene.gloss_brushes``.

    Adds an entry for every ``<brushes_folder>/<name>/icon.png`` not already
    in the library, and prunes entries whose icon no longer exists on disk.
    Brush metadata other than the name (mesh, sv_id, brush_type) is left at
    its property defaults for entries discovered on disk, since those fields
    aren't persisted alongside the icon.
    """
    bl_idname = "gloss.refresh_brush_lib"
    bl_label = "Refresh Brush Library"

    def execute(self, context):
        scene = context.scene
        folder = scene.gloss_config.brushes_folder
        if not folder or not os.path.isdir(bpy.path.abspath(folder)):
            self.report({'ERROR'}, "Brushes folder is not set or does not exist")
            return {'CANCELLED'}
        folder = bpy.path.abspath(folder)

        on_disk = set()
        for entry in os.listdir(folder):
            brush_dir = os.path.join(folder, entry)
            if os.path.isdir(brush_dir) and os.path.isfile(os.path.join(brush_dir, "icon.png")):
                on_disk.add(entry)

        in_memory = {b.name for b in scene.gloss_brushes}

        for stale in list(in_memory - on_disk):
            remove_item_by_name(scene.gloss_brushes, stale)
            if scene.current_brush == stale:
                scene.current_brush = ""

        added = 0
        for name in sorted(on_disk - in_memory):
            brush = scene.gloss_brushes.add()
            brush.name = name
            added += 1

        removed = len(in_memory - on_disk)
        self.report(
            {'INFO'},
            f"Brush library refreshed (+{added} / -{removed}, total {len(scene.gloss_brushes)})",
        )
        return {'FINISHED'}

    
class GLOSS_OT_save_brush(bpy.types.Operator):
    """Ask the server to persist a named brush."""
    bl_idname = "gloss.save_brush"
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

class GLOSS_OT_remove_brush(bpy.types.Operator):
    """Remove a brush entry from the scene collection."""
    bl_idname = "gloss.remove_brush"
    bl_label = "Remove Brush"
    bl_options = {'UNDO'}

    brush_name: bpy.props.StringProperty()

    def execute(self, context):
        remove_item_by_name(context.scene.gloss_brushes, self.brush_name)
        return {'FINISHED'}


################################## Texture Operators ####################################

class GLOSS_OT_FillTexture(bpy.types.Operator):
    """Send a texture generation request for the selected faces."""
    bl_idname = "gloss.fill"
    bl_label = "Generate Texture"
    bl_options = {'REGISTER', 'UNDO'} 

    def execute(self, context):
        try:
            send_fill_request(context)
        except RuntimeError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}

        self.report({'INFO'}, "Texture generation request sent")
        return {'FINISHED'}


class GLOSS_OT_ShowFaceIds(bpy.types.Operator):
    """Copy a comma-separated list of the selected face IDs to the clipboard."""
    bl_idname = "gloss.show_face_ids"
    bl_label = "Show Face IDs"
    bl_options = {'REGISTER'}

    def execute(self, context):
        obj = context.edit_object or context.scene.current_paint_mesh
        if obj is None or obj.type != 'MESH' or obj.mode != 'EDIT':
            self.report({'ERROR'}, "Enter Edit Mode on a mesh and select faces")
            return {'CANCELLED'}

        ids = collect_selected_face_indices(obj)
        if not ids:
            self.report({'WARNING'}, "No faces selected")
            return {'CANCELLED'}

        text = ", ".join(str(i) for i in ids)
        context.window_manager.clipboard = text
        self.report({'INFO'}, f"Face IDs ({len(ids)}): {text}")
        return {'FINISHED'}


class GLOSS_OT_ClearAllPaintTexture(bpy.types.Operator):
    """Clear the entire active paint texture."""
    bl_idname = "gloss.clear_all_texture"
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

        
class GLOSS_OT_ClearPaintTexture(bpy.types.Operator):
    """Send a clear request for the selected faces."""
    bl_idname = "gloss.clear_texture"
    bl_label = "Clear Selected Face Texture"
    bl_options = {'REGISTER', 'UNDO'} 
    
    def execute(self, context):
        try:
            send_clear_request(context)
        except RuntimeError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}

        self.report({'INFO'}, "Clear request sent to server")
        return {'FINISHED'}

        
class GLOSS_OT_Undo_Fill(bpy.types.Operator):
    """Undo Fill Operation, you can only consecutively undo twice"""
    bl_idname = "gloss.undo_texture"
    bl_label = "Undo Fill"
    bl_options = {'REGISTER', 'UNDO'} 
    
    def execute(self, context):
        try:
            undo_texture()
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}

        # The server keeps no history of its own, so the restored texture is
        # pushed to it. Without this the server would keep conditioning the
        # next stroke on the state the user just undid.
        if not push_texture_to_server(context, reason="after undo"):
            self.report({'WARNING'},
                        "Undone locally, but the server copy is now stale")
            return {'FINISHED'}

        self.report({'INFO'}, "Undone and synced to server")
        return {"FINISHED"}
        

class GLOSS_OT_show_brush_menu(bpy.types.Operator):
    """Open the context menu for a brush library entry."""
    bl_idname = "gloss.show_brush_menu"
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
            "gloss.set_brush",
            icon='FILE_TICK'
        ).brush_name = self.brush_name

        layout.operator(
            "gloss.save_brush",
            icon='FILE_TICK'
        ).brush_name = self.brush_name

        layout.operator(
            "gloss.remove_brush",
            icon='TRASH'
        ).brush_name = self.brush_name


class GLOSS_OT_Purge(bpy.types.Operator):
    """Remove duplicate datablocks (.001, .002, ...) and their related images"""
    bl_idname = "gloss.purge"
    bl_label = "Purge Duplicates"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        removed = purge_duplicates(context)
        self.report(
            {'INFO'},
            (f"Purged {removed['objects']} obj, {removed['meshes']} mesh, "
             f"{removed['materials']} mat, {removed['images']} img"),
        )
        return {'FINISHED'}


class GLOSS_OT_ReloadAddon(bpy.types.Operator):
    """Reload all Blender scripts (useful during development)"""
    bl_idname = "gloss.gloss_reload_addon"
    bl_label = "🔄 Reload Add-on"

    def execute(self, context):
        bpy.ops.script.reload()
        self.report({'INFO'}, "Scripts reloaded successfully!")
        return {'FINISHED'}
