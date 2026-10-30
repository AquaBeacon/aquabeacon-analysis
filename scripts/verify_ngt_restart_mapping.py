"""Independent saved-artifact audit for the all-frame restart mapping run."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import UTC, datetime

import cv2
import numpy as np
from run_ngt_restart_mapping import (
    CONFIG,
    DATABASE,
    EXPERIMENT,
    IMAGES,
    OUT,
    ROOT,
    SOURCE,
    SOURCE_IMAGES,
    colmap,
    read_csv,
    write_json,
)

from aquabeacon_analysis.ngt_binary import sha256
from aquabeacon_analysis.ngt_position import bracket_interpolate, calibrated_rays
from aquabeacon_analysis.restart_mapping import temporal_windows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment', required=True, choices=[EXPERIMENT])
    parser.parse_args()
    cv2.setNumThreads(2)
    manifest = json.loads((OUT / 'manifest.json').read_text(encoding='utf-8'))
    for path, expected in manifest['inputs_sha256'].items():
        assert sha256(ROOT / path) == expected, path
    source = read_csv(SOURCE / 'frames.csv')
    rows = read_csv(OUT / 'frame_evaluation.csv')
    assert len(rows) == len(source) == 33564
    for i, (row, original) in enumerate(zip(rows, source, strict=True)):
        assert int(row['index']) == i
        assert row['source_frame'] == original['source_frame']
        assert row['source_image_name'] == original['synthetic_ns'] + '.jpg'
        assert row['image_sha256'] == original['image_sha256']
        assert int(row['features']) >= 0
        assert int(row['attempted_windows']) >= 1
    positions = read_csv(ROOT / f'results/{EXPERIMENT}/position_evaluation/positions_5hz.csv')
    t = np.array([float(r['parent_video_s']) for r in positions])
    xyz = np.array([[float(r['child_east_declination0_m']), float(r['child_north_declination0_m']),
        -float(r['pressure_depth_m'])] if r['magnetic_position_available'] == 'True' else [np.nan]*3 for r in positions])
    times = np.array([float(r['parent_video_s']) for r in rows])
    expected = bracket_interpolate(t, xyz, times, .25)
    actual = np.array([[float(r[k]) for k in ['east_m', 'north_m', 'up_m']] for r in rows])
    np.testing.assert_allclose(actual, expected, equal_nan=True, atol=1e-10)
    # Recheck frame-to-source pixel equality and ray equivalence at sample boundaries and every window.
    windows = temporal_windows(times, CONFIG['window_width_s'], CONFIG['window_stride_s'])
    indices = sorted({0, len(rows)-1, *[int(ix[0]) for ix in windows], *[int(ix[-1]) for ix in windows]})
    x1, y1, x2, y2 = CONFIG['crop_xyxy']
    full_k = np.array(manifest['intrinsic_provenance']['camera_matrix_px'])
    crop_k = np.array(manifest['crop_camera_matrix'])
    for index in indices:
        row = rows[index]
        assert sha256(SOURCE_IMAGES / row['source_image_name']) == row['image_sha256']
        assert sha256(IMAGES / row['image_name']) == row['crop_sha256']
        original = cv2.imread(str(SOURCE_IMAGES / row['source_image_name']))
        cropped = cv2.imread(str(IMAGES / row['image_name']))
        np.testing.assert_array_equal(cropped, original[y1:y2, x1:x2])
    pixels = np.array([[200., 50.], [400., 400.], [600., 600.]])
    np.testing.assert_allclose(calibrated_rays(pixels, crop_k, manifest['distortion']),
        calibrated_rays(pixels + [x1, y1], full_k, manifest['distortion']), atol=1e-12)
    with colmap.Database.open(DATABASE) as db:
        assert db.num_images() == len(rows)
        assert db.num_pose_priors() == sum(r['optimizer_prior'] == 'True' for r in rows)
        expected_parameters = [crop_k[0, 0], crop_k[1, 1], crop_k[0, 2], crop_k[1, 2], *manifest['distortion'][:4]]
        for camera in db.read_all_cameras():
            np.testing.assert_allclose(camera.params, expected_parameters, atol=1e-10)
            assert (camera.width, camera.height) == (x2-x1, y2-y1)
            assert not camera.has_bogus_params(.1, 10, 1)
    with sqlite3.connect(f'file:{DATABASE.as_posix()}?mode=ro', uri=True) as db:
        assert db.execute('SELECT count(*) FROM keypoints').fetchone()[0] == len(rows)
        assert db.execute('SELECT count(*) FROM descriptors').fetchone()[0] == len(rows)
    camera_errors, point_errors = [], []
    all_positioned, all_structure = set(), set()
    for window in windows:
        assert np.all(np.diff(window) == 1)
    models = json.loads((OUT / 'models.json').read_text(encoding='utf-8'))
    for item in models:
        folder = ROOT / item['folder']
        model = colmap.Reconstruction(folder)
        assert model.num_points3D() == item['points']
        assert model.num_reg_images() == item['registered_images']
        if item['structural']:
            all_structure.update(im.name for im in model.images.values() if im.has_pose)
        if not item['position_registered']:
            continue
        registered = colmap.Reconstruction(folder / 'position_registered')
        fit = item['position_fit']
        rotation, offset = np.array(fit['rotation']), np.array(fit['translation'])
        assert abs(np.linalg.det(rotation) - 1) < 1e-9
        assert fit['accepted'] and fit['correspondences'] >= 6
        by_name = {r['image_name']: r for r in read_csv(folder / 'camera_centers.csv')}
        for image_id, im in model.images.items():
            if not im.has_pose:
                continue
            expect = fit['scale'] * rotation @ im.projection_center() + offset
            got = registered.images[image_id].projection_center()
            camera_errors.append(float(np.linalg.norm(expect-got)))
            saved = np.array([float(by_name[im.name][k]) for k in ['x', 'y', 'z']])
            np.testing.assert_allclose(saved, got, atol=1e-7)
            all_positioned.add(im.name)
        for point_id, point in model.points3D.items():
            expect = fit['scale'] * rotation @ point.xyz + offset
            point_errors.append(float(np.linalg.norm(expect-registered.points3D[point_id].xyz)))
    assert max(camera_errors, default=0) < 1e-6
    assert max(point_errors, default=0) < 1e-6
    for row in rows:
        assert (row['position_registered'] == 'True') == (row['image_name'] in all_positioned)
        assert (row['registered_structural'] == 'True') == (row['image_name'] in all_structure)
    report_manifest = json.loads((OUT / 'report_manifest.json').read_text(encoding='utf-8'))
    for path, digest in report_manifest['inputs_sha256'].items():
        assert sha256(ROOT / path) == digest, path
    for graph in OUT.glob('*.plot.json'):
        for suffix in ['.png', '.svg', '.figure.pickle']:
            assert graph.with_name(graph.name.replace('.plot.json', suffix)).is_file()
    processes = {}
    for phase in ['prepare', 'features', 'matching', 'map']:
        record = json.loads((OUT / f'{phase}.process.json').read_text())
        assert record['status'] == 'completed' and record.get('ended_utc')
        processes[phase] = {'pid': record['pid'], 'started_utc': record['started_utc'],
                            'ended_utc': record['ended_utc']}
    extension_process = OUT / 'extended_restarts/map.process.json'
    if extension_process.exists():
        record = json.loads(extension_process.read_text())
        assert record['status'] == 'completed' and record.get('ended_utc')
        processes['extended_restarts'] = {k: record[k] for k in ['pid', 'started_utc', 'ended_utc']}
    result = {'status': 'passed', 'pid': os.getpid(), 'checked_utc': datetime.now(UTC).isoformat(),
        'frames_checked': len(rows), 'pixel_and_ray_check_samples': len(indices),
        'windows': len(windows), 'models': len(models), 'input_hashes': 'matched',
        'max_registered_camera_transform_error_m': max(camera_errors, default=0),
        'max_registered_point_transform_error_m': max(point_errors, default=0),
        'unique_registered_structure_frames': len(all_structure), 'unique_positioned_frames': len(all_positioned),
        'missing_position_policy': 'matches adjacent-row interpolation without extrapolation',
        'process_records': processes,
        'browser_actual_screen': 'Not tested; prior file URL policy restriction preserved. Node VM check separate.',
        'formal_evaluation': 'Python/COLMAP only; Blender is display only'}
    write_json(OUT / 'verification.json', result)
    print(json.dumps(result), flush=True)


if __name__ == '__main__':
    main()
