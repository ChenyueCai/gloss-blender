# Glaze Blender Add-on

`glaze-blender` is a Blender add-on for texture-assisted mesh painting. It adds a `Glaze` panel to the 3D View sidebar, lets you load reference and paint meshes, optionally load a paint texture from disk, and sends generation requests to a websocket server that returns updated RGBA texture chunks.

## What It Does

- Creates a Blender sidebar panel for the Glaze workflow.
- Loads a reference mesh and a paint mesh into the scene.
- Loads a reference image and applies it to the reference mesh.
- Loads a paint texture onto the paint mesh and can sync it to the server.
- Sends texture-generation requests to a websocket backend.
- Streams server outputs back into Blender and updates the active paint texture.

## Repository Layout

- `__init__.py`: add-on registration and Blender scene properties.
- `protocol.py`: wire-protocol constants shared with the server.
- `ui_panel.py`: panel layout, progress rows, and user-facing workflow.
- `operators.py`: Blender operators for loading assets, generation, syncing, and brush actions.
- `backend.py`: request builders and the routed handlers that apply server results.
- `client.py`: websocket client, reconnect logic, and the message router.
- `utils/mesh.py`: mesh import, texture application, and texture update utilities.
- `utils/io.py`: binary encoding, the pixel codec, and chunk reassembly.
- `mock_server.py`: reference server implementation for testing without a GPU.
- `tests/`: headless test suite (Blender is stubbed).
- `assets/example_config.yaml`: sample config file.

## Requirements

- Blender 4.x
- Python packages listed in [`requirement.txt`](./requirement.txt)
- A websocket server that speaks the Glaze message protocol

Install Python dependencies into Blender's Python environment or the interpreter used by your add-on workflow:

```bash
python -m pip install -r requirement.txt
```

## Install the Add-on

1. Copy or symlink this folder into your Blender add-ons directory.
2. Open Blender.
3. Go to `Edit > Preferences > Add-ons`.
4. Enable `GLAZE Painter`.
5. Open `View3D > Sidebar > Glaze`.

## Panel Workflow

The add-on exposes three main sections in the `Glaze` sidebar panel.

### 1. Configuration

- Set the websocket URL.
- Load a YAML config file.
- Edit the loaded mesh, reference image, and reference texture folders directly in the panel. The brushes folder is configured through YAML and hidden from the panel.
- Choose 1K or 4K texture resolution before loading a paint mesh.
- Reconnect the websocket client.
- Check the current connection status.

Default server URL:

```text
ws://localhost:10017/websocket
```

### 2. Assets

- `Load Reference Mesh`: imports the mesh shown on the reference side.
- `Load Paint Mesh`: imports the mesh that receives generated textures.
- Both mesh file pickers start in the configured Mesh Folder (`mesh_folder`) when set. You can still browse elsewhere; a missing configured folder reports an error.
- `Choose Reference Image`: selects a reference image from disk.
- The reference image file picker starts in the configured Reference Images folder (`single_views_folder`) when set. You can still browse to another folder.
- `Apply to Reference Mesh`: applies the selected reference image.
- `Load Texture`: loads a paint texture onto the paint mesh and uploads it to
  the server. There is no toggle: client and server always hold the same
  pixels.

Reference image behavior:

- The selected file name must start with `view<number>.`, such as `view0001.png` or `view0001.basecolor.png`.
- The add-on always loads `single_views_texture_folder/<mesh>/view####.png` for that view ID. It does not use the selected image directly.
- If the view ID cannot be resolved, the texture folder is unset, or the requested texture file is missing, it reports `requested texture does not exist` and cancels.

### 3. Generation

- `Generate Texture`: sends the current paint request to the server, then
  uploads the composited result back so both sides agree.
- `Clear Faces`: asks the server to clear selected regions.
- `Clear All`: clears the paint texture on both sides.
- `Undo`: restores the previous texture snapshot and rolls the server back to
  the same pixels.

