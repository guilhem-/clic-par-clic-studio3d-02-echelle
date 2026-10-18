"""Run by headless Blender: blender -b --factory-startup --python render.py -- <folder>.

Empties the scene, runs scene.py (written by the model), adds a camera and a light if needed,
renders with Cycles on CPU (1280 x 720, denoising) and exports the 3D model (GLB) without the
decor. Writes result.json ({"error": …} on failure).
"""
import json
import sys
import textwrap
import traceback
from pathlib import Path

import bpy
from mathutils import Vector

folder = Path(sys.argv[sys.argv.index("--") + 1])
result = folder / "result.json"


def write(**info):
    result.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")


def frame(cam, objects):
    """Places the camera to see all the objects (three-quarter view, slightly from above)."""
    corners = [o.matrix_world @ Vector(c) for o in objects for c in o.bound_box]
    if not corners:
        return
    center = sum(corners, Vector()) / len(corners)
    radius = max((c - center).length for c in corners)
    cam.location = center + Vector((1.0, -1.3, 0.8)).normalized() * radius * 3.6
    cam.rotation_euler = (center - cam.location).to_track_quat("-Z", "Y").to_euler()


try:
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o, do_unlink=True)
    # Tolerates a BOM and a common indentation (code copied from a block indented by the model).
    code = textwrap.dedent((folder / "scene.py").read_text(encoding="utf-8-sig"))
    exec(compile(code, "scene.py", "exec"), {"bpy": bpy, "__name__": "__scene__"})

    scene = bpy.context.scene
    meshes = [o for o in scene.objects if o.type == "MESH"]
    if not meshes:
        raise RuntimeError("the scene contains no object")
    if scene.camera is None:
        cam = bpy.data.objects.new("Camera", bpy.data.cameras.new("Camera"))
        scene.collection.objects.link(cam)
        scene.camera = cam
        frame(cam, [o for o in meshes if not o.name.startswith("Decor")] or meshes)
    if not any(o.type == "LIGHT" for o in scene.objects):
        sun = bpy.data.objects.new("Sun", bpy.data.lights.new("Sun", "SUN"))
        sun.data.energy = 3
        sun.rotation_euler = (0.8, 0.2, 0.6)
        scene.collection.objects.link(sun)

    # Basic ambient light (light grey background): a scene poorly lit by the model stays readable.
    if scene.world is None:
        scene.world = bpy.data.worlds.new("World")
    scene.world.use_nodes = True
    background = next((n for n in scene.world.node_tree.nodes if n.type == "BACKGROUND"), None)
    if background is not None:
        background.inputs[0].default_value = (0.8, 0.82, 0.85, 1)
        background.inputs[1].default_value = max(background.inputs[1].default_value, 0.7)

    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = 48
    scene.cycles.use_denoising = True
    scene.render.resolution_x, scene.render.resolution_y, scene.render.resolution_percentage = 1280, 720, 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.filepath = str(folder / "render.png")
    bpy.ops.render.render(write_still=True)

    bpy.ops.object.select_all(action="DESELECT")
    for o in meshes:
        o.select_set(not o.name.startswith("Decor"))
    bpy.ops.export_scene.gltf(filepath=str(folder / "model.glb"), export_format="GLB", use_selection=True)
    write(ok=True, objects=len(meshes))
except Exception:
    write(error=traceback.format_exc()[-1500:])
