import re
import threading
import time
from queue import Empty

import bmesh
import bpy
import numpy as np
import torch

from .client import ws_client
from .utils.io import from_binary, send_large_image, to_binary
from .utils.mesh import (
    apply_texture,
    base_name,
    binarize_paint_alpha,
    clear_faces_local,
    clear_texture,
    get_current_texture,
    update_texture,
)

_TEXTURE_STATE = {
    "active": False,
    "textures_meta": {},
    "expected_views": None,
    "num_completed_views": 0,
    "obj": None,
    "soft_merge": True,
    "start_time": None,
}


def server_status_text():
    """Return the websocket status string shown in the panel."""
    if ws_client.is_connected:
        return f"Connected: {ws_client.uri}"
    if ws_client.last_error:
        return f"Disconnected: {ws_client.last_error}"
    return ws_client.status


def server_status_icon():
    """Return the Blender icon id that matches the websocket state."""
    return "CHECKMARK" if ws_client.is_connected else "ERROR"


_DUP_NAME_RE = re.compile(r"\.\d{3,}(?:\.|$)")


def purge_duplicates(context):
    """Remove ORPHAN datablocks whose names contain Blender duplicate suffixes.

    Catches names like ``mesh.001``, ``mesh.001.paint.png``, and
    ``mesh.bar.001`` across objects, meshes, materials, and images. Only
    datablocks with no remaining users are removed — actively-linked ones
    are kept, so loading a second mesh whose internal asset names collide
    with the first's (and therefore got auto-suffixed ``.001`` by the
    importer) doesn't wipe its materials, textures, or mesh data. Returns
    a dict with the per-collection removal counts.
    """
    removed = {"objects": 0, "meshes": 0, "materials": 0, "images": 0}

    for key, coll in (
        ("objects", bpy.data.objects),
        ("meshes", bpy.data.meshes),
        ("materials", bpy.data.materials),
        ("images", bpy.data.images),
    ):
        targets = [
            item for item in coll if _DUP_NAME_RE.search(item.name) and item.users == 0
        ]
        for item in targets:
            coll.remove(item, do_unlink=True)
        removed[key] = len(targets)

    scene = context.scene
    if (
        scene.current_paint_mesh is not None
        and scene.current_paint_mesh.name not in bpy.data.objects
    ):
        scene.current_paint_mesh = None
    if (
        scene.current_reference_mesh is not None
        and scene.current_reference_mesh.name not in bpy.data.objects
    ):
        scene.current_reference_mesh = None

    return removed


def parse_anchor_face_ids(text):
    """Parse a comma/space separated string of face indices into a list of ints.

    Invalid tokens are skipped silently so a stray comma or space does not
    break the request.
    """
    if not text:
        return []
    ids = []
    for tok in text.replace(";", ",").split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            ids.append(int(tok))
        except ValueError:
            continue
    return ids


def collect_selected_face_indices(obj):
    """Return selected face indices for an edit-mode mesh object.

    Args:
        obj: Blender object to inspect.

    Returns:
        list[int]: Selected face indices, or an empty list when ``obj`` is
        missing, is not a mesh, or is not in edit mode.
    """
    if obj is None or obj.type != "MESH" or obj.mode != "EDIT":
        return []

    selected_faces = []
    bm = bmesh.from_edit_mesh(obj.data)
    bm.faces.ensure_lookup_table()
    for face in bm.faces:
        if face.select:
            selected_faces.append(face.index)
    return selected_faces


def reset_texture_state():
    """Clear the in-progress server texture update state."""
    _TEXTURE_STATE.update(
        {
            "active": False,
            "textures_meta": {},
            "expected_views": None,
            "num_completed_views": 0,
            "obj": None,
            "soft_merge": True,
            "start_time": None,
        }
    )


