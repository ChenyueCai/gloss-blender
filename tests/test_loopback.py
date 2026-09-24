"""End-to-end: the real WSClient against the real mock server, over a socket.

This is the closest thing to the Blender workflow that can run without
Blender. It starts ``mock_server.py`` in-process, drives the actual
``WSClient`` (real asyncio thread, real frames), and asserts that a brush icon
and a texture both arrive intact when their messages are interleaved -- the
scenario that used to lose the icon.

Skipped when the ``websockets`` package is unavailable; it ships with the
add-on's Blender environment, not the cluster conda envs.
"""

import pathlib
import sys
import threading
import time
import unittest

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import harness  # noqa: E402

HAVE_WEBSOCKETS = harness.real_websockets_available()

harness.install()

if HAVE_WEBSOCKETS:
    import asyncio
    import websockets

from gloss_blender import protocol  # noqa: E402
from gloss_blender.utils import io  # noqa: E402

ADDON_DIR = pathlib.Path(__file__).resolve().parent.parent
TEXTURE_SIZE = 256


def load_mock_server():
    """Import ``mock_server.py`` by path (repo root is not a package)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "gloss_mock_server", ADDON_DIR / "mock_server.py")
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(ADDON_DIR))
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(HAVE_WEBSOCKETS, "websockets not installed")
class TestLoopback(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mock = load_mock_server()
        cls.ready = threading.Event()
        cls.loop = None
        cls.port = None

        def run_server():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            cls.loop = loop

            async def serve():
                handler = cls.mock.make_handler(TEXTURE_SIZE, brush_delay=0.2)
                # Port 0: let the OS pick, so a lingering TIME_WAIT socket
                # from a previous run cannot fail the suite.
                async with websockets.serve(
                    handler, "127.0.0.1", 0, max_size=128 * 1024 * 1024
                ) as server:
                    cls.port = next(iter(server.sockets)).getsockname()[1]
                    cls.ready.set()
                    await asyncio.Future()

            try:
                loop.run_until_complete(serve())
            except Exception:
                import traceback
                traceback.print_exc()
                cls.ready.set()

        cls.thread = threading.Thread(target=run_server, daemon=True)
        cls.thread.start()
        assert cls.ready.wait(timeout=15), "mock server did not start"
        assert cls.port, "mock server failed to bind (see traceback above)"

    def setUp(self):
        from gloss_blender.client import WSClient
        self.client = WSClient()
        self.client.stop()
        self.client.receive_queue = []
        self.client._handlers = {}
        for key in self.client.stats:
            self.client.stats[key] = 0
        self.client.configure(f"ws://127.0.0.1:{self.port}/websocket")
        self.client.start()
        deadline = time.time() + 15
        while not self.client.is_connected and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(self.client.is_connected, "client never connected")

    def tearDown(self):
        self.client.stop()

    def pump(self, until, timeout=30):
        """Drive the router from this thread until ``until()`` or timeout."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.client.poll_messages()
            if until():
                return True
            time.sleep(0.02)
        return False

    def test_brush_icon_and_texture_both_arrive_when_interleaved(self):
        """REGRESSION: the icon survives a concurrent texture stream."""
        icons, statuses, fill_status = [], [], []
        assembler = io.ChunkAssembler()
        textures = {}

        self.client.register_handler(
            protocol.MSG_BRUSH_ICON,
            lambda p: icons.append((p["brush_name"],
                                    np.asarray(io.decode_pixels(p["image"],
                                                                p["dtype"])))))
        self.client.register_handler(
            protocol.MSG_STATUS, lambda p: statuses.append(p["data"]["message"]))
        self.client.register_handler(
            protocol.MSG_BRUSH_STATUS,
            lambda p: statuses.append(p["data"]["state"]))
        self.client.register_handler(
            protocol.MSG_FILL_STATUS, lambda p: fill_status.append(p["data"]))

        def on_chunk(payload):
            result = assembler.add(payload)
            if result:
                textures[result[0]] = np.asarray(result[1])

        self.client.register_handler(protocol.MSG_TEXTURE_CHUNK, on_chunk)

        # Fire both requests back to back so their replies interleave.
        self.client.send({"type": "add_brush",
                          "data": {"brush_name": "MyBrush"}})
        self.client.send({"type": "fill", "data": {"target_faces": [0, 1]}})

        ok = self.pump(lambda: icons and textures)
        self.assertTrue(ok, f"timed out; icons={len(icons)} textures={len(textures)}")

        self.assertEqual(icons[0][0], "MyBrush")
        self.assertEqual(icons[0][1].shape, (256, 256, 3))
        self.assertEqual(textures[0].size, TEXTURE_SIZE * TEXTURE_SIZE * 4)
        self.assertIn(protocol.STATE_READY, statuses)
        self.assertTrue(fill_status, "no fill progress reported")
        self.assertEqual(self.client.stats["unhandled"], 0,
                         "a message went unrouted")

    def test_texture_content_survives_the_round_trip(self):
        assembler = io.ChunkAssembler()
        textures = {}

        def on_chunk(payload):
            result = assembler.add(payload)
            if result:
                textures[result[0]] = np.asarray(result[1])

        self.client.register_handler(protocol.MSG_TEXTURE_CHUNK, on_chunk)
        self.client.send({"type": "fill", "data": {"target_faces": [0]}})
        self.assertTrue(self.pump(lambda: textures), "no texture received")

        expected = self.mock.make_rgba_texture(TEXTURE_SIZE).reshape(-1)
        np.testing.assert_allclose(textures[0], expected, atol=1.0 / 255.0)

    def test_server_errors_surface_instead_of_hanging(self):
        errors = []
        self.client.register_handler(protocol.MSG_ERROR, errors.append)
        # Not valid JSON -> the server must answer with a tagged error.
        self.client.send(b"" if False else "{not json")
        self.assertTrue(self.pump(lambda: errors, timeout=15),
                        "server error never reached the client")
        self.assertIn("message", errors[0]["data"])

    def test_uploaded_texture_is_acknowledged(self):
        """A push must be acknowledged, so the panel can show "in sync"."""
        statuses = []
        self.client.register_handler(
            protocol.MSG_TEXTURE_SYNCED,
            lambda p: statuses.append(p["data"]["message"]))

        pixels = np.full(TEXTURE_SIZE * TEXTURE_SIZE * 4, 0.5, dtype=np.float32)
        msgs = io.send_large_image(
            "texture", "set_texture", pixels,
            msg_type="image", dtype=io.DTYPE_UINT8,
            extra={"mesh_name": "chair_pnt"},
        )
        for msg in msgs:
            self.client.send(io.to_binary(msg))

        self.assertTrue(
            self.pump(lambda: any("server texture updated" in s for s in statuses)),
            f"upload was not acknowledged; saw {statuses}")


if __name__ == "__main__":
    unittest.main()
