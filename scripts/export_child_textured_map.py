"""Project child camera color images onto locally triangulated sparse SLAM points."""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
from pathlib import Path

import cv2
import numpy as np
import trimesh
from PIL import Image
from render_child_vslam_artifacts import read_ply_xyz
from scipy.spatial import Delaunay
from scipy.spatial.transform import Rotation


def projected_surface(points, position, quaternion, matrix, width=1280, height=720):
    """Return visible world vertices, UVs and conservative image-space triangles.

    Pose is T_world_camera; OpenCV camera axes are right/down/forward.
    UVs use GL's bottom-left origin. No vertices are invented between map points.
    """
    camera = (points - position) @ Rotation.from_quat(quaternion).as_matrix()
    front = camera[:, 2] > 1e-6
    camera, world = camera[front], points[front]
    pixel = camera @ matrix.T
    pixel = pixel[:, :2] / pixel[:, 2:3]
    valid = ((pixel[:, 0] >= 0) & (pixel[:, 0] < width) &
             (pixel[:, 1] >= 0) & (pixel[:, 1] < height))
    pixel, camera, world = pixel[valid], camera[valid], world[valid]
    # Avoid duplicate projected points, which make Delaunay degenerate.
    _, unique = np.unique(np.round(pixel, 2), axis=0, return_index=True)
    pixel, camera, world = pixel[unique], camera[unique], world[unique]
    if len(world) < 4:
        return world, np.empty((len(world), 2)), np.empty((0, 3), dtype=int)
    faces = Delaunay(pixel).simplices
    corners = pixel[faces]
    longest = np.max(np.linalg.norm(corners - np.roll(corners, 1, axis=1), axis=2), axis=1)
    depth = camera[faces, 2]
    # Conservative bridges: <=80 image px and <=20% depth spread.
    world_edges = np.linalg.norm(world[faces] - np.roll(world[faces], 1, axis=1), axis=2)
    robust_radius = np.percentile(np.linalg.norm(points - np.median(points, axis=0), axis=1), 90)
    keep = ((longest <= 80) & (np.max(depth, axis=1) / np.min(depth, axis=1) <= 1.2) &
            (np.max(world_edges, axis=1) <= robust_radius * 0.08))
    faces = faces[keep]
    uv = np.column_stack((pixel[:, 0] / width, 1 - pixel[:, 1] / height))
    return world, uv, faces


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def export(root: Path, experiment: str, sample_count: int = 24):
    output = root / 'results' / experiment / 'vislam_depth'
    run = output / 'calibrated_mono_color_120fps_full'
    dataset = root / 'data/interim/orbslam3_color_120fps' / experiment
    with (run / 'MapPoints_manifest.csv').open(encoding='utf-8') as stream:
        maps = list(csv.DictReader(stream))
    largest = max(maps, key=lambda row: int(row['keyframes']))
    points = read_ply_xyz(run / largest['ply_file'])
    with (run / 'camera_trajectory_status.csv').open(encoding='utf-8') as stream:
        rows = [row for row in csv.DictReader(stream) if row['pose_status'] == 'saved']
    selected = np.unique(np.linspace(0, len(rows) - 1, min(sample_count, len(rows))).astype(int))
    calibration_path = root / 'calibration/parent_camera_underwater_20260924_provisional.json'
    calibration = json.loads(calibration_path.read_text(encoding='utf-8'))
    matrix = np.array(calibration['proxy_direct_camera_matrix_px'])
    distortion = np.array(calibration['proxy_direct_distortion_coefficients'])
    center = np.median(points, axis=0)
    radius = float(np.percentile(np.linalg.norm(points - center, axis=1), 95))
    central = points[np.linalg.norm(points - center, axis=1) <= radius]
    basis = np.linalg.svd(central - center, full_matrices=False)[2].T
    scene = trimesh.Scene()
    plane_scene = trimesh.Scene()
    payload = {'center': center.tolist(), 'radius': radius, 'map_center': center.tolist(),
               'map_radius': float(np.percentile(np.linalg.norm(points-center, axis=1), 80)),
               'surfaces': [],
               'basis': basis.tolist(),
               'points': points[::max(1, len(points)//12000)].reshape(-1).tolist()}
    observations = []
    for number, index in enumerate(selected):
        row = rows[index]
        position = np.array([float(row[k]) for k in ('x_slam', 'y_slam', 'z_slam')])
        quaternion = [float(row[k]) for k in ('qx', 'qy', 'qz', 'qw')]
        vertices, uv, faces = projected_surface(points, position, quaternion, matrix)
        if len(faces) == 0:
            continue
        source = dataset / 'mav0/cam0/data' / (row['utc_ns'] + '.jpg')
        frame = cv2.imread(str(source))
        if frame is None:
            raise FileNotFoundError(source)
        frame = cv2.undistort(frame, matrix, distortion)
        texture = Image.fromarray(cv2.cvtColor(cv2.resize(frame, (640, 360)), cv2.COLOR_BGR2RGB))
        mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
        mesh.visual = trimesh.visual.texture.TextureVisuals(
            uv=uv, material=trimesh.visual.material.PBRMaterial(
                baseColorTexture=texture, metallicFactor=0, roughnessFactor=1,
                doubleSided=True))
        scene.add_geometry(mesh, node_name=f'camera_{number}_{row["utc_ns"]}')
        rotation = Rotation.from_quat(quaternion).as_matrix()
        if number == 0:
            payload['basis'] = (rotation @ np.diag([1, -1, -1])).tolist()
        depth = float(np.median(((vertices - position) @ rotation)[:, 2]))
        corners = np.array([[0, 0, 1], [1280, 0, 1], [1280, 720, 1], [0, 720, 1]])
        plane_vertices = (corners @ np.linalg.inv(matrix).T * depth) @ rotation.T + position
        plane_uv = np.array([[0, 1], [1, 1], [1, 0], [0, 0]])
        plane_faces = np.array([[0, 1, 2], [0, 2, 3]])
        plane_mesh = trimesh.Trimesh(vertices=plane_vertices, faces=plane_faces, process=False)
        plane_mesh.visual = trimesh.visual.texture.TextureVisuals(
            uv=plane_uv, material=mesh.visual.material)
        plane_scene.add_geometry(plane_mesh, node_name=f'image_plane_{number}')
        buffer = io.BytesIO()
        texture.save(buffer, format='JPEG', quality=90)
        payload['surfaces'].append({'xyz': vertices[faces].reshape(-1).tolist(),
                                    'uv': uv[faces].reshape(-1).tolist(),
                                    'image': base64.b64encode(buffer.getvalue()).decode(),
                                    'plane_xyz': plane_vertices[plane_faces].reshape(-1).tolist(),
                                    'plane_uv': plane_uv[plane_faces].reshape(-1).tolist(),
                                    'time': row['utc_ns']})
        observations.append({'utc_ns': row['utc_ns'], 'triangles': len(faces),
                             'source_image_sha256': digest(source)})
    if not observations:
        raise ValueError('no surface triangles could be textured')
    plane_points = np.concatenate([np.array(s['plane_xyz']).reshape(-1, 3)
                                   for s in payload['surfaces']])
    plane_center = np.median(plane_points, axis=0)
    payload['center'] = plane_center.tolist()
    payload['radius'] = float(np.percentile(np.linalg.norm(
        plane_points - plane_center, axis=1), 95))
    glb = output / 'textured_map_color_120fps.glb'
    glb.write_bytes(scene.export(file_type='glb'))
    plane_glb = output / 'image_planes_color_120fps.glb'
    plane_glb.write_bytes(plane_scene.export(file_type='glb'))
    html = output / 'textured_map_color_120fps.html'
    template = (root / 'scripts/templates/textured_map_viewer.html').read_text(encoding='utf-8')
    html.write_text(template.replace('__DATA__', json.dumps(payload, separators=(',', ':')))
                    .replace('__EXPERIMENT__', experiment), encoding='utf-8')
    manifest = {'experiment_id': experiment, 'run_name': run.name, 'map_id': largest['map_id'],
                'method': 'image-space Delaunay of visible existing MapPoints; no new 3D vertices',
                'visibility_definition': 'positive camera depth and calibrated image bounds',
                'occlusion_check_performed': False,
                'pose_convention': 'T_world_camera, quaternion xyzw; camera x right, y down, z forward',
                'units': 'arbitrary monocular SLAM units', 'max_projected_edge_px': 80,
                'max_triangle_depth_ratio': 1.2, 'sampled_saved_poses': len(selected),
                'max_world_edge_fraction_of_90percent_map_radius': 0.08,
                'html_initial_view': 'first sampled camera right/up/back axes; GLB retains SLAM world axes',
                'image_plane_depth': 'median positive depth of visible map vertices for each camera',
                'textured_observations': observations,
                'calibration_sha256': digest(calibration_path),
                'trajectory_sha256': digest(run / 'camera_trajectory_status.csv'),
                'map_sha256': digest(run / largest['ply_file']),
                'run_manifest_sha256': digest(run / 'run_manifest.json'),
                'git_commit': json.loads((run / 'run_manifest.json').read_text(
                    encoding='utf-8'))['git_commit'],
                'export_script_sha256': digest(Path(__file__)),
                'viewer_template_sha256': digest(root / 'scripts/templates/textured_map_viewer.html'),
                'artifacts_sha256': {p.name: digest(p) for p in (html, glb, plane_glb)}}
    (output / 'textured_map_manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    report = output / 'report.md'
    heading = '## カラー画像を貼り付けた3Dマップ'
    text = report.read_text(encoding='utf-8')
    if heading not in text:
        text += (f'\n{heading}\n\n'
                 f'最大Atlas地図の保存姿勢から{len(observations)}画像を選び、歪み補正したカラー画像を'
                 '同じ地図の可視MapPointを結ぶ三角形へ投影した。親機観測は使っていない。\n\n'
                 '- [操作可能なテクスチャ付き3D表示](textured_map_color_120fps.html)\n'
                 '- [画像埋め込みの3DファイルGLB](textured_map_color_120fps.glb)\n'
                 '- [入力・方式・成果物ハッシュ](textured_map_manifest.json)\n\n'
                 'これは疎点群からの局所的な画像投影であり、密な表面復元や閉じたメッシュではない。'
                 '点が少ない区間、画像上80 px以上の辺、深度差20%超の三角形は貼り付けない。'
                 '全画像を同時に表示すると重なりが生じるため、HTMLでは画像を切り替えられる。'
                 '姿勢が保存されない区間や別Atlas地図は合成していない。尺度は未確定のSLAM単位。\n')
        report.write_text(text, encoding='utf-8')
    text = report.read_text(encoding='utf-8')
    if 'image_planes_color_120fps.glb' not in text:
        text += ('\n### 画像全体を3D平面として配置した版\n\n'
                 '- [画像平面のテクスチャ付きGLB](image_planes_color_120fps.glb)\n\n'
                 'HTMLの「画像平面」が有効な場合、各カメラの推定姿勢と可視点の中央値深度に'
                 'カラー画像全体を平面として配置する。平面は表面形状の推定結果ではなく、'
                 '撮影方向と画像の位置関係を確認するための表示。チェックを外すと疎点群表面への投影に切り替わる。'
                 '表面版では画像上の辺と深度差の制限に加え、地図点の90%半径の8%を超える3D辺も除外する。\n')
        report.write_text(text, encoding='utf-8')
    text = report.read_text(encoding='utf-8')
    if '遮蔽判定' not in text:
        text += ('\n投影対象はカメラ前方かつ画角内のMapPoint。遮蔽判定は未実施のため、'
                 '背後の点に画像が投影される場合がある。表面版を精密なテクスチャ復元として評価しない。\n')
        report.write_text(text, encoding='utf-8')
    print(experiment, len(observations), 'images', sum(x['triangles'] for x in observations), 'triangles')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('experiment_id')
    args = parser.parse_args()
    export(Path(__file__).resolve().parents[1], args.experiment_id)
