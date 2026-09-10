import re
import threading
import time

import bmesh
import bpy
import numpy as np
import torch

from .client import ws_client
from .protocol import (
    DTYPE_UINT8,
    MSG_BRUSH_STATUS,
    MSG_ERROR,
    MSG_FILL_STATUS,
    MSG_STATUS,
    MSG_TEXTURE_CHUNK,
    MSG_TEXTURE_SYNCED,
    STATE_ERROR,
    STATE_PREPARING,
    STATE_READY,
    STATE_SYNCED,
    STATE_SYNCING,
)
from .utils.io import (
    ChunkAssembler,
    decode_pixels,
    from_binary,
    send_large_image,
    to_binary,
)
from .utils.mesh import (
    apply_texture,
    base_name,
    binarize_paint_alpha,
    clear_faces_local,
    clear_texture,
    get_current_texture,
    update_texture,
)

#: Seconds to wait for a server response before giving up on a fill.
TEXTURE_TIMEOUT_S = 120

_TEXTURE_STATE = {
    "active": False,
    "assembler": None,
    "num_completed_views": 0,
    "obj": None,
    "soft_merge": True,
    "start_time": None,
    # "fill" | "clear" -- decides whether the result must be pushed back.
    "operation": None,
}


def tag_redraw_all():
    """Repaint the sidebar so timer-driven state changes become visible.

    Handlers run from a ``bpy.app.timers`` callback, outside any UI event, so
    Blender will not repaint on its own until the next mouse move.
    """
    try:
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type in {"VIEW_3D", "PROPERTIES"}:
                    area.tag_redraw()
    except Exception:
        pass


def set_fill_state(state, message=""):
    """Publish fill progress to the panel and request a repaint."""
    try:
        session = bpy.context.scene.glaze_session
        session.fill_state = state
        session.fill_message = message
    except Exception:
        pass
    tag_redraw_all()


def set_brush_state(state, message=""):
    """Publish brush-creation progress to the panel and request a repaint."""
    try:
        session = bpy.context.scene.glaze_session
        session.brush_state = state
        session.brush_message = message
    except Exception:
        pass
    tag_redraw_all()


def set_sync_state(state, message=""):
    """Publish client/server texture agreement to the panel."""
    try:
        session = bpy.context.scene.glaze_session
        session.sync_state = state
        session.sync_message = message
    except Exception:
        pass
    tag_redraw_all()


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
            "assembler": None,
            "num_completed_views": 0,
            "obj": None,
            "soft_merge": True,
            "start_time": None,
            "operation": None,
        }
    )


def on_texture_chunk(payload):
    """Reassemble one texture chunk and apply the view once it is complete.

    Registered against ``MSG_TEXTURE_CHUNK`` on the client router, so this is
    the only consumer of texture frames -- no polling, and no queue shared with
    another timer.

    Args:
        payload: Decoded chunk carrying ``view_id``, ``chunk_index``,
            ``chunk_total``, ``num_views``, ``image`` and optionally ``dtype``.
    """
    if not _TEXTURE_STATE["active"]:
        # A late chunk from a request that already timed out or was reset.
        return

    assembler = _TEXTURE_STATE["assembler"]
    view_id = payload.get("view_id")
    result = assembler.add(payload)

    if result is None:
        received, expected = assembler.progress(view_id)
        if expected:
            set_fill_state(
                STATE_PREPARING,
                f"receiving view {_TEXTURE_STATE['num_completed_views'] + 1}"
                f"/{assembler.num_views or 1} ({received}/{expected} chunks)",
            )
        return

    _, pixels = result
    obj = _TEXTURE_STATE["obj"]
    texture = get_current_texture(obj)
    if texture is None:
        finish_texture_updates(STATE_ERROR, "paint texture went away mid-update")
        return

    h, w = texture.size[1], texture.size[0]
    image = torch.from_numpy(np.asarray(pixels))
    try:
        image = image.reshape((h, w, 4))
    except RuntimeError as exc:
        finish_texture_updates(STATE_ERROR, f"texture size mismatch: {exc}")
        return

    update_texture(obj, image.cpu(), soft_merge=_TEXTURE_STATE["soft_merge"])
    _TEXTURE_STATE["num_completed_views"] += 1

    if _TEXTURE_STATE["num_completed_views"] >= (assembler.num_views or 1):
        operation = _TEXTURE_STATE["operation"]
        finish_texture_updates(STATE_READY, "texture updated")
        # A fill is composited locally (soft merge), so the client's texture is
        # now ahead of the server's. Push it back, otherwise the next stroke
        # would be conditioned on a state the user never sees. A clear is
        # adopted verbatim, so both sides already agree.
        if operation == "fill":
            push_texture_to_server(bpy.context, reason="after generate")
        else:
            set_sync_state(STATE_SYNCED, "in sync with server")


