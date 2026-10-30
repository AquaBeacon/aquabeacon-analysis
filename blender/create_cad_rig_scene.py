"""Build a reusable CAD asset library and inspect exterior/interior views; no log inputs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import bpy
from mathutils import Vector

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cad_assets import load_cad_body


def camera(name, position, target, scale):
    bpy.ops.object.camera_add(location=position)
    obj = bpy.context.object
    obj.name = name
    obj.rotation_euler = (Vector(target) - obj.location).to_track_quat("-Z", "Y").to_euler()
    obj.data.type = "ORTHO"
    obj.data.ortho_scale = scale
    obj.data.clip_start = .001
    return obj


def main():
    repo = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rig-id", default="cad_20261027")
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:])
    if not args.rig_id.replace("_", "").replace("-", "").isalnum():
        raise ValueError("invalid rig ID")
    bundle = repo / "data/interim/cad" / args.rig_id
    output = repo / "blender/output" / args.rig_id
    output.mkdir(parents=True, exist_ok=True)
    profile = json.loads((bundle / "rig.json").read_text(encoding="utf-8"))
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    scene = bpy.context.scene
    scene.unit_settings.system = "METRIC"
    scene.unit_settings.scale_length = 1.0
    scene.render.engine = "BLENDER_WORKBENCH"
    scene.render.resolution_x = 1400
    scene.render.resolution_y = 1100
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.display.shading.light = "STUDIO"
    scene.display.shading.color_type = "MATERIAL"
    scene.display.shading.show_shadows = True
    scene.display.shading.show_cavity = True
    scene.display.shading.cavity_type = "BOTH"
    scene.display.shading.background_type = "WORLD"
    scene.world.color = (.8, .8, .8)
    roots, objects, references = {}, {}, {}
    validation = {}
    assets = json.loads((bundle / "assets.json").read_text(encoding="utf-8"))["assets"]
    for body, offset in (("parent", (-.35, 0, .14)), ("child", (.35, 0, 0))):
        root, imported = load_cad_body(bundle, body)
        root.location = offset
        collection = bpy.data.collections.new(f"{body}_CAD")
        scene.collection.children.link(collection)
        for obj in [root, *imported]:
            for old in list(obj.users_collection):
                old.objects.unlink(obj)
            collection.objects.link(obj)
        collection.asset_mark()
        collection.asset_data.description = "Nominal CAD in meters; no measured sensor extrinsics"
        roots[body], objects[body] = root, imported
        refs = []
        for sensor in profile["sensors"]:
            if sensor["body"] != body:
                continue
            # Empties display exact reference frames in the viewport without
            # pretending to render an absent sensor body or optical center.
            obj = bpy.data.objects.new(sensor["id"] + "__REFERENCE_ONLY", None)
            collection.objects.link(obj)
            obj.parent = root
            obj.location = sensor["reference_position_body_m"]
            obj.empty_display_type = "SPHERE"
            obj.empty_display_size = .004
            obj["reference_kind"] = sensor["reference_kind"]
            obj["limitation"] = sensor["limitation"]
            obj["T_body_sensor"] = "UNKNOWN"
            refs.append(obj)
        references[body] = refs
    bpy.context.view_layer.update()
    # Check the actual importer: Y-up conversion, mm->m, every node and local
    # origin must agree with the numeric CAD snapshot before saving a scene.
    for body, imported in objects.items():
        meshes = {obj.name: obj for obj in imported if obj.type == "MESH"}
        expected = [c for c in profile["components"] if c["body"] == body]
        if set(meshes) != {c["id"] for c in expected}:
            raise RuntimeError(f"CAD node names/count mismatch: {body}")
        errors = [
            (meshes[c["id"]].matrix_world.translation - roots[body].location
             - Vector([row[3] for row in c["T_body_component"][:3]])).length
            for c in expected
        ]
        corners = [obj.matrix_world @ Vector(corner) - roots[body].location
                   for obj in meshes.values() for corner in obj.bound_box]
        bounds = [[op(v[i] for v in corners) for i in range(3)] for op in (min, max)]
        bounds_error = max(abs(a - b) for row, other in zip(bounds, assets[body]["bounds_body_m"])
                           for a, b in zip(row, other))
        if max(errors) > 1e-6 or bounds_error > 1e-6:
            raise RuntimeError(f"CAD importer coordinate mismatch: {body}")
        validation[body] = {"max_component_origin_error_m": max(errors),
                            "bounds_error_m": bounds_error, "mesh_count": len(meshes)}
    cameras = {}
    rendered = []
    component_groups = {c["id"]: c["group"] for c in profile["components"]}
    for body in roots:
        for other, body_objects in objects.items():
            for obj in body_objects:
                obj.hide_render = other != body
        bounds = [obj.matrix_world @ Vector(corner)
                  for obj in objects[body] if obj.type == "MESH" for corner in obj.bound_box]
        center = sum(bounds, Vector()) / len(bounds)
        low = Vector([min(v[i] for v in bounds) for i in range(3)])
        high = Vector([max(v[i] for v in bounds) for i in range(3)])
        center = (low + high) / 2
        extent = max(high - low)
        cam = camera(f"{body}_exterior_camera", center + Vector((1, -1.4, 1.1)) * extent,
                     center, extent * 1.45)
        bpy.context.view_layer.update()
        projected = [cam.matrix_world.inverted() @ point for point in bounds]
        aspect = scene.render.resolution_x / scene.render.resolution_y
        cam.data.ortho_scale = 2 * max(
            max(abs(p.x) for p in projected), max(abs(p.y) for p in projected) * aspect
        ) * 1.10
        cameras[body] = cam
        for mode in ("exterior", "interior"):
            for obj in objects[body]:
                # Keep internal parts/PCB, hide enclosure and exterior for inspection.
                if mode == "interior":
                    obj.hide_render = component_groups.get(obj.name) != "interior"
            scene.camera = cam
            scene.render.filepath = str(output / f"{body}_{mode}.png")
            bpy.ops.render.render(write_still=True)
            rendered.append(f"{body}_{mode}.png")
        for obj in objects[body]:
            obj.hide_render = False
    for body_objects in objects.values():
        for obj in body_objects:
            obj.hide_render = False
    overview = camera("CAD_overview", (1.0, -1.6, 1.2), (0, 0, .12), 1.25)
    scene.camera = overview
    scene.render.filepath = str(output / "overview.png")
    bpy.ops.render.render(write_still=True)
    rendered.append("overview.png")
    scene["rig_id"] = args.rig_id
    scene["scope"] = "future_experiments_only; display offsets are not experiment positions"
    scene["transform_convention"] = profile["transform_convention"]
    bpy.context.preferences.filepaths.save_version = 0
    bpy.ops.wm.save_as_mainfile(filepath=str(output / "cad_rig.blend"))
    bpy.ops.export_scene.gltf(filepath=str(output / "cad_rig.glb"), export_format="GLB")
    manifest = {
        "rig_id": args.rig_id, "blender_version": bpy.app.version_string,
        "units": "m", "profile_sha256": hashlib.sha256((bundle / "rig.json").read_bytes()).hexdigest(),
        "source_assets": json.loads((bundle / "assets.json").read_text(encoding="utf-8")),
        "display_offsets_m": {b: list(root.location) for b, root in roots.items()},
        "rendered": rendered, "body_mesh_nodes": {b: sum(o.type == "MESH" for o in v)
                                                   for b, v in objects.items()},
        "sensor_reference_empties": {b: len(v) for b, v in references.items()},
        "import_validation": validation,
        "generator_sha256": {name: hashlib.sha256((repo / name).read_bytes()).hexdigest()
                             for name in ("blender/cad_assets.py", "blender/create_cad_rig_scene.py")},
        "pid": os.getpid(), "completed_utc": datetime.now(UTC).isoformat(),
        "status": "rendered_pending_visual_review",
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
