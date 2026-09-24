"""Structural assertions on the real Gloss server.

``glaze_interactive.server`` cannot be imported here -- it needs CUDA, kaolin
and a loaded checkpoint. These tests parse it instead, and assert the
properties the responsiveness and routing fixes depend on. They are the
guard rail that stops the old shapes creeping back in.
"""

import ast
import pathlib
import re
import unittest

SERVER = (
    pathlib.Path(__file__).resolve().parents[2]
    / "material-superres-private" / "glaze_interactive" / "server.py"
)

#: Workers that must always exist and stay off the IOLoop. Other
#: ``run_on_executor`` functions come and go as the camera logic is
#: refactored, so the rest are discovered rather than listed.
REQUIRED_WORKERS = {"_prepare_brush_blocking", "_run_fill_blocking"}


def decorator_names(node):
    names = set()
    for dec in node.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        names.add(target.attr if isinstance(target, ast.Attribute) else
                  getattr(target, "id", ""))
    return names


@unittest.skipUnless(SERVER.exists(), "server repo not checked out here")
class TestServerStructure(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = SERVER.read_text()
        cls.tree = ast.parse(cls.source)
        cls.functions = {
            node.name: node
            for node in ast.walk(cls.tree)
            if isinstance(node, ast.FunctionDef)
        }

    def yields_in(self, name):
        node = self.functions[name]
        return [n for n in ast.walk(node) if isinstance(n, (ast.Yield, ast.YieldFrom))]

    # --- fix 2: coroutines are actually driven ----------------------------
    def test_on_message_yields_both_handlers(self):
        """Bare calls left the Future unretrieved, swallowing every error."""
        src = ast.unparse(self.functions["on_message"])
        self.assertIn("yield self._handle_binary_request(message)", src)
        self.assertIn("yield self._handle_json_request(message)", src)

    def test_on_message_reports_failures_to_the_client(self):
        src = ast.unparse(self.functions["on_message"])
        self.assertIn("_send_error", src,
                      "a failed request must tell the client, not hang it")

    def test_fill_and_brush_requests_are_yielded(self):
        src = ast.unparse(self.functions["_handle_json_request"])
        self.assertIn("yield self._fill_face(data)", src)
        self.assertIn("yield self._prepare_brush_blocking(data)", src)

    # --- fix 3: GPU work is off the IOLoop --------------------------------
    def executor_workers(self):
        return [
            name for name, node in self.functions.items()
            if "run_on_executor" in decorator_names(node)
        ]

    def test_required_workers_run_on_the_executor(self):
        for name in sorted(REQUIRED_WORKERS):
            self.assertIn(name, self.functions, f"{name} is missing")
            self.assertIn("run_on_executor", decorator_names(self.functions[name]),
                          f"{name} would block the IOLoop")

    def test_every_executor_worker_is_a_plain_function(self):
        """A stray ``yield`` would make run_on_executor return a generator."""
        workers = self.executor_workers()
        self.assertTrue(workers, "no executor workers found at all")
        for name in workers:
            self.assertEqual(
                self.yields_in(name), [],
                f"{name} contains a yield and would never execute",
            )

    def test_every_executor_worker_is_yielded_by_a_caller(self):
        """An un-yielded worker silently swallows its result and its errors."""
        for name in self.executor_workers():
            self.assertIn(
                f"yield self.{name}(", self.source,
                f"{name} is never yielded, so its Future is dropped",
            )

    def test_executor_has_a_single_worker(self):
        """One worker keeps CUDA work serialized across connections."""
        self.assertIn("GPU_EXECUTOR = ThreadPoolExecutor(max_workers=1", self.source)

    def test_fill_reports_progress_from_the_worker(self):
        src = ast.unparse(self.functions["_fill_face"])
        self.assertIn("add_callback", src,
                      "progress must be marshalled back onto the IOLoop")
        worker = ast.unparse(self.functions["_run_fill_blocking"])
        self.assertIn("progress(", worker, "the worker emits no progress")

    # --- fix 1: every reply is tagged -------------------------------------
    def test_no_untagged_string_replies_remain(self):
        """Bare ``write_message("...")`` is what forced the client to guess."""
        for node in ast.walk(self.tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "write_message"):
                continue
            if not node.args:
                continue
            first = node.args[0]
            self.assertNotIsInstance(
                first, (ast.Constant, ast.JoinedStr),
                f"untagged string reply at line {node.lineno}",
            )

    def test_texture_and_icon_sends_carry_routing_tags(self):
        self.assertIn("msg_type=MSG_TEXTURE_CHUNK", self.source)
        self.assertIn('"type": MSG_BRUSH_ICON', self.source)

    def test_icon_is_downsampled_and_quantized(self):
        src = ast.unparse(self.functions["_send_brush_icon"])
        self.assertIn("BRUSH_ICON_SIZE", src, "icon is still full resolution")
        self.assertIn("DTYPE_UINT8", src, "icon is still float32")

    # --- fix 4: wire format ------------------------------------------------
    def test_textures_go_out_as_uint8(self):
        src = ast.unparse(self.functions["_safe_send_texture"])
        self.assertIn("DTYPE_UINT8", src)

    # --- texture agreement ------------------------------------------------
    def test_every_texture_mutation_is_persisted(self):
        """fill, clear_face, clear_all and upload must all write the session.

        fill used to compute a new texture, send it, and never store it, so the
        server's copy never advanced and every stroke was conditioned on the
        pre-session texture.
        """
        for fn in ("_run_fill_blocking", "_handle_binary_request"):
            self.assertIn(
                "update_texture", ast.unparse(self.functions[fn]),
                f"{fn} changes the texture without persisting it",
            )
        handler = ast.unparse(self.functions["_handle_json_request"])
        self.assertEqual(
            handler.count("self.paint_session.update_texture"), 2,
            "clear_face and clear_all_texture must each persist",
        )

    def test_no_handler_mutates_paint_meshes_by_index(self):
        """``paint_meshes[0]`` clears whichever mesh happens to be first."""
        self.assertNotIn(
            "paint_meshes[0].update_texture", self.source,
            "a handler still mutates the first paint mesh instead of the named one",
        )

    def test_texture_pushes_are_acknowledged(self):
        self.assertIn("MSG_TEXTURE_SYNCED", self.source,
                      "the client cannot tell whether its push landed")

    def test_upload_handler_honours_the_dtype_tag(self):
        src = ast.unparse(self.functions["_handle_binary_request"])
        self.assertIn("decode_pixels", src,
                      "uploads must respect the client's dtype tag")


if __name__ == "__main__":
    unittest.main()


#: Message types the add-on sends that the server currently has no handler
#: for. Anything listed here is a dead button in the panel; the list exists so
#: the mismatch is explicit rather than silent.
KNOWN_UNHANDLED = set()

ADDON = pathlib.Path(__file__).resolve().parents[1]


@unittest.skipUnless(SERVER.exists(), "server repo not checked out here")
class TestCrossRepoContract(unittest.TestCase):
    """Every message the client sends should have a server handler."""

    @classmethod
    def setUpClass(cls):
        cls.server_source = SERVER.read_text()
        cls.client_source = "\n".join(
            (ADDON / name).read_text()
            for name in ("backend.py", "operators.py")
        )

    def sent_types(self):
        return set(re.findall(r'"type":\s*"([a-z_]+)"', self.client_source))

    def handled_types(self):
        return set(re.findall(r'msg_type == "([a-z_]+)"', self.server_source))

    def test_every_sent_message_has_a_handler(self):
        unhandled = self.sent_types() - self.handled_types() - KNOWN_UNHANDLED
        self.assertEqual(
            sorted(unhandled), [],
            "the add-on sends message types the server ignores: "
            f"{sorted(unhandled)}",
        )

    def test_known_unhandled_list_is_still_accurate(self):
        """The exception list must not rot in either direction.

        An entry is stale once the server handles it again, or once the client
        stops sending it.
        """
        handled_again = sorted(KNOWN_UNHANDLED & self.handled_types())
        self.assertEqual(
            handled_again, [],
            f"handled again; drop from KNOWN_UNHANDLED: {handled_again}",
        )
        no_longer_sent = sorted(KNOWN_UNHANDLED - self.sent_types())
        self.assertEqual(
            no_longer_sent, [],
            f"no longer sent; drop from KNOWN_UNHANDLED: {no_longer_sent}",
        )
