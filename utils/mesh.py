# SPDX-FileCopyrightText: Copyright (c) 2025 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import bpy
import bmesh
import gpu
from pathlib import Path
import mathutils

import numpy as np
import torch, torchvision
import cv2
import re

from .config import relativize_new_images


def set_view_center(obj, area):
    """Center a 3D View area on an object."""
    for space in area.spaces:
        if space.type == "VIEW_3D":
            space.region_3d.view_location = obj.location
            space.region_3d.view_rotation = obj.matrix_world.to_quaternion()
            break


def base_name(name):
    """Strip Blender numeric suffixes such as ``.001`` from an object name."""
    # Matches "thing", "thing.001", "thing.123", even "thing.something.001"
    m = re.match(r"^(.*?)(?:\.\d+)?$", name)
    return m.group(1)


SUPPORTED_MESH_EXTS = {".obj", ".gltf", ".glb"}


def load_mesh(mesh_path, name="GlossMesh", remove_existing=False):
    """Import an OBJ/glTF/GLB mesh, normalize it, rename it, and return the object.

    For glTF/GLB scenes that contain multiple mesh parts (and optionally
    cameras, lights, or parent empties), the meshes are joined into a single
    object so the subsequent center+scale normalization treats the asset as
    one piece.

    Args:
        mesh_path: Path to the mesh file to import (.obj, .gltf, or .glb).
        name: Name assigned to the imported mesh object.
        remove_existing: When ``True``, removes an existing object with the same
            target name before importing.

    Raises:
        FileNotFoundError: If ``mesh_path`` does not exist.
        ValueError: If the file extension is unsupported or the import yields
            no mesh objects.
    """

    mesh_path = Path(mesh_path)
    if not mesh_path.exists():
        raise FileNotFoundError(mesh_path)

    ext = mesh_path.suffix.lower()
    if ext not in SUPPORTED_MESH_EXTS:
        raise ValueError(
            f"Unsupported mesh format '{ext}'. Supported: {sorted(SUPPORTED_MESH_EXTS)}"
        )

    # Remove existing object if exists
    if remove_existing:
        if name in bpy.data.objects:
            bpy.data.objects.remove(bpy.data.objects[name], do_unlink=True)
    existing_objs = set(bpy.data.objects)
    existing_images = set(bpy.data.images)

    if ext == ".obj":
        bpy.ops.wm.obj_import(filepath=str(mesh_path))
    else:  # .gltf / .glb
        bpy.ops.import_scene.gltf(filepath=str(mesh_path))
    # The OBJ importer records absolute texture paths (glTF packs and relativizes
    # on its own). Rewrite them now so a saved session travels with its folder.
    relativize_new_images(existing_images)

    imported_objs = [obj for obj in bpy.data.objects if obj not in existing_objs]
    mesh_objs = [obj for obj in imported_objs if obj.type == "MESH"]
    non_mesh_objs = [obj for obj in imported_objs if obj.type != "MESH"]

    if not mesh_objs:
        for obj in non_mesh_objs:
            bpy.data.objects.remove(obj, do_unlink=True)
        raise ValueError(f"No mesh objects found in {mesh_path}")

    # Bake any node transforms (glTF hierarchies often have non-identity
    # parent/local transforms — including the Y-up → Z-up rotation that
    # glTF's importer parks on a parent empty) into the mesh data before
    # normalizing. parent_clear with CLEAR_KEEP_TRANSFORM folds the parent's
    # world transform down into each child's local matrix, so the subsequent
    # transform_apply bakes it into vertex coordinates.
    bpy.ops.object.select_all(action="DESELECT")
    for obj in mesh_objs:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = mesh_objs[0]
    if any(obj.parent is not None for obj in mesh_objs):
        bpy.ops.object.parent_clear(type="CLEAR_KEEP_TRANSFORM")
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)

    if len(mesh_objs) > 1:
        bpy.ops.object.join()
    imported_obj = bpy.context.view_layer.objects.active

    # Drop empties / lights / cameras pulled in by the glTF scene.
    for obj in non_mesh_objs:
        bpy.data.objects.remove(obj, do_unlink=True)

    bpy.context.view_layer.objects.active = imported_obj
    normalize_mesh(imported_obj)
    imported_obj.name = name
    return imported_obj


