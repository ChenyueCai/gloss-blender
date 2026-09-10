"""Minimum viable Glaze server, for testing the add-on without a GPU.

Speaks the tagged protocol in ``protocol.py``: every reply carries a ``type``
so the add-on's router can deliver it to exactly one consumer.

Run from the repository root::

    python mock_server.py --port 10017

``--brush-delay`` simulates slow brush preparation so you can watch the panel
stay responsive and show progress while the server works.
"""

import argparse
import asyncio
import json

import numpy as np
import websockets

from protocol import (
    DTYPE_UINT8,
    MSG_BRUSH_ICON,
    MSG_BRUSH_STATUS,
    MSG_ERROR,
    MSG_FILL_STATUS,
    MSG_STATUS,
    MSG_TEXTURE_CHUNK,
    MSG_TEXTURE_SYNCED,
    STATE_PREPARING,
    STATE_READY,
)
from utils.io import encode_pixels, from_binary, send_large_image, to_binary


def make_rgba_texture(size=1024, rgb=(0.85, 0.25, 0.15)):
    """Build a flat solid-colour RGBA texture in ``[0, 1]``."""
    texture = np.zeros((size, size, 4), dtype=np.float32)
    texture[..., 0], texture[..., 1], texture[..., 2] = rgb
    texture[..., 3] = 1.0
    return texture


def make_icon(size=256, rgb=(0.2, 0.6, 0.9)):
    """Build a flat solid-colour RGB icon in ``[0, 1]``."""
    icon = np.zeros((size, size, 3), dtype=np.float32)
    icon[..., 0], icon[..., 1], icon[..., 2] = rgb
    return icon


async def send_json(ws, msg_type, data):
    """Send one tagged JSON message."""
    await ws.send(json.dumps({"type": msg_type, "data": data}))


async def send_texture(ws, texture, view_id=0, num_views=1):
    """Send a texture as tagged, chunked, uint8 binary frames."""
    msgs = send_large_image(
        "texture", "backproject", texture,
        msg_type=MSG_TEXTURE_CHUNK, dtype=DTYPE_UINT8,
        extra={"view_id": view_id, "num_views": num_views},
    )
    for msg in msgs:
        await ws.send(to_binary(msg))
    return len(msgs)


async def handle_add_brush(ws, data, brush_delay):
    """Report progress, then deliver a small uint8 icon."""
    brush_name = data.get("brush_name", "brush")
    await send_json(ws, MSG_BRUSH_STATUS, {
        "brush_name": brush_name, "state": STATE_PREPARING,
        "message": f"preparing {brush_name}",
    })
    if brush_delay:
        await asyncio.sleep(brush_delay)
    payload, dtype_tag = encode_pixels(make_icon(), dtype=DTYPE_UINT8)
    await ws.send(to_binary({
        "type": MSG_BRUSH_ICON,
        "brush_name": brush_name,
        "dtype": dtype_tag,
        "image": payload,
    }))
    await send_json(ws, MSG_BRUSH_STATUS, {
        "brush_name": brush_name, "state": STATE_READY,
        "message": f"ready: {brush_name}",
    })


def make_handler(texture_size=1024, brush_delay=0.0):
    """Build a websocket handler closed over the mock's settings."""

    async def handler(websocket):
        image_chunks = {}
        async for message in websocket:
            if isinstance(message, bytes):
                payload = from_binary(message)
                if payload.get("type") == "image":
                    idx = payload["chunk_index"]
                    image_chunks[idx] = payload["image"]
                    if len(image_chunks) == payload["chunk_total"]:
                        total = sum(
                            int(np.asarray(c).size) for c in image_chunks.values()
                        )
                        image_chunks = {}
                        await send_json(websocket, MSG_TEXTURE_SYNCED, {
                            "message": f"server texture updated ({total} values)",
                            "mesh_name": payload.get("mesh_name"),
                        })
                continue

            try:
                request = json.loads(message)
            except ValueError:
                await send_json(websocket, MSG_ERROR, {
                    "message": "malformed JSON", "context": "",
                })
                continue

            msg_type, data = request.get("type"), request.get("data") or {}
            print("received:", msg_type)

            try:
                if msg_type == "add_brush":
                    await handle_add_brush(websocket, data, brush_delay)
                elif msg_type in {"fill", "clear_face"}:
                    await send_json(websocket, MSG_FILL_STATUS, {
                        "stage": "inference", "current": 1, "total": 1,
                    })
                    n = await send_texture(websocket, make_rgba_texture(texture_size))
                    await send_json(websocket, MSG_FILL_STATUS, {
                        "stage": "ready", "current": n, "total": n,
                    })
                else:
                    await send_json(websocket, MSG_STATUS, {
                        "message": f"ok: {msg_type}",
                    })
            except Exception as exc:  # surface failures instead of hanging
                await send_json(websocket, MSG_ERROR, {
                    "message": f"{type(exc).__name__}: {exc}",
                    "context": msg_type or "",
                })

    return handler


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=10017)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--texture-size", type=int, default=1024,
                        help="Must match the add-on's paint texture size.")
    parser.add_argument("--brush-delay", type=float, default=0.0,
                        help="Fake brush preparation time, in seconds.")
    args = parser.parse_args()

    handler = make_handler(args.texture_size, args.brush_delay)
    async with websockets.serve(handler, args.host, args.port,
                                max_size=128 * 1024 * 1024):
        print(f"mock server listening on ws://{args.host}:{args.port}")
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
