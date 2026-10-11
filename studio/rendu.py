"""Exécuté par Blender sans interface : blender -b --factory-startup --python rendu.py -- <dossier>.

Vide la scène, exécute scene.py (écrit par le modèle), ajoute au besoin caméra et lumière,
rend en Cycles sur processeur (1280 x 720, débruitage) et exporte le modèle 3D (GLB) sans le
décor. Écrit resultat.json ({"erreur": …} en cas d'échec).
"""
import json
import sys
import textwrap
import traceback
from pathlib import Path

import bpy
from mathutils import Vector

dossier = Path(sys.argv[sys.argv.index("--") + 1])
resultat = dossier / "resultat.json"


def ecrire(**info):
    resultat.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")


def cadrer(cam, objets):
    """Place la caméra pour voir tous les objets (vue de trois quarts, légèrement plongeante)."""
    coins = [o.matrix_world @ Vector(c) for o in objets for c in o.bound_box]
    if not coins:
        return
    centre = sum(coins, Vector()) / len(coins)
    rayon = max((c - centre).length for c in coins)
    cam.location = centre + Vector((1.0, -1.3, 0.8)).normalized() * rayon * 3.6
    cam.rotation_euler = (centre - cam.location).to_track_quat("-Z", "Y").to_euler()


try:
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o, do_unlink=True)
    # Tolère une marque BOM et une indentation commune (code recopié d'un bloc indenté par le modèle).
    code = textwrap.dedent((dossier / "scene.py").read_text(encoding="utf-8-sig"))
    exec(compile(code, "scene.py", "exec"), {"bpy": bpy, "__name__": "__scene__"})

    scene = bpy.context.scene
    maillages = [o for o in scene.objects if o.type == "MESH"]
    if not maillages:
        raise RuntimeError("la scène ne contient aucun objet")
    if scene.camera is None:
        cam = bpy.data.objects.new("Camera", bpy.data.cameras.new("Camera"))
        scene.collection.objects.link(cam)
        scene.camera = cam
        cadrer(cam, [o for o in maillages if not o.name.startswith("Decor")] or maillages)
    if not any(o.type == "LIGHT" for o in scene.objects):
        soleil = bpy.data.objects.new("Soleil", bpy.data.lights.new("Soleil", "SUN"))
        soleil.data.energy = 3
        soleil.rotation_euler = (0.8, 0.2, 0.6)
        scene.collection.objects.link(soleil)

    # Lumière d'ambiance de base (fond gris clair) : une scène peu éclairée par le modèle reste lisible.
    if scene.world is None:
        scene.world = bpy.data.worlds.new("Monde")
    scene.world.use_nodes = True
    fond = next((n for n in scene.world.node_tree.nodes if n.type == "BACKGROUND"), None)
    if fond is not None:
        fond.inputs[0].default_value = (0.8, 0.82, 0.85, 1)
        fond.inputs[1].default_value = max(fond.inputs[1].default_value, 0.7)

    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = 48
    scene.cycles.use_denoising = True
    scene.render.resolution_x, scene.render.resolution_y, scene.render.resolution_percentage = 1280, 720, 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.filepath = str(dossier / "rendu.png")
    bpy.ops.render.render(write_still=True)

    bpy.ops.object.select_all(action="DESELECT")
    for o in maillages:
        o.select_set(not o.name.startswith("Decor"))
    bpy.ops.export_scene.gltf(filepath=str(dossier / "modele.glb"), export_format="GLB", use_selection=True)
    ecrire(ok=True, objets=len(maillages))
except Exception:
    ecrire(erreur=traceback.format_exc()[-1500:])
