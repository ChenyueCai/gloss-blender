"""The panel must only reference operators and properties that exist.

Blender resolves operator ids and property names at draw time, so a panel that
names a deleted operator raises inside the UI rather than at import. These
checks catch that statically -- including the case where a refactor removes an
operator or a scene property while the panel still draws it.
"""

import ast
import pathlib
import re
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import harness  # noqa: E402

ADDON = pathlib.Path(harness.ADDON_DIR)

OPERATOR_CALL = re.compile(r'\.operator\(\s*"([^"]+)"')
BL_IDNAME = re.compile(r'bl_idname\s*=\s*"([^"]+)"')
SESSION_PROP = re.compile(r'\bsession\.([a-z_][a-z_0-9]*)')


def class_properties(source, class_name):
    """Return annotated property names declared on ``class_name``."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return {
                stmt.target.id
                for stmt in node.body
                if isinstance(stmt, ast.AnnAssign)
                and isinstance(stmt.target, ast.Name)
            }
    raise AssertionError(f"class {class_name} not found")


class TestPanelReferences(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.panel = (ADDON / "ui_panel.py").read_text()
        cls.operators = (ADDON / "operators.py").read_text()
        cls.properties = (ADDON / "properties.py").read_text()

    def test_every_operator_drawn_by_the_panel_exists(self):
        declared = set(BL_IDNAME.findall(self.operators))
        used = set(OPERATOR_CALL.findall(self.panel))
        missing = sorted(used - declared)
        self.assertEqual(
            missing, [],
            f"ui_panel draws operators that no longer exist: {missing}",
        )

    def test_every_session_property_drawn_by_the_panel_exists(self):
        declared = class_properties(self.properties, "GlazeSessionState")
        used = set(SESSION_PROP.findall(self.panel))
        missing = sorted(used - declared)
        self.assertEqual(
            missing, [],
            f"ui_panel reads session properties that do not exist: {missing}",
        )

    def test_progress_states_drawn_match_the_protocol(self):
        """Panel state strings must be values the handlers actually write."""
        from glaze_blender import protocol
        harness.install()
        panel_states = set(re.findall(r"'(idle|preparing|ready|error|syncing|synced)'",
                                      self.panel))
        protocol_states = {
            protocol.STATE_PREPARING, protocol.STATE_READY,
            protocol.STATE_ERROR, protocol.STATE_SYNCING,
            protocol.STATE_SYNCED, "idle",
        }
        unknown = sorted(panel_states - protocol_states)
        self.assertEqual(unknown, [], f"panel uses unknown states: {unknown}")


class TestOperatorReferences(unittest.TestCase):
    """Operators must not call backend helpers that were removed."""

    def test_operators_import_only_existing_backend_names(self):
        harness.install()
        import importlib
        backend = importlib.import_module(f"{harness.PACKAGE}.backend")
        source = (ADDON / "operators.py").read_text()
        tree = ast.parse(source)
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "backend":
                imported += [alias.name for alias in node.names]
        missing = [name for name in imported if not hasattr(backend, name)]
        self.assertEqual(missing, [],
                         f"operators.py imports missing backend names: {missing}")


if __name__ == "__main__":
    unittest.main()
