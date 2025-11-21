import bpy
import bmesh
import gpu
from pathlib import Path
import mathutils

import numpy as np
import torch, torchvision


def load_mesh(mesh_path, name="GlazeMesh"):
    
    mesh_path = Path(mesh_path)
    if not mesh_path.exists():
        raise FileNotFoundError(mesh_path)
    
    # Remove existing object if exists
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
    image = bpy.data.images.load(image_path)
    image.name = image.name.split(".")[0] +f".{suffix}.png"
    if not obj.data or not hasattr(obj.data, "materials"):
        return 
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


def get_current_texture():
    view_id = bpy.context.scene.current_view.sv_id
    return bpy.data.images.get("view%04d.paint.png" % view_id)
    


def update_texture(texture: torch.Tensor, copy_prev=False):
    # reshape texture to 4096 * 4096
    print("updating texture...")
    h = w = 4096
    texture = torchvision.transforms.Resize((4096, 4096))(texture.permute(2,0,1).unsqueeze(0)).squeeze(0).permute(1,2,0)
    texture = np.array(texture)
    current_texture = get_current_texture()
    buffer_size = h * w * 4 
    mask = np.flipud(texture[..., 3:4])
    new_texture_pixel = np.concatenate([np.flipud(texture[..., i:i+1]) for i in range(3)], axis=2)
    h, w = new_texture_pixel.shape[0], new_texture_pixel.shape[1]

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
        
        color = mask * new_texture_pixel + current_texture_pixels.reshape(h, w, 4)[..., :3] * (1 - mask)
        alpha = mask + current_texture_pixels.reshape(h, w, 4)[..., 3:4] * (1 - mask)
        pixels = np.concatenate([color, alpha], axis=2).ravel()
        buffer = gpu.types.Buffer('FLOAT', buffer_size, pixels)
        current_texture.pixels.foreach_set(buffer)
        current_texture.update()
    except Exception as e:
        print(f"Texture update failed: {e}")
        
