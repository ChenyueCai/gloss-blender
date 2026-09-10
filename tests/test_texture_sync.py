"""Client and server must hold the same texture after every operation.

The rule under test:

- a **generate** is composited locally (soft merge), so the client's texture
  ends up ahead of the server's and is pushed back automatically;
- a **clear** is adopted verbatim from the server, so no push is needed;
- there is no manual sync path any more -- an optional sync is how the two
  copies used to drift apart.
"""

import pathlib
import sys
import unittest

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import harness  # noqa: E402

harness.install()

from glaze_blender import backend, protocol  # noqa: E402
from glaze_blender.utils import io  # noqa: E402

SIZE = 2  # 2x2 RGBA keeps payloads tiny


class FakeImage:
    """Stands in for a Blender image datablock."""

    def __init__(self, size=SIZE):
        self.size = (size, size)   # Blender order: (width, height)


class SyncRuleTestCase(unittest.TestCase):
    def setUp(self):
        self.pushes = []
        self.applied = []
        self._saved = {
            name: getattr(backend, name)
            for name in ("get_current_texture", "update_texture",
                         "push_texture_to_server")
        }
        backend.get_current_texture = lambda obj: FakeImage()
        backend.update_texture = lambda obj, image, soft_merge=True: (
            self.applied.append(soft_merge))
        backend.push_texture_to_server = lambda ctx, reason="": (
            self.pushes.append(reason) or True)
        backend.reset_texture_state()

    def tearDown(self):
        for name, fn in self._saved.items():
            setattr(backend, name, fn)
        backend.reset_texture_state()

    def deliver_texture(self):
        """Feed one complete single-view texture through the router handler."""
        pixels = np.full(SIZE * SIZE * 4, 0.5, dtype=np.float32)
        msgs = io.send_large_image(
            "texture", "backproject", pixels,
            msg_type=protocol.MSG_TEXTURE_CHUNK, dtype=io.DTYPE_UINT8,
            extra={"view_id": 0, "num_views": 1},
        )
        for msg in msgs:
            backend.on_texture_chunk(msg)


class TestAutomaticSync(SyncRuleTestCase):
    def test_generate_pushes_the_composited_texture_back(self):
        backend.start_texture_updates(object(), soft_merge=True, operation="fill")
        self.deliver_texture()
        self.assertEqual(len(self.pushes), 1,
                         "a generate must sync the client texture to the server")
        self.assertIn("generate", self.pushes[0])

    def test_generate_composites_locally(self):
        backend.start_texture_updates(object(), soft_merge=True, operation="fill")
        self.deliver_texture()
        self.assertEqual(self.applied, [True], "soft merge was not applied")

    def test_clear_adopts_the_server_texture_without_pushing(self):
        backend.start_texture_updates(object(), soft_merge=False, operation="clear")
        self.deliver_texture()
        self.assertEqual(self.pushes, [],
                         "a clear is adopted verbatim; no push should be needed")
        self.assertEqual(self.applied, [False], "clear must not soft-merge")

    def test_state_is_reset_after_completion(self):
        backend.start_texture_updates(object(), soft_merge=True, operation="fill")
        self.deliver_texture()
        self.assertFalse(backend._TEXTURE_STATE["active"])
        self.assertIsNone(backend._TEXTURE_STATE["operation"])

    def test_late_chunks_after_completion_are_ignored(self):
        """A stale reply must not push a second time or reapply the texture."""
        backend.start_texture_updates(object(), soft_merge=True, operation="fill")
        self.deliver_texture()
        self.deliver_texture()  # duplicate delivery from a retry
        self.assertEqual(len(self.pushes), 1)
        self.assertEqual(len(self.applied), 1)


class TestNoManualSyncPath(unittest.TestCase):
    def test_sync_operator_is_gone(self):
        source = (pathlib.Path(harness.ADDON_DIR) / "operators.py").read_text()
        self.assertNotIn('bl_idname = "glaze.set_texture"', source)

    def test_panel_has_no_sync_button(self):
        source = (pathlib.Path(harness.ADDON_DIR) / "ui_panel.py").read_text()
        self.assertNotIn("glaze.set_texture", source)

    def test_loading_a_texture_always_syncs(self):
        """The sync-on-load toggle is gone; loading always pushes."""
        source = (pathlib.Path(harness.ADDON_DIR) / "backend.py").read_text()
        self.assertNotIn("sync_to_server", source)
        self.assertIn("after loading texture", source)

    def test_undo_syncs_the_restored_texture(self):
        source = (pathlib.Path(harness.ADDON_DIR) / "operators.py").read_text()
        self.assertIn("after undo", source,
                      "undo must roll the server back too")


if __name__ == "__main__":
    unittest.main()
