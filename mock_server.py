import asyncio
import json

import torch
import websockets

from utils.io import from_binary, to_binary


def make_rgba_texture(size=1024):
    """Return a flattened solid-color RGBA texture tensor for tests."""
    texture = torch.zeros((size, size, 4), dtype=torch.float32)
    texture[..., 0] = 0.85
    texture[..., 1] = 0.25
    texture[..., 2] = 0.15
    texture[..., 3] = 1.0
    return texture.reshape(-1)


async def handler(websocket):
    """Respond to fill and clear requests with a single mock texture chunk."""
    async for message in websocket:
        if isinstance(message, bytes):
            payload = from_binary(message)
            print("received binary:", payload.get("task_name", "unknown"))
            continue

        payload = json.loads(message)
        message_type = payload.get("type")
        print("received:", message_type)

        if message_type in {"fill", "clear_face"}:
            high_res = payload.get("data", {}).get("high_res", False)
            size = 4096 if high_res else 1024
            response = {
                "view_id": 0,
                "chunk_index": 0,
                "chunk_total": 1,
                "num_views": 1,
                "image": make_rgba_texture(size),
            }
            await websocket.send(to_binary(response))


async def main():
    """Run the local development websocket server until cancelled."""
    async with websockets.serve(handler, "127.0.0.1", 10017, max_size=2**30):
        print("mock server listening on ws://127.0.0.1:10017/websocket")
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
