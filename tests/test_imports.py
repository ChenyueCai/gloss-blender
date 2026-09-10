"""Every add-on runtime module must import cleanly.

An ImportError here means the add-on fails to enable in Blender, which is the
kind of breakage that is otherwise only found by hand. Blender's own modules
are stubbed by ``harness``; this checks our import graph, not Blender's.
"""

import importlib
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import harness  # noqa: E402

harness.install()

RUNTIME_MODULES = [
    "protocol",
    "client",
    "backend",
    "operators",
    "ui_panel",
    "properties",
    "utils.io",
    "utils.image",
    "utils.config",
    "utils.brush",
    "utils.cameras",
    "utils.mesh",
]


class TestModuleImports(unittest.TestCase):
    def test_all_runtime_modules_import(self):
        failures = []
        for name in RUNTIME_MODULES:
            try:
                importlib.import_module(f"{harness.PACKAGE}.{name}")
            except Exception as exc:
                failures.append(f"{name}: {type(exc).__name__}: {exc}")
        self.assertEqual(failures, [], "add-on modules failed to import")

    def test_backend_exposes_router_registration(self):
        backend = importlib.import_module(f"{harness.PACKAGE}.backend")
        self.assertTrue(callable(backend.register_backend_handlers))
        self.assertTrue(callable(backend.unregister_backend_handlers))

    def test_addon_init_registers_backend_handlers(self):
        """The router is useless if nothing subscribes at add-on enable."""
        source = (pathlib.Path(harness.ADDON_DIR) / "__init__.py").read_text()
        self.assertIn("register_backend_handlers()", source)
        self.assertIn("unregister_backend_handlers()", source)
