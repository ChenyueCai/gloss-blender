"""Message routing: the regression tests for dropped brush icons.

Before the router existed, ``receive_queue`` had two destructive consumers --
``poll_str_messages`` (always registered, 0.2 s) and ``poll_bin_messages``
(per brush, 0.1 s). Each popped every message and silently discarded the kinds
it did not recognise, so a brush icon was eaten by whichever timer fired first.
``test_mixed_stream_reaches_every_handler`` is the direct regression for that.
"""

import json
import pathlib
import sys
import unittest

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import harness  # noqa: E402

harness.install()

from gloss_blender import protocol  # noqa: E402
from gloss_blender.client import WSClient, infer_message_type  # noqa: E402
from gloss_blender.utils import io  # noqa: E402


def fresh_client():
    """Return the client singleton with routing state reset for one test."""
    client = WSClient()
    client.receive_queue = []
    client._handlers = {}
    for key in client.stats:
        client.stats[key] = 0
    return client


def icon_frame(brush_name="MyBrush", size=8):
    pixels = np.full((size, size, 3), 0.5, dtype=np.float32)
    payload, tag = io.encode_pixels(pixels, dtype=io.DTYPE_UINT8)
    return io.to_binary({
        "type": protocol.MSG_BRUSH_ICON,
        "brush_name": brush_name,
        "dtype": tag,
        "image": payload,
    })


def texture_frames(num_values=4096, chunk_bytes=1024):
    pixels = (np.arange(num_values, dtype=np.float32) % 256) / 255.0
    msgs = io.send_large_image(
        "texture", "backproject", pixels, chunk_size=chunk_bytes,
        msg_type=protocol.MSG_TEXTURE_CHUNK, dtype=io.DTYPE_UINT8,
        extra={"view_id": 0, "num_views": 1},
    )
    return pixels, [io.to_binary(m) for m in msgs]


class TestMessageTypeInference(unittest.TestCase):
    def test_explicit_type_wins(self):
        self.assertEqual(
            infer_message_type({"type": protocol.MSG_BRUSH_ICON}),
            protocol.MSG_BRUSH_ICON,
        )

    def test_legacy_texture_chunk_is_recognised(self):
        """An old untagged server still routes correctly."""
        self.assertEqual(
            infer_message_type({"chunk_total": 4, "view_id": 0}),
            protocol.MSG_TEXTURE_CHUNK,
        )

    def test_legacy_brush_icon_is_recognised(self):
        self.assertEqual(
            infer_message_type({"brush icon": object()}),
            protocol.MSG_BRUSH_ICON,
        )

    def test_unknown_payload_falls_back_to_status(self):
        self.assertEqual(infer_message_type({}), protocol.MSG_STATUS)


