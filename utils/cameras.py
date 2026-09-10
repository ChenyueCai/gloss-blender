"""Convert Blender cameras into Kaolin-compatible extrinsics + intrinsics.

Both Blender and Kaolin (kaolin.render.camera) use a right-handed,
OpenGL-style camera-local frame: the camera looks down -Z, +Y is up,
+X is right. So the local axes need no permutation; the only differences
are:

  * Kaolin's extrinsic matrix is world-to-camera (Blender's
    ``cam_obj.matrix_world`` is camera-to-world, so it must be inverted).
  * Kaolin uses pixel-space focal lengths and a principal-point offset
    measured from the image center, while Blender stores millimetre
    focal length plus a sensor size and a normalized shift.

The functions here return plain numpy / Python values so they can run
inside Blender (where ``kaolin`` is generally not importable) and be
reconstructed into a ``kaolin.render.camera.Camera`` on the receiver.
"""

import numpy as np


def blender_camera_view_matrix(cam_obj):
    """Return the Kaolin-style world-to-camera matrix as a 4x4 ``np.float32``.

    Blender's ``cam_obj.matrix_world`` is the camera-to-world transform;
    Kaolin's extrinsic is its inverse. The local-axis convention already
    matches (-Z forward, +Y up), so no axis flip is needed.
    """
    m = cam_obj.matrix_world.inverted()
    return np.array([list(row) for row in m], dtype=np.float32)


def blender_camera_intrinsics(cam_obj, scene):
    """Derive Kaolin-style pixel intrinsics from a Blender camera.

    Returns a dict with ``focal_x``, ``focal_y``, ``x0``, ``y0``,
    ``width``, ``height``, ``near``, ``far`` — the arguments expected by
    ``kaolin.render.camera.CameraIntrinsics.from_focal``.

    Honours Blender's ``sensor_fit`` (AUTO/HORIZONTAL/VERTICAL) and the
    render pixel-aspect ratio so non-square pixels round-trip correctly.
    """
    cam = cam_obj.data
    render = scene.render

    scale = render.resolution_percentage / 100.0
    width = int(round(render.resolution_x * scale))
    height = int(round(render.resolution_y * scale))

    pixel_aspect = render.pixel_aspect_x / render.pixel_aspect_y

    sensor_fit = cam.sensor_fit
    if sensor_fit == "AUTO":
        sensor_fit = "HORIZONTAL" if width * pixel_aspect >= height else "VERTICAL"

    if sensor_fit == "HORIZONTAL":
        sensor_size = cam.sensor_width
        view_factor = width
    else:
        sensor_size = cam.sensor_height
        view_factor = height * pixel_aspect

    pixels_per_mm = view_factor / sensor_size
    focal_x = cam.lens * pixels_per_mm
    focal_y = focal_x / pixel_aspect

    # Blender shift is in units of the sensor's larger dimension and is
    # measured from image centre; Kaolin's x0/y0 use the same origin
    # (pixels from centre), with +x right and +y up for the image.
    max_dim = max(width, height)
    x0 = cam.shift_x * max_dim
    y0 = cam.shift_y * max_dim

    return {
        "focal_x": float(focal_x),
        "focal_y": float(focal_y),
        "x0": float(x0),
        "y0": float(y0),
        "width": int(width),
        "height": int(height),
        "near": float(cam.clip_start),
        "far": float(cam.clip_end),
    }


def blender_camera_to_kaolin(cam_obj, scene):
    """Bundle extrinsics + intrinsics into one dict ready for the wire.

    The receiver can rebuild a Kaolin camera with::

        from kaolin.render.camera import Camera
        Camera.from_args(view_matrix=d["view_matrix"], **d["intrinsics"])
    """
    return {
        "view_matrix": blender_camera_view_matrix(cam_obj),
        "intrinsics": blender_camera_intrinsics(cam_obj, scene),
    }
