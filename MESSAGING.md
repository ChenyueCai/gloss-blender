# gloss-blender Messaging

WebSocket protocol between the Blender add-on and the Gloss server.

## Transport

- **Endpoint:** `ws://localhost:10017/websocket` (configurable via `gloss_config.server_url`).
- **Client:** `WSClient` singleton in `client.py` — survives add-on reload.
- **Threading:** an asyncio loop on a daemon thread runs `send_worker` + `receive_worker`
  (`client.py:222`, `:249`). A single `bpy.app.timers` callback, `poll_messages`
  (every 0.05 s), bridges to Blender's main thread.
- **Auto-reconnect:** workers re-queue on `ConnectionClosed` and reconnect with backoff.
- **Send API:** `ws_client.send(data)` — thread-safe; `dict` → JSON, `bytes` → raw frame.
- **Frame ceiling:** 128 MB (`max_size`), server `websocket_max_message_size` 40 MB.

## Routing

**Every server → client message carries a `type`.** `poll_messages` is the sole
consumer of `receive_queue`; it decodes each frame, resolves its type, and calls
the handlers registered for it:

```python
ws_client.register_handler(MSG_TEXTURE_CHUNK, on_texture_chunk)
ws_client.unregister_handler(MSG_BRUSH_ICON, my_one_shot)
```

Several handlers may share a type and are called in registration order; a
handler that raises is caught and logged so it cannot stall the queue.
`ws_client.stats` counts `received` / `dispatched` / `unhandled` /
`decode_errors` / `handler_errors` — `unhandled > 0` means something was
delivered that nothing consumes.

Type constants live in `protocol.py`, mirrored at
`material-superres-private/gloss_interactive/protocol.py`; keep the two in sync.

> **Why this exists.** Replies used to be untagged, and two timers
> (`poll_str_messages`, always registered at 0.2 s, and a per-brush
> `poll_bin_messages` at 0.1 s) each popped *every* message from one shared
> list and discarded the kinds they did not recognise. Whichever fired first
> ate the brush icon, and the brush poller — which had no timeout — then
> polled forever, leaking a timer per attempt.

## Encoding

| Direction | Format | Encoder/decoder |
|---|---|---|
| JSON control messages | UTF-8 JSON text | `json.dumps` / `json.loads` |
| Binary payloads (textures, icons) | Custom aligned binary protocol | `utils/io.py` — `to_binary` / `from_binary` |

The binary protocol (`utils/io.py:84`) supports str / dict / list / numeric
ndarrays with 4-byte alignment and type-coded headers (`BinaryIoDataType`).

### Pixel payloads

Pixels travel as **uint8** by default (`encode_pixels` / `decode_pixels`), tagged
per message with `dtype`. uint8 is lossless for the 8-bit basecolor data both
ends store and is 4× smaller than the previous float32 payload. On the server
the quantization happens on-device, so the GPU→host copy shrinks by the same 4×.

`send_large_image(...)` splits a payload into chunks of at most
`DEFAULT_CHUNK_BYTES` (10 MB), defined identically in `protocol.py`,
`utils/io.py` and the server's `kaolin_tmp_io.py`.

For one 4096×4096 RGBA texture:

| | frames | bytes on wire |
|---|---|---|
| before (float32, 1 MB chunks) | 269 | 268 MB |
| after (uint8, 10 MB chunks) | 7 | 67 MB |

## Outgoing messages (Blender → server)

All have shape `{"type": <name>, "data": {...}}` unless noted.

| `type` | Source | Payload fields |
|---|---|---|
| `add_ref_mesh` | `operators.py` | `mesh_name` |
| `add_pnt_mesh` | `operators.py` | `mesh_name`, `paint_mesh_name`, `high_res` |
| `add_brush` | `operators.py` | `brush_name`, `brush_type`, `brush_mesh`, `sv_id`, `cam_dist`, `cam_fov`, *opt.* `reference_faces` |
| `save_brush` | `operators.py` | `brush_name` |
| `fill` | `backend.py` | `target_faces`, `mesh_name`, `brush_name`, `high_res`, `clip_fill_to_faces`, `max_cameras`, `cam_dist`, `cam_fov`, `dilate`, `syncmvd` |
| `clear_face` | `backend.py` | `target_faces`, `mesh_name`, `high_res` |
| `clear_all_texture` | `backend.py` | `paint_mesh_name` |
| **binary** `image` | `backend.py` | `type:"image"`, `name:"texture"`, `task_name:"set_texture"`, `chunk_index`, `chunk_total`, `dtype`, `image`, `mesh_name` |

