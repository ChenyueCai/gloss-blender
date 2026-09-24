"""The Generation panel's busy state must always expire.

``ui_panel.py`` derives one flag from one value::

    fill_busy = session.fill_state == 'preparing'
    top_row.enabled = not fill_busy

and that flag gates Generate Texture and Clear Faces. So a
``preparing`` that nothing ever clears does not merely look wrong -- it locks
the user out of the add-on for the rest of the session, with no way back short
of restarting Blender.

The rules under test:

- every published ``preparing`` arms the watchdog, whoever published it and
  whether or not a texture update is in flight behind it;
- a server that keeps reporting progress is never timed out;
- a server that goes quiet always is;
- a request is armed locally *before* it goes on the wire, and a request that
  cannot be tracked is never sent at all;
- server progress for a fill this side is not tracking is ignored, so the
  server's trailing ``ready`` cannot re-grey a panel that already finished;
- loading a file forgets any progress state saved inside it.
"""

import pathlib
import sys
import time
import types
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import harness  # noqa: E402

harness.install()

import bpy  # noqa: E402

from gloss_blender import backend  # noqa: E402
from gloss_blender.protocol import (  # noqa: E402
    STATE_ERROR,
    STATE_PREPARING,
    STATE_READY,
)


def fake_scene():
    """A scene carrying every field the request builders read."""
    return types.SimpleNamespace(
        # base_name() needs a str; type != "MESH" keeps bmesh out of the test.
        current_paint_mesh=types.SimpleNamespace(name="croissant", type="EMPTY"),
        current_brush="brush",
        update_texture_4k=True,
        clip_fill_to_faces=False,
        max_cameras=4,
        cam_dist=1.0,
        cam_fov=40.0,
        dilate=True,
        soft_add=True,
        syncmvd=False,
    )


