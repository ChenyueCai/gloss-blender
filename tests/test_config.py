"""A config file that ships next to its data must load from any checkout.

``resolve_config_path`` is the whole contract: relative folders resolve
against the YAML file, ``//`` stays Blender-relative, absolute stays absolute.
"""

import os
import pathlib
import sys
import tempfile
import types
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import harness  # noqa: E402

harness.install()

import bpy  # noqa: E402

try:
    import yaml  # noqa: F401
except ImportError:  # pragma: no cover
    yaml = None

from gloss_blender.utils import config  # noqa: E402


class TestResolveConfigPath(unittest.TestCase):
    base = "/repo/data/config"

    def test_relative_is_anchored_to_the_yaml_directory(self):
        self.assertEqual(config.resolve_config_path("../mesh", self.base), "/repo/data/mesh")
        self.assertEqual(config.resolve_config_path("mesh", self.base), "/repo/data/config/mesh")

    def test_absolute_is_untouched(self):
        self.assertEqual(config.resolve_config_path("/elsewhere/mesh/", self.base), "/elsewhere/mesh")

    def test_blender_relative_is_left_for_bpy_path_abspath(self):
        self.assertEqual(config.resolve_config_path("//../mesh", self.base), "//../mesh")

    def test_home_is_expanded(self):
        self.assertEqual(config.resolve_config_path("~/mesh", self.base),
                         os.path.normpath(os.path.expanduser("~/mesh")))

    def test_empty_stays_empty(self):
        self.assertEqual(config.resolve_config_path("", self.base), "")
        self.assertIsNone(config.resolve_config_path(None, self.base))


class TestBlendRelativeFolders(unittest.TestCase):
    """Saved as ``//``, shown absolute: the panel's red check ignores ``//``."""
    blend_dir = "/repo/data/session"

    def test_round_trip(self):
        rel = "//../mesh/"
        absolute = config.to_absolute_folder(rel, self.blend_dir)
        self.assertEqual(absolute, "/repo/data/mesh")
        self.assertEqual(config.to_relative_folder(absolute, self.blend_dir), "//../mesh")

    def test_non_relative_values_pass_through(self):
        self.assertEqual(config.to_absolute_folder("/abs/mesh", self.blend_dir), "/abs/mesh")
        self.assertEqual(config.to_absolute_folder("", self.blend_dir), "")
        self.assertEqual(config.to_relative_folder("//../mesh", self.blend_dir), "//../mesh")
        self.assertEqual(config.to_relative_folder("", self.blend_dir), "")

    def test_an_unsaved_file_changes_nothing(self):
        self.assertEqual(config.to_absolute_folder("//../mesh", ""), "//../mesh")
        self.assertEqual(config.to_relative_folder("/abs/mesh", ""), "/abs/mesh")

    def test_handlers_rewrite_every_scene(self):
        saved = bpy.data
        cfg = types.SimpleNamespace(mesh_folder="//../mesh/", single_views_folder="//../single_view/",
                                    single_views_texture_folder="", brushes_folder="/abs/brush")
        view = types.SimpleNamespace(image_path="//../single_view/croissant/view0136.basecolor.png")
        bpy.data = types.SimpleNamespace(filepath="/repo/data/session/croissant.blend",
                                         scenes=[types.SimpleNamespace(gloss_config=cfg, current_view=view)])
        try:
            config.absolutize_config_folders_on_load("/repo/data/session/croissant.blend")
            self.assertEqual(cfg.mesh_folder, "/repo/data/mesh")
            self.assertEqual(view.image_path, "/repo/data/single_view/croissant/view0136.basecolor.png")
            self.assertEqual(cfg.single_views_folder, "/repo/data/single_view")
            self.assertEqual(cfg.single_views_texture_folder, "")
            self.assertEqual(cfg.brushes_folder, "/abs/brush")

            config.relativize_config_folders_on_save()
            self.assertEqual(cfg.mesh_folder, "//../mesh")
            self.assertEqual(view.image_path, "//../single_view/croissant/view0136.basecolor.png")
            self.assertEqual(cfg.brushes_folder, "//../../../abs/brush")

            config.absolutize_config_folders_after_save()
            self.assertEqual(cfg.mesh_folder, "/repo/data/mesh")
        finally:
            bpy.data = saved

    def test_session_images_are_relativized_on_save(self):
        saved = bpy.data
        img_abs = types.SimpleNamespace(filepath="/repo/data/texture/croissant/view0394.png",
                                        packed_files=[types.SimpleNamespace(filepath="/repo/data/texture/croissant/view0394.png")])
        img_rel = types.SimpleNamespace(filepath="//../mesh/x.png")
        img_other = types.SimpleNamespace(filepath="/elsewhere/hdri.exr")  # not on a gloss mesh
        node = lambda img: types.SimpleNamespace(image=img)
        mat = types.SimpleNamespace(node_tree=types.SimpleNamespace(nodes=[node(img_abs), node(img_rel), node(None)]))
        ref = types.SimpleNamespace(material_slots=[types.SimpleNamespace(material=mat)])
        cfg = types.SimpleNamespace(mesh_folder="", single_views_folder="", single_views_texture_folder="", brushes_folder="")
        scene = types.SimpleNamespace(gloss_config=cfg, current_reference_mesh=ref, current_paint_mesh=None,
                                      gloss_session=types.SimpleNamespace(loaded_paint_texture="/repo/data/texture/p.png"))
        bpy.data = types.SimpleNamespace(filepath="/repo/data/session/croissant.blend", scenes=[scene],
                                         images=[img_abs, img_rel, img_other])
        try:
            config.relativize_config_folders_on_save("/repo/data/session/croissant.blend")
            self.assertEqual(img_abs.filepath, "//../texture/croissant/view0394.png")
            self.assertEqual(img_abs.packed_files[0].filepath, "//../texture/croissant/view0394.png")
            self.assertEqual(img_rel.filepath, "//../mesh/x.png")
            self.assertEqual(img_other.filepath, "/elsewhere/hdri.exr")
            self.assertEqual(scene.gloss_session.loaded_paint_texture, "//../texture/p.png")

            # a freshly created image is relativized right away
            new_img = types.SimpleNamespace(filepath="/repo/data/mesh/croissant/tex.png")
            bpy.data.images.append(new_img)
            config.relativize_new_images([img_abs, img_rel, img_other])
            self.assertEqual(new_img.filepath, "//../mesh/croissant/tex.png")
        finally:
            bpy.data = saved

    def test_registration_is_idempotent_and_reversible(self):
        config.register_config_handlers()
        config.register_config_handlers()
        try:
            for name, fn in config.CONFIG_HANDLERS:
                self.assertEqual(getattr(bpy.app.handlers, name).count(fn), 1, name)
        finally:
            config.unregister_config_handlers()
        for name, fn in config.CONFIG_HANDLERS:
            self.assertNotIn(fn, getattr(bpy.app.handlers, name))


