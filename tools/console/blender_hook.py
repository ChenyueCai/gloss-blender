"""Runs inside Blender (``--python``): announces the session to the console.

Prints one machine-readable header line that ``ship.py`` turns into session
metadata, then keeps quiet. Everything else the console shows is Blender's and
the add-on's ordinary stdout/stderr.
"""
import json
import os
import platform
import subprocess
import sys

import bpy
from bpy.app.handlers import persistent


def _addon_info():
    info = {"name": "gloss-blender"}
    try:
        import addon_utils

        for mod in addon_utils.modules():
            if mod.__name__ == "gloss-blender":
                bl = addon_utils.module_bl_info(mod)
                ver = bl.get("version")
                info["version"] = ".".join(str(v) for v in ver) if ver else None
                info["path"] = os.path.dirname(mod.__file__)
                info["enabled"] = "gloss-blender" in bpy.context.preferences.addons
                try:
                    info["commit"] = subprocess.check_output(
                        ["git", "-C", info["path"], "rev-parse", "--short", "HEAD"],
                        stderr=subprocess.DEVNULL, timeout=3,
                    ).decode().strip()
                except Exception:
                    info["commit"] = None
                break
    except Exception as exc:  # never let the hook break Blender startup
        info["error"] = repr(exc)
    return info


def _announce(blend):
    header = {
        "blend": blend or None,
        "blender_version": bpy.app.version_string,
        "python": platform.python_version(),
        "host": platform.node(),
        "addon": _addon_info(),
    }
    print("GLOSS_CONSOLE " + json.dumps(header), flush=True)


@persistent
def _on_load_post(_dummy=None):
    _announce(bpy.data.filepath)


# Files given on the command line load after --python scripts run, so announce
# once now (no file yet) and again when the .blend is actually loaded.
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(line_buffering=True)
    except Exception:
        pass

_announce(bpy.data.filepath)
if _on_load_post not in bpy.app.handlers.load_post:
    bpy.app.handlers.load_post.append(_on_load_post)
