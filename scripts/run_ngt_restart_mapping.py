"""Use every child SLAM frame for bounded, independently restarted position-aided mapping."""
from __future__ import annotations

import argparse
import csv
import gc
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

os.environ.setdefault('OPENBLAS_NUM_THREADS', '2')
os.environ.setdefault('OMP_NUM_THREADS', '2')
import cv2
import numpy as np

from aquabeacon_analysis.camera_calibration import load_camera_intrinsics
from aquabeacon_analysis.ngt_binary import sha256
from aquabeacon_analysis.ngt_position import bracket_interpolate
from aquabeacon_analysis.restart_mapping import (
    position_registration,
    prior_anchor_indices,
    temporal_windows,
)

ROOT = Path(__file__).resolve().parents[1]
EXPERIMENT = '20261028_ngt_004'
SOURCE = ROOT / f'data/interim/{EXPERIMENT}/child_only_vslam/okinawa_native120'
WORK = ROOT / f'data/interim/{EXPERIMENT}/wall_mapping/native120_restart'
SOURCE_IMAGES = SOURCE / 'mav0/cam0/data'
IMAGES = WORK / 'wall_images'
DATABASE = WORK / 'features_crop.db'
OUT = ROOT / f'results/{EXPERIMENT}/wall_mapping/native120_restart'
CONFIG_PATH = ROOT / 'config/20261028_ngt/restart_mapping_native120.json'
CONFIG = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
sys.path.insert(0, str(ROOT / f'data/interim/{EXPERIMENT}/wall_mapping/runtime'))
import pycolmap as colmap


def read_csv(path):
    with path.open(encoding='utf-8-sig', newline='') as handle:
        return list(csv.DictReader(handle))


