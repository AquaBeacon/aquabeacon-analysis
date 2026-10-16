"""Build a presentation-only Blender scene from an experiment's estimated poses."""

from __future__ import annotations

import bisect
import csv
import hashlib
import json
import math
import subprocess
import sys
from pathlib import Path

import bpy
from mathutils import Matrix, Quaternion, Vector


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def material(name: str, color: tuple[float, float, float, float], metal: float = 0) -> bpy.types.Material:
    result = bpy.data.materials.new(name)
    result.diffuse_color = color
    result.use_nodes = True
    bsdf = result.node_tree.nodes.get("Principled BSDF")
    bsdf.inputs["Base Color"].default_value = color
    bsdf.inputs["Metallic"].default_value = metal
    bsdf.inputs["Roughness"].default_value = 0.4
    return result


def cube(name: str, location: tuple[float, float, float], scale: tuple[float, float, float],
         surface: bpy.types.Material, parent: bpy.types.Object | None = None) -> bpy.types.Object:
    bpy.ops.mesh.primitive_cube_add(size=1, location=location)
    obj = bpy.context.object
    obj.name = name
    obj.dimensions = scale
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    obj.data.materials.append(surface)
    if parent is not None:
        obj.parent = parent
    return obj


def sphere(name: str, location: tuple[float, float, float], radius: float,
           surface: bpy.types.Material, parent: bpy.types.Object | None = None) -> bpy.types.Object:
    bpy.ops.mesh.primitive_uv_sphere_add(segments=16, ring_count=8, radius=radius, location=location)
    obj = bpy.context.object
    obj.name = name
    obj.data.materials.append(surface)
    if parent is not None:
        obj.parent = parent
    return obj


def curve(name: str, points: list[Vector], surface: bpy.types.Material,
          bevel: float = 0.014) -> None:
    if len(points) < 2:
        return
    data = bpy.data.curves.new(name, "CURVE")
    data.dimensions = "3D"
    data.bevel_depth = bevel
    data.bevel_resolution = 3
    spline = data.splines.new("POLY")
    spline.points.add(len(points) - 1)
    for item, point in zip(spline.points, points, strict=True):
        item.co = (*point, 1)
    obj = bpy.data.objects.new(name, data)
    bpy.context.collection.objects.link(obj)
    obj.data.materials.append(surface)


def text_obj(name: str, body: str, location: tuple[float, float, float], size: float,
             surface: bpy.types.Material) -> None:
    data = bpy.data.curves.new(name, "FONT")
    data.body = body
    data.size = size
    data.extrude = 0.001
    obj = bpy.data.objects.new(name, data)
    bpy.context.collection.objects.link(obj)
    obj.location = location
    obj.data.materials.append(surface)


def nearest_quaternion(rows: list[dict[str, str]], times: list[float], t: float) -> Quaternion | None:
    hi = bisect.bisect_left(times, t)
    candidates = [index for index in (hi - 1, hi) if 0 <= index < len(times)]
    if not candidates:
        return None
    index = min(candidates, key=lambda item: abs(times[item] - t))
    if abs(times[index] - t) > 0.1:
        return None
    row = rows[index]
    return Quaternion((float(row["qw"]), float(row["qx"]), float(row["qy"]), float(row["qz"]))).normalized()


