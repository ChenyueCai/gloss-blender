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
- `ui_panel.py`: panel layout and user-facing workflow.
- `operators.py`: Blender operators for loading assets, generation, syncing, and brush actions.
- `backend.py`: backend-facing request helpers and streamed texture update handling.
- `client.py`: websocket client and reconnect logic.
- `utils/mesh.py`: mesh import, texture application, and texture update utilities.
- `utils/io.py`: binary encoding helpers used for large texture payloads.
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
- `Load Texture`: loads a paint texture onto the paint mesh.
- `Auto Sync`: immediately uploads the loaded paint texture to the server.

Reference image behavior:

- The selected file name must start with `view<number>.`, such as `view0001.png` or `view0001.basecolor.png`.
- The add-on always loads `single_views_texture_folder/<mesh>/view####.png` for that view ID. It does not use the selected image directly.
- If the view ID cannot be resolved, the texture folder is unset, or the requested texture file is missing, it reports `requested texture does not exist` and cancels.

### 3. Generation

- `Generate Texture`: sends the current paint request to the server.
- `Clear Faces`: asks the server to clear selected regions.
- `Sync to Server`: uploads the current Blender texture to the server.
- `Clear All`: clears the local paint texture and notifies the server.
- `Undo`: restores the previous texture history snapshot.

Generation settings:

- `Dist`
- `Max Cam`
- `Clip to Faces`
- `Dilate`
- `Soft Add`

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

The add-on currently sends two kinds of payloads.

JSON messages:

```json
{"type": "add_ref_mesh", "data": {"mesh_name": "chair"}}
{"type": "add_pnt_mesh", "data": {"mesh_name": "chair", "paint_mesh_name": "chair_pnt", "high_res": false}}
{"type": "fill", "data": {"target_faces": [0, 1], "mesh_name": "chair_pnt", "brush_name": "MyBrush", "high_res": false, "max_cameras": 3, "cam_dist": 0.75, "cam_fov": 0.4, "dilate": true}}
{"type": "clear_face", "data": {"target_faces": [0, 1], "mesh_name": "chair_pnt"}}
{"type": "clear_all_texture", "data": {"paint_mesh_name": "chair_pnt"}}
```

Binary messages:

- `Sync to Server` and `Auto Sync` use `utils.io.send_large_image(...)`.
- The server is expected to return binary messages encoded with `utils.io.to_binary(...)`.

Expected binary response fields:

- `view_id`
- `chunk_index`
- `chunk_total`
- `num_views`
- `image`

`image` is expected to decode into a float tensor that reshapes to `H x W x 4`, matching the active Blender paint texture size.

## Minimal Mock Server

This repository does not include the production backend, but the script below is enough to test the add-on end to end with a solid-color response.

Run the included mock server from the repository root:

```bash
python mock_server.py
```

`mock_server.py`:

```python
import asyncio
import json

import torch
import websockets

from utils.io import to_binary


def make_rgba_texture(size=1024):
    texture = torch.zeros((size, size, 4), dtype=torch.float32)
    texture[..., 0] = 0.85
    texture[..., 1] = 0.25
    texture[..., 2] = 0.15
    texture[..., 3] = 1.0
    return texture.reshape(-1)


async def handler(websocket):
    async for message in websocket:
        if isinstance(message, str):
            payload = json.loads(message)
            print("received:", payload["type"])

            if payload["type"] in {"fill", "clear_face"}:
                response = {
                    "view_id": 0,
                    "chunk_index": 0,
                    "chunk_total": 1,
                    "num_views": 1,
                    "image": make_rgba_texture(1024),
                }
                await websocket.send(to_binary(response))


async def main():
    async with websockets.serve(handler, "127.0.0.1", 10017, max_size=2**28):
        print("mock server listening on ws://127.0.0.1:10017")
        await asyncio.Future()


asyncio.run(main())
```

For the mock server, keep `update_texture_4k: false` so the returned `1024 x 1024 x 4` texture matches the add-on's expected size.

## Development Notes

- The add-on keeps a singleton websocket client across script reloads.
- Texture updates are applied on the Blender main thread through a timer-driven poller.
- Texture history is stored as Blender image copies to support undo.

## Missing Information

- The production websocket backend is not present here, so brush semantics and server-side fill behavior are only documented from the client contract.
- There is no automated test suite in this repository for Blender operators or websocket integration.
- Material setup assumptions are simple: the add-on looks for a Principled BSDF and swaps the Base Color image node.