## Incoming messages (server → Blender)

| `type` | Handler | Payload | Effect |
|---|---|---|---|
| `texture_chunk` | `backend.on_texture_chunk` | `view_id`, `chunk_index`, `chunk_total`, `num_views`, `dtype`, `image` | `ChunkAssembler` reassembles per view, reshapes to `(H, W, 4)`, calls `update_texture`. Progress is mirrored into the panel per chunk. |
| `brush_icon` | one-shot handler from `operators.start_brush_listener` | `brush_name`, `dtype`, `image` (uint8 HWC, 256²) | Writes `<brushes_folder>/<brush_name>/icon.png`, registers the brush in `scene.gloss_brushes`. |
| `brush_status` | `backend.on_brush_status` | `brush_name`, `state`, `message` | Drives the Brush Library progress row. |
| `fill_status` | `backend.on_fill_status` | `stage`, `current`, `total` | Drives the Generation progress row. |
| `error` | `backend.on_server_error` | `message`, `context` | Clears pending state and shows the failure, instead of hanging until timeout. |
| `texture_synced` | `backend.on_texture_synced` | `message`, `mesh_name` | Confirms the client's push landed; drives the "in sync" row. |
| `status` | `backend.on_status` | `message` | Logged. |

### Timeouts

Both long operations are bounded so a silent server surfaces as an error rather
than a stuck panel: `TEXTURE_TIMEOUT_S` (120 s, `backend.py`) and
`BRUSH_TIMEOUT_S` (300 s, `operators.py`).

## Texture agreement

Both sides must always hold the same pixels. Every server path that changes a
texture persists it against the mesh the request named -- `fill`, `clear_face`,
`clear_all_texture` and the `set_texture` upload. `fill` previously did not,
so the server's copy never advanced and each stroke was conditioned on the
pre-session texture.

The client pushes its texture back automatically after a generate (which it
composites locally), after an undo, and when a texture is loaded. A clear needs
no push because the client adopts the server's result verbatim. There is no
manual sync message or button.

## Server-side execution model

`GLOSSWebSocketHandler.on_message` **yields** its handlers, so exceptions
surface and are reported to the client as `error` messages. All GPU work runs
on `GPU_EXECUTOR`, a single-worker `ThreadPoolExecutor` — one worker keeps CUDA
work serialized while leaving the IOLoop free:

- `_load_mesh_model_blocking` — mesh + checkpoint load, camera presample
- `_prepare_brush_blocking` — brush creation and reference rendering
- `_preload_local_cameras_blocking` — local camera presample
- `_run_fill_blocking` — the fill stroke itself

`_run_fill_blocking` receives a `progress(stage, current, total)` callback that
hops back to the IOLoop via `add_callback`, so `fill_status` messages stream out
while the stroke runs. Measured on a 1 s synthetic stroke: longest IOLoop stall
21 ms (vs 1002 ms inline), first progress message at 251 ms (vs never).

## Lifecycle

```
addon enable    → register_client() → ws_client.start() → connect, register poll_messages
                → register_backend_handlers()  (subscribe to the router)
operator action → ws_client.send(...) [JSON or binary]
server reply    → asyncio recv → receive_queue → poll_messages → handler → Blender state
addon disable   → unregister_backend_handlers() → unregister_client() → ws_client.stop()
```

## Reference implementation

`mock_server.py` is the minimum viable server: it speaks the tagged protocol,
replies to `add_brush` with `brush_status` + a uint8 `brush_icon`, and to
`fill` / `clear_face` with `fill_status` + chunked `texture_chunk` frames.
`--brush-delay` fakes slow preparation so you can watch the panel stay
responsive.