def normalize_mesh(obj, normalize=True, eps=1e-6):
    """Center a mesh at the origin and optionally scale it to unit bounds.

    The mesh is edited in-place by entering Edit Mode, offsetting vertices by
    the bounding-box midpoint, and dividing by the largest axis length when
    ``normalize`` is enabled.
    """
    bpy.ops.object.mode_set(mode="EDIT")

    # Get the BMesh from the object's mesh data
    bm = bmesh.from_edit_mesh(obj.data)
    points = [v.co.copy() for v in bm.verts]

    # Compute min/max for each dimension (x, y, z)
    xs = [p.x for p in points]
    ys = [p.y for p in points]
    zs = [p.z for p in points]

    vmin = mathutils.Vector((min(xs), min(ys), min(zs)))
    vmax = mathutils.Vector((max(xs), max(ys), max(zs)))

    # Center point = midpoint of bounding box
    vmid = (vmin + vmax) * 0.5

    # Denominator for normalization
    if normalize:
        den = max(
            vmax.x - vmin.x,
            vmax.y - vmin.y,
            vmax.z - vmin.z,
        )
        den = max(den, eps)

    # Iterate through vertices and modify their coordinates
    for vert in bm.verts:
        # Example: Scale all vertices by 0.5
        vert.co -= vmid
        # print("before",vert.co, den)
        if normalize:
            vert.co /= den
        # print("after", vert.co)

    # Update the mesh in Edit Mode
    bmesh.update_edit_mesh(obj.data)

    # Return to Object Mode (optional)
    bpy.ops.object.mode_set(mode="OBJECT")