def _texture_poll():
    """Consume queued texture chunks and apply completed server responses.

    Returns:
        float | None: Blender timer delay in seconds while polling should
        continue, or ``None`` to stop after completion, timeout, or a missing
        target texture.
    """
    drained = False

    while True:
        try:
            rec_message = ws_client.ws_chunks.get_nowait()
        except Empty:
            break

        drained = True

        if not isinstance(rec_message, bytes):
            continue

        msg = from_binary(rec_message)
        if msg.get("chunk_total") is None:
            continue

        view_id = msg["view_id"]
        chunk_index = msg["chunk_index"]
        chunk_total = msg["chunk_total"]
        num_views = msg["num_views"]
        image_chunk = msg["image"]
        print(
            "[TextureUpdate] received chunk",
            chunk_index,
            "of",
            chunk_total,
            "for view",
            view_id,
        )

        _TEXTURE_STATE["expected_views"] = num_views

        meta = _TEXTURE_STATE["textures_meta"].setdefault(view_id, {})
        if chunk_index in meta:
            continue

        meta[chunk_index] = image_chunk

        if len(meta) == chunk_total:
            ordered = [meta[index] for index in sorted(meta)]
            image = ordered[0] if len(ordered) == 1 else torch.cat(ordered)
            obj = _TEXTURE_STATE["obj"]
            texture = get_current_texture(obj)
            if texture is None:
                reset_texture_state()
                return None

            h, w = texture.size[1], texture.size[0]
            image = image.reshape((h, w, 4))
            update_texture(obj, image.cpu(), soft_merge=_TEXTURE_STATE["soft_merge"])

            _TEXTURE_STATE["num_completed_views"] += 1
            del _TEXTURE_STATE["textures_meta"][view_id]

    if (
        _TEXTURE_STATE["expected_views"] is not None
        and _TEXTURE_STATE["num_completed_views"] >= _TEXTURE_STATE["expected_views"]
    ):
        print("[TextureUpdate] completed all views")
        reset_texture_state()
        return None

    if (
        _TEXTURE_STATE["start_time"]
        and time.time() - _TEXTURE_STATE["start_time"] > 120
    ):
        print("[TextureUpdate] timed out waiting for server response")
        reset_texture_state()
        return None

    return 0.05 if drained else 0.1


def start_texture_updates(obj, soft_merge=True):
    """Start the timer-driven texture update loop for ``obj``.

    Args:
        obj: Blender object whose active paint texture should be updated from
            server responses.
        soft_merge: Whether completed views should be blended into the existing
            texture instead of replacing pixels directly.
    """
    if _TEXTURE_STATE["active"]:
        print("[TextureUpdate] already running")
        return

    _TEXTURE_STATE.update(
        {
            "active": True,
            "textures_meta": {},
            "expected_views": None,
            "num_completed_views": 0,
            "obj": obj,
            "soft_merge": soft_merge,
            "start_time": time.time(),
        }
    )
    bpy.app.timers.register(_texture_poll, persistent=True)


def load_paint_texture(context, filepath, sync_to_server=True):
    """Apply a paint texture to the active paint mesh and optionally sync it.

    Args:
        context: Blender context containing ``scene.current_paint_mesh``.
        filepath: Texture file to load.
        sync_to_server: When ``True``, immediately sends the new texture to the
            websocket server if a connection is active.

    Raises:
        RuntimeError: If no paint mesh is loaded.
    """
    obj = context.scene.current_paint_mesh
    if obj is None:
        raise RuntimeError("Load a paint mesh before loading a paint texture")

    image = apply_texture(obj, filepath, suffix="paint")
    binarize_paint_alpha(image)
    context.scene.glaze_session.loaded_paint_texture = filepath

    if sync_to_server and ws_client.is_connected:
        sync_paint_texture(context)


def _sync_paint_texture_worker(pixels, mesh_name):
    """Chunk, serialize, and queue a paint texture from a background thread."""
    try:
        msgs = send_large_image("texture", "set_texture", pixels)
        for msg in msgs:
            msg["mesh_name"] = mesh_name
            ws_client.send(to_binary(msg))
    except Exception as e:
        print(f"sync_paint_texture worker failed: {e}")


def sync_paint_texture(context):
    """Send the active paint texture to the websocket server in binary chunks.

    Pixel readout happens on the calling (main) thread because Blender data
    access is main-thread-only; chunking, serialization, and queuing are
    dispatched to a background thread so the UI does not block.

    Args:
        context: Blender context containing the current paint mesh.

    Raises:
        RuntimeError: If the server is disconnected, the paint mesh is missing,
            or the mesh does not have an active paint texture.
    """
    if not ws_client.is_connected:
        raise RuntimeError("Server not connected")

    paint_obj = context.scene.current_paint_mesh
    if paint_obj is None:
        raise RuntimeError("Load a paint mesh before syncing a texture")

    current_paint_texture = get_current_texture(paint_obj)
    if current_paint_texture is None:
        raise RuntimeError("Paint mesh does not have an active paint texture")

    h = current_paint_texture.size[1]
    w = current_paint_texture.size[0]
    buffer_size = h * w * 4
    current_texture_pixels = np.empty(buffer_size, dtype=np.float32)
    current_paint_texture.pixels.foreach_get(current_texture_pixels)

    threading.Thread(
        target=_sync_paint_texture_worker,
        args=(current_texture_pixels, paint_obj.name),
        daemon=True,
    ).start()


