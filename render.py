import bpy
import os

# ------------------------------------------------
# CONFIG (change per render)
# ------------------------------------------------
CAMERA_NAME = "Camera"              # name of camera in .blend
MATERIAL_NAME = "Material"          # material to override
IMAGE_FP = "/absolute/path/to/image.png"
OUTPUT_FP = "/absolute/path/to/output.png"

# ------------------------------------------------
# LOAD IMAGE
# ------------------------------------------------
def load_image(fp):
    img = bpy.data.images.load(fp)
    return img


# ------------------------------------------------
# SET BASE COLOR TEXTURE
# ------------------------------------------------
def set_basecolor(image_fp):
    obj = bpy.context.view_layer.objects.active
    if obj is None:
        raise RuntimeError("No active object")
    mat = obj.active_material
    if mat is None:
        raise RuntimeError("Active object has no material")


    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links

    bsdf = nodes.get("Principled BSDF")
    if bsdf is None:
        raise RuntimeError("Principled BSDF not found")

    # Reuse or create Image Texture node
    tex_node = None
    for n in nodes:
        if n.type == 'TEX_IMAGE':
            tex_node = n
            break

    if tex_node is None:
        tex_node = nodes.new("ShaderNodeTexImage")
        tex_node.location = (-400, 0)

    img = load_image(image_fp)
    tex_node.image = img
    tex_node.interpolation = 'Smart'

    # Ensure link to Base Color
    links.new(tex_node.outputs["Color"], bsdf.inputs["Base Color"])


# ------------------------------------------------
# SET CAMERA
# ------------------------------------------------
def get_all_cameras():
    cams = [obj for obj in bpy.data.objects if obj.type == 'CAMERA']
    if not cams:
        raise RuntimeError("No cameras found in scene")
    return cams

def set_camera(cam):
    bpy.context.scene.camera = cam


# ------------------------------------------------
# RENDER
# ------------------------------------------------
def render(output_fp):
    scene = bpy.context.scene
    scene.render.filepath = output_fp
    bpy.ops.render.render(write_still=True)


# ------------------------------------------------
# MAIN
# ------------------------------------------------
METHODS = ["material-superres-final", "texgen", "paint3d", "hunyuan3dpaint"]

CAMERAS = get_all_cameras()
selected = [5, 6, 9, 10, 16, 25, 31, 32, 47]
TEXTURE_FNS = ["view%04d" % i for i in range(50) if i in selected]

# for cam in CAMERAS:
#     set_camera(cam)
#     for texture_fn in TEXTURE_FNS:
#         for method in METHODS:
#             TEXTURE_DIR = f"/Users/chenyuecai/Desktop/download/{method}-test/turtle"
#             OUT_DIR = f"/Users/chenyuecai/Desktop/render/turtle/{texture_fn}"
#             os.makedirs(OUT_DIR, exist_ok=True)
#             set_basecolor(os.path.join(TEXTURE_DIR, F"{texture_fn}.png"))
#             print(os.path.join(TEXTURE_DIR, F"{texture_fn}.png"))
#             bpy.context.scene.render.image_settings.file_format = 'PNG'
#             bpy.context.scene.render.image_settings.color_mode = 'RGBA'
#             bpy.context.scene.render.filepath = os.path.join(OUT_DIR,f"{texture_fn}_{method}_{cam.name}_diffuse.png")
#             bpy.ops.render.render(write_still=True)

# FOR RENDDER GEOMETRY ONLY
for cam in CAMERAS:
    set_camera(cam)
    for texture_fn in TEXTURE_FNS:
        method = "normal"
        OUT_DIR = f"/Users/chenyuecai/Desktop/render/turtle/{texture_fn}"
        bpy.context.scene.render.image_settings.file_format = 'PNG'
        bpy.context.scene.render.image_settings.color_mode = 'RGBA'
        bpy.context.scene.render.filepath = os.path.join(OUT_DIR,f"{texture_fn}_{method}_{cam.name}_normal.png")
        bpy.ops.render.render(write_still=True)

