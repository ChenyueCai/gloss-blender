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
from .utils.config import register_config_handlers, unregister_config_handlers
from .utils.io import (
    ChunkAssembler,
    send_large_image,
    to_binary,
)
from .utils.mesh import (
    apply_texture,
    base_name,
    binarize_paint_alpha,
    clear_texture,
    get_current_texture,
    update_texture,
)

#: Seconds of silence from the server before an in-flight fill is failed.
#: Refreshed by every sign of life, so a slow but talkative server is never
#: killed for taking its time.
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

#: Seconds of silence before an in-flight brush creation is failed.
BRUSH_TIMEOUT_S = 300


def _read_session(field):
    """Read one field of the scene's session state, or ``None`` if unavailable."""
    try:
        return getattr(bpy.context.scene.gloss_session, field)
    except Exception:
        # No scene yet (registration, headless import), or the property is gone.
        return None


class _BusyGuard:
    """Makes a published ``preparing`` a promise that always resolves.

    The panel greys out whole rows while a state reads ``preparing``
    (``ui_panel.py``), so any path that publishes it must also guarantee that
    something takes it away again. Rather than ask each of those paths to
    remember, the guarantee hangs off the publisher: the guard arms on the
    transition into ``preparing`` and a timer keyed off the *published* value --
    not off whatever private bookkeeping happens to be in flight -- carries it
    to a terminal state.

    This is the bug the class exists to prevent: the fill watchdog used to key
    off ``_TEXTURE_STATE["active"]``, so a ``preparing`` published without a
    texture update behind it (a stray ``fill_status`` arriving after the fill
    had already finished) had nothing left to end it, and every button in the
    Generation panel stayed greyed out for the rest of the session.
    """

    def __init__(self, name, field, on_timeout, timeout_s):
        self.name = name
        self.field = field
        self.timeout_s = timeout_s
        self._on_timeout = on_timeout
        self.deadline = None
        #: Last published state, for when the scene property cannot be read.
        self.mirror = None
        # One bound method for the guard's lifetime: bpy.app.timers matches
        # callbacks by identity, and ``self._tick`` is a fresh object per access.
        self.tick = self._tick

    def publish(self, state):
        """Mirror a published state, then arm or disarm the deadline."""
        self.mirror = state
        if state == STATE_PREPARING:
            self.arm()
        else:
            self.deadline = None

    def arm(self):
        """Start, or push back, the deadline. Every sign of life calls this."""
        self.deadline = time.time() + self.timeout_s
        if not bpy.app.timers.is_registered(self.tick):
            bpy.app.timers.register(self.tick, persistent=True)

    def published_state(self):
        """What the panel actually shows, falling back to the mirror.

        The scene wins whenever it is readable, so loading a file -- which
        resets the property to its default -- retires the guard instead of
        stamping the previous file's timeout onto the new one.
        """
        state = _read_session(self.field)
        return self.mirror if state is None else state

    def _tick(self):
        """Timer body. Returning ``None`` unregisters it."""
        if self.published_state() != STATE_PREPARING:
            self.deadline = None
            return None
        if self.deadline is None:
            # Busy with no deadline: start the countdown rather than trust a
            # state nothing accounted for.
            self.deadline = time.time() + self.timeout_s
            return 1.0
        if time.time() < self.deadline:
            return 1.0
        print(f"[{self.name}] timed out waiting for server response")
        self._on_timeout()
        return None

    def reset(self):
        """Forget the state and stop ticking. Used at add-on teardown."""
        self.deadline = None
        self.mirror = None
        if bpy.app.timers.is_registered(self.tick):
            bpy.app.timers.unregister(self.tick)


_FILL_GUARD = _BusyGuard(
    "TextureUpdate",
    "fill_state",
    lambda: finish_texture_updates(STATE_ERROR, "timed out waiting for server"),
    TEXTURE_TIMEOUT_S,
)

_BRUSH_GUARD = _BusyGuard(
    "CreateBrush",
    "brush_state",
    lambda: set_brush_state(STATE_ERROR, "timed out waiting for server"),
    BRUSH_TIMEOUT_S,
)


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
    """Publish fill progress to the panel and request a repaint.

    Every transition is mirrored into ``_FILL_GUARD`` and arms or disarms the
    watchdog, which is what stops the panel from latching. The watchdog used to
    key off ``_TEXTURE_STATE["active"]``, so a ``preparing`` published without
    an in-flight texture update behind it -- a stray ``fill_status`` arriving
    after the fill already finished, say -- had nothing left to end it and
    greyed out every button in the Generation panel for the rest of the
    session.
    """
    try:
        session = bpy.context.scene.gloss_session
        session.fill_state = state
        session.fill_message = message
    except Exception:
        # No scene yet. The guard's mirror still drives the watchdog, so the
        # state stays recoverable either way.
        pass
    _FILL_GUARD.publish(state)
    tag_redraw_all()


def set_brush_state(state, message=""):
    """Publish brush-creation progress to the panel and request a repaint.

    Guarded like the fill state: the Add Brush row greys out on ``preparing``,
    and ``on_brush_status`` publishes that straight from the server, so a
    trailing progress message could otherwise latch the row shut.
    """
    try:
        session = bpy.context.scene.gloss_session
        session.brush_state = state
        session.brush_message = message
    except Exception:
        pass
    _BRUSH_GUARD.publish(state)
    tag_redraw_all()


