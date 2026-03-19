import bpy
import yaml
import os

def load_glaze_config_from_yaml(yaml_path):
    """Load scene Glaze configuration values from a YAML file.

    Keys are copied onto ``bpy.context.scene.glaze_config`` when the property
    exists there. The legacy YAML key ``mesh_file_path`` is accepted as an alias
    for ``mesh_folder``. The runtime flag ``scene.update_texture_4k`` can also
    be set from the YAML key ``update_texture_4k``. Unknown keys are ignored and
    logged with ``print``.

    Args:
        yaml_path: Path to a YAML configuration file.

    Raises:
        FileNotFoundError: If ``yaml_path`` does not exist.
        RuntimeError: If ``Scene.glaze_config`` has not been registered yet.

    Example:
        In Blender's Python console::

            from glaze_blender.utils.config import load_glaze_config_from_yaml
            load_glaze_config_from_yaml("/path/to/config.yaml")
    """
    if not os.path.exists(yaml_path):
        raise FileNotFoundError(f"YAML file not found: {yaml_path}")

    with open(yaml_path, 'r') as f:
        data = yaml.safe_load(f)

    scene = bpy.context.scene
    if not hasattr(scene, "glaze_config"):
        raise RuntimeError("Scene.glaze_config is not registered")

    cfg = scene.glaze_config
    aliases = {
        "mesh_file_path": "mesh_folder",
    }

    # Iterate over all keys in YAML and set corresponding PropertyGroup attributes
    for key, value in data.items():
        if key == "update_texture_4k":
            requested_resolution = bool(value)
            resolution_locked = (
                scene.current_paint_mesh is not None
                or bool(scene.glaze_session.loaded_paint_texture)
            )
            if resolution_locked and requested_resolution != scene.update_texture_4k:
                print(
                    "Warning: update_texture_4k is locked for the active session; "
                    "keep the current value until a fresh start."
                )
                continue
            scene.update_texture_4k = requested_resolution
            continue
        target_key = aliases.get(key, key)
        if hasattr(cfg, target_key):
            setattr(cfg, target_key, value)
        else:
            print(f"Warning: GlazeConfig has no property named '{key}'")

    print(f"Loaded GlazeConfig from {yaml_path}")