def build(root: Path, experiment: str) -> dict[str, object]:
    output = root / "results" / experiment / "calibrated_estimate"
    position_path = output / "fused_position.csv"
    parent_path = root / "results" / experiment / "localization" / "parent_fused_attitude.csv"
    child_path = root / "results" / experiment / "localization" / "child_fused_attitude.csv"
    mount_path = root / "calibration" / "parent_camera_imu_provisional.json"
    rows, parent_rows, child_rows = read_csv(position_path), read_csv(parent_path), read_csv(child_path)
    mount = json.loads(mount_path.read_text(encoding="utf-8"))
    valid = [row for row in rows if row["fused_x_m"] and row["fused_y_m"] and row["fused_z_up_m"]]
    if len(valid) < 2:
        raise ValueError("insufficient fused positions")
    t0 = float(rows[0]["window_time_s"])
    fps = 24
    frame_of = lambda row: max(1, round((float(row["window_time_s"]) - t0) * fps) + 1)
    parent_times = [float(row["window_time_s"]) for row in parent_rows]
    child_times = [float(row["window_time_s"]) for row in child_rows]
    parent_first = nearest_quaternion(parent_rows, parent_times, float(valid[0]["window_time_s"]))
    child_first = nearest_quaternion(child_rows, child_times, float(valid[0]["window_time_s"]))
    if parent_first is None or child_first is None:
        raise ValueError("attitude unavailable at first published position")
    mount_matrix = Matrix(mount["optical_to_imu_rotation_matrix"])
    parent_forward = parent_first @ (mount_matrix @ Vector((0, 0, 1)))
    parent_forward.z = 0
    parent_forward.normalize()
    parent_right = parent_forward.cross(Vector((0, 0, 1))).normalized()
    world_to_display = Matrix((parent_forward, parent_right, Vector((0, 0, 1))))
    child_initial_display = Matrix(((0, 0, 1), (1, 0, 0), (0, -1, 0)))

    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    scene.cycles.samples = 24
    scene.render.resolution_x = 1200
    scene.render.resolution_y = 800
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.film_transparent = False
    scene.world.color = (0.015, 0.025, 0.04)
    scene.frame_start = 1
    scene.frame_end = frame_of(rows[-1])
    scene.render.fps = fps
    blue = material("Parent - cyan", (0.02, 0.58, 0.8, 1), 0.35)
    orange = material("Child - amber", (1.0, 0.32, 0.06, 1), 0.2)
    track = material("Published child trajectory", (1.0, 0.64, 0.1, 1))
    acoustic = material("Parent-child line", (0.35, 0.7, 0.9, 1))
    dark = material("Reference plane", (0.015, 0.075, 0.11, 1))
    pale = material("Labels and axes", (0.75, 0.88, 0.93, 1))
    green = material("X forward", (0.27, 0.8, 0.52, 1))
    red = material("Y right", (0.94, 0.29, 0.3, 1))

    positions = [Vector((float(row["fused_x_m"]), float(row["fused_y_m"]),
                         float(row["fused_z_up_m"]))) for row in valid]
    xs, ys, zs = ([point[axis] for point in positions] for axis in range(3))
    extent = max(max(xs) - min(xs), max(ys) - min(ys), 2.0)
    floor_z = min(zs) - 0.45
    cube("Horizontal reference plane", (0, 0, floor_z - 0.03),
         (extent * 2.5, extent * 2.5, 0.04), dark)
    for offset in range(-math.ceil(extent), math.ceil(extent) + 1):
        curve(f"X grid {offset}", [Vector((-extent, offset, floor_z)),
                                    Vector((extent, offset, floor_z))], pale, 0.002)
        curve(f"Y grid {offset}", [Vector((offset, -extent, floor_z)),
                                    Vector((offset, extent, floor_z))], pale, 0.002)
    curve("Local +X initial optical heading", [Vector((0, 0, floor_z + 0.01)),
                                               Vector((1, 0, floor_z + 0.01))], green, 0.018)
    curve("Local +Y camera right", [Vector((0, 0, floor_z + 0.01)),
                                     Vector((0, 1, floor_z + 0.01))], red, 0.018)
    text_obj("X label", "+X initial heading", (1.05, 0, floor_z + 0.02), 0.1, pale)
    text_obj("Y label", "+Y right", (0, 1.05, floor_z + 0.02), 0.1, pale)

    segment: list[Vector] = []
    previous_time = None
    segment_count = 0
    for row in rows:
        if row["fused_x_m"]:
            current_time = float(row["window_time_s"])
            if segment and previous_time is not None and current_time - previous_time > 1.1:
                curve(f"Child trajectory segment {segment_count}", segment, track)
                segment_count += 1
                segment = []
            segment.append(Vector((float(row["fused_x_m"]), float(row["fused_y_m"]),
                                   float(row["fused_z_up_m"]))))
            previous_time = current_time
        elif segment:
            curve(f"Child trajectory segment {segment_count}", segment, track)
            segment_count += 1
            segment = []
            previous_time = None
    curve(f"Child trajectory segment {segment_count}", segment, track)

    parent_rig = bpy.data.objects.new("Parent camera pose - provisional mount", None)
    bpy.context.collection.objects.link(parent_rig)
    parent_rig.rotation_mode = "QUATERNION"
    cube("Parent camera proxy", (0, 0, 0), (0.24, 0.14, 0.32), blue, parent_rig)
    cube("Parent forward mark", (0, 0, 0.21), (0.09, 0.07, 0.13), pale, parent_rig)
    child_rig = bpy.data.objects.new("Child proxy - independent yaw gauge", None)
    bpy.context.collection.objects.link(child_rig)
    child_rig.rotation_mode = "QUATERNION"
    sphere("Child body proxy", (0, 0, 0), 0.13, orange, child_rig)
    cube("Child forward mark", (0, 0, 0.17), (0.08, 0.08, 0.18), pale, child_rig)
    bpy.ops.mesh.primitive_cylinder_add(vertices=12, radius=0.012, depth=1)
    line = bpy.context.object
    line.name = "Conditional parent-child distance line"
    line.data.materials.append(acoustic)
    line.rotation_mode = "QUATERNION"
    for row in rows:
        frame = frame_of(row)
        t = float(row["window_time_s"])
        pq = nearest_quaternion(parent_rows, parent_times, t)
        if pq is not None:
            parent_rig.rotation_quaternion = (world_to_display @ pq.to_matrix() @ mount_matrix).to_quaternion()
            parent_rig.keyframe_insert(data_path="rotation_quaternion", frame=frame)
        available = bool(row["fused_x_m"] and row["fused_y_m"] and row["fused_z_up_m"])
        child_rig.hide_render = not available
        child_rig.hide_viewport = not available
        line.hide_render = not available
        line.hide_viewport = not available
        for obj in (child_rig, line):
            obj.keyframe_insert(data_path="hide_render", frame=frame)
            obj.keyframe_insert(data_path="hide_viewport", frame=frame)
        if not available:
            continue
        position = Vector((float(row["fused_x_m"]), float(row["fused_y_m"]),
                           float(row["fused_z_up_m"])))
        child_rig.location = position
        child_rig.keyframe_insert(data_path="location", frame=frame)
        cq = nearest_quaternion(child_rows, child_times, t)
        if cq is not None:
            child_relative = child_first.inverted() @ cq
            child_rig.rotation_quaternion = (child_initial_display @ child_relative.to_matrix()).to_quaternion()
            child_rig.keyframe_insert(data_path="rotation_quaternion", frame=frame)
        distance = position.length
        line.location = position / 2
        line.rotation_quaternion = Vector((0, 0, 1)).rotation_difference(position)
        line.scale = (1, 1, max(distance, 0.001))
        for path in ("location", "rotation_quaternion", "scale"):
            line.keyframe_insert(data_path=path, frame=frame)
    center = Vector(((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2,
                     (min(zs) + max(zs)) / 2))
    bpy.ops.object.camera_add(location=center + Vector((extent * 1.5, -extent * 2.3, extent * 1.6)))
    camera = bpy.context.object
    camera.name = "Overview camera"
    direction = center - camera.location
    camera.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
    camera.data.type = "ORTHO"
    camera.data.ortho_scale = extent * 3.2
    scene.camera = camera
    bpy.ops.object.light_add(type="AREA", location=center + Vector((0, -3, 5)))
    light = bpy.context.object
    light.data.energy = 1300
    light.data.shape = "DISK"
    light.data.size = 7
    scene.view_settings.view_transform = "AgX"
    chosen = valid[len(valid) // 2]
    scene.frame_set(frame_of(chosen))
    scene.render.filepath = str(output / "blender_preview.png")
    bpy.ops.wm.save_as_mainfile(filepath=str(output / "pose_trajectory.blend"))
    bpy.ops.render.render(write_still=True)
    metadata = {
        "experiment_id": experiment,
        "role": "presentation visualization only; no evaluation calculation",
        "scene_file": "pose_trajectory.blend", "preview_file": "blender_preview.png",
        "frame_start": scene.frame_start, "frame_end": scene.frame_end,
        "fps": fps, "preview_frame": scene.frame_current,
        "published_position_rows": len(valid), "trajectory_segments": segment_count + bool(segment),
        "coordinate_frame": "parent camera fixed at origin; +X initial optical heading, +Y right, +Z gravity up",
        "parent_pose": "P_MAIN fused quaternion with provisional optical-to-IMU rotation",
        "child_pose": "C_MAIN fused camera quaternion re-based to its first sample; yaw gauge is independent of parent",
        "magnetometer": "BNO085 magnetic reports exist but are not used in this scene's attitude; the logged quaternion is Game Rotation Vector",
        "geometry": "stylized proxies, not measured body or sensor-center geometry",
        "missing_position": "child proxy and distance line hidden; trajectory split at missing samples",
        "input_sha256": {str(path.relative_to(root)): sha256(path) for path in
                         (position_path, parent_path, child_path, mount_path)},
        "git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(),
        "blender_version": bpy.app.version_string,
    }
    (output / "blender_scene_manifest.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return metadata


def main() -> None:
    args = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    if len(args) != 2:
        raise SystemExit("usage: blender -b --python blender/create_pose_scene.py -- ROOT EXPERIMENT_ID")
    metadata = build(Path(args[0]).resolve(), args[1])
    print("AQUABEACON_SCENE", json.dumps({key: metadata[key] for key in
                                          ("experiment_id", "published_position_rows", "preview_frame")}))


if __name__ == "__main__":
    main()