@unittest.skipIf(yaml is None, "PyYAML not installed")
class TestLoadYaml(unittest.TestCase):
    def setUp(self):
        self._saved_scene = bpy.context.scene
        self.cfg = types.SimpleNamespace(server_url="", mesh_folder="", single_views_folder="",
                                         single_views_texture_folder="", brushes_folder="")
        bpy.context.scene = types.SimpleNamespace(
            gloss_config=self.cfg, current_paint_mesh=None, update_texture_4k=True,
            gloss_session=types.SimpleNamespace(loaded_paint_texture=""))
        self.tmp = tempfile.TemporaryDirectory()
        self.yaml_path = os.path.join(self.tmp.name, "config", "example.yaml")
        os.makedirs(os.path.dirname(self.yaml_path))
        with open(self.yaml_path, "w", encoding="utf-8") as fh:
            fh.write("server_url: ws://localhost:10017/websocket\n"
                     "mesh_folder: ../mesh\n"
                     "single_views_folder: ../single_view\n"
                     "single_views_texture_folder: ../texture\n"
                     "brushes_folder: ../brush\n"
                     "update_texture_4k: false\n")

    def tearDown(self):
        bpy.context.scene = self._saved_scene
        self.tmp.cleanup()

    def test_folders_resolve_next_to_the_yaml(self):
        config.load_gloss_config_from_yaml(self.yaml_path)

        root = os.path.realpath(self.tmp.name)
        self.assertEqual(os.path.realpath(self.cfg.mesh_folder), os.path.join(root, "mesh"))
        self.assertEqual(os.path.realpath(self.cfg.brushes_folder), os.path.join(root, "brush"))
        self.assertTrue(os.path.isabs(self.cfg.single_views_folder))
        self.assertEqual(self.cfg.server_url, "ws://localhost:10017/websocket")
        self.assertFalse(bpy.context.scene.update_texture_4k)


@unittest.skipIf(yaml is None, "PyYAML not installed")
class TestDefaultConfig(unittest.TestCase):
    """``apply_default_config`` fills empty scenes and leaves configured ones alone."""

    def setUp(self):
        self._saved_data = bpy.data
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "data", "config.yaml")
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("server_url: ws://example:1/ws\nmesh_folder: mesh\n")

    def tearDown(self):
        bpy.data = self._saved_data
        self.tmp.cleanup()

    @staticmethod
    def _scene(mesh_folder=""):
        cfg = types.SimpleNamespace(server_url="", mesh_folder=mesh_folder, single_views_folder="",
                                    single_views_texture_folder="", brushes_folder="")
        return types.SimpleNamespace(gloss_config=cfg, current_paint_mesh=None, update_texture_4k=True,
                                     gloss_session=types.SimpleNamespace(loaded_paint_texture=""))

    def test_empty_scenes_get_the_shipped_config(self):
        empty, configured = self._scene(), self._scene("/already/mesh")
        bpy.data = types.SimpleNamespace(scenes=[empty, configured], images=[])

        self.assertEqual(config.apply_default_config(path=self.path), 1)
        self.assertEqual(os.path.realpath(empty.gloss_config.mesh_folder),
                         os.path.join(os.path.realpath(self.tmp.name), "data", "mesh"))
        self.assertEqual(empty.gloss_config.server_url, "ws://example:1/ws")
        self.assertEqual(configured.gloss_config.mesh_folder, "/already/mesh")
        self.assertEqual(configured.gloss_config.server_url, "")

    def test_missing_file_is_a_no_op(self):
        scene = self._scene()
        bpy.data = types.SimpleNamespace(scenes=[scene], images=[])
        self.assertEqual(config.apply_default_config(path=os.path.join(self.tmp.name, "nope.yaml")), 0)
        self.assertEqual(scene.gloss_config.mesh_folder, "")

    def test_shipped_default_points_at_the_data_folder(self):
        self.assertEqual(os.path.basename(os.path.dirname(config.DEFAULT_CONFIG_PATH)), "data")
        self.assertIn(config.apply_default_config_on_load, [fn for _, fn in config.CONFIG_HANDLERS])


if __name__ == "__main__":
    unittest.main()
