# glaze-blender Messaging

WebSocket protocol between the Blender add-on and the Glaze server.

## Transport

- **Endpoint:** `ws://localhost:10017/websocket` (configurable via `glaze_config.server_url`).
- **Client:** `WSClient` singleton in `client.py` — survives add-on reload.
- **Threading:** asyncio loop on a daemon thread runs `send_worker` + `receive_worker` (`client.py:82`, `:108`). A `bpy.app.timers` callback (`poll_str_messages`, every 0.2 s) bridges to Blender's main thread.
- **Auto-reconnect:** workers re-queue on `ConnectionClosed` and reconnect with backoff.
- **Send API:** `ws_client.send(data)` — thread-safe; `dict` → JSON, `bytes` → raw frame.

## Encoding

| Direction | Format | Encoder/decoder |
|---|---|---|
| JSON control messages | UTF-8 JSON text | `json.dumps` / `json.loads` |
| Binary payloads (textures, icons) | Custom aligned binary protocol | `utils/io.py` — `to_binary` / `from_binary` |

The binary protocol (`utils/io.py:84`) supports str / dict / list / numeric ndarrays with 4-byte alignment and type-coded headers (`BinaryIoDataType`).

## Outgoing messages (Blender → server)

All have shape `{"type": <name>, "data": {...}}` unless noted.

| `type` | Source | Payload fields |
|---|---|---|
| `add_ref_mesh` | `operators.py:54` | `mesh_name` |
| `add_pnt_mesh` | `operators.py:90` | `mesh_name`, `paint_mesh_name`, `high_res` |
| `add_brush` | `operators.py:290`, `:360` | `brush_name`, `brush_type`, `brush_mesh`, `sv_id`, `cam_dist`, `cam_fov`, *opt.* `reference_faces` |
| `save_brush` | `operators.py:400` | `brush_name` |
| `fill` | `backend.py:332` | `target_faces`, `mesh_name`, `brush_name`, `high_res`, `update`, `max_cameras`, `cam_dist`, `cam_fov`, `dilate`, `camera_mode`, `use_local_camera`, *opt.* `anchor_face_ids` |
| `precompute_local_cameras` | `backend.py:364` | `target_faces`, `mesh_name`, `max_cameras`, `cam_dist`, `cam_fov` |
| `clear_face` | `backend.py:392` | `target_faces`, `mesh_name`, `high_res` |
| `clear_all_texture` | `backend.py:413` | `paint_mesh_name` |
| **binary** `image` (`task_name="set_texture"`) | `backend.py:285` | `type:"image"`, `name:"texture"`, `task_name`, `chunk_index`, `chunk_total`, `image`, `mesh_name` |

The texture upload uses `send_large_image()` (`utils/io.py:445`) to split pixels into ≤10 MB chunks before binary-encoding each.

## Incoming messages (server → Blender)

Two consumer paths, both Blender timers, share `ws_client.receive_queue`; binary frames also land in `ws_client.ws_chunks`.

| Consumer | Trigger | Recognized keys | Effect |
|---|---|---|---|
| `_texture_poll` (`backend.py:134`) | `start_texture_updates()` after `fill` / `clear_face` | `view_id`, `chunk_index`, `chunk_total`, `num_views`, `image` | Reassembles chunks per `view_id`, reshapes to `(H, W, 4)`, calls `update_texture`; finishes when `num_completed_views == num_views`. 120 s timeout. |
| `start_brush_listener._poll` (`operators.py:229`) | After `add_brush` is sent | `brush icon` (HxWxC tensor) | Saves `<brushes_folder>/<brush_name>/icon.png`, registers brush in `scene.glaze_brushes`. |
| `poll_str_messages` (`client.py:234`) | Every 0.2 s | any `str` | Printed only — no structured handler. |

## Lifecycle

```
addon enable           → register_client()  → ws_client.start()  → connects, registers timer
operator action        → ws_client.send(...) [JSON or binary]
server reply           → asyncio recv → receive_queue / ws_chunks → timer drains → Blender state update
addon disable / quit   → unregister_client() → ws_client.stop()
```

`ws_client.restart(uri)` (operator: `glaze.reconnect_server`) reconnects, optionally to a new URL. UI status comes from `server_status_text()` / `server_status_icon()` in `backend.py:31,40`.

## Reference implementation

`mock_server.py` is the minimum viable server: accepts any JSON `type`, replies to `fill` / `clear_face` with one binary chunk `{view_id, chunk_index, chunk_total, num_views, image}` — exactly the shape `_texture_poll` decodes.
