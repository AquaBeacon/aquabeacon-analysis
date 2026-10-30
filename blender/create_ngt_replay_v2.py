"""Editable, metric, retrospective NGT replay with frozen CAD and measured attitudes."""
from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VARIANTS = ("position_attitude", "map_vehicles", "map_only")


def sample_indices(times, start, stop, count):
    """Exact earlier source sample; never interpolate positions or bridge missing data."""
    if count < 2 or start < times[0] or stop > times[-1] or start >= stop:
        raise ValueError("Invalid replay interval/count")
    return [max(0, bisect.bisect_right(times, start+(stop-start)*i/(count-1))-1)
            for i in range(count)]


def reveal_frame(time_s, times):
    """First original timeline frame at or after an observation, including future sentinel."""
    return bisect.bisect_left(times, float(time_s))+1


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def clean_existing(args, bpy):
    """Export standalone figures from the verified movie scene without rebuilding geometry."""
    import numpy as np
    from mathutils import Vector

    output = ROOT/f"results/{args.experiment}/presentation_v2/render/{args.variant}"
    source_blend = output/(args.variant+".blend")
    bpy.ops.wm.open_mainfile(filepath=str(source_blend))
    scene = bpy.context.scene
    verification = json.loads((output/"verification.json").read_text(encoding="utf8"))
    index = verification["source_indices"][len(verification["source_indices"])*3//4]
    scene.frame_set(index+1)
    names = {"AquaBeacon_title_band", "Title", "Experiment", "Retrospective note",
             "CAD disclaimer", "Pose disclaimer", "Map semantics", "Map guide"}
    hidden = []
    for obj in scene.objects:
        if obj.name in names:
            obj.hide_render = obj.hide_viewport = True
            hidden.append(obj.name)
    alternate_map_camera = False
    if args.variant == "map_only":
        points = np.load(output.parent.parent/"data/points.npz")["xyz"]
        cam = scene.camera
        center = Vector((points.min(axis=0)+points.max(axis=0))/2)
        cam.location = center+Vector((.15, -1.6, .42)).normalized()*25
        cam.rotation_euler = (center-cam.location).to_track_quat("-Z", "Y").to_euler()
        cam.rotation_euler.rotate_axis("Z", math.radians(22))
        cam.data.ortho_scale = 14.8
        layout = bpy.data.objects["Presentation_layout_16x9"]
        s = cam.data.ortho_scale/16
        layout.scale, layout.location = (s,)*3, (0, 0, -8*s)
        # Calibrated orthographic scale reference; not a reconstructed edge.
        curve = bpy.data.curves.new("Clean_map_scale_1m", "CURVE")
        curve.dimensions, curve.bevel_depth = "3D", .006
        spline = curve.splines.new("POLY")
        spline.points.add(1)
        spline.points[0].co, spline.points[1].co = (0, 0, 0, 1), (1, 0, 0, 1)
        obj = bpy.data.objects.new("Clean_map_scale_1m", curve)
        scene.collection.objects.link(obj)
        obj.parent, obj.location = cam, (-6.9, -2.2, -7)
        curve.materials.append(bpy.data.materials["AquaBeacon #00739C"])
        font = bpy.data.curves.new("Clean_map_scale_label", "FONT")
        font.body, font.size, font.font = "1 m", .22, bpy.data.fonts[0]
        obj = bpy.data.objects.new("Clean_map_scale_label", font)
        scene.collection.objects.link(obj)
        obj.parent, obj.location = cam, (-6.9, -2.55, -7)
        font.materials.append(bpy.data.materials["Ink #1A2023"])
        alternate_map_camera = True
    scene["clean_standalone_export"] = True
    scene["clean_hidden_labels_json"] = json.dumps(hidden)
    bpy.context.window.scene = scene
    scene.render.filepath = str(output/(args.variant+"_clean_1080.png"))
    bpy.ops.render.render(write_still=True)
    primary_frame = scene.frame_current
    local_point_count = None
    for local in bpy.data.scenes:
        if not local.name.startswith("LOCAL_ORIGINAL"):
            continue
        cloud = np.load(output.parent.parent/"data/points.npz")
        source = json.loads((output.parent.parent/"data/scene.json").read_text(encoding="utf8"))
        reveal = np.searchsorted(np.asarray(source["times"]), cloud["reveal_parent_video_s"])+1
        old_collection = bpy.data.collections["FINAL_MAP_POINTS_BY_OBSERVATION_TIME"]
        fine_collection = bpy.data.collections.new("LOCAL_3MM_POINT_GLYPHS_SAME_CENTRES")
        local.collection.children.unlink(old_collection)
        local.collection.children.link(fine_collection)
        offsets = np.array([(1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0),
                            (0, 0, 1), (0, 0, -1)])
        local_point_count = 0
        for original in old_collection.objects:
            copied = original.copy()
            copied.data = original.data.copy()
            xyz = cloud["xyz"][reveal == original["reveal_frame"]]
            copied.data.vertices.foreach_set("co", (xyz[:, None, :]+offsets[None, :, :]*.003).ravel())
            copied.data.update()
            copied["point_glyph_radius_m"] = .003
            fine_collection.objects.link(copied)
            local_point_count += len(xyz)
        assert local_point_count == len(cloud["xyz"])
        local["point_glyph_radius_m"] = .003
        for obj in local.objects:
            if obj.type == "FONT" and obj.data.body != "0.25 m":
                obj.hide_render = obj.hide_viewport = True
        local.frame_set(scene.frame_end)
        local.render.filepath = str(output/"original_points_local_clean_1080.png")
        bpy.ops.render.render(write_still=True, scene=local.name)
    scene.frame_set(primary_frame)
    bpy.context.window.scene = scene
    clean_blend = output/(args.variant+"_clean.blend")
    bpy.ops.wm.save_as_mainfile(filepath=str(clean_blend), compress=True)
    archive = ROOT/verification.get("movie_renderer_source_archive", str(
        (output.parent/"renderer_movie_source.py").relative_to(ROOT)))
    renderer_key = next((k for k in verification["inputs"]
                         if k.replace("\\", "/") == "blender/create_ngt_replay_v2.py"), None)
    movie_original = (verification["inputs"].pop(renderer_key) if renderer_key else
                      verification["inputs"][str(archive.relative_to(ROOT))])
    if not archive.exists() or sha(archive) != movie_original:
        archive = output.parent/f"renderer_movie_source_{movie_original[:12]}.py"
        if not archive.exists():
            if sha(Path(__file__)) != movie_original:
                raise ValueError("Movie renderer changed before its exact source was archived")
            archive.write_bytes(Path(__file__).read_bytes())
    archive_key = str(archive.relative_to(ROOT))
    assert sha(archive) == movie_original
    verification["inputs"][archive_key] = movie_original
    wrapper_archive = ROOT/verification.get("movie_wrapper_source_archive", str(
        (output.parent/"wrapper_movie_source.py").relative_to(ROOT)))
    wrapper_key = next((k for k in verification["inputs"]
                        if k.replace("\\", "/") == "scripts/render_ngt_replay_v2.py"), None)
    wrapper_original = (verification["inputs"].pop(wrapper_key) if wrapper_key else
                        verification["inputs"][str(wrapper_archive.relative_to(ROOT))])
    if not wrapper_archive.exists() or sha(wrapper_archive) != wrapper_original:
        wrapper_archive = output.parent/f"wrapper_movie_source_{wrapper_original[:12]}.py"
        wrapper_source = ROOT/"scripts/render_ngt_replay_v2.py"
        if not wrapper_archive.exists():
            if sha(wrapper_source) != wrapper_original:
                raise ValueError("Movie wrapper changed before its exact source was archived")
            wrapper_archive.write_bytes(wrapper_source.read_bytes())
    wrapper_archive_key = str(wrapper_archive.relative_to(ROOT))
    assert sha(wrapper_archive) == wrapper_original
    verification["inputs"][wrapper_archive_key] = wrapper_original
    verification["movie_renderer_source_archive"] = str(archive.relative_to(ROOT))
    verification["movie_wrapper_source_archive"] = str(wrapper_archive.relative_to(ROOT))
    verification["source_archive_note"] = (
        "Verbatim movie-renderer source archived before additive clean-export CLI was added. "
        "Existing movie code and geometry were not rerendered or changed.")
    (output/"verification.json").write_text(json.dumps(verification, indent=2), encoding="utf8")
    clean_info = {"variant": args.variant, "source_blend": str(source_blend.relative_to(ROOT)),
                  "source_blend_sha256": sha(source_blend), "clean_blend_sha256": sha(clean_blend),
                  "clean_png_sha256": sha(output/(args.variant+"_clean_1080.png")),
                  "renderer_source_sha256": sha(Path(__file__)), "hidden_objects": hidden,
                  "geometry_and_original_poses_unchanged": True,
                  "alternate_camera_fitted_to_all_map_points": alternate_map_camera,
                  "camera_matrix_world": [list(row) for row in scene.camera.matrix_world],
                  "camera_ortho_scale": scene.camera.data.ortho_scale,
                  "active_scene": scene.name, "source_index": index,
                  "local_point_glyph_radius_m": .003 if local_point_count else None,
                  "local_source_point_count": local_point_count,
                  "local_point_centres_from_original_npz": local_point_count is not None}
    (output/"clean_verification.json").write_text(json.dumps(clean_info, indent=2), encoding="utf8")


def main():
    import bpy
    import numpy as np
    from mathutils import Quaternion, Vector

    sys.path.insert(0, str(ROOT / "blender"))
    from cad_assets import load_cad_body
    from create_ngt_visualizations import constant_keyframes, curves

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", required=True, choices=["20261028_ngt_004"])
    parser.add_argument("--variant", choices=VARIANTS, required=True)
    parser.add_argument("--frames", type=int, default=225)
    parser.add_argument("--fps", type=int, default=15)
    parser.add_argument("--draft", action="store_true")
    parser.add_argument("--render", action="store_true")
    parser.add_argument("--clean-existing", action="store_true")
    args = parser.parse_args(sys.argv[sys.argv.index("--")+1:])
    if args.clean_existing:
        clean_existing(args, bpy)
        return
    source = ROOT / f"results/{args.experiment}/presentation_v2/data/scene.json"
    data = json.loads(source.read_text(encoding="utf8"))
    times = data["times"]
    output = source.parent.parent / "render" / args.variant
    output.mkdir(parents=True, exist_ok=True)
    interval = data["render_interval"]
    start, stop = ((times[0], times[-1]) if args.variant == "position_attitude" else
                   (interval["start_parent_video_s"], interval["end_parent_video_s"]))
    selected = sample_indices(times, start, stop, args.frames)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.context.preferences.filepaths.save_version = 0
    scene = bpy.context.scene
    scene.name = args.variant
    scene.render.engine = "BLENDER_EEVEE"
    scene.render.threads_mode, scene.render.threads = "FIXED", 2
    scene.render.resolution_x, scene.render.resolution_y = 1920, 1080
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.fps = args.fps
    scene.frame_start, scene.frame_end = 1, len(times)
    scene.unit_settings.system, scene.unit_settings.scale_length = "METRIC", 1
    scene.view_settings.view_transform = "Standard"
    scene.view_settings.look = "None"
    scene.render.film_transparent = False
    scene.world = bpy.data.worlds.new("AquaBeacon pale blue studio")
    scene.world.use_nodes = True
    scene.world.node_tree.nodes["Background"].inputs[0].default_value = (.88, .925, .94, 1)
    scene.world.node_tree.nodes["Background"].inputs[1].default_value = 1
    world_nodes = scene.world.node_tree.nodes
    lighting = world_nodes.new("ShaderNodeBackground")
    lighting.inputs[0].default_value = (.88, .925, .94, 1)
    lighting.inputs[1].default_value = .28
    ray = world_nodes.new("ShaderNodeLightPath")
    mix = world_nodes.new("ShaderNodeMixShader")
    scene.world.node_tree.links.new(ray.outputs["Is Camera Ray"], mix.inputs[0])
    scene.world.node_tree.links.new(lighting.outputs[0], mix.inputs[1])
    scene.world.node_tree.links.new(world_nodes["Background"].outputs[0], mix.inputs[2])
    scene.world.node_tree.links.new(mix.outputs[0], world_nodes["World Output"].inputs[0])
    # Eevee uses the graphics device; only two CPU render threads are allocated.
    if hasattr(scene, "eevee"):
        if hasattr(scene.eevee, "taa_render_samples"):
            scene.eevee.taa_render_samples = 8 if args.render else 24
        if hasattr(scene.eevee, "use_raytracing"):
            scene.eevee.use_raytracing = False
    scene["purpose"] = "Presentation only; formal evaluation is outside Blender"
    scene["coordinate_frame"] = "Unchanged metric ENU; pressure depth negative Z"
    scene["replay_policy"] = "Exact original 5Hz sample hold; invalid poses fully hidden"
    scene["point_chronology"] = "Retrospective final geometry revealed at second distinct source observation"
    scene["cad_policy"] = "Frozen cad_20261028; design origins are not calibrated measurement centres"
    scene["source_scene_sha256"] = sha(source)
    scene["source_times_json"] = json.dumps(times)
    scene["output_source_indices_json"] = json.dumps(selected)
    scene["map_variant_camera_and_times_identical"] = True

    def collection(name):
        result = bpy.data.collections.new(name)
        scene.collection.children.link(result)
        return result

    def move(obj, target):
        for old in list(obj.users_collection):
            old.objects.unlink(obj)
        target.objects.link(obj)

    world_coll = collection("METRIC_WORLD_CAD_AND_TRACKS")
    map_coll = collection("FINAL_MAP_POINTS_BY_OBSERVATION_TIME")
    inset_coll = collection("MAGNIFIED_ATTITUDE_CAD_INSETS")
    overlay_coll = collection("EDITABLE_TITLES_TIME_AND_NOTES")
    guide_coll = collection("METRIC_REFERENCE_GUIDES_NOT_SURVEYED_POOL")
    studio_coll = collection("EDITABLE_STUDIO_LIGHTS_AND_CAMERAS")

    def mat(name, color, emission=False):
        result = bpy.data.materials.new(name)
        result.diffuse_color = (*color, 1)
        result.use_nodes = True
        nodes = result.node_tree.nodes
        shader = nodes.get("Principled BSDF")
        shader.inputs["Base Color"].default_value = (*color, 1)
        shader.inputs["Roughness"].default_value = .42
        if emission:
            glow = nodes.new("ShaderNodeEmission")
            glow.inputs["Color"].default_value = (*color, 1)
            glow.inputs["Strength"].default_value = 1
            result.node_tree.links.new(glow.outputs[0], nodes.get("Material Output").inputs[0])
        return result

    ink = mat("Ink #1A2023", (.0097, .0144, .0168), True)
    white = mat("White title", (1, 1, 1), True)
    blue = mat("AquaBeacon #00739C", (0, .171, .332), True)
    green = mat("Child green", (.015, .28, .16), True)
    pale = mat("Inset pale blue", (.88, .925, .94), True)
    grid_mat = mat("Fine reference grid", (.66, .75, .79), True)

    # The orthographic camera gives an uncluttered direct engineering view.
    bpy.ops.object.camera_add()
    cam = bpy.context.object
    cam.name = "Presentation_1920x1080"
    cam.data.type, cam.data.clip_start, cam.data.clip_end = "ORTHO", .01, 1000
    move(cam, studio_coll)
    scene.camera = cam
    points = None
    if args.variant != "position_attitude":
        npz_path = source.parent / data.get("points_npz", "points.npz")
        cloud = np.load(npz_path)
        points = cloud["xyz"]
        # No display crop, no normalization, no invented connecting surface.
        context_points = [list(p) for p in points[::20]]
        for body in ("parent", "child"):
            context_points.extend(p for p, valid, t in zip(data[body]["position_enu_m"],
                                  data[body]["position_valid"], times, strict=True)
                                  if valid and start <= t <= stop)
        context_points = np.asarray(context_points)
        low, high = context_points.min(axis=0), context_points.max(axis=0)
        center = Vector((low+high)/2)
        extent = max(high-low)
        view_direction = Vector((.15, -1.6, .42)).normalized()
        cam.location = center+view_direction*max(10, extent*2)
        cam.rotation_euler = (center-cam.location).to_track_quat("-Z", "Y").to_euler()
        cam.data.ortho_scale = max(5, extent*1.5)
        bpy.context.view_layer.update()
        inverse = cam.matrix_world.inverted()
        projected = np.array([tuple(inverse@Vector(p)) for p in context_points])
        # Reserve the right fifth for the attitude inset in both map variants.
        scale = max(np.ptp(projected[:, 0])*1.55, np.ptp(projected[:, 1])*2.6, 5)
        cam.data.ortho_scale = float(scale)
        cam.location += cam.rotation_euler.to_quaternion()@Vector((scale*.12, 0, 0))
    else:
        view_direction = Vector((.8, -1.3, .8)).normalized()
        cam.data.ortho_scale = 7.2

    # Camera-local overlay uses a consistent 16 x 9 layout at any metric zoom.
    ui = bpy.data.objects.new("Presentation_layout_16x9", None)
    overlay_coll.objects.link(ui)
    ui.parent = cam
    ui.location = (0, 0, -8*cam.data.ortho_scale/16)
    ui.scale = (cam.data.ortho_scale/16,)*3
    font = bpy.data.fonts.load("C:/Windows/Fonts/BIZ-UDGothicR.ttc")

    def text(name, body, xy, size=.23, material=ink):
        curve = bpy.data.curves.new(name, "FONT")
        curve.body, curve.size, curve.font = body, size, font
        obj = bpy.data.objects.new(name, curve)
        overlay_coll.objects.link(obj)
        obj.parent, obj.location = ui, (*xy, 0)
        curve.materials.append(material)
        return obj

    def panel(name, xy, width, height, material, z=-.01):
        mesh = bpy.data.meshes.new(name)
        mesh.from_pydata([(-width/2, -height/2, 0), (width/2, -height/2, 0),
                          (width/2, height/2, 0), (-width/2, height/2, 0)], [], [(0, 1, 2, 3)])
        obj = bpy.data.objects.new(name, mesh)
        overlay_coll.objects.link(obj)
        obj.parent, obj.location = ui, (*xy, z)
        mesh.materials.append(material)
        return obj

    titles = {"position_attitude": "親機・子機の推定位置と姿勢",
              "map_vehicles": "観測が増えるにつれて見える地図と機体の動き",
              "map_only": "観測時刻に沿って地図を表示"}
    panel("AquaBeacon_title_band", (0, 4.02), 16, .96, blue)
    text("Title", titles[args.variant], (-7.58, 3.84), .39, white)
    text("Experiment", "長田高校プール  2026.10.28 / 区間004", (-7.55, 3.13), .20)
    if args.variant == "map_only":
        text("Map semantics", "最終点群を観測時刻から再生（オンライン生成の再現ではありません）",
             (-7.55, -3.80), .21)
        text("Map guide", "疎な再構成点 / メートル座標 / 面の補間なし", (-7.55, -4.18), .19)
    else:
        # A solid inset backing keeps remote world geometry from crossing the CAD views.
        panel("Attitude_inset_backing", (5.85, -.03), 3.8, 5.75, pale)
        text("Attitude heading", "姿勢を拡大", (4.22, 2.64), .25)
        text("Attitude meaning", "磁北基準・精度未検証", (4.22, 2.27), .18)
        text("Parent label", "親機", (4.22, 1.89), .23, blue)
        text("Child label", "子機", (4.22, -.40), .23, green)
        text("Scale note", "右は拡大表示 / 赤：前方", (4.22, -2.70), .17)
        text("CAD disclaimer", "CAD原点と測定中心は未校正。子機の前方軸は暫定。", (-7.55, -3.82), .20)
        text("Pose disclaimer", "位置・姿勢の欠測時は左の機体を非表示 / 子カメラ・ライトは名目CAD配置", (-7.55, -4.18), .19)
        if args.variant == "map_vehicles":
            text("Retrospective note", "最終点群の観測時刻再生", (-7.55, 2.75), .20)
    for index in sorted(set(selected)):
        total = sum(1 for t in cloud["reveal_parent_video_s"] if t <= times[index]) if points is not None else None
        label = f"動画時刻  {times[index]:7.1f} s"
        if total is not None:
            label += f"     {total:,} / {len(points):,} 点"
        obj = text("Source_time_"+str(index), label, (-7.55, -3.35), .25)
        for f, hidden in ((0, True), (index+1, False), (index+2, True)):
            obj.hide_render = obj.hide_viewport = hidden
            obj.keyframe_insert(data_path="hide_render", frame=f)
            obj.keyframe_insert(data_path="hide_viewport", frame=f)
        constant_keyframes(obj)

    def hide_driver(obj, root, prop):
        for field in ("hide_render", "hide_viewport"):
            driver = obj.driver_add(field).driver
            driver.type = "SCRIPTED"
            var = driver.variables.new()
            var.name, var.targets[0].id = "valid", root
            var.targets[0].data_path = '["'+prop+'"]'
            driver.expression = "valid < 0.5"

    body_roots, mesh_counts = {}, {}
    styled_materials = set()
    if args.variant != "map_only":
        for body, color in (("parent", blue), ("child", green)):
            root, imported = load_cad_body(ROOT/"data/interim/cad/cad_20261028", body)
            root.name = body+"_EXACT_METRIC_CAD_POSE"
            body_roots[body] = root
            mesh_counts[body] = sum(o.type == "MESH" for o in imported)
            root.rotation_mode = "QUATERNION"
            root["pose_valid"], root["attitude_valid"] = 0.0, 0.0
            move(root, world_coll)
            forward = (1, 0, 0) if body == "parent" else (0, 1, 0)
            axis_mat = mat(body+" forward axis red", (.65, .035, .02), True)
            arrow = curves(scene, body+"_FRD_forward_axis", [(0, 0, 0),
                           tuple(.36*v for v in forward)], [[0, 1]], axis_mat, .005)
            arrow.parent = root
            imported.add(arrow)
            for obj in imported:
                move(obj, world_coll)
                hide_driver(obj, root, "pose_valid")
                if obj.type == "MESH":
                    # Preserve the actual mesh and local transforms; tune existing surfaces.
                    for polygon in obj.data.polygons:
                        polygon.use_smooth = True
                    for material in obj.data.materials:
                        if material and material.use_nodes and material.name not in styled_materials:
                            styled_materials.add(material.name)
                            shader = material.node_tree.nodes.get("Principled BSDF")
                            if shader:
                                rgba = list(shader.inputs["Base Color"].default_value)
                                shader.inputs["Base Color"].default_value = (
                                    rgba[0]*.32, rgba[1]*.32, rgba[2]*.32, rgba[3])
                                shader.inputs["Roughness"].default_value = .68
                                shader.inputs["Metallic"].default_value = 0
                                shader.inputs["Specular IOR Level"].default_value = .12
            # Linked duplicate hierarchy is an editable actual CAD pose inset.
            inset = bpy.data.objects.new(body+"_MAGNIFIED_ATTITUDE_ONLY", None)
            inset_coll.objects.link(inset)
            inset.parent = cam
            s = cam.data.ortho_scale/16
            inset.location = (5.85*s, (1.05 if body == "parent" else -1.25)*s, -4*s)
            inset.scale = (s*5.2,)*3
            inset.rotation_mode = "QUATERNION"
            copies = {}
            for original in imported:
                copied = original.copy()
                copied.animation_data_clear()
                inset_coll.objects.link(copied)
                copies[original] = copied
            for original, copied in copies.items():
                copied.parent = copies.get(original.parent, inset)
                hide_driver(copied, root, "attitude_valid")
            for i in range(len(times)):
                record = data[body]
                root["pose_valid"] = float(record["pose_valid"][i])
                root["attitude_valid"] = float(record["attitude_valid"][i])
                if record["position_valid"][i]:
                    root.location = record["position_enu_m"][i]
                if record["attitude_valid"][i]:
                    root.rotation_quaternion = Quaternion(record["cad_quaternion_wxyz"][i])
                for field in ("location", "rotation_quaternion", '["pose_valid"]', '["attitude_valid"]'):
                    root.keyframe_insert(data_path=field, frame=i+1)
            constant_keyframes(root)
            valid_points = record["position_enu_m"]
            segments, segment = [], []
            for i, valid in enumerate(record["position_valid"]):
                if valid:
                    segment.append(i)
                elif segment:
                    segments.append(segment)
                    segment = []
            if segment:
                segments.append(segment)
            for segment_number, segment in enumerate(segments):
                track = curves(scene, body+f"_metric_track_no_gap_bridges_{segment_number}",
                               valid_points, [segment], color, .007)
                move(track, world_coll)
                track["is_position_trace_not_reconstructed_geometry"] = True
                for frame, hidden in ((0, True), (segment[0]+1, False)):
                    track.hide_render = track.hide_viewport = hidden
                    track.keyframe_insert(data_path="hide_render", frame=frame)
                    track.keyframe_insert(data_path="hide_viewport", frame=frame)
                for number, source_index in enumerate(segment):
                    track.data.bevel_factor_end = number/max(1, len(segment)-1)
                    track.data.keyframe_insert(data_path="bevel_factor_end", frame=source_index+1)
                constant_keyframes(track)
                constant_keyframes(track.data)
            # inset orientation is in camera coordinates; keyframes retain the actual world pose.
            for i in range(len(times)):
                if args.variant == "position_attitude":
                    p = Vector(data["parent"]["position_enu_m"][i])
                    c = Vector(data["child"]["position_enu_m"][i]) if data["child"]["position_valid"][i] else p
                    center = (p+c)/2
                    cam.location = center+view_direction*10
                    cam.rotation_euler = (center-cam.location).to_track_quat("-Z", "Y").to_euler()
                    cam.location += cam.rotation_euler.to_quaternion()@Vector((cam.data.ortho_scale*.12, 0, 0))
                    cam.keyframe_insert(data_path="location", frame=i+1)
                    cam.keyframe_insert(data_path="rotation_euler", frame=i+1)
                if record["attitude_valid"][i]:
                    inset.rotation_quaternion = cam.rotation_euler.to_quaternion().inverted()@Quaternion(record["cad_quaternion_wxyz"][i])
                inset.keyframe_insert(data_path="rotation_quaternion", frame=i+1)
            constant_keyframes(inset)
        if args.variant == "position_attitude":
            constant_keyframes(cam)

    if points is not None:
        # Tiny octahedra are discrete point glyphs, not a fabricated continuous surface.
        point_mat = mat("Reconstructed source RGB", (.1, .4, .3))
        nodes = point_mat.node_tree.nodes
        attr = nodes.new("ShaderNodeVertexColor")
        attr.layer_name = "source_rgb"
        shader = nodes.get("Principled BSDF")
        point_mat.node_tree.links.new(attr.outputs["Color"], shader.inputs["Base Color"])
        point_mat.node_tree.links.new(attr.outputs["Color"], shader.inputs["Emission Color"])
        shader.inputs["Emission Strength"].default_value = .5
        shader.inputs["Roughness"].default_value = .8
        offsets = np.array([(1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)])
        facets = np.array([(0, 2, 4), (2, 1, 4), (1, 3, 4), (3, 0, 4),
                           (2, 0, 5), (1, 2, 5), (3, 1, 5), (0, 3, 5)])
        # Group by original 5Hz frame: actual observation chronology is unchanged.
        reveal = np.searchsorted(np.asarray(times), cloud["reveal_parent_video_s"])+1
        radius = .020
        for f in np.unique(reveal):
            use = reveal == f
            xyz, rgb = points[use], cloud["rgb"][use]
            vertices = (xyz[:, None, :]+offsets[None, :, :]*radius).reshape(-1, 3)
            faces = (facets[None, :, :]+np.arange(len(xyz))[:, None, None]*6).reshape(-1, 3)
            mesh = bpy.data.meshes.new(f"source_points_observed_{f}")
            mesh.from_pydata(vertices.tolist(), [], faces.tolist())
            mesh.update()
            colors = mesh.color_attributes.new(name="source_rgb", type="FLOAT_COLOR", domain="POINT")
            srgb = np.repeat(rgb/255, 6, axis=0)
            linear = np.where(srgb <= .04045, srgb/12.92, ((srgb+.055)/1.055)**2.4)
            rgba = np.column_stack((linear, np.ones(len(linear)))).astype(np.float32)
            colors.data.foreach_set("color", rgba.ravel())
            obj = bpy.data.objects.new(mesh.name, mesh)
            map_coll.objects.link(obj)
            mesh.materials.append(point_mat)
            obj["source_point_count"], obj["reveal_frame"] = len(xyz), int(f)
            obj["is_reconstructed_surface"] = False
            for frame, hidden in ((0, True), (int(f), False)):
                obj.hide_render = obj.hide_viewport = hidden
                obj.keyframe_insert(data_path="hide_render", frame=frame)
                obj.keyframe_insert(data_path="hide_viewport", frame=frame)
            constant_keyframes(obj)
        scene["source_point_count"] = len(points)
    else:
        all_positions = [p for body in ("parent", "child") for p, valid in
                         zip(data[body]["position_enu_m"], data[body]["position_valid"], strict=True) if valid]
        low = [math.floor(min(p[j] for p in all_positions))-1 for j in range(2)]
        high = [math.ceil(max(p[j] for p in all_positions))+1 for j in range(2)]
        gp, segments = [], []
        for axis in range(2):
            for v in range(low[axis], high[axis]+1, 2):
                p, q = [0, 0, 0], [0, 0, 0]
                p[axis] = q[axis] = v
                p[1-axis], q[1-axis] = low[1-axis], high[1-axis]
                segments.append([len(gp), len(gp)+1])
                gp.extend((p, q))
        grid = curves(scene, "2m_reference_grid_at_z0_NOT_POOL", gp, segments, grid_mat, .002)
        move(grid, guide_coll)
        text("Grid meaning", "格子 2 m / z=0 の座標参照", (-7.55, 2.75), .19)

    # Broad studio lighting shows the real CAD detail without inventing an environment.
    energies = (1800, 1100, 1600) if args.variant == "position_attitude" else (200, 150, 250)
    for name, location, energy, size in (("Key softbox", (4, -6, 12), energies[0], 8),
                                         ("Fill softbox", (-8, -2, 7), energies[1], 7),
                                         ("Rim softbox", (3, 8, 8), energies[2], 6)):
        bpy.ops.object.light_add(type="AREA", location=location)
        lamp = bpy.context.object
        lamp.name, lamp.data.energy, lamp.data.shape, lamp.data.size = name, energy, "DISK", size
        lamp.rotation_euler = (-lamp.location).to_track_quat("-Z", "Y").to_euler()
        move(lamp, studio_coll)
    # Distant soft lights keep the same exposure as the metric vehicles move.
    for name, rotation, energy in (("Soft key sun", (.5, -.3, -.4), 1.6),
                                    ("Soft fill sun", (1.1, .5, 2.5), .65)):
        bpy.ops.object.light_add(type="SUN")
        lamp = bpy.context.object
        lamp.name, lamp.rotation_euler = name, rotation
        lamp.data.energy, lamp.data.angle = (0 if args.variant == "position_attitude" else energy), .45
        move(lamp, studio_coll)
    # Insets remain lit consistently as the metric camera follows the vehicles.
    bpy.ops.object.light_add(type="AREA")
    lamp = bpy.context.object
    s = cam.data.ortho_scale/16
    lamp.name, lamp.parent = "Camera attached inset softbox", cam
    lamp.location = (5.85*s, 2*s, 0)
    lamp.data.energy, lamp.data.size = (6 if args.variant == "position_attitude" else 180)*s*s, 5*s
    move(lamp, studio_coll)
    local_scene_name = None
    if points is not None:
        # A second camera makes the genuine narrow local structure inspectable.
        # Only the viewport changes: all original points remain linked in the scene.
        bins = np.floor(points[:, 0]/.5).astype(int)
        labels, counts = np.unique(bins, return_counts=True)
        dense_x = (labels[int(np.argmax(counts))]+.5)*.5
        neighborhood = points[np.abs(points[:, 0]-dense_x) <= .6]
        local_center = Vector(np.median(neighborhood, axis=0))
        local_scene = bpy.data.scenes.new("LOCAL_ORIGINAL_POINTS_1_2M_NEIGHBORHOOD")
        local_scene_name = local_scene.name
        local_scene.world = scene.world
        local_scene.render.engine = "BLENDER_EEVEE"
        local_scene.render.threads_mode, local_scene.render.threads = "FIXED", 2
        local_scene.render.resolution_x, local_scene.render.resolution_y = 1920, 1080
        local_scene.render.resolution_percentage = 100
        local_scene.render.image_settings.file_format = "PNG"
        local_scene.view_settings.view_transform = "Standard"
        local_scene.collection.children.link(map_coll)
        local_scene.collection.children.link(studio_coll)
        local_scene.unit_settings.system, local_scene.unit_settings.scale_length = "METRIC", 1
        local_scene["viewport_only_no_geometry_crop"] = True
        local_scene["neighborhood_point_count"] = len(neighborhood)
        local_scene["source_covariance_eigenvalues_m2"] = list(np.linalg.eigvalsh(np.cov(points.T)))
        local_camera_data = bpy.data.cameras.new("Local_point_geometry_camera")
        local_camera = bpy.data.objects.new("Local_point_geometry_camera", local_camera_data)
        studio_coll.objects.link(local_camera)
        local_camera.data.type, local_camera.data.ortho_scale = "ORTHO", 2
        local_camera.location = local_center+Vector((.4, -1.8, .6)).normalized()*5
        local_camera.rotation_euler = (local_center-local_camera.location).to_track_quat("-Z", "Y").to_euler()
        local_scene.camera = local_camera
        for body, xy, size in (("再構成点群の局所拡大", (-7.5, 3.8), .36),
                               ("0.25 m", (-7.5, -3.4), .24),
                               ("幅2.0 mの局所表示 / 面の補間なし / 全点を元シーンに保持", (-7.5, -4.05), .21)):
            curve = bpy.data.curves.new("Local_view_label", "FONT")
            curve.body, curve.size, curve.font = body, size, font
            obj = bpy.data.objects.new("Local_view_label", curve)
            local_scene.collection.objects.link(obj)
            obj.parent, obj.location, obj.scale = local_camera, (xy[0]/8, xy[1]/8, -1), (.125,)*3
            curve.materials.append(ink)
        bar = curves(local_scene, "Local_orthographic_scale_0_25m", [(0, 0, 0), (.25, 0, 0)],
                     [[0, 1]], blue, .002)
        bar.parent, bar.location = local_camera, (-.94, -.36, -1)
        local_scene.frame_set(len(times))
        scene["covariance_eigenvalues_m2"] = list(np.linalg.eigvalsh(np.cov(points.T)))
        scene["map_coverage_note"] = "Near-linear reconstructed coverage; not a recovered broad wall"
    draft_index = selected[len(selected)*3//4]
    scene.frame_set(draft_index+1)
    scene["cad_material_display_gain"] = .32
    bpy.context.window.scene = scene
    bpy.ops.file.pack_all()
    blend_path = output / ("draft.blend" if args.draft else args.variant+".blend")
    bpy.ops.wm.save_as_mainfile(filepath=str(blend_path))
    names = {body: root.name for body, root in body_roots.items()}
    bpy.ops.wm.open_mainfile(filepath=str(blend_path))
    scene = bpy.context.scene
    checks = []
    for index in sorted({0, len(times)//2, len(times)-1, draft_index, *selected}):
        scene.frame_set(index+1)
        entry = {"source_index": index, "parent_video_s": times[index]}
        for body, name in names.items():
            root = bpy.data.objects[name]
            rec = data[body]
            assert root.scale == Vector((1, 1, 1))
            assert bool(root["pose_valid"]) == rec["pose_valid"][index]
            if rec["position_valid"][index]:
                assert (root.location-Vector(rec["position_enu_m"][index])).length < 1e-5
            if rec["attitude_valid"][index]:
                expected = Quaternion(rec["cad_quaternion_wxyz"][index])
                assert abs(abs(root.rotation_quaternion.dot(expected))-1) < 1e-5
            if index in {0, draft_index, len(times)-1}:
                assert all(o.hide_render == (not rec["pose_valid"][index])
                           for o in root.children_recursive)
            entry[body+"_pose_valid"] = rec["pose_valid"][index]
        if points is not None:
            visible_count = sum(o["source_point_count"] for o in scene.objects
                                if "source_point_count" in o and not o.hide_render)
            expected_count = int(np.sum(cloud["reveal_parent_video_s"] <= times[index]))
            assert visible_count == expected_count
            entry["visible_points"] = visible_count
        checks.append(entry)
    if args.variant == "map_only":
        assert not any("CAD" in o.name or "track" in o.name for o in scene.objects)
    scene.frame_set(draft_index+1)
    scene.render.filepath = str(output/("draft.png" if args.draft else args.variant+"_1080.png"))
    bpy.ops.render.render(write_still=True)
    if local_scene_name and not args.draft:
        local_scene = bpy.data.scenes[local_scene_name]
        local_scene.frame_set(len(times))
        local_scene.render.filepath = str(output/"original_points_local_closeup_1080.png")
        bpy.ops.render.render(write_still=True, scene=local_scene.name)
    if args.render:
        frames_dir = output/"frames"
        frames_dir.mkdir(exist_ok=True)
        for output_index, source_index in enumerate(selected):
            scene.frame_set(source_index+1)
            scene.render.filepath = str(frames_dir/f"{output_index:04d}.png")
            bpy.ops.render.render(write_still=True)
    verification = {"variant": args.variant, "source_scene_sha256": sha(source),
                    "reopened_saved_blend_checked": True, "cad_mesh_counts": mesh_counts,
                    "render_engine": scene.render.engine, "source_indices": selected,
                    "output_times": [times[i] for i in selected], "fps": args.fps,
                    "resolution": [1920, 1080], "draft": args.draft,
                    "point_glyph_radius_m": .020, "checks": checks,
                    "all_mesh_transforms_preserved": True, "metric_root_scale": 1,
                    "position_interpolation": "CONSTANT; exact source samples",
                    "attitude_interpolation": "CONSTANT; exact prepared CAD quaternions"}
    verification.update(camera_matrix_world=[list(row) for row in scene.camera.matrix_world],
                        camera_ortho_scale=scene.camera.data.ortho_scale,
                        main_scene=scene.name, main_camera=scene.camera.name,
                        git_commit=subprocess.check_output(["git", "rev-parse", "HEAD"],
                                                           cwd=ROOT, text=True).strip(),
                        visibility_drivers_checked_on_all_cad_children=True,
                        inputs={str(p.relative_to(ROOT)): sha(p) for p in
                                (source, Path(__file__), ROOT/"scripts/render_ngt_replay_v2.py",
                                 ROOT/"blender/cad_assets.py",
                                 ROOT/"data/interim/cad/cad_20261028/assets.json")},
                        render_config={"frames": args.frames, "fps": args.fps,
                                       "engine": "BLENDER_EEVEE", "resolution": [1920, 1080],
                                       "threads": 2, "cad_material_display_gain": .32,
                                       "point_glyph_radius_m": .020,
                                       "map_view_direction": [.15, -1.6, .42],
                                       "position_and_attitude": "Exact source sample hold"})
    if points is not None:
        verification["source_covariance_eigenvalues_m2"] = list(np.linalg.eigvalsh(np.cov(points.T)))
        verification["map_coverage"] = "Near-linear; not a reconstructed broad wall"
    (output/("draft_verification.json" if args.draft else "verification.json")).write_text(
        json.dumps(verification, indent=2), encoding="utf8")


if __name__ == "__main__":
    main()