class FillLivenessTestCase(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.armed_when_sent = []
        self._saved_send = backend.ws_client.send
        self._saved_ws = getattr(backend.ws_client, "ws", None)

        def record(data):
            self.sent.append(data)
            # Snapshot the local state at the moment the request leaves, so the
            # ordering rule is observable rather than assumed.
            self.armed_when_sent.append(backend._TEXTURE_STATE["active"])

        backend.ws_client.send = record
        backend.ws_client.ws = object()  # is_connected -> True

        backend.reset_texture_state()
        backend._FILL_GUARD.reset()
        backend._BRUSH_GUARD.reset()

    def tearDown(self):
        backend.ws_client.send = self._saved_send
        backend.ws_client.ws = self._saved_ws
        backend.reset_texture_state()
        backend._FILL_GUARD.reset()
        backend._BRUSH_GUARD.reset()

    def context(self):
        return types.SimpleNamespace(scene=fake_scene())


def fake_session(**overrides):
    """The progress fields of ``Scene.gloss_session``."""
    fields = dict(fill_state="idle", fill_message="", brush_state="idle",
                  brush_message="", sync_state="idle", sync_message="")
    fields.update(overrides)
    return types.SimpleNamespace(**fields)


class TestBusyStateAlwaysExpires(FillLivenessTestCase):
    def test_a_bare_progress_message_is_ignored(self):
        """The case that stranded the panel: progress with nothing in flight.

        Before, this published ``preparing`` and leaned on the watchdog to undo
        it two minutes later. Now it never reaches the panel at all.
        """
        backend.on_fill_status({"data": {"stage": "rendering"}})

        guard = backend._FILL_GUARD
        self.assertFalse(backend._TEXTURE_STATE["active"],
                         "no texture update is in flight -- that is the point")
        self.assertNotEqual(guard.published_state(), STATE_PREPARING)
        self.assertFalse(bpy.app.timers.is_registered(guard.tick))

    def test_the_servers_trailing_ready_does_not_regrey_the_panel(self):
        """A generate that succeeded must leave Generate clickable.

        The server's last ``fill_status`` (stage ``ready``) lands after the
        final chunk has already finished the fill.
        """
        guard = backend._FILL_GUARD
        backend.start_texture_updates(object(), operation="fill")
        backend.finish_texture_updates(STATE_READY, "texture updated")

        backend.on_fill_status({"data": {"stage": "ready", "current": 7, "total": 7}})

        self.assertEqual(guard.published_state(), STATE_READY)
        self.assertIsNone(guard.deadline)
        self.assertIsNone(guard.tick(), "the watchdog retires on its next tick")

    def test_a_ready_stage_mid_fill_is_a_heartbeat_not_completion(self):
        """Only the chunk handler may finish a fill."""
        guard = backend._FILL_GUARD
        backend.start_texture_updates(object(), operation="fill")
        guard.deadline = time.time() + 0.5

        backend.on_fill_status({"data": {"stage": "ready"}})

        self.assertTrue(backend._TEXTURE_STATE["active"])
        self.assertEqual(guard.published_state(), STATE_PREPARING)
        self.assertGreater(guard.deadline,
                           time.time() + backend.TEXTURE_TIMEOUT_S - 5)

    def test_a_stranded_preparing_times_out(self):
        guard = backend._FILL_GUARD
        guard.publish(STATE_PREPARING)
        guard.deadline = time.time() - 1

        self.assertIsNone(guard.tick(), "the timer should retire")
        self.assertEqual(guard.published_state(), STATE_ERROR)
        self.assertNotEqual(guard.published_state(), STATE_PREPARING,
                            "the panel must become usable again")

    def test_preparing_without_a_deadline_starts_one(self):
        """A busy state from anywhere is adopted rather than trusted."""
        guard = backend._FILL_GUARD
        guard.mirror, guard.deadline = STATE_PREPARING, None

        self.assertEqual(guard.tick(), 1.0)
        self.assertIsNotNone(guard.deadline)

    def test_a_talkative_server_is_never_killed(self):
        """Progress refreshes the deadline, so a slow fill is not a stalled one."""
        guard = backend._FILL_GUARD
        backend.start_texture_updates(object(), operation="fill")
        guard.deadline = time.time() + 0.5

        backend.on_fill_status({"data": {"stage": "view", "current": 3, "total": 9}})

        self.assertGreater(guard.deadline,
                           time.time() + backend.TEXTURE_TIMEOUT_S - 5)
        self.assertEqual(guard.tick(), 1.0)

    def test_a_terminal_state_retires_the_watchdog(self):
        guard = backend._FILL_GUARD
        backend.start_texture_updates(object(), operation="fill")
        self.assertTrue(bpy.app.timers.is_registered(guard.tick))

        backend.finish_texture_updates(STATE_READY, "texture updated")

        self.assertIsNone(guard.deadline)
        self.assertIsNone(guard.tick())

    def test_a_reset_scene_retires_the_guard(self):
        """Loading a file resets the property; the stale mirror must not win."""
        guard = backend._FILL_GUARD
        guard.publish(STATE_PREPARING)
        self.assertEqual(guard.mirror, STATE_PREPARING)

        # A freshly loaded scene reports the property's default.
        bpy.context.scene = types.SimpleNamespace(
            gloss_session=types.SimpleNamespace(fill_state="idle", fill_message="")
        )
        try:
            self.assertEqual(guard.published_state(), "idle")
            self.assertIsNone(guard.tick())
        finally:
            bpy.context.scene = None


class TestSavedProgressDoesNotSurviveALoad(FillLivenessTestCase):
    """The progress rows are scene properties, so a .blend can carry them.

    ``template.blend`` did exactly this: saved during the trailing-``ready``
    window, it reopened with ``fill_state == 'preparing'`` and no watchdog, and
    the Generation panel stayed grey for the whole session.
    """

    def setUp(self):
        super().setUp()
        self._saved_data = bpy.data
        self.session = fake_session(fill_state=STATE_PREPARING, fill_message="ready",
                                    brush_state=STATE_PREPARING, brush_message="b",
                                    sync_state="synced", sync_message="in sync")
        bpy.data = types.SimpleNamespace(
            images=[], scenes=[types.SimpleNamespace(gloss_session=self.session)])

    def tearDown(self):
        bpy.data = self._saved_data
        super().tearDown()

    def test_loading_a_file_resets_every_row_to_idle(self):
        backend._FILL_GUARD.publish(STATE_PREPARING)  # a stale mirror, too

        backend.reset_transient_state_on_load("/some/file.blend")

        for field in ("fill_state", "brush_state", "sync_state"):
            self.assertEqual(getattr(self.session, field), "idle", field)
        for field in ("fill_message", "brush_message", "sync_message"):
            self.assertEqual(getattr(self.session, field), "", field)
        self.assertFalse(backend._TEXTURE_STATE["active"])
        self.assertIsNone(backend._FILL_GUARD.mirror)
        self.assertFalse(bpy.app.timers.is_registered(backend._FILL_GUARD.tick))

    def test_registration_installs_and_removes_the_load_handler(self):
        backend.register_backend_handlers()
        try:
            self.assertIn(backend.reset_transient_state_on_load,
                          bpy.app.handlers.load_post)
            backend.register_backend_handlers()
            self.assertEqual(
                bpy.app.handlers.load_post.count(backend.reset_transient_state_on_load),
                1, "re-registering must not stack handlers")
        finally:
            backend.unregister_backend_handlers()
        self.assertNotIn(backend.reset_transient_state_on_load,
                         bpy.app.handlers.load_post)


class TestTheBrushRowCannotLatchEither(FillLivenessTestCase):
    """The Add Brush row greys out on ``brush_state == 'preparing'`` too."""

    def test_a_trailing_brush_status_is_guarded(self):
        guard = backend._BRUSH_GUARD
        backend.on_brush_status({"data": {"state": STATE_PREPARING,
                                          "brush_name": "b"}})

        self.assertTrue(bpy.app.timers.is_registered(guard.tick))
        self.assertIsNotNone(guard.deadline)

    def test_a_stranded_brush_preparing_times_out(self):
        guard = backend._BRUSH_GUARD
        backend.on_brush_status({"data": {"state": STATE_PREPARING,
                                          "brush_name": "b"}})
        guard.deadline = time.time() - 1

        self.assertIsNone(guard.tick())
        self.assertEqual(guard.published_state(), STATE_ERROR)

    def test_the_two_brush_deadlines_share_one_constant(self):
        """operators.py must not drift from the panel guard."""
        from gloss_blender import operators

        self.assertIs(operators.BRUSH_TIMEOUT_S, backend.BRUSH_TIMEOUT_S)


class TestRequestsAreTrackedBeforeTheyAreSent(FillLivenessTestCase):
    def test_fill_is_armed_before_it_is_sent(self):
        backend.send_fill_request(self.context())

        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.armed_when_sent, [True],
                         "the router must be armed before the request goes out")

    def test_clear_is_armed_before_it_is_sent(self):
        backend.send_clear_request(self.context())

        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.armed_when_sent, [True])

    def test_a_second_fill_while_busy_is_never_sent(self):
        backend.send_fill_request(self.context())

        with self.assertRaises(RuntimeError):
            backend.send_fill_request(self.context())

        self.assertEqual(len(self.sent), 1,
                         "an untracked request must not reach the server")

    def test_a_clear_while_a_fill_runs_is_never_sent(self):
        backend.send_fill_request(self.context())

        with self.assertRaises(RuntimeError):
            backend.send_clear_request(self.context())

        self.assertEqual(len(self.sent), 1)

    def test_clear_refuses_while_disconnected(self):
        backend.ws_client.ws = None

        with self.assertRaises(RuntimeError):
            backend.send_clear_request(self.context())

        self.assertEqual(self.sent, [])
        self.assertFalse(backend._TEXTURE_STATE["active"],
                         "a refused clear must not leave the panel busy")

    def test_arming_twice_raises_rather_than_silently_returning(self):
        backend.start_texture_updates(object(), operation="fill")

        with self.assertRaises(RuntimeError):
            backend.start_texture_updates(object(), operation="fill")


if __name__ == "__main__":
    unittest.main()
