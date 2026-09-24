"""The add-on must only reference operators and properties that exist.

Blender resolves operator ids and property names lazily, at draw or click
time, so a panel naming a deleted operator -- or reading a scene property that
is no longer registered -- fails in front of the user rather than at import.
These checks catch both statically.

Everything here parses with ``ast`` rather than grepping, so paths like
``"scene.gltf"`` inside a docstring cannot be mistaken for attribute access.
"""

import ast
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import harness  # noqa: E402

ADDON = pathlib.Path(harness.ADDON_DIR)

#: Attributes every Blender scene already has, so they need no registration.
BUILTIN_SCENE_ATTRS = {
    "objects", "collection", "frame_current", "frame_start", "frame_end",
    "render", "name", "camera", "world", "view_layers", "tool_settings",
    "cursor", "unit_settings", "gravity", "use_gravity", "animation_data",
}


def parse(filename):
    return ast.parse((ADDON / filename).read_text())


def _is_target(node, target):
    """True when ``node`` is ``<target>`` or ``something.<target>``."""
    if isinstance(node, ast.Name):
        return node.id == target
    return isinstance(node, ast.Attribute) and node.attr == target


def attribute_reads(tree, target):
    """Names read as ``<target>.<name>``."""
    return {
        node.attr for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and _is_target(node.value, target)
    }


def prop_calls(tree, target):
    """Names named by ``layout.prop(<target>, "name")``."""
    found = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "prop"
                and len(node.args) >= 2):
            continue
        if not _is_target(node.args[0], target):
            continue
        if isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str):
            found.add(node.args[1].value)
    return found


def referenced(tree, target):
    """Every property name reached through ``target``, in either form."""
    return attribute_reads(tree, target) | prop_calls(tree, target)


def operator_ids_called(tree):
    """Operator ids drawn by ``layout.operator("gloss.x")``."""
    ids = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "operator"
                and node.args
                and isinstance(node.args[0], ast.Constant)):
            ids.add(node.args[0].value)
    return ids


def operator_ids_declared(tree):
    """Operator ids declared as ``bl_idname = "gloss.x"``."""
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for stmt in node.body:
                if (isinstance(stmt, ast.Assign)
                        and any(getattr(t, "id", None) == "bl_idname" for t in stmt.targets)
                        and isinstance(stmt.value, ast.Constant)):
                    ids.add(stmt.value.value)
    return ids


def annotated_properties(tree, class_name):
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return {
                stmt.target.id for stmt in node.body
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
            }
    raise AssertionError(f"class {class_name} not found")


def scene_registrations(tree):
    """``bpy.types.Scene.<name> = ...`` assignments."""
    names = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if (isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Attribute)
                    and target.value.attr == "Scene"):
                names.add(target.attr)
    return names


def scene_deletions(tree):
    """``del bpy.types.Scene.<name>`` statements."""
    names = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Delete):
            continue
        for target in node.targets:
            if (isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Attribute)
                    and target.value.attr == "Scene"):
                names.add(target.attr)
    return names


class TestPanelReferences(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.panel = parse("ui_panel.py")
        cls.operators = parse("operators.py")
        cls.properties = parse("properties.py")

    def test_every_operator_drawn_by_the_panel_exists(self):
        missing = sorted(operator_ids_called(self.panel)
                         - operator_ids_declared(self.operators))
        self.assertEqual(
            missing, [],
            f"ui_panel draws operators that no longer exist: {missing}")

    def test_every_session_property_drawn_by_the_panel_exists(self):
        declared = annotated_properties(self.properties, "GlossSessionState")
        missing = sorted(referenced(self.panel, "session") - declared)
        self.assertEqual(
            missing, [],
            f"ui_panel reads session properties that do not exist: {missing}")


class TestSceneProperties(unittest.TestCase):
    """Scene properties read at draw or send time must be registered.

    Dropping a registration while a panel or request builder still reads the
    property surfaces as "Scene object has no attribute ..." the first time a
    user opens the panel or clicks Generate.
    """

    @classmethod
    def setUpClass(cls):
        cls.init = parse("__init__.py")
        cls.registered = scene_registrations(cls.init)

    def assert_all_registered(self, filename):
        used = referenced(parse(filename), "scene") - BUILTIN_SCENE_ATTRS
        missing = sorted(used - self.registered)
        self.assertEqual(
            missing, [],
            f"{filename} uses scene properties never registered in "
            f"__init__.py: {missing}")

    def test_panel_scene_properties_are_registered(self):
        self.assert_all_registered("ui_panel.py")

    def test_backend_scene_properties_are_registered(self):
        self.assert_all_registered("backend.py")

    def test_operator_scene_properties_are_registered(self):
        self.assert_all_registered("operators.py")

    def test_registrations_are_non_trivial(self):
        """Guard the guard: a broken parser would vacuously pass."""
        self.assertGreater(len(self.registered), 10)

    def test_every_registered_property_is_also_unregistered(self):
        """A property left behind on unregister leaks across add-on reloads."""
        leaked = sorted(self.registered - scene_deletions(self.init))
        self.assertEqual(
            leaked, [], f"registered but never deleted on unregister: {leaked}")

    def test_no_property_is_deleted_without_being_registered(self):
        """``del`` of a property that was never registered raises on unregister."""
        phantom = sorted(scene_deletions(self.init) - self.registered)
        self.assertEqual(
            phantom, [], f"deleted on unregister but never registered: {phantom}")


class TestOperatorReferences(unittest.TestCase):
    def test_operators_import_only_existing_backend_names(self):
        harness.install()
        import importlib
        backend = importlib.import_module(f"{harness.PACKAGE}.backend")
        tree = parse("operators.py")
        imported = [
            alias.name for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == "backend"
            for alias in node.names
        ]
        missing = [name for name in imported if not hasattr(backend, name)]
        self.assertEqual(
            missing, [],
            f"operators.py imports missing backend names: {missing}")


if __name__ == "__main__":
    unittest.main()
