"""Create an inspectable Blender scene of the experimental body and PCB layouts."""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
import sys
from pathlib import Path

import bpy
from mathutils import Vector

SOURCE_MESHES = set()


def material(name: str, color: tuple[float, float, float, float]):
    value = bpy.data.materials.new(name)
    value.diffuse_color = color
    return value


def load_part(root: Path, relative: str, name: str, color, transforms=(), offset=(0, 0, 0)):
    path = root / relative
    SOURCE_MESHES.add(path)
    before = set(bpy.data.objects)
    bpy.ops.wm.stl_import(filepath=str(path))
    added = list(set(bpy.data.objects) - before)
    if len(added) != 1:
        raise RuntimeError(f"expected one imported object: {path}")
    obj = added[0]
    obj.name = name
    obj.data.materials.append(color)
    # OpenSCAD placement ops are nested outer-first; apply them from inside out.
    for transform in reversed(transforms):
        if "rotate" in transform:
            angles = tuple(math.radians(v) for v in transform["rotate"])
            for axis, angle in zip("XYZ", angles, strict=True):
                if angle:
                    matrix = __import__("mathutils").Matrix.Rotation(angle, 4, axis)
                    obj.matrix_world = matrix @ obj.matrix_world
        if "translate" in transform:
            obj.matrix_world.translation += Vector(transform["translate"])
    obj.matrix_world.translation += Vector(offset)
    return obj


def label(name: str, position, color, size=12):
    curve = bpy.data.curves.new(name, "FONT")
    curve.body = name
    curve.size = size
    curve.extrude = 0.15
    obj = bpy.data.objects.new(name, curve)
    bpy.context.collection.objects.link(obj)
    obj.location = position
    obj.data.materials.append(color)
    return obj


def marker(name: str, position, color, radius=5):
    bpy.ops.mesh.primitive_uv_sphere_add(segments=12, ring_count=8, radius=radius, location=position)
    obj = bpy.context.object
    obj.name = name
    obj.data.materials.append(color)
    return obj


def line(name: str, points, color, radius=0.8):
    curve = bpy.data.curves.new(name, "CURVE")
    curve.dimensions = "3D"
    curve.bevel_depth = radius
    curve.bevel_resolution = 2
    spline = curve.splines.new("POLY")
    spline.points.add(len(points) - 1)
    for point, coordinates in zip(spline.points, points, strict=True):
        point.co = (*coordinates, 1)
    obj = bpy.data.objects.new(name, curve)
    bpy.context.collection.objects.link(obj)
    obj.data.materials.append(color)
    return obj


def box_outline(name: str, center, dimensions, color):
    x, y, z = center
    hx, hy, hz = (v / 2 for v in dimensions)
    for sx in (-1, 1):
        for sy in (-1, 1):
            line(f"{name} vertical", [(x + sx * hx, y + sy * hy, z - hz),
                                     (x + sx * hx, y + sy * hy, z + hz)], color)
    for sz in (-1, 1):
        for sy in (-1, 1):
            line(f"{name} x", [(x - hx, y + sy * hy, z + sz * hz),
                              (x + hx, y + sy * hy, z + sz * hz)], color)
        for sx in (-1, 1):
            line(f"{name} y", [(x + sx * hx, y - hy, z + sz * hz),
                              (x + sx * hx, y + hy, z + sz * hz)], color)


def camera(target, distance, name):
    bpy.ops.object.camera_add(location=(target[0] + distance, target[1] - distance,
                                        target[2] + distance * 0.8))
    obj = bpy.context.object
    obj.name = name
    direction = Vector(target) - obj.location
    obj.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
    obj.data.type = "ORTHO"
    obj.data.ortho_scale = distance * 1.5
    return obj


def camera_at(location, target, scale, name):
    bpy.ops.object.camera_add(location=location)
    obj = bpy.context.object
    obj.name = name
    obj.rotation_euler = (Vector(target) - obj.location).to_track_quat("-Z", "Y").to_euler()
    obj.data.type = "ORTHO"
    obj.data.ortho_scale = scale
    return obj


def render(path: Path, cam):
    bpy.context.scene.camera = cam
    bpy.context.scene.render.filepath = str(path)
    bpy.ops.render.render(write_still=True)


