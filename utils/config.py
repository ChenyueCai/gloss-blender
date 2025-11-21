import bpy
import yaml
import os

def load_glaze_config_from_yaml(yaml_path):
    """
    Reads a YAML file and sets the values to the Scene's GlazeConfig PropertyGroup.
    """
    if not os.path.exists(yaml_path):
        raise FileNotFoundError(f"YAML file not found: {yaml_path}")

    with open(yaml_path, 'r') as f:
        data = yaml.safe_load(f)

    scene = bpy.context.scene
    if not hasattr(scene, "glaze_config"):
        raise RuntimeError("Scene.glaze_config is not registered")

    cfg = scene.glaze_config

    # Iterate over all keys in YAML and set corresponding PropertyGroup attributes
    for key, value in data.items():
        if hasattr(cfg, key):
            setattr(cfg, key, value)
        else:
            print(f"Warning: GlazeConfig has no property named '{key}'")

    print(f"Loaded GlazeConfig from {yaml_path}")