def write_csv(path, rows):
    if not rows:
        return
    with path.open('w', encoding='utf-8', newline='') as handle:
        writer = csv.DictWriter(handle, list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')


def prepare():
    source = read_csv(SOURCE / 'frames.csv')
    reference = json.loads((ROOT / f'results/{EXPERIMENT}/child_only_vslam/summary.json').read_text())
    assert len(source) == reference['input_frames'] == 33564
    sync_path = ROOT / 'results/20261028_ngt_000/synchronization/manifest.json'
    sync = json.loads(sync_path.read_text())['cameras']
    parent_times = np.array([(sync['child']['utc_at_video_zero_s']
        + sync['child']['time_scale'] * float(row['source_video_s'])
        - sync['parent']['utc_at_video_zero_s']) / sync['parent']['time_scale'] for row in source])
    position_path = ROOT / f'results/{EXPERIMENT}/position_evaluation/positions_5hz.csv'
    positions = read_csv(position_path)
    times = np.array([float(row['parent_video_s']) for row in positions])
    values = np.array([[float(row['child_east_declination0_m']),
        float(row['child_north_declination0_m']), -float(row['pressure_depth_m'])]
        if row['magnetic_position_available'] == 'True' else [np.nan] * 3 for row in positions])
    priors = bracket_interpolate(times, values, parent_times, CONFIG['prior_max_bracket_s'])
    rows = []
    IMAGES.mkdir(exist_ok=True)
    x1, y1, x2, y2 = CONFIG['crop_xyxy']
    for i, (row, pt, prior) in enumerate(zip(source, parent_times, priors, strict=True)):
        source_name = row['synthetic_ns'] + '.jpg'
        source_path = SOURCE_IMAGES / source_name
        if sha256(source_path) != row['image_sha256']:
            raise ValueError(f'Source frame hash differs: {source_path}')
        name = row['synthetic_ns'] + '.png'
        frame = cv2.imread(str(source_path))
        if frame is None or frame.shape[:2] != (720, 1280):
            raise ValueError(f'Unexpected source image: {source_path}')
        cv2.imwrite(str(IMAGES / name), frame[y1:y2, x1:x2], [cv2.IMWRITE_PNG_COMPRESSION, 1])
        rows.append({'index': i, 'image_name': name, 'source_frame': row['source_frame'],
            'child_video_s': row['source_video_s'], 'parent_video_s': pt,
            'source_image_name': source_name, 'image_sha256': row['image_sha256'],
            'crop_sha256': sha256(IMAGES / name), 'prior_available': bool(np.isfinite(prior).all()),
            'east_m': prior[0], 'north_m': prior[1], 'up_m': prior[2]})
        if i % 2000 == 0:
            print(f'Prepared and source-hash verified {i+1}/{len(source)} frames', flush=True)
    write_csv(WORK / 'frame_priors.csv', rows)
    k, d, provenance = load_camera_intrinsics(ROOT, 'C_MAIN_CAM', (1280, 720), 'main')
    k[0, 2] -= x1
    k[1, 2] -= y1
    mask = np.zeros((y2-y1, x2-x1), np.uint8)
    rx1, ry1, rx2, ry2 = CONFIG['wall_roi_xyxy']
    mask[ry1-y1:ry2-y1, rx1-x1:rx2-x1] = 255
    cv2.imwrite(str(WORK / 'wall_mask_crop.png'), mask)
    camera = colmap.Camera(model='OPENCV', width=x2-x1, height=y2-y1,
        params=[k[0, 0], k[1, 1], k[0, 2], k[1, 2], *d[:4]])
    if camera.has_bogus_params(.1, 10, 1):
        raise ValueError('Crop intrinsics rejected by COLMAP; retain optical principal point')
    paths = [SOURCE / 'frames.csv', SOURCE / 'export_manifest.json', sync_path, position_path,
             CONFIG_PATH, Path(__file__), ROOT / 'src/aquabeacon_analysis/restart_mapping.py',
             Path(provenance['source']), ROOT / 'calibration/camera_defaults.json']
    manifest = {'experiment_id': EXPERIMENT, 'created_utc': datetime.now(UTC).isoformat(),
        'git_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        'config': CONFIG, 'frame_count': len(rows), 'baseline_input_frames': reference['input_frames'],
        'prior_count': int(np.isfinite(priors).all(axis=1).sum()),
        'parent_interval_s': [float(parent_times[0]), float(parent_times[-1])],
        'nominal_fps': 120000/1001, 'camera_matrix': k.tolist(), 'distortion': d.tolist(),
        'intrinsic_provenance': provenance, 'pycolmap': colmap.__version__,
        'crop_camera_matrix': k.tolist(), 'crop_size': [x2-x1, y2-y1],
        'crop_operation': 'Principal point translated by (-640,-36); focal length and distortion unchanged',
        'inputs_sha256': {str(p.relative_to(ROOT)): sha256(p) for p in paths},
        'input_images': str(IMAGES.relative_to(ROOT)), 'raw_modified': False,
        'prior_independence': 'Temporally interpolated 5Hz estimates; not 33564 independent fixes',
        'prior_validity': 'Conditionally estimated position, not exact surveyed camera center',
        'all_frames_wall_visible': False}
    write_json(OUT / 'manifest.json', manifest)
    shutil.copy2(CONFIG_PATH, OUT / 'config.json')
    shutil.copy2(__file__, OUT / 'generate.py')
    print(json.dumps({k: manifest[k] for k in ['frame_count', 'prior_count', 'parent_interval_s']}), flush=True)


def with_deadline(operation, seconds):
    token = colmap.CancellationToken()
    timer = threading.Timer(seconds, token.cancel)
    timer.daemon = True
    timer.start()
    try:
        result = operation(token)
        if token.is_cancelled:
            raise TimeoutError(f'Phase deadline {seconds}s reached; intermediate files retained')
        return result
    finally:
        timer.cancel()


def features():
    manifest = json.loads((OUT / 'manifest.json').read_text(encoding='utf-8'))
    k, d = np.array(manifest['camera_matrix']), manifest['distortion']
    reader = colmap.ImageReaderOptions(camera_model='OPENCV',
        camera_params=','.join(map(str, [k[0, 0], k[1, 1], k[0, 2], k[1, 2], *d[:4]])),
        camera_mask_path=str(WORK / 'wall_mask_crop.png'))
    options = colmap.FeatureExtractionOptions(num_threads=CONFIG['num_threads'], use_gpu=False,
        max_image_size=CONFIG['feature_max_image_size'])
    options.sift.max_num_features = CONFIG['feature_max_count']
    options.sift.peak_threshold = CONFIG['feature_peak_threshold']
    options.sift.first_octave = CONFIG['feature_first_octave']
    with_deadline(lambda token: colmap.extract_features(DATABASE, IMAGES,
        camera_mode=colmap.CameraMode.SINGLE, reader_options=reader,
        extraction_options=options, device=colmap.Device.cpu, cancellation_token=token),
        CONFIG['phase_timeout_s'])
    rows = read_csv(WORK / 'frame_priors.csv')
    with colmap.Database.open(DATABASE) as db:
        lookup = {im.name: im.image_id for im in db.read_all_images()}
        for row in rows:
            row['image_id'] = lookup.get(row['image_name'], -1)
            row['features'] = db.num_keypoints_for_image(row['image_id']) if row['image_id'] >= 0 else -1
        assert len(lookup) == len(rows)
    write_csv(WORK / 'frame_features.csv', rows)
    print(json.dumps({'processed_images': len(lookup),
        'feature_eligible_images': sum(r['features'] >= CONFIG['pair_min_features'] for r in rows)}), flush=True)


def matching():
    rows = read_csv(WORK / 'frame_features.csv')
    anchors = set(prior_anchor_indices([float(r['parent_video_s']) for r in rows],
        [r['prior_available'] == 'True' for r in rows], 1 / CONFIG['optimizer_prior_hz']))
    for i, row in enumerate(rows):
        row['optimizer_prior'] = i in anchors
    write_csv(WORK / 'optimizer_frame_priors.csv', rows)
    eligible = np.array([int(r['features']) >= CONFIG['pair_min_features'] for r in rows])
    count = 0
    with (WORK / 'pairs.txt').open('w', encoding='utf-8') as handle:
        for offset in CONFIG['pair_frame_offsets']:
            for i in np.flatnonzero(eligible[:-offset] & eligible[offset:]):
                handle.write(f"{rows[i]['image_name']} {rows[i+offset]['image_name']}\n")
                count += 1
    write_json(OUT / 'pairing.json', {'pairs': count, 'all_images': len(rows),
        'eligible_images': int(eligible.sum()), 'offsets': CONFIG['pair_frame_offsets'],
        'exclusion': 'Only matching pairs with <25 SIFT features omitted; every frame accounted for'})
    print(json.dumps({'pairs': count, 'eligible_images': int(eligible.sum())}), flush=True)
    geometry = colmap.TwoViewGeometryOptions()
    geometry.ransac.max_num_trials = 3000
    geometry.ransac.random_seed = 7
    with_deadline(lambda token: colmap.match_image_pairs(DATABASE,
        matching_options=colmap.FeatureMatchingOptions(num_threads=CONFIG['num_threads'], use_gpu=False),
        pairing_options=colmap.ImportedPairingOptions(match_list_path=str(WORK / 'pairs.txt'), block_size=300),
        verification_options=geometry, device=colmap.Device.cpu, cancellation_token=token),
        CONFIG['phase_timeout_s'])
    # Priors are added once to this run-owned database. The source images/databases are read-only.
    lookup = {r['image_name']: r for r in rows}
    with colmap.Database.open(DATABASE) as db:
        if not db.read_all_pose_priors():
            for im in db.read_all_images():
                row = lookup[im.name]
                if not row['optimizer_prior']:
                    continue
                prior = colmap.PosePrior()
                prior.corr_data_id = im.data_id
                prior.position = [float(row[k]) for k in ['east_m', 'north_m', 'up_m']]
                prior.position_covariance = np.diag(CONFIG['prior_sigma_m'])**2
                prior.coordinate_system = colmap.PosePriorCoordinateSystem.CARTESIAN
                db.write_pose_prior(prior)
        write_json(OUT / 'database_summary.json', {'images': db.num_images(),
            'verified_pairs': db.num_verified_image_pairs(), 'priors': db.num_pose_priors()})


def export_model(model, folder, lookup):
    folder.mkdir(parents=True, exist_ok=True)
    model.write(folder)
    model.export_PLY(folder / 'points_local.ply')
    images = sorted([im for im in model.images.values() if im.has_pose], key=lambda im: im.name)
    paired = [im for im in images if lookup[im.name]['optimizer_prior'] == 'True']
    source = np.array([im.projection_center() for im in paired]).reshape(-1, 3)
    target = np.array([[float(lookup[im.name][k]) for k in ['east_m', 'north_m', 'up_m']]
                       for im in paired]).reshape(-1, 3)
    fit = position_registration(source, target, CONFIG['position_fit_min_second_first_ratio'],
                                CONFIG['position_fit_huber_delta_m'])
    fit['anchor_image_names'] = [im.name for im in paired]
    structural = model.num_points3D() >= CONFIG['min_structural_points']
    fit['structural'] = structural
    if fit['accepted'] and structural:
        model.transform(colmap.Sim3d(fit['scale'], colmap.Rotation3d(np.array(fit['rotation'])),
                                    np.array(fit['translation'])))
        aligned = folder / 'position_registered'
        aligned.mkdir(exist_ok=True)
        model.write(aligned)
        model.write_text(aligned)
        model.export_PLY(aligned / 'points.ply')
    write_json(folder / 'position_registration.json', fit)
    cameras = [{'image_name': im.name, 'parent_video_s': lookup[im.name]['parent_video_s'],
        'x': im.projection_center()[0], 'y': im.projection_center()[1], 'z': im.projection_center()[2],
        'prior_available': lookup[im.name]['prior_available']} for im in images]
    write_csv(folder / 'camera_centers.csv', cameras)
    return {'registered_images': model.num_reg_images(), 'points': model.num_points3D(),
        'reprojection_px': model.compute_mean_reprojection_error(), 'structural': structural,
        'position_registered': bool(fit['accepted'] and structural), 'position_fit': fit,
        'first_parent_s': min(float(r['parent_video_s']) for r in cameras),
        'last_parent_s': max(float(r['parent_video_s']) for r in cameras),
        'folder': str(folder.relative_to(ROOT))}


def reconstruct():
    rows = read_csv(WORK / 'optimizer_frame_priors.csv')
    lookup = {r['image_name']: r for r in rows}
    times = np.array([float(r['parent_video_s']) for r in rows])
    windows = temporal_windows(times, CONFIG['window_width_s'], CONFIG['window_stride_s'])
    # Read-only SQLite counts avoid importing every descriptor/match for screening empty windows.
    with sqlite3.connect(f"file:{DATABASE.as_posix()}?mode=ro", uri=True) as db:
        edge_ids = np.array([r[0] for r in db.execute('SELECT pair_id FROM two_view_geometries WHERE rows >= 30')], dtype=np.int64)
    edge_a, edge_b = edge_ids // 2147483647, edge_ids % 2147483647
    for number, indices in enumerate(windows):
        destination = OUT / f'window_{number:02d}'
        destination.mkdir(parents=True, exist_ok=True)
        result_path = destination / 'summary.json'
        if result_path.exists():
            print(f'Window {number} already recorded; preserving result', flush=True)
            continue
        selected = [rows[i] for i in indices]
        ids = [int(row['image_id']) for row in selected]
        edge_count = int((np.isin(edge_a, ids) & np.isin(edge_b, ids)).sum())
        result = {'window': number, 'input_frames': len(selected), 'first_index': int(indices[0]),
            'last_index': int(indices[-1]), 'first_parent_s': float(times[indices[0]]),
            'last_parent_s': float(times[indices[-1]]), 'verified_pairs_ge30': edge_count,
            'prior_frames': sum(r['prior_available'] == 'True' for r in selected),
            'models': [], 'status': 'insufficient_verified_pairs'}
        start = time.monotonic()
        print(json.dumps({'starting_window': number, 'verified_pairs': edge_count,
                          'interval': [result['first_parent_s'], result['last_parent_s']]}), flush=True)
        if edge_count >= 10:
            options = colmap.IncrementalPipelineOptions(num_threads=CONFIG['num_threads'], random_seed=7,
                image_names=[r['image_name'] for r in selected],
                max_num_models=CONFIG['mapper_max_models_per_window'],
                min_model_size=CONFIG['mapper_min_model_size'], init_num_trials=100,
                ba_refine_focal_length=False, ba_refine_principal_point=False,
                ba_refine_extra_params=False, ba_local_max_num_iterations=15,
                ba_global_max_num_iterations=25, ba_global_max_refinements=2,
                ba_min_num_residuals_for_cpu_multi_threading=1,
                max_runtime_seconds=CONFIG['mapper_max_seconds_per_window'],
                use_prior_position=True, use_robust_loss_on_prior_position=True)
            options.mapper.num_threads = CONFIG['num_threads']
            options.mapper.init_min_num_inliers = CONFIG['mapper_min_inliers']
            options.mapper.init_min_tri_angle = CONFIG['mapper_init_min_triangle_deg']
            options.mapper.abs_pose_min_num_inliers = 20
            options.mapper.abs_pose_refine_focal_length = False
            options.mapper.abs_pose_refine_extra_params = False
            options.triangulation.min_angle = 1.
            options.mapper.filter_min_tri_angle = 1.
            model_root = WORK / f'window_{number:02d}'
            model_root.mkdir(exist_ok=True)
            models = colmap.incremental_mapping(DATABASE, IMAGES, model_root, options=options)
            result['status'] = 'models_returned' if models else 'no_model'
            for model_number, model in models.items():
                result['models'].append({'model': int(model_number), **export_model(
                    model, model_root / str(model_number), lookup)})
            del models
            gc.collect()
        result['elapsed_s'] = time.monotonic() - start
        result['runtime_limit_s'] = CONFIG['mapper_max_seconds_per_window']
        result['time_limit_may_have_truncated'] = result['elapsed_s'] >= result['runtime_limit_s']
        write_json(result_path, result)
        print(json.dumps(result, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment', required=True, choices=[EXPERIMENT])
    parser.add_argument('--phase', required=True, choices=['prepare', 'features', 'matching', 'map'])
    args = parser.parse_args()
    WORK.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    cv2.setNumThreads(2)
    record = {'pid': os.getpid(), 'started_utc': datetime.now(UTC).isoformat(),
        'owner': 'native120_restart_mapping', 'phase': args.phase, 'purpose': __doc__,
        'command': sys.argv, 'threads': CONFIG['num_threads'], 'config_sha256': sha256(CONFIG_PATH),
        'generator_sha256': sha256(Path(__file__))}
    write_json(OUT / f'{args.phase}.process.json', record)
    try:
        {'prepare': prepare, 'features': features, 'matching': matching, 'map': reconstruct}[args.phase]()
        record['status'] = 'completed'
    except BaseException as error:
        record['status'] = 'failed'
        record['error'] = str(error)
        raise
    finally:
        record['ended_utc'] = datetime.now(UTC).isoformat()
        write_json(OUT / f'{args.phase}.process.json', record)


if __name__ == '__main__':
    main()