def main():
    repo = Path(__file__).resolve().parents[1]
    experiment = sys.argv[sys.argv.index("--") + 1] if "--" in sys.argv else "20260922_cmain_023"
    output = repo / "results" / experiment / "body_geometry_review"
    output.mkdir(parents=True, exist_ok=True)
    root = repo.parent / "mechanical"
    reference = repo / "calibration" / "experiment_body_geometry_20260922.json"
    geometry = json.loads(reference.read_text(encoding="utf-8"))

    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    deck = material("parent deck orange", (0.95, 0.42, 0.12, 1))
    body = material("child frame teal", (0.12, 0.58, 0.66, 1))
    board = material("main PCB green", (0.12, 0.7, 0.25, 1))
    logger = material("Spresense purple", (0.55, 0.28, 0.85, 1))
    pale = material("vessel outline gray", (0.48, 0.55, 0.62, 1))
    red = material("confirm orientation red", (0.95, 0.16, 0.17, 1))
    white = material("labels white", (0.95, 0.95, 0.9, 1))

    # Two separate layouts in one scene, 500 mm apart. Every component is in mm.
    parent_offset = (-300, 0, 0)
    child_offset = (300, 0, 0)
    load_part(root, "parts/parent/exterior/printed/parent_simple_deck/parent_simple_deck.stl",
              "P_MAIN simple deck", deck, offset=parent_offset)
    box_outline("P_MAIN enclosure", (parent_offset[0], 0, 50.15), (210, 230, 100.3), pale)
    parent_layout = json.loads((root / geometry["parent"]["interior_assembly"] / "placement.json").read_text(encoding="utf-8"))
    for source_label, name, shade, file_name in (
        ("plate", "P_MAIN interior plate (design)", deck, "parent_plate_p1.stl"),
        ("p_main", "P_MAIN PCB (design)", board, "parent_main.stl"),
        ("spresense", "Spresense (CAD orientation)", logger, "spresense_logger.stl"),
    ):
        item = next(item for item in parent_layout["items"] if item["label"] == source_label)
        relative = f"{item['part']}/{file_name}"
        load_part(root, relative, name, shade, item["ops"],
                  (parent_offset[0] - 105, -115, 0))
    parent_mount = (parent_offset[0] - 48, 0, -17)
    marker("P camera mount face center, not optical center", parent_mount, red)
    label("P camera mount", (parent_mount[0] - 55, 0, -28), white, 10)
    label("P_MAIN", (parent_offset[0] - 82, -58, 63), white, 12)
    label("Spresense", (parent_offset[0] + 32, -62, 63), white, 10)
    # The placeholder SD notch points roughly +Y in this CAD orientation.
    line("CAD SD direction (provisional)",
         [(parent_offset[0] + 56, -63, 57), (parent_offset[0] + 56, -24, 57)], red, 1.6)
    label("CAD SD?", (parent_offset[0] + 55, -20, 58), red, 8)
    parent_objects = set(bpy.data.objects)

    for part, name in (("front", "C_MAIN front frame"), ("rear", "C_MAIN rear frame"),
                       ("guard", "C_MAIN lower guard")):
        load_part(root, f"parts/child/exterior/printed/child_simple_{part}/child_simple_{part}.stl",
                  name, body, offset=child_offset)
    child_layout = json.loads((root / geometry["child"]["interior_assembly"] / "placement.json").read_text(encoding="utf-8"))
    item = next(item for item in child_layout["items"] if item["label"] == "child_main")
    load_part(root, "parts/child/interior/pcb/child_main/child_main.stl", "C_MAIN PCB (design)",
              board, item["ops"], child_offset)
    marker("C camera bridge mount face center, not optical center",
           (child_offset[0], 82, 100), red)
    label("C camera mount +Y", (child_offset[0] - 70, 87, 127), white, 9)
    label("C_MAIN PCB +Y side", (child_offset[0] - 70, 30, 154), white, 9)
    for z in (0, 200):
        points = [(child_offset[0] + 57.15 * math.cos(i * math.pi / 12),
                   57.15 * math.sin(i * math.pi / 12), z) for i in range(25)]
        line("C_MAIN tube reference", points, pale)
    child_objects = set(bpy.data.objects) - parent_objects

    scene = bpy.context.scene
    scene.render.engine = "BLENDER_WORKBENCH"
    scene.display.shading.color_type = "MATERIAL"
    scene.display.shading.light = "STUDIO"
    scene.display.shading.show_cavity = True
    scene.display.shading.background_type = "WORLD"
    scene.render.resolution_x = 1400
    scene.render.resolution_y = 1100
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.world.color = (0.07, 0.09, 0.12)
    parent_camera = camera((parent_offset[0], 0, 32), 310, "Parent isometric camera")
    child_camera = camera((child_offset[0], 0, 95), 340, "Child isometric camera")
    parent_top = camera_at((parent_offset[0], 0, 500), (parent_offset[0], 0, 0),
                           330, "Parent top camera")
    child_front = camera_at((child_offset[0], 500, 105),
                            (child_offset[0], 0, 105), 370, "Child cutaway front camera")
    child_side = camera_at((child_offset[0] + 500, 0, 105),
                           (child_offset[0], 0, 105), 370, "Child cutaway side camera")
    for obj in child_objects:
        obj.hide_render = True
    render(output / "parent_isometric.png", parent_camera)
    render(output / "parent_top.png", parent_top)
    for obj in child_objects:
        obj.hide_render = False
    for obj in parent_objects:
        obj.hide_render = True
    render(output / "child_isometric.png", child_camera)
    hidden = [obj for obj in bpy.data.objects if obj.name in
              {"C_MAIN front frame", "C_MAIN rear frame", "C_MAIN lower guard"}]
    for obj in hidden:
        obj.hide_render = True
    render(output / "child_interior_front.png", child_front)
    render(output / "child_interior_side.png", child_side)
    for obj in hidden:
        obj.hide_render = False
    for obj in parent_objects:
        obj.hide_render = False
    scene.camera = parent_camera
    bpy.context.preferences.filepaths.save_version = 0
    bpy.ops.wm.save_as_mainfile(filepath=str(output / "body_geometry_review.blend"))
    manifest = {
        "experiment_id": experiment,
        "status": "visual_review_pending",
        "scene_units": "millimeters represented as Blender units",
        "source_calibration": str(reference.relative_to(repo)),
        "source_calibration_sha256": hashlib.sha256(reference.read_bytes()).hexdigest(),
        "source_mesh_sha256": {
            str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(SOURCE_MESHES)
        },
        "git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip(),
        "review": ["Check C-MAIN PCB is on the +Y camera side", "Check P-MAIN and Spresense occupy the same enclosure side at opposite corners", "Check physical Spresense SD opening faces outward; CAD SD marker is provisional", "Check camera mount positions; spheres are mount centers, not optical centers"],
    }
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "report.md").write_text(
        "# 実験機体の3D位置確認\n\n"
        "状態: 実機との目視照合待ち。モデル座標を実測値として解析にはまだ適用していない。\n\n"
        "- [Blender 3Dシーン](body_geometry_review.blend): 親機と子機を500 mm離して配置。"
        "単位はBlender上の1単位=1 mm。オブジェクトごとに表示を切り替えられる。\n"
        "- 親機: [等角](parent_isometric.png)、[上面](parent_top.png)。緑=P-MAIN、紫=Spresense、"
        "橙=底板・内装板、灰色線=箱外形、赤=モデルの暫定SD向きとカメラ取付面。\n"
        "- 子機: [等角](child_isometric.png)、[正面断面](child_interior_front.png)、"
        "[側面断面](child_interior_side.png)。青緑=外装、緑=C-MAIN基板、"
        "赤=カメラブリッジ取付面。断面図では外装3部品を一時的に非表示。\n\n"
        "確認点: (1) C-MAIN基板が+Yのカメラ取付面側か、(2) P-MAINとSpresenseが"
        "箱の同じ側・反対の角か、(3) Spresenseの実際のSD口が外向きか、"
        "(4) カメラ取付面の位置が実機と一致するか。モデルのSpresense SD形状は仮位置。"
        "赤い球は取付面の中心で、光学中心ではない。\n\n"
        "原典ハッシュ、校正ファイルのハッシュ、Git SHAは [manifest.json](manifest.json) を参照。\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
