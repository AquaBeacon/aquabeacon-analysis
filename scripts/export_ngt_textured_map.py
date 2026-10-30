"""Export child-only Nagata sparse photo surfaces and an offline WebGL viewer."""
from __future__ import annotations

import os

os.environ.setdefault('OMP_NUM_THREADS', '2')
os.environ.setdefault('OPENBLAS_NUM_THREADS', '1')

import argparse
import base64
import csv
import hashlib
import io
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import cv2
import numpy as np
import trimesh
from PIL import Image
from scipy.spatial.transform import Rotation

from aquabeacon_analysis.ngt_textured_map import (
    path_segments,
    projected_surface,
    scaled_intrinsics,
    source_video_seconds,
)

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')


def read_points(path):
    text = path.read_text(encoding='utf-8')
    return np.array([[float(x) for x in row.split()[:3]]
                     for row in text.split('end_header\n', 1)[1].splitlines() if row])


def export(experiment, sample_count=64):
    started_utc = datetime.now(UTC).isoformat()
    cv2.setNumThreads(2)
    output = ROOT / 'results' / experiment / 'presentation_mapping'
    output.mkdir(parents=True, exist_ok=True)
    (output / 'textures').mkdir(exist_ok=True)
    run = ROOT / 'results' / experiment / 'child_only_vslam/okinawa_native120'
    run_manifest = json.loads((run / 'run_manifest.json').read_text(encoding='utf-8'))
    if run_manifest.get('parent_observations_used') is not False:
        raise ValueError('explicit child-only provenance required')
    dataset = ROOT / run_manifest['dataset']
    defaults_path = ROOT / 'calibration/camera_defaults.json'
    defaults = json.loads(defaults_path.read_text(encoding='utf-8'))
    calibration_path = ROOT / defaults['underwater_intrinsics']['C_MAIN_CAM']
    calibration = json.loads(calibration_path.read_text(encoding='utf-8'))
    matrix = scaled_intrinsics(calibration['camera_matrix_px'], calibration['image_size_px'],
                               run_manifest['export']['resize'])
    distortion = np.asarray(calibration['distortion_coefficients'])
    config_path = ROOT / 'config/20261028_ngt/child_only_monocular.yaml'
    # Refuse to project a different calibration onto the existing reconstruction.
    storage = cv2.FileStorage(str(config_path), cv2.FILE_STORAGE_READ)
    used = [storage.getNode('Camera1.' + name).real() for name in ['fx', 'fy', 'cx', 'cy']]
    storage.release()
    np.testing.assert_allclose(used, [matrix[0, 0], matrix[1, 1], matrix[0, 2], matrix[1, 2]])
    inputs = [run / 'run_manifest.json', run / 'MapPoints_manifest.csv',
              run / 'KeyFrameTrajectory.txt', run / 'CameraTrajectory.txt',
              dataset / 'frames.csv', dataset / 'export_manifest.json', defaults_path,
              calibration_path, config_path,
              ROOT / 'scripts/export_ngt_textured_map.py',
              ROOT / 'scripts/verify_ngt_textured_map.mjs',
              ROOT / 'src/aquabeacon_analysis/ngt_textured_map.py',
              ROOT / 'scripts/templates/ngt_textured_map_viewer.html']
    with (run / 'MapPoints_manifest.csv').open(encoding='utf-8') as stream:
        records = list(csv.DictReader(stream))
    largest = max(records, key=lambda row: int(row['keyframes']))
    with (dataset / 'frames.csv').open(encoding='utf-8') as stream:
        frame_rows = list(csv.DictReader(stream))
    frame_ns = np.array([int(row['synthetic_ns']) for row in frame_rows], dtype=np.int64)
    poses = np.atleast_2d(np.loadtxt(run / 'KeyFrameTrajectory.txt'))
    poses = poses[np.argsort(poses[:, 0])]
    keyframes = []
    for pose in poses:
        index = int(np.argmin(np.abs(frame_ns - int(pose[0]))))
        if abs(int(frame_ns[index]) - int(pose[0])) > 1000:
            raise ValueError('Keyframe image time association exceeds 1000 ns rounding tolerance')
        frame = frame_rows[index]
        keyframes.append({'synthetic_ns': frame['synthetic_ns'],
                          'source_video_s': float(frame['source_video_s']),
                          'source_frame': int(frame['source_frame']),
                          'position': pose[1:4].tolist(), 'quaternion_xyzw': pose[4:8].tolist(),
                          'source_image': (dataset / 'mav0/cam0/data' /
                                           (frame['synthetic_ns'] + '.jpg')).relative_to(ROOT)
                                          .as_posix()})
    maps = []
    for record in records:
        path = run / record['ply_file']
        inputs.append(path)
        points = read_points(path)
        if len(points) != int(record['map_points']):
            raise ValueError('PLY manifest point count mismatch')
        dest = output / f"map_{record['map_id']}_original.ply"
        dest.write_bytes(path.read_bytes())
        maps.append({'map_id': record['map_id'], 'points': points.tolist(),
                     'point_count': len(points), 'keyframe_count': int(record['keyframes']),
                     'coordinate_frame': 'orbslam_map_' + record['map_id'],
                     'poses_available': record == largest,
                     'source_ply': path.relative_to(ROOT).as_posix(),
                     'original_ply': dest.name,
                     'note': '独立地図。別地図との位置・回転・尺度は接続していません。'})
    selected_map = next(m for m in maps if m['map_id'] == largest['map_id'])
    points = np.asarray(selected_map['points'])
    center = np.median(points, axis=0)
    radius = float(np.percentile(np.linalg.norm(points - center, axis=1), 95))
    indices = np.unique(np.linspace(0, len(keyframes) - 1,
                                    min(sample_count, len(keyframes))).astype(int))
    scene = trimesh.Scene()
    surfaces = []
    seen_faces = set()
    color_total, color_count = np.zeros((len(points), 3)), np.zeros(len(points))
    parameters = {'max_edge_px': 80.0, 'max_depth_ratio': 1.15,
                  'max_world_edge_fraction': 0.08, 'visibility_cell_px': 8}
    for number, index in enumerate(indices):
        kf = keyframes[index]
        source = ROOT / kf['source_image']
        frame = cv2.imread(str(source))
        if frame is None:
            raise FileNotFoundError(source)
        height, width = frame.shape[:2]
        if [width, height] != run_manifest['export']['resize']:
            raise ValueError('Unexpected extracted image dimensions')
        undistorted = cv2.undistort(frame, matrix, distortion)
        image = cv2.cvtColor(undistorted, cv2.COLOR_BGR2RGB)
        ids, uv, faces = projected_surface(points, kf['position'], kf['quaternion_xyzw'],
                                           matrix, [width, height], **parameters)
        px = np.clip((uv[:, 0] * width).astype(int), 0, width - 1)
        py = np.clip(((1 - uv[:, 1]) * height).astype(int), 0, height - 1)
        valid_image = cv2.undistort(np.full((height, width), 255, dtype=np.uint8),
                                    matrix, distortion) > 254
        valid_vertex = valid_image[py, px]
        faces = faces[valid_vertex[faces].all(axis=1)]
        color_total[ids[valid_vertex]] += image[py[valid_vertex], px[valid_vertex]]
        color_count[ids[valid_vertex]] += 1
        unique_faces = []
        for face in faces:
            key = tuple(sorted(ids[face].tolist()))
            if key not in seen_faces:
                unique_faces.append(face)
                seen_faces.add(key)
        faces = np.asarray(unique_faces, dtype=int).reshape(-1, 3)
        if not len(faces):
            continue
        texture = Image.fromarray(image).resize((960, 540), Image.Resampling.LANCZOS)
        original_name = f'textures/photo_{number:03d}_source.jpg'
        corrected_name = f'textures/photo_{number:03d}_undistorted.jpg'
        (output / original_name).write_bytes(source.read_bytes())
        texture.save(output / corrected_name, quality=92)
        buffer = io.BytesIO()
        texture.save(buffer, format='JPEG', quality=92)
        used_ids, inverse = np.unique(faces.ravel(), return_inverse=True)
        vertices, mesh_uv = points[ids[used_ids]], uv[used_ids]
        compact_faces = inverse.reshape(-1, 3)
        material = trimesh.visual.material.PBRMaterial(
            baseColorTexture=texture, metallicFactor=0, roughnessFactor=1, doubleSided=True)
        mesh = trimesh.Trimesh(vertices=vertices, faces=compact_faces, process=False)
        mesh.visual = trimesh.visual.texture.TextureVisuals(uv=mesh_uv, material=material)
        scene.add_geometry(mesh, node_name=f'photo_{number:03d}')
        surfaces.append({'source_video_s': kf['source_video_s'], 'keyframe_index': int(index),
                         'source_image': kf['source_image'], 'source_image_sha256': digest(source),
                         'review_source_image': original_name, 'undistorted_image': corrected_name,
                         'triangle_count': len(faces), 'vertices': vertices.tolist(),
                         'faces': compact_faces.tolist(), 'uv': mesh_uv.tolist(),
                         'original_point_indices': ids[used_ids].tolist(),
                         'image': base64.b64encode(buffer.getvalue()).decode('ascii')})
        inputs.append(source)
    if not surfaces:
        raise ValueError('No conservative photo surface could be constructed')
    colored = color_count > 0
    colors = np.tile([70, 160, 185], (len(points), 1))
    colors[colored] = np.round(color_total[colored] / color_count[colored, None]).astype(int)
    trimesh.points.PointCloud(points, colors=colors).export(output / 'colored_points.ply')
    (output / 'textured_map.glb').write_bytes(scene.export(file_type='glb'))
    # OBJ/MTL preserves editable vertices and per-photo UV/material assignments.
    obj_files = trimesh.exchange.obj.export_obj(scene, return_texture=True)
    if isinstance(obj_files, tuple):
        obj_text, assets = obj_files
        (output / 'textured_map.obj').write_text(obj_text, encoding='utf-8')
        for name, content in assets.items():
            (output / name).write_bytes(content if isinstance(content, bytes) else content.encode())
    summary_path = run.parent / 'summary.json'
    summary = json.loads(summary_path.read_text(encoding='utf-8'))
    inputs.append(summary_path)
    record = {'schema_version': 1, 'experiment_id': experiment,
              'coordinate_frame': selected_map['coordinate_frame'],
              'units': 'arbitrary_monocular_slam_units', 'parent_observations_used': False,
              'map_id': selected_map['map_id'], 'mesh_glb': 'textured_map.glb',
              'points': {'xyz': points.tolist(), 'rgb': colors.tolist(),
                         'map_point_id': None, 'first_observed_source_video_s': None,
                         'chronology_method': 'unavailable_no_point_ids_in_ply'},
              'keyframes': keyframes, 'camera_path_segments': path_segments(keyframes),
              'maps': maps, 'center': center.tolist(), 'radius': radius,
              'bounds': [points.min(axis=0).tolist(), points.max(axis=0).tolist()],
              'basis': (Rotation.from_quat(keyframes[len(keyframes)//2]['quaternion_xyzw'])
                        .as_matrix() @ np.diag([1, -1, -1])).tolist(),
              'default_map_index': maps.index(selected_map), 'surfaces': surfaces,
              'time': {'basis': 'source child-video PTS seconds; synthetic timestamp NOT UTC',
                       'input_start_s': source_video_seconds(frame_ns[0]),
                       'input_end_s': source_video_seconds(frame_ns[-1]),
                       'keyframe_start_s': keyframes[0]['source_video_s'],
                       'keyframe_end_s': keyframes[-1]['source_video_s']},
              'coverage': {k: summary[k] for k in ['input_frames', 'tracked_frames',
                                                 'tracked_fraction', 'saved_pose_frames',
                                                 'saved_pose_fraction']},
              'limitations': ['疎点群を結ぶ近似表面です。密な写真測量・閉じた面ではありません。',
                              '任意SLAM単位。実寸・形状の精度は未検証です。',
                              '点ID未保存のため点の生成時刻は不明。点群は静的に表示します。',
                              '画像の時刻は撮影時刻。三角形の生成時刻ではありません。',
                              '8pxセル内の近接深度で遮蔽を近似。密な遮蔽判定ではありません。',
                              '原動画のDewarp/EIS/ケース条件の一致は独立に未検証。']}
    write_json(output / 'scene.json', record)
    template_path = ROOT / 'scripts/templates/ngt_textured_map_viewer.html'
    html = template_path.read_text(encoding='utf-8').replace(
        '__DATA__', json.dumps(record, ensure_ascii=False, separators=(',', ':'), allow_nan=False))
    (output / 'textured_map.html').write_text(html, encoding='utf-8')
    manifest = {'experiment_id': experiment, 'analysis_name': 'presentation_mapping',
                'generated_utc': datetime.now(UTC).isoformat(),
                'process': {'pid': os.getpid(), 'owner': 'export_ngt_textured_map',
                            'started_utc': started_utc, 'ended_utc': datetime.now(UTC).isoformat(),
                            'purpose': 'bounded offline sparse photo mesh export',
                            'opencv_threads': 2, 'background_processes_started': []},
                'git_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT,
                                                       text=True, timeout=10).strip(),
                'reconstruction_git_commit': run_manifest['git_commit'],
                'method': 'Original PLY vertices; image-space Delaunay; conservative edge/depth '
                          'and sparse foreground filters; exact duplicate triangles removed',
                'parameters': parameters, 'sampled_keyframes': len(indices),
                'textured_photos': len(surfaces), 'triangles': len(seen_faces),
                'colored_points': int(colored.sum()), 'original_points': len(points),
                'parent_observations_used': False, 'dense_reconstruction': False,
                'point_chronology_available': False, 'occlusion': 'approximate sparse cells only',
                'undistortion_border': 'vertices outside full-valid source-image mask excluded',
                'pose_convention': 'T_world_camera, xyzw; OpenCV right/down/forward',
                'calibration': {'source': calibration_path.relative_to(ROOT).as_posix(),
                                'image_geometry': 'MP4 full frame 1920x1080 resized 1280x720; no crop',
                                'matrix_px': matrix.tolist(), 'distortion': distortion.tolist(),
                                'texture': 'cv2.undistort with same output K; GL UV bottom-left',
                                'status': calibration['status'], 'conditions': defaults['conditions'],
                                'condition_match_independently_verified': False},
                'inputs_sha256': {p.relative_to(ROOT).as_posix(): digest(p) for p in inputs},
                'artifacts_sha256': {p.relative_to(output).as_posix(): digest(p)
                                     for p in output.rglob('*') if p.is_file()
                                     and p.name not in ['manifest.json', 'report.md']
                                     and not p.name.endswith('verification.json')}}
    write_json(output / 'manifest.json', manifest)
    report = f'''# 子機写真を使う疎な3D表面 — {experiment}

[操作可能な3D表示](textured_map.html) / [テクスチャ埋込みGLB](textured_map.glb) /
[OBJ](textured_map.obj) / [写真色付き点群PLY](colored_points.ply) /
[元の点・姿勢・写真・独立地図](scene.json) / [入力と成果物ハッシュ](manifest.json)

最大独立地図 {selected_map['map_id']} の元の{len(points):,}点を保持し、
{len(surfaces)}枚の実画像を{len(seen_faces):,}個の局所三角形へ投影した。
頂点の補間・平滑化・壁の生成・尺度合わせはしていない。各写真の原画像と歪み補正画像は
textures/ に保存。親機観測は使っていない。5つの地図は別の原点・尺度であり接続しない。

共通沖縄水中校正のMP4用Kを1280×720に2/3縮小し、既存SLAM設定との一致を検証した。
LRF用の行列へ置き換えない。歪み係数は保持する。暫定校正・撮影条件一致の未検証は残る。

全入力{summary['input_frames']:,}フレーム中、追跡状態OKは{summary['tracked_fraction']:.2%}、
最大地図の保存姿勢は{summary['saved_pose_fraction']:.2%}。失敗区間は復元しない。
KeyFrameTrajectoryはMapPointsと同じ元地図座標。CameraTrajectoryは再基準化されるため
このメッシュとの重ね合わせに使わない。時間は子機原動画PTS秒でUTCではない。

## 適用限界

''' + '\n'.join('- ' + item for item in record['limitations']) + '''

三角形の辺は画像80px以下、深度比1.15以下、3D辺は地図90%半径の8%以下。
8px画像セル内で最近点から3%深度以内だけを候補にするが、遮蔽を完全には検証できない。
同じ三頂点の重複は除去するが、別の三頂点で重なる面は残り得る。画像スライダーで確認する。
その他の独立地図は原点を合わせず選択表示する。点のIDがPLYへ保存されておらず、
TrackedFeaturesのIDを点へ対応付ける根拠はないため、点の出現時刻を推測していない。

ブラウザ実画面はローカルfile URLのポリシーにより未確認。HTMLの制御ロジックと
出力メッシュの数値検証を別途記録する。写真の歪み補正画像は実画像として確認する。
'''
    (output / 'report.md').write_text(report, encoding='utf-8')
    print(json.dumps({'output': str(output), 'photos': len(surfaces),
                      'triangles': len(seen_faces), 'points': len(points)}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment', required=True)
    parser.add_argument('--sample-count', type=int, default=64, choices=range(4, 129))
    args = parser.parse_args()
    export(args.experiment, args.sample_count)