There is deliberately no manual sync button. See
[Texture agreement](#texture-agreement).

Generation settings:

- `Dist` — how far each candidate camera sits from the surface it is anchored to.
- `Max Cam` — upper bound on how many cameras a single fill uses.
- `Clip to Faces`
- `Dilate`
- `Soft Add`

Camera selection behavior:

- The server places one candidate camera on each selected face, looking down
  that face's normal at `Dist`.
- It then picks greedily: the camera covering the most selected texture area
  wins, the area it covered is subtracted, and each next camera is the one
  covering the most of what is still uncovered.
- Selection stops at `Max Cam`, or earlier when no remaining camera would add
  any new area. There are no camera modes to choose between.

Texture resolution behavior:

- `update_texture_4k` can be loaded from config or edited in the Configuration section before painting starts.
- Default behavior is `true`, which uses `4096 x 4096` textures.
- Set `update_texture_4k: false` in the config file or disable Use 4K Texture before loading a paint mesh to use `1024 x 1024` textures.
- The toggle is disabled once a paint mesh or paint texture is loaded, matching the config loader's session lock.

Face selection behavior:

- If the paint mesh is in Edit Mode, selected faces are sent as `target_faces`.
- If no faces are selected, the add-on sends an empty list and the server decides how to interpret that request.

## Config File

Example config:

```yaml
server_url: ws://localhost:10017/websocket
mesh_folder: /path/to/meshes
single_views_folder: /path/to/reference/images
single_views_texture_folder: /path/to/reference/textures
brushes_folder: /path/to/brushes
update_texture_4k: true
```

Notes:

- Panel edits update the scene settings; they do not rewrite the YAML file. Reference texture lookup and brush icon reads/writes use the current folder values. Click the Brush Library refresh button to rescan a changed brushes folder.
- Folder fields support absolute paths and Blender's `//` paths relative to the blend file.
- After editing the server URL, click Reconnect to switch the active connection.
- The legacy config key `mesh_file_path` is accepted as an alias for `mesh_folder`.
- Unused entries (`single_views_cam_folder`, `preload_mode`, and `cache_folder`) have been removed. Older YAML files containing them still load; these keys are ignored.
- The server implementation is not included in this repository.

## Websocket Protocol

Full details in [`MESSAGING.md`](./MESSAGING.md). In short:

- Every message, in both directions, is tagged: `{"type": ..., "data": {...}}`
  for JSON, and a `type` key inside the binary payload for pixel data.
- The client routes each incoming message to a registered handler
  (`ws_client.register_handler(msg_type, fn)`); there is exactly one consumer
  per type.
- Pixel payloads travel as **uint8** (tagged `dtype`) in chunks of at most
  10 MB. A 4K RGBA texture is 7 frames / 67 MB, down from 269 frames / 268 MB.

Requests the add-on sends:

```json
{"type": "add_ref_mesh", "data": {"mesh_name": "chair"}}
{"type": "add_pnt_mesh", "data": {"mesh_name": "chair", "paint_mesh_name": "chair_pnt", "high_res": false}}
{"type": "add_brush", "data": {"brush_name": "MyBrush", "brush_type": "AutoSampledReferenceBrush", "brush_mesh": "chair", "sv_id": 1, "cam_dist": 0.75, "cam_fov": 0.4}}
{"type": "fill", "data": {"target_faces": [0, 1], "mesh_name": "chair_pnt", "brush_name": "MyBrush", "high_res": false, "max_cameras": 3, "cam_dist": 0.75, "cam_fov": 0.4, "dilate": true, "clip_fill_to_faces": true, "syncmvd": false}}
{"type": "clear_face", "data": {"target_faces": [0, 1], "mesh_name": "chair_pnt", "high_res": false}}
{"type": "clear_all_texture", "data": {"paint_mesh_name": "chair_pnt"}}
```

Replies the add-on understands: `texture_chunk`, `brush_icon`, `brush_status`,
`fill_status`, `error`, `status`. Untagged replies from an older server are
still recognised for `texture_chunk` and `brush_icon`.

## Texture agreement

The server holds the authoritative texture and persists it; Blender holds a
copy. They are kept identical automatically:

| Operation | How the two sides converge |
|---|---|
| Generate Texture | The server persists its result, then the client composites it locally (soft merge) and pushes the composited texture back. |
| Clear Faces | The server clears and persists; the client adopts the returned texture verbatim, so no push is needed. |
| Clear All | Both sides zero the same texture. |
| Undo | The client restores a history snapshot and pushes it, since the server keeps no history of its own. |
| Load Texture | The client uploads the newly loaded texture. |

The `Sync to Server` button is gone. It was optional, so the two copies drifted:
a fill was never written back, which left the server conditioning every stroke
on the pre-session texture while the client accumulated the visible result.

The sync row in the Generation panel shows `syncing ...` then `in sync with
server`, and turns red if a push fails, so a stale server copy is visible
rather than silent.

**Cost.** A push is a full texture upload — 67 MB at 4K, uint8. Dirty-region
transfer would cut this by roughly 10-100x and is the next planned change.

## Progress and cancellation

Long server operations report progress instead of freezing the panel:

- **Generation** shows `inference 2/5`, then `receiving view 1/1 (3/7 chunks)`.
  `Generate Texture` and `Clear Faces` are disabled while a fill is in flight.
- **Brush Library** shows `preparing <name>` and disables the create buttons.
- Failures arrive as an `error` message and are shown in red, so the panel
  never sits in a permanent "working" state. Both operations also have local
  timeouts (120 s for a fill, 300 s for a brush).

## Minimal Mock Server

`mock_server.py` implements the protocol with solid-colour responses, enough to
exercise the add-on end to end without a GPU:

```bash
python mock_server.py --port 10017 --texture-size 1024 --brush-delay 3
```

- `--texture-size` must match the add-on's paint texture size, so keep
  `update_texture_4k: false` in the config when using the default `1024`.
- `--brush-delay` fakes slow brush preparation, so you can confirm the panel
  stays responsive and shows `preparing ...` while the server works.

## Tests

```bash
python -m unittest discover -s tests -t .
```

The suite stubs Blender, so it runs in any Python with `numpy`, `torch` and
`tornado`. `tests/test_loopback.py` additionally drives the real websocket
client against `mock_server.py` over a socket and is skipped when the
`websockets` package is not installed.

## Development Notes

- The add-on keeps a singleton websocket client across script reloads.
  `WSClient._ensure_runtime_state()` backfills fields added after that
  singleton was constructed, so reloading after an update does not raise.
- All server messages are applied on the Blender main thread through the
  `poll_messages` timer; handlers call `tag_redraw_all()` so timer-driven state
  changes repaint the sidebar.
- The send queue is constructed **inside** the worker event loop. Building it
  on the main thread binds it to the wrong loop on Python <= 3.9 and silently
  drops every send; Blender 4.x ships Python 3.11, where it happens to work.
- Texture history is stored as Blender image copies to support undo.

## Missing Information

- The production websocket backend is not in this repository, so brush
  semantics and server-side fill behaviour are documented from the client
  contract plus the server sources under
  `material-superres-private/glaze_interactive/`.
- Blender-side behaviour (panel repaint, no UI freeze, icon rendering) is not
  covered by the automated tests; it needs a manual pass in Blender against
  `mock_server.py`.
- Material setup assumptions are simple: the add-on looks for a Principled BSDF
  and swaps the Base Color image node.