def strip_to_diffuse_normal(obj):
    """Strip every Principled BSDF material on ``obj`` down to Normal only.

    The Base Color is left for ``apply_texture`` to replace with a blank
    paint canvas (preserving the previous "base color starts black"
    behavior). All other BSDF inputs are unlinked and reset to neutral
    defaults; nodes that no longer feed anything are removed. Materials
    without a Principled BSDF are left untouched.
    """
    if obj is None or getattr(obj, "type", None) != "MESH":
        return

    KEEP_INPUTS = {"Normal"}
    # Float-typed Principled BSDF inputs and the neutral default to fall back to.
    NEUTRAL_SCALARS = {
        "Metallic": 0.0,
        "Roughness": 0.5,
        "Specular": 0.5,                  # 3.x legacy
        "Specular IOR Level": 0.5,        # 4.x
        "Anisotropic": 0.0,
        "Anisotropic Rotation": 0.0,
        "Sheen": 0.0,                     # 3.x legacy
        "Sheen Weight": 0.0,              # 4.x
        "Sheen Roughness": 0.5,
        "Clearcoat": 0.0,                 # 3.x legacy
        "Clearcoat Roughness": 0.03,
        "Coat Weight": 0.0,               # 4.x
        "Coat Roughness": 0.03,
        "Coat IOR": 1.5,
        "Subsurface": 0.0,                # 3.x legacy
        "Subsurface Weight": 0.0,         # 4.x
        "Subsurface Scale": 0.05,
        "Transmission": 0.0,              # 3.x legacy
        "Transmission Weight": 0.0,       # 4.x
        "Emission Strength": 0.0,
        "Alpha": 1.0,
        "IOR": 1.45,
    }
    # Color (RGBA) Principled BSDF inputs. Note: in Blender 4.x "Specular Tint"
    # and "Sheen Tint" were promoted from Float to Color, so they live here.
    NEUTRAL_COLORS = {
        # Mirror the previous load-paint-mesh behavior: diffuse starts black so
        # if apply_texture's image link is missing for any reason, the BSDF
        # still falls back to black instead of the importer's baked default.
        "Base Color": (0.0, 0.0, 0.0, 1.0),
        "Emission": (0.0, 0.0, 0.0, 1.0),         # 3.x legacy color socket
        "Emission Color": (0.0, 0.0, 0.0, 1.0),   # 4.x
        "Specular Tint": (1.0, 1.0, 1.0, 1.0),    # 4.x (was scalar pre-4.x)
        "Sheen Tint": (1.0, 1.0, 1.0, 1.0),       # 4.x (was scalar pre-4.x)
        "Coat Tint": (1.0, 1.0, 1.0, 1.0),
        "Subsurface Color": (0.8, 0.8, 0.8, 1.0),
    }
    # Vector (3-tuple) Principled BSDF inputs.
    NEUTRAL_VECTORS = {
        "Subsurface Radius": (1.0, 0.2, 0.1),
    }

    for mat in obj.data.materials:
        if mat is None or not mat.use_nodes:
            continue

        nodes = mat.node_tree.nodes
        links = mat.node_tree.links
        bsdf = next((n for n in nodes if n.type == "BSDF_PRINCIPLED"), None)
        if bsdf is None:
            continue

        for socket in bsdf.inputs:
            if socket.name in KEEP_INPUTS:
                continue
            for link in list(socket.links):
                links.remove(link)
            try:
                if socket.type == "VALUE" and socket.name in NEUTRAL_SCALARS:
                    socket.default_value = NEUTRAL_SCALARS[socket.name]
                elif socket.type == "RGBA" and socket.name in NEUTRAL_COLORS:
                    socket.default_value = NEUTRAL_COLORS[socket.name]
                elif socket.type == "VECTOR" and socket.name in NEUTRAL_VECTORS:
                    socket.default_value = NEUTRAL_VECTORS[socket.name]
            except (TypeError, ValueError, AttributeError):
                pass

        # Sweep orphan nodes (e.g. metal/roughness Image Texture + Separate
        # Color chains) until the graph is stable.
        protected_types = {"OUTPUT_MATERIAL", "BSDF_PRINCIPLED"}
        while True:
            removed = False
            for node in list(nodes):
                if node.type in protected_types:
                    continue
                if any(out.is_linked for out in node.outputs):
                    continue
                nodes.remove(node)
                removed = True
            if not removed:
                break


