import bpy
import yaml
import os

#: Keys whose values are directories on disk.
FOLDER_KEYS = ("mesh_folder", "single_views_folder", "single_views_texture_folder", "brushes_folder")


def resolve_config_path(value, base_dir):
    """Turn a folder value from a YAML file into an absolute path.

    Three spellings are accepted, so a config can ship inside a data folder and
    still load from any checkout location:

    - absolute (``/data/mesh``) and ``~``-prefixed paths are used as they are;
    - Blender-relative paths (``//../mesh``) are kept verbatim -- every operator
      that reads a folder passes it through ``bpy.path.abspath``, which resolves
      ``//`` against the open .blend;
    - anything else (``../mesh``, ``mesh``) is relative to the YAML file itself.

    Empty values stay empty.
    """
    if not value:
        return value
    value = str(value)
    if value.startswith("//"):
        return value
    value = os.path.expanduser(value)
    if not os.path.isabs(value):
        value = os.path.join(base_dir, value)
    return os.path.normpath(value)


def to_absolute_folder(value, blend_dir):
    """``//../mesh/`` -> ``/data/mesh`` for a .blend saved in ``/data/session``."""
    if not value or not str(value).startswith("//") or not blend_dir:
        return value
    return os.path.normpath(os.path.join(blend_dir, str(value)[2:]))


def to_relative_folder(value, blend_dir):
    """``/data/mesh`` -> ``//../mesh`` for a .blend saved in ``/data/session``.

    Anything not absolute is returned as it is; a folder on another drive
    (no relative form) stays absolute too.
    """
    value = str(value) if value else value
    # ``//`` is Blender's own relative prefix; os.path.isabs would call it absolute.
    if not value or not blend_dir or value.startswith("//") or not os.path.isabs(value):
        return value
    try:
        rel = os.path.relpath(os.path.normpath(value), blend_dir)
    except ValueError:
        return value
    return "//" + rel.replace(os.sep, "/")


def _blend_dir():
    filepath = getattr(bpy.data, "filepath", "")
    return os.path.dirname(filepath) if filepath else ""


#: Other scene-level path fields that travel with a session: (group, field).
EXTRA_PATH_FIELDS = (
    ("current_view", "image_path"),
    ("gloss_session", "loaded_paint_texture"),
)


def session_images(scene):
    """Image datablocks the add-on put on the reference and paint meshes.

    Only these are rewritten on save, so an unrelated image elsewhere in the
    file (an HDRI on another drive, say) is left exactly as the user set it.
    """
    found = []
    for attr in ("current_reference_mesh", "current_paint_mesh"):
        obj = getattr(scene, attr, None)
        if obj is None:
            continue
        for slot in getattr(obj, "material_slots", ()):
            mat = getattr(slot, "material", None)
            tree = getattr(mat, "node_tree", None) if mat else None
            for node in getattr(tree, "nodes", ()):
                img = getattr(node, "image", None)
                if img is not None and img not in found:
                    found.append(img)
    return found


def relativize_images(images, blend_dir):
    """Rewrite absolute image file paths as ``//`` paths relative to ``blend_dir``.

    Blender's importers and ``bpy.data.images.load`` record whatever path they
    were given -- absolute, for anything picked through the add-on -- and a
    later save keeps it, so a session painted on one machine pointed at that
    machine's home directory. Packed images are rewritten too: their path is
    only a label then, but it is still a label that travels.
    """
    for img in images:
        # The image's own path, plus the "packed from" record of each packed
        # file, which Blender fills with the absolute path at pack time.
        holders = [img] + list(getattr(img, "packed_files", ()))
        for holder in holders:
            path = getattr(holder, "filepath", "")
            if not path or path.startswith("//"):
                continue
            rel = to_relative_folder(path, blend_dir)
            if rel != path:
                try:
                    holder.filepath = rel
                except Exception:
                    pass


def relativize_new_images(before, blend_dir=None):
    """Relativize every image that did not exist in the ``before`` set."""
    blend_dir = _blend_dir() if blend_dir is None else blend_dir
    if not blend_dir:
        return
    relativize_images([i for i in bpy.data.images if i not in before], blend_dir)


def _map_config_folders(fn):
    """Apply ``fn(value, blend_dir)`` to every stored path of every scene."""
    blend_dir = _blend_dir()
    if not blend_dir:
        return
    for scene in getattr(bpy.data, "scenes", ()):
        targets = [(getattr(scene, "gloss_config", None), key) for key in FOLDER_KEYS]
        targets += [(getattr(scene, group, None), key) for group, key in EXTRA_PATH_FIELDS]
        for owner, key in targets:
            if owner is None:
                continue
            try:
                setattr(owner, key, fn(getattr(owner, key), blend_dir))
            except Exception:
                pass


@bpy.app.handlers.persistent
def absolutize_config_folders_on_load(*_args):
    """Show real folders in the panel after a load.

    A shipped session stores its folders Blender-relative (``//../mesh/``) so
    the file works from any clone location. Blender's panel, however, checks a
    DIR_PATH field for existence without resolving ``//`` for add-on
    properties, so every such field draws red even though the operators (which
    do resolve it) work. Expanding to absolute paths in memory keeps the panel
    honest; :func:`relativize_config_folders_on_save` puts ``//`` back when the
    file is written.
    """
    _map_config_folders(to_absolute_folder)