def set_sync_state(state, message=""):
    """Publish client/server texture agreement to the panel."""
    try:
        session = bpy.context.scene.gloss_session
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
    """Surface server-side fill progress in the panel.

    Progress only means something while this side is tracking a fill. The
    server sends one last ``fill_status`` (its own ``ready`` stage) *after* the
    final texture chunk, by which time ``on_texture_chunk`` has already
    finished the fill and published ``ready``. Writing that trailing message
    back as ``preparing`` re-greyed Generate and Clear Faces after every
    successful generate -- for 120 s until the watchdog fired, or for good if
    the file was saved in that window. So: nothing in flight, nothing to show;
    and completion is decided by the chunk handler, never by the server's word.
    """
    if not _TEXTURE_STATE["active"]:
        return
    data = payload.get("data") or {}
    stage = data.get("stage", "working")
    if stage == STATE_READY:
        # Sign of life, but not completion: chunks may still be on the wire.
        _FILL_GUARD.arm()
        return
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
    print(f"[gloss] server error during {context_type!r}: {message}")
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
        print("[gloss]", message)


def finish_texture_updates(state=STATE_READY, message=""):
    """Tear down the in-flight fill and publish its terminal state."""
    reset_texture_state()
    set_fill_state(state, message)


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

    Raises:
        RuntimeError: If a texture update is already in flight.
    """
    if _TEXTURE_STATE["active"]:
        # Callers send only after arming, so this is the last line of defence
        # against a second request reaching the server untracked and having its
        # chunks land on the previous request's assembler.
        raise RuntimeError("A texture update is already running")

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
    # Publishing "preparing" arms the watchdog; nothing else has to remember to.
    set_fill_state(STATE_PREPARING, "waiting for server")


#: Progress properties on ``Scene.gloss_session`` and the value that means
#: "nothing in flight". They are scene data, so Blender saves them into the
#: .blend along with everything else.
_TRANSIENT_STATES = (
    ("fill_state", "fill_message"),
    ("brush_state", "brush_message"),
    ("sync_state", "sync_message"),
)


@bpy.app.handlers.persistent
def reset_transient_state_on_load(*_args):
    """Forget saved progress when a file is loaded.

    ``fill_state`` and friends are scene properties, so a file saved while a
    row read ``preparing`` reopens reading ``preparing`` -- with no request in
    flight and no watchdog armed, because the guard only arms when a state is
    published during the session. That locked the whole Generation panel until
    Blender was restarted. After a load nothing can be in flight, by
    definition, so every row starts over at ``idle``.
    """
    reset_texture_state()
    _FILL_GUARD.reset()
    _BRUSH_GUARD.reset()
    for scene in getattr(bpy.data, "scenes", ()):
        session = getattr(scene, "gloss_session", None)
        if session is None:
            continue
        for state_field, message_field in _TRANSIENT_STATES:
            try:
                setattr(session, state_field, "idle")
                setattr(session, message_field, "")
            except Exception:
                pass
    tag_redraw_all()


def register_backend_handlers():
    """Wire backend consumers onto the client router. Called at add-on register."""
    if reset_transient_state_on_load not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(reset_transient_state_on_load)
    register_config_handlers()
    ws_client.register_handler(MSG_TEXTURE_CHUNK, on_texture_chunk)
    ws_client.register_handler(MSG_FILL_STATUS, on_fill_status)
    ws_client.register_handler(MSG_BRUSH_STATUS, on_brush_status)
    ws_client.register_handler(MSG_TEXTURE_SYNCED, on_texture_synced)
    ws_client.register_handler(MSG_ERROR, on_server_error)
    ws_client.register_handler(MSG_STATUS, on_status)


def unregister_backend_handlers():
    """Remove backend consumers from the client router and stop the watchdog."""
    if reset_transient_state_on_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(reset_transient_state_on_load)
    unregister_config_handlers()
    reset_texture_state()
    _FILL_GUARD.reset()
    _BRUSH_GUARD.reset()
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
    context.scene.gloss_session.loaded_paint_texture = filepath

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
        print(f"[gloss] texture push failed ({reason}): {exc}")
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
        RuntimeError: If no paint mesh is loaded, the server is disconnected,
            or a texture update is already in flight.
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
    # Arm first, send second. The old order put the request on the wire before
    # start_texture_updates() had a chance to refuse a busy state, so a second
    # generate reached the server with nothing on this side tracking its reply.
    start_texture_updates(obj, soft_merge=context.scene.soft_add, operation="fill")
    ws_client.send({"type": "fill", "data": fill_info})
    set_fill_state(STATE_PREPARING, f"generating ({len(target_faces)} faces)")
    return target_faces


def send_clear_request(context):
    """Queue a clear request for the selected paint-mesh faces.

    Args:
        context: Blender context containing the active paint mesh.

    Returns:
        list[int]: Face indices sent with the request.

    Raises:
        RuntimeError: If no paint mesh is loaded, the server is disconnected,
            or a texture update is already in flight.
    """
    obj = context.scene.current_paint_mesh
    if obj is None:
        raise RuntimeError("Load a paint mesh before clearing texture regions")
    # The docstring always promised this check; without it a clear issued while
    # disconnected sent into the void and left the panel waiting on a reply that
    # could never arrive.
    if not ws_client.is_connected:
        raise RuntimeError("Server not connected")

    target_faces = collect_selected_face_indices(obj)
    clear_info = {
        "target_faces": target_faces,
        "mesh_name": base_name(obj.name),
        "high_res": context.scene.update_texture_4k,
    }
    start_texture_updates(obj, soft_merge=False, operation="clear")
    ws_client.send({"type": "clear_face", "data": clear_info})
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
