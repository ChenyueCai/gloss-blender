"""Pixel codec, chunking and reassembly contract."""

import math
import pathlib
import sys
import unittest

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import harness  # noqa: E402

harness.install()

from gloss_blender import protocol  # noqa: E402
from gloss_blender.utils import io  # noqa: E402


class TestChunkSizeAgreement(unittest.TestCase):
    """The 1 MB vs 10 MB divergence that split 4K textures into ~269 frames."""

    def test_addon_matches_protocol(self):
        self.assertEqual(io.DEFAULT_CHUNK_BYTES, protocol.DEFAULT_CHUNK_BYTES)


class TestPixelCodec(unittest.TestCase):
    def test_uint8_roundtrip_within_quantization_error(self):
        original = np.linspace(0.0, 1.0, 4096, dtype=np.float32)
        encoded, tag = io.encode_pixels(original, dtype=io.DTYPE_UINT8)
        self.assertEqual(tag, io.DTYPE_UINT8)
        self.assertEqual(encoded.dtype, np.uint8)
        restored = io.decode_pixels(encoded, tag)
        self.assertLessEqual(float(np.abs(restored - original).max()), 1.0 / 255.0)

    def test_uint8_is_exact_for_8bit_values(self):
        """Textures originate as 8-bit, so the common case is lossless."""
        original = (np.arange(256, dtype=np.float32) / 255.0)
        encoded, tag = io.encode_pixels(original, dtype=io.DTYPE_UINT8)
        np.testing.assert_array_equal(io.decode_pixels(encoded, tag), original)

    def test_float32_passthrough_is_bit_exact(self):
        original = np.random.RandomState(0).rand(1024).astype(np.float32)
        encoded, tag = io.encode_pixels(original, dtype=io.DTYPE_FLOAT32)
        np.testing.assert_array_equal(io.decode_pixels(encoded, tag), original)

    def test_out_of_range_values_are_clamped(self):
        encoded, tag = io.encode_pixels(
            np.array([-3.0, 0.5, 9.0], dtype=np.float32), dtype=io.DTYPE_UINT8)
        restored = io.decode_pixels(encoded, tag)
        self.assertAlmostEqual(float(restored[0]), 0.0)
        self.assertAlmostEqual(float(restored[2]), 1.0)


class TestChunking(unittest.TestCase):
    @staticmethod
    def expected_chunks(num_elements, bytes_per_elem, chunk_bytes):
        per_chunk = max(1, chunk_bytes // bytes_per_elem)
        return math.ceil(num_elements / per_chunk)

    def test_chunk_count_matches_formula(self):
        pixels = np.zeros(3_000_000, dtype=np.float32)
        msgs = io.send_large_image("t", "task", pixels, dtype=io.DTYPE_UINT8)
        self.assertEqual(
            len(msgs),
            self.expected_chunks(pixels.size, 1, io.DEFAULT_CHUNK_BYTES),
        )
        self.assertTrue(all(m["type"] == "image" for m in msgs))
        self.assertTrue(all(m["dtype"] == io.DTYPE_UINT8 for m in msgs))

    def test_4k_texture_frame_count_before_and_after(self):
        """A 4096x4096 RGBA texture: 269 float32/1MB frames -> 7 uint8/10MB."""
        elements = 4096 * 4096 * 4
        before = self.expected_chunks(elements, 4, 10_000_00)   # old server default
        after = self.expected_chunks(elements, 1, io.DEFAULT_CHUNK_BYTES)
        self.assertEqual(before, 269)
        self.assertEqual(after, 7)
        self.assertLess(after, before)

    def test_extra_fields_ride_on_every_chunk(self):
        msgs = io.send_large_image(
            "t", "task", np.zeros(32, dtype=np.float32),
            msg_type=protocol.MSG_TEXTURE_CHUNK,
            extra={"view_id": 3, "num_views": 2},
        )
        for msg in msgs:
            self.assertEqual(msg["type"], protocol.MSG_TEXTURE_CHUNK)
            self.assertEqual(msg["view_id"], 3)
            self.assertEqual(msg["num_views"], 2)


class TestChunkAssembler(unittest.TestCase):
    def _messages(self, pixels, chunk_bytes=1024):
        return io.send_large_image(
            "t", "task", pixels, chunk_size=chunk_bytes,
            msg_type=protocol.MSG_TEXTURE_CHUNK, dtype=io.DTYPE_UINT8,
            extra={"view_id": 0, "num_views": 1},
        )

    def test_reassembles_multi_chunk_payload(self):
        pixels = (np.arange(8192, dtype=np.float32) % 256) / 255.0
        msgs = self._messages(pixels)
        self.assertGreater(len(msgs), 1, "test needs a genuinely chunked payload")

        assembler = io.ChunkAssembler()
        results = [assembler.add(m) for m in msgs]
        self.assertTrue(all(r is None for r in results[:-1]))
        view_id, restored = results[-1]
        self.assertEqual(view_id, 0)
        np.testing.assert_allclose(np.asarray(restored), pixels, atol=1.0 / 255.0)

    def test_out_of_order_chunks_reassemble_correctly(self):
        pixels = (np.arange(8192, dtype=np.float32) % 256) / 255.0
        msgs = self._messages(pixels)
        assembler = io.ChunkAssembler()
        completed = None
        for msg in reversed(msgs):
            completed = assembler.add(msg) or completed
        self.assertIsNotNone(completed)
        np.testing.assert_allclose(
            np.asarray(completed[1]), pixels, atol=1.0 / 255.0)

    def test_duplicate_chunks_are_ignored(self):
        pixels = (np.arange(4096, dtype=np.float32) % 256) / 255.0
        msgs = self._messages(pixels)
        assembler = io.ChunkAssembler()
        for msg in msgs[:-1]:
            assembler.add(msg)
            assembler.add(msg)  # duplicate
        self.assertIsNotNone(assembler.add(msgs[-1]))

    def test_progress_reports_partial_state(self):
        pixels = (np.arange(8192, dtype=np.float32) % 256) / 255.0
        msgs = self._messages(pixels)
        assembler = io.ChunkAssembler()
        assembler.add(msgs[0])
        received, expected = assembler.progress(0)
        self.assertEqual(received, 1)
        self.assertEqual(expected, len(msgs))

    def test_interleaved_views_do_not_mix(self):
        a = np.full(2048, 0.25, dtype=np.float32)
        b = np.full(2048, 0.75, dtype=np.float32)
        msgs_a = io.send_large_image("a", "t", a, chunk_size=1024,
                                     dtype=io.DTYPE_UINT8,
                                     extra={"view_id": 0, "num_views": 2})
        msgs_b = io.send_large_image("b", "t", b, chunk_size=1024,
                                     dtype=io.DTYPE_UINT8,
                                     extra={"view_id": 1, "num_views": 2})
        assembler = io.ChunkAssembler()
        done = {}
        for msg_a, msg_b in zip(msgs_a, msgs_b):
            for result in (assembler.add(msg_a), assembler.add(msg_b)):
                if result:
                    done[result[0]] = np.asarray(result[1])
        self.assertEqual(set(done), {0, 1})
        np.testing.assert_allclose(done[0], a, atol=1 / 255)
        np.testing.assert_allclose(done[1], b, atol=1 / 255)


if __name__ == "__main__":
    unittest.main()