@bpy.app.handlers.persistent
def relativize_config_folders_on_save(*args):
    """Store every session path Blender-relative, so the .blend stays portable.

    Covers the config folders, the reference-view and paint-texture fields,
    and the images on the reference and paint meshes. ``save_pre`` receives
    the destination path, which matters for Save As into another folder.
    """
    _map_config_folders(to_relative_folder)
    target = args[0] if args and isinstance(args[0], str) and args[0] else getattr(bpy.data, "filepath", "")
    blend_dir = os.path.dirname(target) if target else ""
    if blend_dir:
        for scene in getattr(bpy.data, "scenes", ()):
            relativize_images(session_images(scene), blend_dir)


@bpy.app.handlers.persistent
def absolutize_config_folders_after_save(*_args):
    """Undo the save-time rewrite in memory, so the panel stays readable."""
    _map_config_folders(to_absolute_folder)


#: The example session config shipped with the add-on, ``<add-on>/data/config.yaml``.
DEFAULT_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "config.yaml"
)


def scene_has_config(scene):
    """``True`` when any folder is set on ``scene.gloss_config``."""
    cfg = getattr(scene, "gloss_config", None)
    return cfg is not None and any(getattr(cfg, key, "") for key in FOLDER_KEYS)


def apply_default_config(scene=None, path=None):
    """Load the shipped config into every scene that has no folders set.

    A fresh scene, or one saved before any config was loaded, gets
    ``data/config.yaml`` so the panel is usable without clicking Load Config.
    A scene that already has folders is left alone, so an explicit Load
    Config or hand-edited paths are never overwritten. Returns the number of
    scenes updated; 0 when the file is absent.
    """
    path = path or DEFAULT_CONFIG_PATH
    if not os.path.isfile(path):
        return 0
    scenes = [scene] if scene is not None else list(getattr(bpy.data, "scenes", ()))
    applied = 0
    for target in scenes:
        if target is None or scene_has_config(target):
            continue
        load_gloss_config_from_yaml(path, scene=target)
        applied += 1
    return applied


@bpy.app.handlers.persistent
def apply_default_config_on_load(*_args):
    """Give an unconfigured .blend the shipped config after it opens."""
    try:
        apply_default_config()
    except Exception as exc:  # noqa: BLE001 - a handler must never break file load
        print(f"[gloss] default config not applied: {exc}")


CONFIG_HANDLERS = (
    ("load_post", absolutize_config_folders_on_load),
    ("load_post", apply_default_config_on_load),
    ("save_pre", relativize_config_folders_on_save),
    ("save_post", absolutize_config_folders_after_save),
)


def register_config_handlers():
    for name, fn in CONFIG_HANDLERS:
        bucket = getattr(bpy.app.handlers, name)
        if fn not in bucket:
            bucket.append(fn)


def unregister_config_handlers():
    for name, fn in CONFIG_HANDLERS:
        bucket = getattr(bpy.app.handlers, name)
        if fn in bucket:
            bucket.remove(fn)


def load_gloss_config_from_yaml(yaml_path, scene=None):
    """Load scene Gloss configuration values from a YAML file.

    Loads the server URL, mesh folder, reference image and texture folders,
    brushes folder, and ``scene.update_texture_4k``. Folder values may be
    relative to the YAML file (see :func:`resolve_config_path`), so a config
    checked in next to its data keeps working after a clone. The legacy key
    ``mesh_file_path`` is accepted as ``mesh_folder``. Unknown keys are ignored and
    logged with ``print``. Resolution stays locked during a paint session.

    Args:
        yaml_path: Path to a YAML configuration file.

    Raises:
        FileNotFoundError: If ``yaml_path`` does not exist.
        RuntimeError: If ``Scene.gloss_config`` has not been registered yet.

    Example:
        In Blender's Python console::

            from gloss_blender.utils.config import load_gloss_config_from_yaml
            load_gloss_config_from_yaml("/path/to/config.yaml")
    """
    if not os.path.exists(yaml_path):
        raise FileNotFoundError(f"YAML file not found: {yaml_path}")

    with open(yaml_path, 'r') as f:
        data = yaml.safe_load(f)

    if scene is None:
        scene = bpy.context.scene
    if not hasattr(scene, "gloss_config"):
        raise RuntimeError("Scene.gloss_config is not registered")

    cfg = scene.gloss_config
    base_dir = os.path.dirname(os.path.abspath(yaml_path))

    # Iterate over all keys in YAML and set corresponding PropertyGroup attributes
    for key, value in (data or {}).items():
        if key == "update_texture_4k":
            requested_resolution = bool(value)
            resolution_locked = (
                scene.current_paint_mesh is not None
                or bool(scene.gloss_session.loaded_paint_texture)
            )
            if resolution_locked and requested_resolution != scene.update_texture_4k:
                print(
                    "Warning: update_texture_4k is locked for the active session; "
                    "keep the current value until a fresh start."
                )
                continue
            scene.update_texture_4k = requested_resolution
            continue
        target_key = "mesh_folder" if key == "mesh_file_path" else key
        if target_key in FOLDER_KEYS:
            setattr(cfg, target_key, resolve_config_path(value, base_dir))
        elif target_key == "server_url":
            setattr(cfg, target_key, value)
        else:
            print(f"Warning: GlossConfig has no property named '{key}'")

    print(f"Loaded GlossConfig from {yaml_path}")
