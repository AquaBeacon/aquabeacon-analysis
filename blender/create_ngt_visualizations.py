"""Presentation-only Blender scenes for NGT trajectories and saved wall fragments."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import bpy
from mathutils import Vector

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def finite(point):
    return point is not None and len(point) == 3 and all(x is not None and math.isfinite(x) for x in point)


def material(name, rgb):
    m = bpy.data.materials.new(name)
    m.diffuse_color = (*rgb, 1)
    return m


def setup(scene):
    scene.unit_settings.system = "METRIC"
    scene.unit_settings.scale_length = 1
    scene.render.engine = "BLENDER_WORKBENCH"
    scene.render.resolution_x, scene.render.resolution_y = 1400, 850
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.threads_mode = "FIXED"
    scene.render.threads = 2
    scene.display.shading.light = "STUDIO"
    scene.display.shading.color_type = "MATERIAL"
    scene.display.shading.show_shadows = False
    scene.display.shading.show_cavity = True
    scene.display.shading.cavity_type = "BOTH"
    scene.display.shading.background_type = "WORLD"
    if scene.world is None:
        scene.world = bpy.data.worlds.new("White background")
    scene.world.color = (.92, .96, .98)
    scene.view_settings.view_transform = "Standard"
    scene.render.film_transparent = False
    scene["purpose"] = "Presentation only. Formal evaluation is performed in Python, not Blender."


def curves(scene, name, points, segments, mat, radius):
    curve = bpy.data.curves.new(name, "CURVE")
    curve.dimensions = "3D"
    curve.bevel_depth = radius
    curve.bevel_resolution = 1
    for segment in segments:
        if len(segment) < 2:
            continue
        spline = curve.splines.new("POLY")
        spline.points.add(len(segment)-1)
        for vertex, index in zip(spline.points, segment, strict=True):
            vertex.co = (*points[index], 1)
    obj = bpy.data.objects.new(name, curve)
    scene.collection.objects.link(obj)
    obj.data.materials.append(mat)
    return obj


def point_mesh(scene, name, points, mat, radius):
    # Small octahedra are display glyphs, not reconstructed wall surfaces.
    verts, faces = [], []
    offsets = [(1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)]
    facets = [(0, 2, 4), (2, 1, 4), (1, 3, 4), (3, 0, 4),
              (2, 0, 5), (1, 2, 5), (3, 1, 5), (0, 3, 5)]
    for p in points:
        if not finite(p):
            continue
        base = len(verts)
        verts.extend([tuple(p[i]+radius*v[i] for i in range(3)) for v in offsets])
        faces.extend([tuple(base+i for i in f) for f in facets])
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(verts, [], faces)
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    scene.collection.objects.link(obj)
    obj.data.materials.append(mat)
    obj["point_glyph_radius_m"] = radius
    obj["is_reconstructed_surface"] = False
    return obj


def camera(scene, name, points, direction=(.5, -1.3, .85), minimum=2.):
    points = [Vector(p) for p in points if finite(p)]
    low = Vector([min(p[j] for p in points) for j in range(3)])
    high = Vector([max(p[j] for p in points) for j in range(3)])
    center, extent = (low+high)/2, max(max(high-low), minimum)
    data = bpy.data.cameras.new(name)
    obj = bpy.data.objects.new(name, data)
    scene.collection.objects.link(obj)
    obj.location = center+Vector(direction).normalized()*extent*2
    obj.rotation_euler = (center-obj.location).to_track_quat("-Z", "Y").to_euler()
    data.type = "ORTHO"
    data.clip_start = .001
    data.clip_end = 10000
    scene.camera = obj
    bpy.context.view_layer.update()
    inv = obj.matrix_world.inverted()
    projected = [inv@p for p in points]
    aspect = scene.render.resolution_x/scene.render.resolution_y
    data.ortho_scale = max(minimum, 2*max(max(abs(p.x) for p in projected),
                           max(abs(p.y) for p in projected)*aspect)*1.2)
    return obj


def render(scene, cam, path):
    scene.camera = cam
    scene.render.filepath = str(path)
    bpy.ops.render.render(write_still=True, scene=scene.name)


def open_camera_view():
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type == "VIEW_3D":
                area.spaces.active.region_3d.view_perspective = "CAMERA"


def animate(obj, point, frame):
    visible = finite(point)
    obj.hide_render = obj.hide_viewport = not visible
    if visible:
        obj.location = point
    for prop in ("location", "hide_render", "hide_viewport"):
        obj.keyframe_insert(data_path=prop, frame=frame)


def constant_keyframes(obj):
    # keyframe_insert uses BEZIER for numeric properties in Blender 5.2 even when
    # the UI preference is CONSTANT; enforce the actual saved channel curves.
    for layer in obj.animation_data.action.layers:
        for strip in layer.strips:
            bag = strip.channelbag(obj.animation_data.action_slot)
            if bag:
                for curve in bag.fcurves:
                    for key in curve.keyframe_points:
                        key.interpolation = "CONSTANT"


def trajectory(experiment):
    out = ROOT / f"results/{experiment}/position_visualization"
    source = out / "scene_data.json"
    data = json.loads(source.read_text(encoding="utf-8"))
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.context.preferences.edit.keyframe_new_interpolation_type = "CONSTANT"
    bpy.context.preferences.filepaths.save_version = 0
    scene = bpy.context.scene
    scene.name = "Parent_Child_Sample_Hold"
    setup(scene)
    blue = material("Parent GNSS - blue", (.03, .36, .69))
    orange = material("Child range-ray estimate - orange", (.95, .24, .045))
    green = material("Observed parent-child line", (.06, .55, .36))
    gray = material("Water-reference grid", (.55, .69, .74))
    for body, mat in [("parent", blue), ("child", orange)]:
        curves(scene, body+"_track_no_gap_bridges", data[body], data[body+"_segments"], mat, .011)
    bpy.ops.mesh.primitive_cube_add(size=1)
    parent = bpy.context.object
    parent.name = "Parent_PROXY_not_actual_dimensions"
    parent.scale = (.25, .20, .10)
    parent.data.materials.append(blue)
    bpy.ops.mesh.primitive_uv_sphere_add(segments=16, ring_count=8, radius=.12)
    child = bpy.context.object
    child.name = "Child_PROXY_not_actual_dimensions"
    child.data.materials.append(orange)
    bpy.ops.mesh.primitive_cylinder_add(vertices=12, radius=1, depth=1)
    link = bpy.context.object
    link.name = "Observed_range_ray_line"
    link.rotation_mode = "QUATERNION"
    link.data.materials.append(green)
    for frame, (p, c) in enumerate(zip(data["parent"], data["child"], strict=True), start=1):
        animate(parent, p, frame)
        animate(child, c, frame)
        valid = finite(p) and finite(c)
        link.hide_render = link.hide_viewport = not valid
        if valid:
            direction = Vector(c)-Vector(p)
            link.location = (Vector(p)+Vector(c))/2
            link.rotation_quaternion = direction.to_track_quat("Z", "Y")
            link.scale = (.008, .008, direction.length)
        for prop in ("location", "rotation_quaternion", "scale", "hide_render", "hide_viewport"):
            link.keyframe_insert(data_path=prop, frame=frame)
    for obj in (parent, child, link):
        constant_keyframes(obj)
    points = [p for p in data["parent"]+data["child"] if finite(p)]
    bounds = [[op(p[j] for p in points) for j in range(3)] for op in (min, max)]
    grid_points, grid_segments = [], []
    step = 1 if max(bounds[1][j]-bounds[0][j] for j in range(2)) < 10 else 5
    for axis in (0, 1):
        other = 1-axis
        for value in range(math.floor(bounds[0][axis]/step)*step, math.ceil(bounds[1][axis]/step)*step+1, step):
            p, q = [0, 0, 0], [0, 0, 0]
            p[axis] = q[axis] = value
            p[other], q[other] = bounds[0][other]-1, bounds[1][other]+1
            grid_segments.append([len(grid_points), len(grid_points)+1])
            grid_points.extend([p, q])
    curves(scene, "Reference_grid_at_parent_z0_not_pool_floor", grid_points, grid_segments, gray, .003)
    scene.frame_start, scene.frame_end, scene.render.fps = 1, len(data["times"]), 5
    scene["first_parent_video_s"] = data["times"][0]
    scene["time_formula"] = "parent_video_s = first_parent_video_s + (frame-1)/5"
    scene["coordinate_frame"] = data["notes"]
    scene["model_status"] = data["body_geometry"]
    scene.frame_set(data["preview_index"]+1)
    overview = camera(scene, "Overview", points, minimum=2)
    render(scene, overview, out / "blender_overview.png")
    sample = data["preview_index"]
    close = camera(scene, "Parent_child_closeup", [data["parent"][sample], data["child"][sample]], minimum=2.6)
    render(scene, close, out / "blender_closeup.png")
    # Save close-up as opening camera; overview remains selectable.
    scene.camera = close
    checks = []
    for index in sorted({0, sample, len(data["times"])-1,
                         next((i for i, p in enumerate(data["child"]) if not finite(p)), 0)}):
        scene.frame_set(index+1)
        if finite(data["child"][index]):
            error = (child.location-Vector(data["child"][index])).length
            assert error < 1e-5 and not child.hide_render
        else:
            error = None
            assert child.hide_render and link.hide_render
        checks.append({"frame": index+1, "child_error_m": error, "child_hidden": child.hide_render})
    scene.frame_set(sample+1)
    path = out / "parent_child.blend"
    open_camera_view()
    bpy.ops.wm.save_as_mainfile(filepath=str(path))
    return {"experiment": experiment, "blend": str(path.relative_to(ROOT)), "source_sha256": digest(source),
            "frames": len(data["times"]), "checks": checks, "scene_policy": data["notes"]}


def walls():
    out = ROOT / "results/20261028_ngt_004/map_visualization"
    source = out / "map_data.json"
    data = json.loads(source.read_text(encoding="utf-8"))
    models = [m for m in data["maps"] if m.get("model") != "combined"
              and m["mode"] == "aquabeacon_prior" and m["valid_structure"]]
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.context.preferences.filepaths.save_version = 0
    palette = [(.04, .39, .64), (.9, .32, .1), (.12, .55, .32), (.5, .3, .7)]
    summary = []
    for index in range(-1, len(models)):
        scene = bpy.context.scene if index == -1 else bpy.data.scenes.new(f"Wall_fragment_{models[index]['model']}")
        bpy.context.window.scene = scene
        if index == -1:
            scene.name = "All_four_wall_fragments"
        setup(scene)
        selected = models if index == -1 else [models[index]]
        blue = material("Reconstructed camera", (.03, .4, .7))
        orange = material("Input position prior", (.95, .32, .05))
        points = []
        for m in selected:
            color = palette[models.index(m)]
            mat = material(f"Fragment_{m['model']}", color)
            radius = .004 if index == -1 else .0018
            obj = point_mesh(scene, f"Wall_points_{m['model']}", m["points"], mat, radius)
            obj["source_pointcloud"] = m["source_pointcloud"]
            obj["point_count"] = len(m["points"])
            curves(scene, f"Camera_path_{m['model']}", m["cameras"], m["camera_segments"], blue, radius)
            point_mesh(scene, f"Position_priors_{m['model']}", m["priors"], orange, radius*2)
            points.extend(m["points"]+m["cameras"]+m["priors"])
        scene["coordinate_status"] = "Unchanged position-prior coordinates; no additional alignment; metric scale conditional"
        scene["geometry_status"] = "Sparse points displayed as octahedral glyphs; not a surface mesh"
        cam = camera(scene, "Wall_view", points, direction=(.6, -1, .8), minimum=.4)
        if index == -1:
            render(scene, cam, out / "blender_wall_overview.png")
        else:
            render(scene, cam, out / f"blender_wall_fragment{models[index]['model']}.png")
        summary.append({"scene": scene.name, "models": [m["model"] for m in selected],
                        "points": sum(len(m["points"]) for m in selected)})
    bpy.context.window.scene = bpy.data.scenes["All_four_wall_fragments"]
    path = out / "wall_map.blend"
    open_camera_view()
    bpy.ops.wm.save_as_mainfile(filepath=str(path))
    return {"experiment": "20261028_ngt_004", "blend": str(path.relative_to(ROOT)),
            "source_sha256": digest(source), "scenes": summary}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", required=True, action="append",
                        choices=[f"20261028_ngt_{i:03}" for i in (2, 3, 4)])
    args = parser.parse_args(sys.argv[sys.argv.index("--")+1:])
    record = {"pid": os.getpid(), "started_utc": datetime.now(UTC).isoformat(),
              "owner": "ngt_visualization", "purpose": __doc__, "blender": bpy.app.version_string,
              "threads": 2, "source_sha256": digest(Path(__file__)), "outputs": []}
    print(json.dumps(record), flush=True)
    log = ROOT / "results/20261028_ngt_000/position_evaluation/visualization_blender_process.json"
    log.write_text(json.dumps(record, indent=2), encoding="utf-8")
    try:
        for experiment in dict.fromkeys(args.experiment):
            result = trajectory(experiment)
            record["outputs"].append(result)
            out = ROOT / f"results/{experiment}/position_visualization"
            (out / "blender_manifest.json").write_text(json.dumps({**record, "output": result}, indent=2), encoding="utf-8")
        if "20261028_ngt_004" in args.experiment:
            result = walls()
            record["outputs"].append(result)
            (ROOT / "results/20261028_ngt_004/map_visualization/blender_manifest.json").write_text(
                json.dumps({**record, "output": result}, indent=2), encoding="utf-8")
        record["status"] = "completed"
    except BaseException as error:
        record["status"], record["error"] = "failed", str(error)
        raise
    finally:
        record["ended_utc"] = datetime.now(UTC).isoformat()
        log.write_text(json.dumps(record, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