def apply_texture(obj, image_path, suffix=""):
    """Create or load an image, then connect it to each material's Base Color.

    Args:
        obj: Mesh object whose materials should be updated.
        image_path: Optional file path to load. When missing or invalid, a new
            blank image is created at the scene's configured texture size.
        suffix: Name suffix appended to the Blender image datablock.

    Side Effects:
        May create new Blender image datablocks, remove existing Base Color
        image nodes, and attach new ``ShaderNodeTexImage`` nodes.
    """
    # if image_path is None, create new node and set image path
    # if image_path exist but there is no prev bpy image, create one
    # if image_path exist and prev bpy image exist, replace the previous image

    if not bpy.context.scene.update_texture_4k:
        width, height = 1024, 1024  # 4096
    else:
        width, height = 4096, 4096
    image_name = obj.name + f".{suffix}.png"
    image = None
    if image_path is None or not Path(image_path).exists():
        existing = bpy.data.images.get(image_name)
        if existing is not None:
            bpy.data.images.remove(existing, do_unlink=True)
        image = bpy.data.images.new(
            image_name, width=width, height=height, alpha=True, float_buffer=False
        )
        pixels = np.zeros(width * height * 4, dtype=np.float32)  # RGBA=0,0,0,0
        image.pixels.foreach_set(pixels)
        image.update()
    else:
        # Loaded textures replace any existing canonical image so that
        # downstream get_current_texture() / sync_paint_texture() see the
        # newly loaded pixels. If the file's resolution doesn't match the
        # configured paint canvas size, resize via PIL on disk before load.
        loaded = bpy.data.images.load(image_path)
        relativize_new_images({i for i in bpy.data.images if i is not loaded})
        src_w, src_h = loaded.size[0], loaded.size[1]
        if (src_w, src_h) != (width, height):
            buf = np.empty(src_w * src_h * 4, dtype=np.float32)
            loaded.pixels.foreach_get(buf)
            arr = buf.reshape(src_h, src_w, 4)
            tensor = torch.from_numpy(arr).permute(2, 0, 1).unsqueeze(0)
            tensor = torchvision.transforms.Resize((height, width))(tensor)
            arr = tensor.squeeze(0).permute(1, 2, 0).contiguous().numpy()
            bpy.data.images.remove(loaded, do_unlink=True)
            existing = bpy.data.images.get(image_name)
            if existing is not None:
                bpy.data.images.remove(existing, do_unlink=True)
            image = bpy.data.images.new(
                image_name, width=width, height=height, alpha=True, float_buffer=False
            )
            image.pixels.foreach_set(arr.ravel())
            image.update()
        else:
            existing = bpy.data.images.get(image_name)
            if existing is not None and existing is not loaded:
                bpy.data.images.remove(existing, do_unlink=True)
            loaded.name = image_name
            image = loaded
    for mat in obj.data.materials:
        if mat is None or not mat.use_nodes:
            continue

        nodes = mat.node_tree.nodes
        links = mat.node_tree.links
        bsdf = None
        tex_node = None

        # Find the Principled BSDF node
        for node in nodes:
            if node.type == "BSDF_PRINCIPLED":
                bsdf = node
                break
        if bsdf is None:
            continue

        # Find old texture node connected to Base Color
        for link in links:
            if link.to_node == bsdf and link.to_socket.name == "Base Color":
                tex_node = link.from_node
                break

        # Remove old texture node if exists
        if tex_node and tex_node.type == "TEX_IMAGE":
            nodes.remove(tex_node)

        # Create new texture node
        new_tex = nodes.new("ShaderNodeTexImage")
        new_tex.image = image
        new_tex.location = (-400, 300)

        # Connect to BSDF base color
        links.new(new_tex.outputs["Color"], bsdf.inputs["Base Color"])

    return image


def binarize_paint_alpha(image, color_threshold=1e-4, alpha_threshold=1e-3):
    """Force ``image`` into the paint convention: RGB clamped to ``[0, 1]``,
    alpha binarized to ``{0, 1}`` so painted regions are 1 and the rest are 0.

    Three input cases are handled uniformly:

    - File has no alpha channel (Blender exposes a constant alpha of 1): the
      mask is derived from RGB — any non-near-black pixel becomes painted.
    - File has a non-binary alpha (soft masks, anti-aliased edges, gradients):
      thresholded to ``{0, 1}``.
    - File already has a binary alpha: preserved (idempotent).
    """
    if image is None:
        return
    w, h = image.size[0], image.size[1]
    if w == 0 or h == 0:
        return

    buf = np.empty(w * h * 4, dtype=np.float32)
    image.pixels.foreach_get(buf)
    arr = buf.reshape(h, w, 4)

    rgb = np.clip(arr[..., :3], 0.0, 1.0)
    alpha = arr[..., 3]

    # A constant alpha across the whole image means the source had no usable
    # alpha channel (Blender pads RGB-only files with alpha=1). Fall back to
    # deriving the mask from color content.
    if float(alpha.max() - alpha.min()) < alpha_threshold:
        painted = rgb.max(axis=-1) > color_threshold
    else:
        painted = alpha > alpha_threshold

    arr[..., :3] = rgb
    arr[..., 3] = painted.astype(np.float32)
    image.pixels.foreach_set(arr.ravel())
    image.update()


def get_current_texture(obj):
    """Return the active paint texture image datablock for ``obj``."""
    return bpy.data.images.get(f"{obj.name}.paint.png")


def clear_texture(obj):
    """Fill the active paint texture for ``obj`` with transparent black."""
    current_texture = get_current_texture(obj)
    if not bpy.context.scene.update_texture_4k:
        h = w = 1024  # 4096
    else:
        h = w = 4096
    buffer_size = h * w * 4
    pixels = np.zeros(buffer_size, dtype=np.float32)
    buffer = gpu.types.Buffer("FLOAT", buffer_size, pixels)
    current_texture.pixels.foreach_set(buffer)