def on_fill_status(payload):
    """Surface server-side fill progress in the panel."""
    data = payload.get("data") or {}
    stage = data.get("stage", "working")
    current, total = data.get("current"), data.get("total")
    if current is not None and total:
        set_fill_state(STATE_PREPARING, f"{stage} {current}/{total}")
    else:
        set_fill_state(STATE_PREPARING, str(stage))


def on_brush_status(payload):
    """Surface server-side brush-creation progress in the panel."""
    data = payload.get("data") or {}
    state = data.get("state", STATE_PREPARING)
    name = data.get("brush_name", "")
    message = data.get("message") or f"{state} {name}".strip()
    set_brush_state(state, message)


def on_texture_synced(payload):
    """Server acknowledged our push; the two copies now match."""
    data = payload.get("data") or {}
    set_sync_state(STATE_SYNCED, data.get("message", "in sync with server"))


def on_server_error(payload):
    """Clear any pending progress state when the server reports a failure."""
    data = payload.get("data") or {}
    message = data.get("message", "server error")
    context_type = data.get("context", "")
    print(f"[glaze] server error during {context_type!r}: {message}")
    if context_type in {"fill", "clear_face"} or _TEXTURE_STATE["active"]:
        finish_texture_updates(STATE_ERROR, message)
    if context_type == "add_brush":
        set_brush_state(STATE_ERROR, message)
    if context_type == "image":
        set_sync_state(STATE_ERROR, message)


def on_status(payload):
    """Print a server status line."""
    data = payload.get("data") or {}
    message = data.get("message", "")
    if message:
        print("[glaze]", message)


def finish_texture_updates(state=STATE_READY, message=""):
    """Tear down the in-flight fill and publish its terminal state."""
    reset_texture_state()
    set_fill_state(state, message)


def _texture_watchdog():
    """Fail an in-flight fill that the server never finished.

    Replaces the timeout that used to live inside the polling loop; the router
    is event-driven, so a silent server would otherwise hang the panel forever.
    """
    if not _TEXTURE_STATE["active"]:
        return None
    started = _TEXTURE_STATE["start_time"]
    if started and time.time() - started > TEXTURE_TIMEOUT_S:
        print("[TextureUpdate] timed out waiting for server response")
        finish_texture_updates(STATE_ERROR, "timed out waiting for server")
        return None
    return 1.0


def start_texture_updates(obj, soft_merge=True, operation="fill"):
    """Arm the router to apply server texture responses for ``obj``.

    Args:
        obj: Blender object whose active paint texture should be updated from
            server responses.
        soft_merge: Whether completed views should be blended into the existing
            texture instead of replacing pixels directly.
        operation: ``"fill"`` or ``"clear"``. A fill is composited locally, so
            its result is pushed back to the server; a clear is adopted
            verbatim, so both sides already agree.
    """
    if _TEXTURE_STATE["active"]:
        print("[TextureUpdate] already running")
        return

    _TEXTURE_STATE.update(
        {
            "active": True,
            "assembler": ChunkAssembler(),
            "num_completed_views": 0,
            "obj": obj,
            "soft_merge": soft_merge,
            "start_time": time.time(),
            "operation": operation,
        }
    )
    set_fill_state(STATE_PREPARING, "waiting for server")
    if not bpy.app.timers.is_registered(_texture_watchdog):
        bpy.app.timers.register(_texture_watchdog, persistent=True)