def send_fill_request(context):
    """Queue a fill request for the selected paint-mesh faces.

    The outgoing payload derives ``mesh_name`` from
    ``base_name(context.scene.current_paint_mesh.name)`` so it stays aligned
    with the currently loaded paint mesh.

    Args:
        context: Blender context containing the active paint mesh and fill
            settings.

    Returns:
        list[int]: Face indices sent with the request.

    Raises:
        RuntimeError: If no paint mesh is loaded or the server is disconnected.
    """
    obj = context.scene.current_paint_mesh
    if obj is None:
        raise RuntimeError("Load a paint mesh before generating a texture")
    if not ws_client.is_connected:
        raise RuntimeError("Server not connected")

    target_faces = collect_selected_face_indices(obj)
    fill_info = {
        "target_faces": target_faces,
        "mesh_name": base_name(obj.name),
        "brush_name": context.scene.current_brush,
        "high_res": context.scene.update_texture_4k,
        "clip_fill_to_faces": context.scene.clip_fill_to_faces,
        "max_cameras": context.scene.max_cameras,
        "cam_dist": context.scene.cam_dist,
        "cam_fov": context.scene.cam_fov,
        "dilate": context.scene.dilate,
        "camera_mode": context.scene.fill_camera_mode,
        "use_local_camera": context.scene.use_local_camera,
        "syncmvd": context.scene.syncmvd,
    }
    if context.scene.fill_camera_mode == "CAMERA":
        fill_info["anchor_face_ids"] = parse_anchor_face_ids(
            context.scene.fill_anchor_face_ids
        )
    ws_client.send({"type": "fill", "data": fill_info})
    start_texture_updates(obj, soft_merge=context.scene.soft_add)
    return target_faces


def send_precompute_local_cameras_request(context):
    """Queue a precompute-local-cameras request for the selected paint-mesh faces.

    Args:
        context: Blender context containing the active paint mesh and camera
            settings.

    Returns:
        list[int]: Face indices sent with the request.

    Raises:
        RuntimeError: If no paint mesh is loaded or the server is disconnected.
    """
    obj = context.scene.current_paint_mesh
    if obj is None:
        raise RuntimeError("Load a paint mesh before precomputing local cameras")
    if not ws_client.is_connected:
        raise RuntimeError("Server not connected")

    target_faces = collect_selected_face_indices(obj)
    precompute_info = {
        "target_faces": target_faces,
        "mesh_name": base_name(obj.name),
        "max_cameras": context.scene.max_cameras,
        "cam_dist": context.scene.cam_dist,
        "cam_fov": context.scene.cam_fov,
    }
    ws_client.send({"type": "precompute_local_cameras", "data": precompute_info})
    return target_faces


def send_clear_request(context):
    """Queue a clear request for the selected paint-mesh faces.

    Args:
        context: Blender context containing the active paint mesh.

    Returns:
        list[int]: Face indices sent with the request.

    Raises:
        RuntimeError: If no paint mesh is loaded or the server is disconnected.
    """
    obj = context.scene.current_paint_mesh
    if obj is None:
        raise RuntimeError("Load a paint mesh before clearing texture regions")

    target_faces = collect_selected_face_indices(obj)
    clear_info = {
        "target_faces": target_faces,
        "mesh_name": base_name(obj.name),
        "high_res": context.scene.update_texture_4k,
    }
    ws_client.send({"type": "clear_face", "data": clear_info})
    start_texture_updates(obj, soft_merge=False)
    return target_faces


def clear_all_texture(context):
    """Clear the active paint texture locally and notify the server if needed.

    Args:
        context: Blender context containing the active paint mesh.

    Raises:
        RuntimeError: If no paint mesh is loaded.
    """
    obj = context.scene.current_paint_mesh
    if obj is None:
        raise RuntimeError("Load a paint mesh before clearing its texture")

    clear_texture(obj)
    if ws_client.is_connected:
        ws_client.send(
            {
                "type": "clear_all_texture",
                "data": {"paint_mesh_name": obj.name},
            }
        )