def update_texture(obj, texture: torch.Tensor, soft_merge=True):
    """Merge a server-produced RGBA texture into the current paint texture.

    The incoming tensor is resized to the active texture resolution, the
    previous texture is copied into numbered history images, and the new content
    is either alpha-blended or replaced depending on ``soft_merge``.
    """
    # reshape texture to 4096 * 4096
    # use a soft margin composite with current mask
    print("updating texture...")
    if not bpy.context.scene.update_texture_4k:
        h = w = 1024  # 4096
    else:
        h = w = 4096
    texture = (
        torchvision.transforms.Resize((h, w))(texture.permute(2, 0, 1).unsqueeze(0))
        .squeeze(0)
        .permute(1, 2, 0)
    )
    texture = np.array(texture)
    # get current texture and mask; create new texture pixels
    current_texture = get_current_texture(obj)
    buffer_size = h * w * 4
    # alpha channel of the rec texture
    texture_alpha = np.flipud(texture[..., 3:4])  # set to the target face only
    new_texture_arr = np.concatenate(
        [np.flipud(texture[..., i : i + 1]) for i in range(3)], axis=2
    )
    h, w = new_texture_arr.shape[0], new_texture_arr.shape[1]

    try:
        buffer_size = h * w * 4
        current_texture_pixels = np.empty(buffer_size, dtype=np.float32)
        current_texture.pixels.foreach_get(current_texture_pixels)

        print("copying prev texture")
        base_name = current_texture.name + " history"
        k = 0
        existing_names = {img.name for img in bpy.data.images}
        pattern = re.compile(re.escape(base_name) + r"\.(\d{3})$")
        for name in existing_names:
            m = pattern.match(name)
            if m:
                d = int(m.group(1))
                if d > k:
                    k = d
        k += 1
        name = f"{base_name}.{k:03d}"
        texture_copy = bpy.data.images.new(
            name=name,
            width=w,
            height=h,
            alpha=current_texture.alpha_mode != "NONE",  # True if original has alpha
            float_buffer=current_texture.is_float,  # True if original is float
        )
        texture_copy.pixels.foreach_set(current_texture_pixels)
        texture_copy.update()
        keep_latest_six_history(current_texture)

        if soft_merge:
            current_texture_arr = current_texture_pixels.reshape(h, w, 4)
            current_texture_alpha = current_texture_arr[..., 3:4]
            mask = (texture_alpha - current_texture_alpha).clip(0.0, 1.0)

            mask_soft = expand_mask_soft(mask[:, :, 0], max_distance=15)[
                :, :, np.newaxis
            ]
            w_inpaint = mask_soft * current_texture_alpha * texture_alpha + mask

            color = w_inpaint * new_texture_arr + current_texture_arr[..., :3] * (
                1 - w_inpaint
            )

            alpha = mask + current_texture_arr[..., 3:4] * (1 - mask)
            pixels = np.concatenate([color, alpha], axis=2).ravel()

            buffer = gpu.types.Buffer("FLOAT", buffer_size, pixels)
        else:
            # Clear path. The server returns the full post-clear texture —
            # alpha=1 on regions that should remain painted, alpha=0 on the
            # newly cleared faces — so adopt it directly.
            color = new_texture_arr
            alpha = texture_alpha
            pixels = np.concatenate([color, alpha], axis=2).ravel()
            buffer = gpu.types.Buffer("FLOAT", buffer_size, pixels)
        current_texture.pixels.foreach_set(buffer)
        current_texture.update()
    except Exception as e:
        print(f"Texture update failed: {e}")


def get_last_texture_name(texture):
    """Return the newest history image name for ``texture``, if one exists."""

    base = texture.name + " history"
    imgs = bpy.data.images

    # Pattern like: "BaseName history.001"
    pattern = re.compile(re.escape(base) + r"\.(\d{3})$")

    max_n = -1
    for img in imgs:
        m = pattern.search(img.name)
        if m:
            num = int(m.group(1))
            if num > max_n:
                max_n = num
    if max_n == -1:
        return None
    else:

        last_name = f"{base}.{max_n:03d}"
        return last_name


