"""Import the add-on package headlessly, with Blender stubbed out.

The add-on directory is named ``glaze-blender``, which is not a valid Python
identifier, and its ``__init__.py`` registers Blender classes on import. So we
bind a synthetic package whose ``__path__`` points at the directory without
executing ``__init__.py``, letting individual modules be imported by their
relative names.
"""

import pathlib
import sys
import types

ADDON_DIR = pathlib.Path(__file__).resolve().parent.parent
PACKAGE = "glaze_blender"


def _stub_bpy():
    """A ``bpy`` surface broad enough to import every add-on module.

    Registered as a package with real submodules in ``sys.modules`` so that
    ``import bpy.utils.previews`` and ``from bpy.props import ...`` resolve
    the way they do inside Blender.
    """
    bpy = types.ModuleType("bpy")
    bpy.__path__ = []  # mark as a package

    class _Timers:
        def __init__(self):
            self.registered = []

        def register(self, fn, persistent=False):
            self.registered.append(fn)

        def unregister(self, fn):
            if fn in self.registered:
                self.registered.remove(fn)

        def is_registered(self, fn):
            return fn in self.registered

    app = types.ModuleType("bpy.app")
    app.timers = _Timers()
    bpy.app = app
    sys.modules["bpy.app"] = app
    bpy.data = types.SimpleNamespace(images=[])
    bpy.context = types.SimpleNamespace(scene=None, window_manager=None)
    bpy.path = types.SimpleNamespace(abspath=lambda p: p)

    # bpy.props -- every declaration is a no-op descriptor.
    props = types.ModuleType("bpy.props")

    class _Prop:
        def __init__(self, *args, **kwargs):
            pass

    for name in ("StringProperty", "IntProperty", "BoolProperty",
                 "FloatProperty", "EnumProperty", "PointerProperty",
                 "CollectionProperty", "FloatVectorProperty",
                 "IntVectorProperty"):
        setattr(props, name, lambda *a, **k: _Prop())
    bpy.props = props
    sys.modules["bpy.props"] = props

    # bpy.types -- base classes the add-on subclasses.
    bpy_types = types.ModuleType("bpy.types")

    class _Base:
        pass

    for name in ("PropertyGroup", "Panel", "Operator", "Menu",
                 "AddonPreferences", "Scene", "UIList"):
        setattr(bpy_types, name, type(name, (_Base,), {}))
    bpy_types.SpaceView3D = types.SimpleNamespace(
        draw_handler_add=lambda *a, **k: None,
        draw_handler_remove=lambda *a, **k: None,
    )
    bpy.types = bpy_types
    sys.modules["bpy.types"] = bpy_types

    # bpy.utils and bpy.utils.previews
    utils = types.ModuleType("bpy.utils")
    utils.register_class = lambda cls: None
    utils.unregister_class = lambda cls: None
    previews = types.ModuleType("bpy.utils.previews")
    previews.new = lambda: {}
    previews.remove = lambda collection: None
    utils.previews = previews
    bpy.utils = utils
    sys.modules["bpy.utils"] = utils
    sys.modules["bpy.utils.previews"] = previews
    return bpy


def _stub_blender_extras():
    """Stub the other Blender-only modules the add-on imports."""
    for name in ("bmesh", "blf", "mathutils"):
        sys.modules.setdefault(name, types.ModuleType(name))
    if "gpu" not in sys.modules:
        gpu = types.ModuleType("gpu")
        gpu.types = types.SimpleNamespace(Buffer=lambda *a, **k: None)
        gpu.state = types.SimpleNamespace()
        sys.modules["gpu"] = gpu
    if not hasattr(sys.modules["mathutils"], "Vector"):
        sys.modules["mathutils"].Vector = lambda *a, **k: None


def _stub_websockets():
    """Stand-in for ``websockets`` so ``client`` imports without the dependency.

    Only the names touched at import time and in exception handlers are needed.
    """
    ws = types.ModuleType("websockets")

    class ConnectionClosed(Exception):
        pass

    ws.ConnectionClosed = ConnectionClosed

    async def _connect(*args, **kwargs):  # pragma: no cover - never called
        raise RuntimeError("stub websockets cannot connect")

    ws.connect = _connect
    return ws


def real_websockets_available():
    """True when the genuine ``websockets`` package is importable.

    Handles the case where our own stub is already in ``sys.modules``:
    ``find_spec`` raises on a module whose ``__spec__`` is ``None``.
    """
    import importlib.util

    existing = sys.modules.get("websockets")
    if existing is not None:
        return not getattr(existing, "__glaze_stub__", False)
    try:
        return importlib.util.find_spec("websockets") is not None
    except (ImportError, ValueError):
        return False


def install():
    """Make ``glaze_blender.*`` importable. Idempotent.

    The real ``websockets`` package is always preferred when installed, so
    test-module import order cannot leave a stub in ``sys.modules`` that a
    later test mistakes for the real thing.
    """
    if "bpy" not in sys.modules:
        sys.modules["bpy"] = _stub_bpy()
    _stub_blender_extras()
    if "websockets" not in sys.modules and not real_websockets_available():
        stub = _stub_websockets()
        stub.__glaze_stub__ = True
        sys.modules["websockets"] = stub
    if PACKAGE not in sys.modules:
        pkg = types.ModuleType(PACKAGE)
        pkg.__path__ = [str(ADDON_DIR)]
        sys.modules[PACKAGE] = pkg
    return sys.modules[PACKAGE]