class TestRouting(unittest.TestCase):
    def test_mixed_stream_reaches_every_handler(self):
        """REGRESSION: a status line must not consume the brush icon.

        This is the exact interleaving that used to lose icons: text frames
        arriving before and between the binary ones.
        """
        client = fresh_client()
        seen = {"status": [], "icon": [], "texture": []}
        client.register_handler(
            protocol.MSG_STATUS,
            lambda p: seen["status"].append(p["data"]["message"]))
        client.register_handler(
            protocol.MSG_BRUSH_ICON,
            lambda p: seen["icon"].append(p["brush_name"]))
        client.register_handler(
            protocol.MSG_TEXTURE_CHUNK,
            lambda p: seen["texture"].append(p["chunk_index"]))

        pixels, tex_frames = texture_frames()
        client.receive_queue = [
            json.dumps({"type": protocol.MSG_STATUS,
                        "data": {"message": "Loaded ref chair."}}),
            icon_frame("MyBrush"),
            json.dumps({"type": protocol.MSG_STATUS,
                        "data": {"message": "Added MyBrush."}}),
            *tex_frames,
            json.dumps({"type": protocol.MSG_STATUS,
                        "data": {"message": "done"}}),
        ]
        expected_total = len(client.receive_queue)

        client.poll_messages()

        self.assertEqual(seen["icon"], ["MyBrush"], "brush icon was dropped")
        self.assertEqual(len(seen["status"]), 3)
        self.assertEqual(len(seen["texture"]), len(tex_frames))
        self.assertEqual(client.stats["received"], expected_total)
        self.assertEqual(client.stats["dispatched"], expected_total)
        self.assertEqual(client.stats["unhandled"], 0, "a message was dropped")
        self.assertEqual(client.stats["decode_errors"], 0)

    def test_texture_reassembles_bit_exactly_through_the_router(self):
        client = fresh_client()
        assembler = io.ChunkAssembler()
        done = {}

        def on_chunk(payload):
            result = assembler.add(payload)
            if result:
                done[result[0]] = np.asarray(result[1])

        client.register_handler(protocol.MSG_TEXTURE_CHUNK, on_chunk)
        pixels, frames = texture_frames()
        client.receive_queue = list(frames)
        client.poll_messages()

        self.assertIn(0, done)
        np.testing.assert_allclose(done[0], pixels, atol=1.0 / 255.0)

    def test_two_brush_listeners_do_not_steal_each_others_icons(self):
        """Concurrent brush creations must each get their own icon."""
        client = fresh_client()
        got = {}

        def listener(name):
            def handler(payload):
                if payload.get("brush_name") != name:
                    return
                got[name] = True
                client.unregister_handler(protocol.MSG_BRUSH_ICON, handler)
            return handler

        client.register_handler(protocol.MSG_BRUSH_ICON, listener("alpha"))
        client.register_handler(protocol.MSG_BRUSH_ICON, listener("beta"))
        client.receive_queue = [icon_frame("beta"), icon_frame("alpha")]
        client.poll_messages()

        self.assertEqual(got, {"alpha": True, "beta": True})

    def test_handler_exception_does_not_stall_the_queue(self):
        client = fresh_client()
        delivered = []

        def boom(payload):
            raise RuntimeError("handler is broken")

        client.register_handler(protocol.MSG_STATUS, boom)
        client.register_handler(
            protocol.MSG_STATUS, lambda p: delivered.append(p))
        client.receive_queue = [
            json.dumps({"type": protocol.MSG_STATUS, "data": {"message": "a"}}),
            json.dumps({"type": protocol.MSG_STATUS, "data": {"message": "b"}}),
        ]
        client.poll_messages()

        self.assertEqual(len(delivered), 2, "a raising handler blocked others")
        self.assertEqual(client.stats["handler_errors"], 2)
        self.assertEqual(client.receive_queue, [])

    def test_unregister_stops_delivery(self):
        client = fresh_client()
        calls = []
        handler = calls.append
        client.register_handler(protocol.MSG_STATUS, handler)
        client.unregister_handler(protocol.MSG_STATUS, handler)
        client.receive_queue = [
            json.dumps({"type": protocol.MSG_STATUS, "data": {"message": "x"}})]
        client.poll_messages()
        self.assertEqual(calls, [])
        self.assertEqual(client.stats["unhandled"], 1)

    def test_unregistering_an_unknown_handler_is_safe(self):
        client = fresh_client()
        client.unregister_handler(protocol.MSG_STATUS, lambda p: None)

    def test_legacy_bare_string_still_routes_as_status(self):
        client = fresh_client()
        seen = []
        client.register_handler(
            protocol.MSG_STATUS, lambda p: seen.append(p["data"]["message"]))
        client.receive_queue = ["Loaded ref chair."]
        client.poll_messages()
        self.assertEqual(seen, ["Loaded ref chair."])

    def test_malformed_binary_is_counted_not_fatal(self):
        client = fresh_client()
        seen = []
        client.register_handler(protocol.MSG_STATUS, seen.append)
        client.receive_queue = [
            b"\x00\x01\x02not-a-valid-frame",
            json.dumps({"type": protocol.MSG_STATUS, "data": {"message": "ok"}}),
        ]
        client.poll_messages()
        self.assertEqual(client.receive_queue, [], "queue must always drain")
        self.assertEqual(len(seen), 1, "later messages still delivered")


if __name__ == "__main__":
    unittest.main()


class TestReloadSafety(unittest.TestCase):
    """The singleton outlives add-on reloads; new fields must backfill."""

    def test_start_backfills_fields_added_after_construction(self):
        client = WSClient.__new__(WSClient)
        client._initialized = True   # as if built by an older module version
        client._running = True       # short-circuit the actual startup
        client.uri = "ws://x/websocket"
        client.start()               # must not raise AttributeError
        self.assertTrue(hasattr(client, "_queue_ready"))
        self.assertTrue(hasattr(client, "_handlers"))
        self.assertTrue(hasattr(client, "stats"))

    def test_register_handler_backfills(self):
        client = WSClient.__new__(WSClient)
        client._initialized = True
        client.register_handler(protocol.MSG_STATUS, lambda p: None)
        self.assertIn(protocol.MSG_STATUS, client._handlers)