def register_backend_handlers():
    """Wire backend consumers onto the client router. Called at add-on register."""
    ws_client.register_handler(MSG_TEXTURE_CHUNK, on_texture_chunk)
    ws_client.register_handler(MSG_FILL_STATUS, on_fill_status)
    ws_client.register_handler(MSG_BRUSH_STATUS, on_brush_status)
    ws_client.register_handler(MSG_TEXTURE_SYNCED, on_texture_synced)
    ws_client.register_handler(MSG_ERROR, on_server_error)
    ws_client.register_handler(MSG_STATUS, on_status)


def unregister_backend_handlers():
    """Remove backend consumers from the client router."""
    ws_client.unregister_handler(MSG_TEXTURE_CHUNK, on_texture_chunk)
    ws_client.unregister_handler(MSG_FILL_STATUS, on_fill_status)
    ws_client.unregister_handler(MSG_BRUSH_STATUS, on_brush_status)
    ws_client.unregister_handler(MSG_TEXTURE_SYNCED, on_texture_synced)
    ws_client.unregister_handler(MSG_ERROR, on_server_error)
    ws_client.unregister_handler(MSG_STATUS, on_status)


def load_paint_texture(context, filepath):
    """Apply a paint texture to the active paint mesh and sync it to the server.

    The sync is unconditional: the client/server contract is that both sides
    always hold the same pixels, and an optional sync is exactly how the two
    copies used to drift apart.

    Args:
        context: Blender context containing ``scene.current_paint_mesh``.
        filepath: Texture file to load.

    Raises:
        RuntimeError: If no paint mesh is loaded.
    """
    obj = context.scene.current_paint_mesh
    if obj is None:
        raise RuntimeError("Load a paint mesh before loading a paint texture")

    image = apply_texture(obj, filepath, suffix="paint")
    binarize_paint_alpha(image)
    context.scene.glaze_session.loaded_paint_texture = filepath

    if ws_client.is_connected:
        push_texture_to_server(context, reason="after loading texture")
    else:
        set_sync_state(STATE_ERROR, "not connected: server copy is stale")


def _sync_paint_texture_worker(pixels, mesh_name):
    """Chunk, serialize, and queue a paint texture from a background thread.

    Uploads as uint8: a 4K RGBA texture is 64 MB instead of 256 MB, and the
    destination is 8-bit anyway. The ``dtype`` tag rides on every chunk so the
    server knows how to rebuild it.
    """
    try:
        msgs = send_large_image(
            "texture", "set_texture", pixels,
            msg_type="image", dtype=DTYPE_UINT8,
            extra={"mesh_name": mesh_name},
        )
        for msg in msgs:
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

    # base_name strips Blender's ".001" duplicate suffix. Every other message
    # keys on it; this one used the raw object name, so the server's lookup
    # missed and it silently fell back to a 4096 reshape.
    threading.Thread(
        target=_sync_paint_texture_worker,
        args=(current_texture_pixels, base_name(paint_obj.name)),
        daemon=True,
    ).start()


def push_texture_to_server(context, reason=""):
    """Upload the client's paint texture so both sides hold the same pixels.

    This is the single mechanism keeping client and server in agreement. It
    runs automatically after a generate, after an undo, and when a paint
    texture is loaded -- there is deliberately no manual sync button, because
    an optional sync is how the two copies used to drift apart.

    Failures are reported into the panel rather than raised, since callers are
    usually timers or operators that already succeeded locally.
    """
    try:
        sync_paint_texture(context)
        set_sync_state(STATE_SYNCING, f"syncing {reason}".strip())
        return True
    except RuntimeError as exc:
        set_sync_state(STATE_ERROR, str(exc))
        print(f"[glaze] texture push failed ({reason}): {exc}")
        return False


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
        "syncmvd": context.scene.syncmvd,
    }
    ws_client.send({"type": "fill", "data": fill_info})
    start_texture_updates(obj, soft_merge=context.scene.soft_add, operation="fill")
    set_fill_state(STATE_PREPARING, f"generating ({len(target_faces)} faces)")
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
    start_texture_updates(obj, soft_merge=False, operation="clear")
    set_fill_state(STATE_PREPARING, f"clearing ({len(target_faces)} faces)")
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
                "data": {"paint_mesh_name": base_name(obj.name)},
            }
        )
        # Both sides zero the same texture, so they agree without a push.
        set_sync_state(STATE_SYNCED, "cleared on both sides")