def undo_texture():
    """Restore the most recent saved texture history image.

    Raises:
        Exception: If the active material does not contain the expected
            Principled BSDF and image texture setup, or if no history image is
            available.
    """
    # --- CONFIG ---
    material = bpy.context.object.active_material

    # ------------------------------

    # Locate the Principled BSDF and its Base Color image node
    nodes = material.node_tree.nodes
    links = material.node_tree.links

    # Find Principled BSDF
    principled = None
    for n in nodes:
        if n.type == "BSDF_PRINCIPLED":
            principled = n
            break

    if not principled:
        raise Exception("No Principled BSDF found")

    # Find linked Image Texture node
    base_color_input = principled.inputs["Base Color"]
    old_image_node = None

    for link in base_color_input.links:
        if link.from_node.type == "TEX_IMAGE":
            old_image_node = link.from_node
            break

    if not old_image_node:
        raise Exception("No Image Texture node connected to Base Color")

    old_img = old_image_node.image
    old_name = old_img.name
    new_img_name = get_last_texture_name(old_image_node.image)

    if new_img_name is None:
        raise Exception(f"Reached max undo limit")

    # Get the new image
    new_img = bpy.data.images.get(new_img_name)
    if not new_img:
        raise Exception(f"Image '{new_img_name}' not found")

    # -----------------------------------------------------
    # 🔄 Replace the image
    # -----------------------------------------------------
    old_image_node.image = new_img  # swap image

    replace_image(old_img, new_img)


def replace_image(old_img, new_img):
    """Delete ``old_img`` and rename ``new_img`` to the original image name."""
    old_name = old_img.name
    old_img.user_clear()
    if old_img.packed_file:
        try:
            old_img.unpack(method="REMOVE")
        except:
            pass
    bpy.data.images.remove(old_img)
    new_img.name = old_name


def keep_latest_six_history(current_tex):
    """Keep at most six numbered history textures for ``current_tex``.

    History textures follow the naming pattern ``<image.name> history.NNN``.
    Older history images beyond the newest six are removed.
    """
    base = current_tex.name + " history"
    imgs = bpy.data.images

    # Pattern for matching numbered histories
    pattern = re.compile(re.escape(base) + r"\.(\d{3})$")

    # Collect all matches (img, number)
    histories = []
    for img in imgs:
        m = pattern.search(img.name)
        if m:
            histories.append((img, int(m.group(1))))

    # If fewer than 2 exist, nothing to delete
    if len(histories) <= 6:
        return

    # Sort by number (descending) — newest first
    histories.sort(key=lambda x: x[1], reverse=True)

    # Keep only the first 2, delete the rest
    to_delete = histories[6:]

    for img, n in to_delete:
        img.user_clear()
        try:
            if img.packed_file:
                img.unpack(method="REMOVE")
        except:
            pass
        bpy.data.images.remove(img)


def expand_mask_soft(mask_arr, max_distance=20):
    """Expand a 2D binary mask outward with an exponential falloff.

    Args:
        mask_arr: Binary NumPy array where non-zero pixels mark the filled
            region.
        max_distance: Maximum distance in pixels over which the soft mask fades.

    Returns:
        numpy.ndarray: Floating-point mask in ``[0, 1]`` with the same height
        and width as ``mask_arr``.
    """
    mask_arr = mask_arr.astype(np.uint8)

    # Compute distance transform from the background
    dist = cv2.distanceTransform(1 - mask_arr, cv2.DIST_L2, 5)

    # Clip distances to max_distance
    dist = np.clip(dist, 0, max_distance)

    # Normalize to [0,1] and invert (inside=1, outside decreases)
    soft_mask = np.exp(-dist / max_distance * 3)  # exponential falloff
    soft_mask = np.clip(soft_mask, 0.0, 1.0)

    return soft_mask
