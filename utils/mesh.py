import bpy
import bmesh
import gpu
from pathlib import Path
import mathutils

import os
import numpy as np
import torch, torchvision
import cv2

def load_mesh(mesh_path, name="GlazeMesh", remove_existing=False):
    
    mesh_path = Path(mesh_path)
    if not mesh_path.exists():
        raise FileNotFoundError(mesh_path)
    
    # Remove existing object if exists
    if remove_existing:
        if name in bpy.data.objects:
            bpy.data.objects.remove(bpy.data.objects[name], do_unlink=True)
    existing_objs = set(bpy.data.objects)
    # Import OBJ
    bpy.ops.wm.obj_import(filepath=str(mesh_path))
    imported_objs = [obj for obj in bpy.data.objects if obj not in existing_objs]
    for obj in imported_objs:
        if obj.type == 'MESH':
            imported_obj = obj
        else:
            bpy.data.objects.remove(obj, do_unlink=True)
    bpy.context.view_layer.objects.active = imported_obj
    normalize_mesh(imported_obj)
    imported_obj.name = name
    return imported_obj


def normalize_mesh(obj, normalize=True, eps=1e-6):
    bpy.ops.object.mode_set(mode='EDIT')

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
    print(vmid)

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
        #print("before",vert.co, den)
        if normalize:
            vert.co /= den
        #print("after", vert.co)

    # Update the mesh in Edit Mode
    bmesh.update_edit_mesh(obj.data)

    # Return to Object Mode (optional)
    bpy.ops.object.mode_set(mode='OBJECT')


def duplicate_mesh(obj, new_name, location=(0,0,0)):
    obj_copy = obj.copy()
    obj_copy.data = obj.data.copy()  # separate mesh data
    obj_copy.name = new_name
    new_mats = []
    for mat in obj.data.materials:
        if mat:
            new_mats.append(mat.copy())
        else:
            new_mats.append(None)

    obj_copy.data.materials.clear()
    for m in new_mats:
        obj_copy.data.materials.append(m)

    bpy.context.collection.objects.link(obj_copy)
    obj_copy.location = location
    return obj_copy

def duplicate_image(orig_img, new_name):
    """
    Duplicate a Blender image with all pixels and assign a new name.
    
    Args:
        orig_img (bpy.types.Image): the original Blender image
        new_name (str): new name for the duplicated image
    
    Returns:
        bpy.types.Image: the new duplicated image
    """
    # 1. Create new image with same size and alpha setting
    new_img = bpy.data.images.new(
        name=new_name,
        width=orig_img.size[0],
        height=orig_img.size[1],
        alpha=orig_img.alpha_mode != 'NONE',   # True if original has alpha
        float_buffer=orig_img.is_float         # True if original is float
    )
    
    # 2. Copy pixel data
    buffer = np.array(orig_img.pixels[:], dtype=np.float32)
    new_img.pixels.foreach_set(buffer)
    
    # 3. Update to make Blender aware
    new_img.update()
    
    return new_img

def apply_texture(obj, image_path, suffix=''):
    # if image_path is None, create new node and set image path
    # if image_path exist but there is no prev bpy image, create one
    # if image_path exist and prev bpy image exist, replace the previous image
    
    width, height = 4096, 4096
    if image_path is None or not Path(image_path).exists():
        image_name = obj.name + f".{suffix}.png"
    else:
        img_name = os.path.splitext(os.path.basename(image_path))[0]
        image_name = f"{obj.name}_{img_name}.{suffix}.png"
    image = None
    if image_path is None or not Path(image_path).exists():
        image = bpy.data.images.new(image_name, width=width, height=height, alpha=True, float_buffer=False)
        pixels = np.zeros(width * height * 4, dtype=np.float32)  # RGBA=0,0,0,0
        image.pixels.foreach_set(pixels)
        image.update()
    if image_path is not None and Path(image_path).exists():
        texture_image =  bpy.data.images.get(image_name)
        if texture_image is not None:
            print(f"LOAD IN {image_path}")
            return
        else:
            image = bpy.data.images.load(image_path)
            image.name = image_name
    for mat in obj.data.materials:
        if mat is None or not mat.use_nodes:
            continue

        nodes = mat.node_tree.nodes
        links = mat.node_tree.links
        bsdf = None
        tex_node = None

        # Find the Principled BSDF node
        for node in nodes:
            if node.type == 'BSDF_PRINCIPLED':
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
        if tex_node and tex_node.type == 'TEX_IMAGE':
            nodes.remove(tex_node)

        # Create new texture node
        new_tex = nodes.new('ShaderNodeTexImage')
        new_tex.image = image
        new_tex.location = (-400, 300)

        # Connect to BSDF base color
        links.new(new_tex.outputs['Color'], bsdf.inputs['Base Color'])


def get_current_texture(obj):
    return bpy.data.images.get(f"{obj.name}.paint.png")
    


def update_texture(obj, texture: torch.Tensor, copy_prev=False):
    # reshape texture to 4096 * 4096
    # use a soft margin composite with current mask
    print("updating texture...")
    h = w = 4096
    texture = torchvision.transforms.Resize((4096, 4096))(texture.permute(2,0,1).unsqueeze(0)).squeeze(0).permute(1,2,0)
    texture = np.array(texture)
    # get current texture and mask; create new texture pixels
    current_texture = get_current_texture(obj)
    buffer_size = h * w * 4 
    # alpha channel of the rec texture 
    texture_alpha = np.flipud(texture[..., 3:4]) # set to the target face only 
    new_texture_arr = np.concatenate([np.flipud(texture[..., i:i+1]) for i in range(3)], axis=2)
    h, w = new_texture_arr.shape[0], new_texture_arr.shape[1]
    print("here")
    try:
        buffer_size = h * w * 4 
        current_texture_pixels = np.empty(buffer_size, dtype=np.float32)
        current_texture.pixels.foreach_get(current_texture_pixels)
        
        if copy_prev:
            print("copying prev texture")
            base_name = current_texture.name + " history"
            name = base_name
            i = 1
            existing_names = {img.name for img in bpy.data.images}
            while name in existing_names:
                name = f"{base_name}.{i:03d}"
                i += 1
            texture_copy = bpy.data.images.new(
                name=name,
                width=w, height=h,
                alpha=current_texture.alpha_mode != 'NONE',   # True if original has alpha
                float_buffer=current_texture.is_float         # True if original is float
            )
            texture_copy.pixels.foreach_set(current_texture_pixels)
            texture_copy.update()
        
        current_texture_arr = current_texture_pixels.reshape(h, w, 4)
        current_texture_alpha = current_texture_arr[..., 3:4]
        mask = (texture_alpha - current_texture_alpha).clip(0.0,1.0)
        print("here")
        mask_soft = expand_mask_soft(mask[:, :,0], max_distance=15)[:,:,np.newaxis]
        w_inpaint = mask_soft * current_texture_alpha * texture_alpha + mask
        
        color = w_inpaint * new_texture_arr + current_texture_arr[..., :3] * (1 - w_inpaint)
        print("here")
        alpha = mask + current_texture_arr[..., 3:4] * (1 - mask)
        pixels = np.concatenate([color, alpha], axis=2).ravel()
        
        buffer = gpu.types.Buffer('FLOAT', buffer_size, pixels)
        current_texture.pixels.foreach_set(buffer)
        current_texture.update()
    except Exception as e:
        print(f"Texture update failed: {e}")
        

def expand_mask_soft(mask_arr, max_distance=20):
    """
    Expands a binary mask outward with a soft gradient.
    
    Args:
        mask (torch.Tensor): binary mask (0 or 1), in the shape of B C H W
        max_distance (int): how far the soft gradient extends
    
    Returns:
        torch.Tensor: non binary soft mask with values in [0,1], , in the shape of B C H W
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


def composite_inpaint(texture_existing, texture_inpaint,
                      mask_existing, mask_inpaint, mask_fill, 
                      soft=True, soft_margin=20):
    """ 
    Composite the existing texture map with inpaint texture over regions that mask_fill covers, 
    Expand the mask_fill with soft margin if needed
    
    Args:
        texture_existing (_type_): existing texture map in B 4 H W, range(0,1)
        texture_inpaint (_type_): inpaint texture map in B 4 H W, range(0,1)
        mask_existing (_type_): binary mask of the existing texture (0 or 1), in the shape of B C H W
        mask_inpaint (_type_): binary mask of the inpaint texture (0 or 1), in the shape of B C H W
        mask_fill (_type_): binary mask to be filled (0 or 1), in the shape of B C H W
        soft (bool, optional): _description_. Defaults to True.
        soft_margin (int, optional): _description_. Defaults to 20.

    Returns:
        _type_: updated texture map, range(0,1) for all channels
    """
    
    if soft:
        mask_fill_soft = expand_mask_soft(mask_fill, max_distance=soft_margin)
        w_inpaint = (mask_fill_soft * mask_existing * mask_inpaint + mask_fill)
    else:
        w_inpaint = mask_fill
    composite = texture_inpaint * w_inpaint + texture_existing * (1.0 - w_inpaint)
    alpha = mask_existing + mask_fill
    composite[:, 3, ...] = alpha
    return composite
