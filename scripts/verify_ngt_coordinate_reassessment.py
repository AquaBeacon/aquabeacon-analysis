"""Verify corrected pixel frames, position invariants, mapping database and saved deliverables."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import unquote

os.environ.setdefault('OPENBLAS_NUM_THREADS', '2')
os.environ.setdefault('OMP_NUM_THREADS', '2')
import cv2
import numpy as np
from evaluate_ngt_position import numbers, read_csv, write_json
from recalculate_ngt_positions import OUT, ROOT, RUN
from remap_ngt_corrected import DATABASE, OLD_WORK, POSITION, WORK, colmap, descriptor_digest
from remap_ngt_corrected import OUT as MAP_OUT

from aquabeacon_analysis.camera_geometry import display_geometry, transform_pixels
from aquabeacon_analysis.editable_plots import PlotDocument
from aquabeacon_analysis.ngt_binary import sha256
from aquabeacon_analysis.ngt_position import bracket_interpolate, calibrated_rays, centered_scatter
from aquabeacon_analysis.restart_mapping import position_registration, temporal_windows


def load(path):
    return json.loads(path.read_text(encoding='utf-8'))


def matches_digest(db):
    digest = hashlib.sha256()
    for pair, rows, cols, blob in db.execute('SELECT pair_id,rows,cols,data FROM matches ORDER BY pair_id'):
        digest.update(np.array([pair, rows, cols], dtype=np.int64).tobytes())
        digest.update(blob if blob is not None else b'NULL')
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment', required=True, choices=['20261028_ngt_000'])
    parser.add_argument('--phase', choices=['inputs', 'final'], default='final')
    args = parser.parse_args()
    cv2.setNumThreads(2)
    checks = []
    record = {'pid': os.getpid(), 'owner': 'coordinate_reassessment',
        'purpose': 'Verify ' + args.phase, 'started_utc': datetime.now(UTC).isoformat(),
        'status': 'running'}
    process_path = OUT / f'verify_{args.phase}.process.json'
    write_json(process_path, record)

    def check(name, condition, detail=''):
        checks.append({'check': name, 'passed': bool(condition), 'detail': detail})
        if not condition:
            raise AssertionError(name + ': ' + str(detail))

    def hashes(path):
        value = load(path)
        for relative, expected in value['inputs_sha256'].items():
            check(f'{path.parent.name}: source {relative}', sha256(ROOT / relative) == expected)
        return value

    try:
        audit = hashes(OUT / 'sensor_axis_audit.json')
        check('independent axis selections agree', audit['rankings']['train'][0]['matrix'] ==
              audit['rankings']['holdout'][0]['matrix'])
        check('held-out axis evidence', audit['rankings']['holdout'][0]['consistent_independent_pairs'] >= 3)
        geometry = load(ROOT / 'calibration/20261031_ngt_camera_frames.json')
        check('axis audit reference', sha256(OUT / 'sensor_axis_audit.json') == geometry['axis_evidence_sha256'])
        k = np.array(geometry['intrinsics_source']['camera_matrix_px'])
        d = np.array(geometry['intrinsics_source']['distortion'])
        kd = np.array(geometry['parent_proxy_matrix_display'])
        dd = np.array(geometry['parent_proxy_distortion_display'])
        a, rotation, size = display_geometry((1280, 720), 1)
        check('parent display dimensions', size == (720, 1280))
        check('parent IMU frame chain', np.allclose(np.array(geometry['R_camera_imu_from_display_optical']) @ rotation,
              geometry['R_camera_imu_from_sensor_optical'], atol=1e-12))
        count = 0
        for suffix, expected_rows in [('002', 300), ('003', 750), ('004', 1400)]:
            base = ROOT / f'results/20261028_ngt_{suffix}/position_evaluation'
            hashes(base / RUN / 'manifest.json')
            old, new = [read_csv(p / 'positions_5hz.csv') for p in [base, base / RUN]]
            check(suffix + ': all source cells', len(old) == len(new) == expected_rows)
            flags = ['optical_available', 'range_formula_available', 'magnetic_attitude_available', 'magnetic_position_available']
            check(suffix + ': missing flags unchanged', all(x[f] == y[f] for x, y in zip(old, new, strict=True) for f in flags))
            check(suffix + ': source time and distance preserved', all(x[f] == y[f] for x, y in zip(old, new, strict=True)
                for f in ['parent_video_s', 'range_m', 'light_x_px', 'light_y_px']))
            xy = numbers(new, ['light_x_px', 'light_y_px'])
            sensor_xy = transform_pixels(xy, np.linalg.inv(a))
            sensor = calibrated_rays(sensor_xy, k, d) @ rotation.T
            displayed = calibrated_rays(xy, kd, dd)
            check(suffix + ': sensor and display rays', np.allclose(sensor, displayed, atol=1e-10, equal_nan=True))
            xyz = numbers(new, ['relative_magnetic_north_m', 'relative_magnetic_east_m', 'relative_down_m'])
            good = np.isfinite(xyz).all(axis=1)
            check(suffix + ': ray distance preserved', np.allclose(np.linalg.norm(xyz[good], axis=1),
                  numbers(new, ['range_m'])[good, 0], atol=1e-10))
            events = read_csv(base / RUN / 'positions_at_acoustic_events.csv')
            actual = centered_scatter(numbers(events, ['relative_magnetic_north_m', 'relative_magnetic_east_m', 'relative_down_m']))
            expected = load(base / RUN / 'metrics.json')['relative_scatter']
            check(suffix + ': event RMS matches saved metric', np.isclose(actual['centered_rms_3d_m'], expected['centered_rms_3d_m']))
            count += len(new)
        check('all position cells retained', count == 2450)
        for relative, stat in load(OUT / 'raw_source_stats.json').items():
            now = (ROOT / relative).stat()
            check('raw unchanged: ' + relative, now.st_size == stat['size'] and now.st_mtime_ns == stat['mtime_ns'])
        # Independently decode identical source indices with rotation on and off.
        video = ROOT / audit['source']
        sensor_cap = cv2.VideoCapture(str(video), cv2.CAP_FFMPEG, [cv2.CAP_PROP_N_THREADS, 2])
        display_cap = cv2.VideoCapture(str(video), cv2.CAP_FFMPEG, [cv2.CAP_PROP_N_THREADS, 2])
        try:
            sensor_cap.set(cv2.CAP_PROP_ORIENTATION_AUTO, 0)
            for index in [31469, 39960, 44955]:
                for cap in [sensor_cap, display_cap]:
                    cap.set(cv2.CAP_PROP_POS_FRAMES, index)
                ok1, raw = sensor_cap.read()
                ok2, displayed = display_cap.read()
                check(f'parent frame {index}: actual clockwise pixels', ok1 and ok2 and
                      np.array_equal(cv2.rotate(raw, cv2.ROTATE_90_CLOCKWISE), displayed))
        finally:
            sensor_cap.release()
            display_cap.release()
        mapping = hashes(MAP_OUT / 'manifest.json')
        rows = read_csv(WORK / 'optimizer_frame_priors.csv')
        old_rows = read_csv(OLD_WORK / 'optimizer_frame_priors.csv')
        check('all native frame hashes verified in preparation', mapping['all_native_image_hashes_verified'] == 33564)
        check('source frame identity and native image hashes retained', len(rows) == len(old_rows) == 33564 and
              all(x['source_frame'] == y['source_frame'] and x['image_sha256'] == y['image_sha256']
                  for x, y in zip(rows, old_rows, strict=True)))
        positions = read_csv(POSITION)
        t = numbers(positions, ['parent_video_s'])[:, 0]
        pos = np.array([[float(r['child_east_declination0_m']), float(r['child_north_declination0_m']), -float(r['pressure_depth_m'])]
            if r['magnetic_position_available'] == 'True' else [np.nan] * 3 for r in positions])
        times = numbers(rows, ['parent_video_s'])[:, 0]
        expected = bracket_interpolate(t, pos, times, .25)
        check('all corrected priors obey gap-aware interpolation', np.allclose(expected,
              numbers(rows, ['east_m', 'north_m', 'up_m']), atol=1e-10, equal_nan=True))
        check('same 870 prior anchor cells', sum(r['optimizer_prior'] == 'True' for r in rows) == 870 and
              all(x['optimizer_prior'] == y['optimizer_prior'] for x, y in zip(rows, old_rows, strict=True)))
        check('child original-video orientation sampled at three times', len(mapping['child_image_orientation_checks']) == 3 and
              all(c['mean_abs_pixel_difference'] < c['opposite_rotation_difference'] / 2 for c in mapping['child_image_orientation_checks']))
        old_db = OLD_WORK / 'features_crop.db'
        check('legacy database unchanged', sha256(old_db) == mapping['source_database_sha256'])
        with sqlite3.connect(f'file:{old_db.as_posix()}?mode=ro', uri=True) as source, \
                sqlite3.connect(f'file:{DATABASE.as_posix()}?mode=ro', uri=True) as target:
            check('all descriptors unchanged', descriptor_digest(source) == descriptor_digest(target) == mapping['descriptor_digest'])
            check('raw descriptor matches unchanged', matches_digest(source) == matches_digest(target))
            query = 'SELECT image_id,rows,cols,data FROM keypoints ORDER BY image_id'
            keypoint_rows = 0
            for old, new in zip(source.execute(query), target.execute(query), strict=True):
                check_header = old[:3] == new[:3]
                p = np.frombuffer(old[3], np.float32).reshape(old[1:3]).copy()
                q = np.frombuffer(new[3], np.float32).reshape(new[1:3])
                p[:, :2] += np.array([640, 36], np.float32)
                if not (check_header and np.array_equal(p, q)):
                    raise AssertionError('Keypoint transport mismatch at image ' + str(old[0]))
                keypoint_rows += 1
            check('all 33564 keypoint arrays transported exactly', keypoint_rows == 33564)
        k_colmap = np.array(mapping['camera_matrix_colmap_display'])
        k_cv = np.array(mapping['camera_matrix_opencv_display'])
        check('explicit COLMAP half-pixel conversion', np.allclose(k_colmap[:2, 2] - k_cv[:2, 2], .5))
        check('unused fifth distortion term is zero', mapping['distortion_display'][4] == 0)
        expected_params = [k_colmap[0, 0], k_colmap[1, 1], *k_colmap[:2, 2], *mapping['distortion_display'][:4]]
        lookup = {r['image_name']: r for r in rows}
        with colmap.Database.open(DATABASE) as db:
            check('database image count', db.num_images() == 33564)
            check('geometric verification nonempty', db.num_verified_image_pairs() == mapping['database_counts']['verified_pairs'] > 0)
            cameras = db.read_all_cameras()
            check('both original camera IDs retained', len(cameras) == 2)
            for cam in cameras:
                check(f'camera {cam.camera_id}: dimensions and parameters', (cam.width, cam.height) == (1280, 720) and
                      np.allclose(cam.params, expected_params, atol=1e-10))
            priors = db.read_all_pose_priors()
            images = {im.data_id: im for im in db.read_all_images()}
            check('database prior count', len(priors) == 870)
            for prior in priors:
                row = lookup[images[prior.corr_data_id].name]
                if row['optimizer_prior'] != 'True' or not np.allclose(prior.position,
                        [float(row[k]) for k in ['east_m', 'north_m', 'up_m']], atol=1e-10):
                    raise AssertionError('Incorrect stored position prior')
            check('all 870 database priors match corrected positions', True)
        if args.phase == 'final':
            report_manifest = hashes(OUT / 'manifest.json')
            for name, expected_hash in report_manifest['outputs_sha256'].items():
                check('report output hash: ' + name, sha256(OUT / name) == expected_hash)
            summary = load(MAP_OUT / 'summary.json')
            old_summary = load(MAP_OUT.parent / 'native120_restart/summary.json')
            wall_mask = (times >= 1415) & (times < 1540)
            check('wall denominator matches original nominal interval', int(wall_mask.sum()) ==
                  summary['wall']['input_frames'] == old_summary['wall_interval']['input_frames'] == 14985)
            check('boundary spill retained in total denominator', len(rows) == 33564 and summary['nominal_boundary_spill_frames'] == 1)
            check('19 windows completed without process failure', load(MAP_OUT / 'all.process.json')['status'] == 'completed'
                  and summary['windows'] == 19 and not summary['failed_process_windows'])
            windows = temporal_windows(times, 20, 15)
            check('all frames covered by a reconstruction window', len(windows) == 19 and
                  len(set(np.concatenate(windows))) == len(rows))
            models = load(MAP_OUT / 'models.json')
            structural, positioned = set(), set()
            max_camera_error, max_point_error = 0., 0.
            for item in models:
                folder = ROOT / item['folder']
                model = colmap.Reconstruction(folder)
                images = [im for im in model.images.values() if im.has_pose]
                check(f"model {item['window']}/{item['model']}: counts", model.num_points3D() == item['points'] and
                      len(images) == item['registered_images'])
                check(f"model {item['window']}/{item['model']}: finite points", all(np.isfinite(p.xyz).all() for p in model.points3D.values()))
                if item['structural']:
                    structural.update(im.name for im in images)
                if not item['position_registered']:
                    continue
                aligned = colmap.Reconstruction(folder / 'position_registered')
                fit = item['position_fit']
                check(f"model {item['window']}/{item['model']}: registration gate", fit['accepted'] and fit['structural'] and
                      fit['correspondences'] >= 6 and min(fit['weighted_second_first_ratios']) >= .001)
                rot, trans = np.array(fit['rotation']), np.array(fit['translation'])
                anchor_images = [im for im in images if lookup[im.name]['optimizer_prior'] == 'True']
                anchor_images.sort(key=lambda im: im.name)
                fitted = np.array([fit['scale'] * rot @ im.projection_center() + trans for im in anchor_images])
                targets = np.array([[float(lookup[im.name][k]) for k in ['east_m', 'north_m', 'up_m']] for im in anchor_images])
                check(f"model {item['window']}/{item['model']}: saved fit residual", np.isclose(
                    np.sqrt(np.mean(np.sum((fitted - targets)**2, axis=1))), fit['fit_rms_m'], atol=1e-8))
                for im in images:
                    expect = fit['scale'] * rot @ im.projection_center() + trans
                    max_camera_error = max(max_camera_error, float(np.linalg.norm(expect - aligned.images[im.image_id].projection_center())))
                    positioned.add(im.name)
                for pid, p in model.points3D.items():
                    expect = fit['scale'] * rot @ p.xyz + trans
                    max_point_error = max(max_point_error, float(np.linalg.norm(expect - aligned.points3D[pid].xyz)))
            check('all camera and point Sim3 transforms', max_camera_error < 1e-6 and max_point_error < 1e-6,
                  {'max_camera_error_m': max_camera_error, 'max_point_error_m': max_point_error})
            evaluated = read_csv(MAP_OUT / 'frame_evaluation.csv')
            check('per-frame structural and positioned flags', all((r['image_name'] in structural) == (r['registered_structural'] == 'True')
                  and (r['image_name'] in positioned) == (r['position_registered'] == 'True') for r in evaluated))
            check('summary unique counts', len(structural) == summary['structural_registered_unique'] and
                  len(positioned) == summary['position_registered_unique'])
            runs = read_csv(MAP_OUT / 'mapping_intervals.csv')
            expanded = [r['state'] for r in runs for _ in range(int(r['samples']))]
            states = ['positioned' if r['position_registered'] == 'True' else 'local' if r['registered_structural'] == 'True'
                      else 'unregistered' for r in evaluated]
            check('all mapping interval cells preserved', len(expanded) == 33564 and expanded == states)
            check('mapping cell edges continuous', all(abs(float(a['end_s']) - float(b['start_s'])) < 1e-7 for a, b in pairwise(runs)))
            old_models = load(MAP_OUT.parent / 'native120_restart/models.json')
            old_lookup = {(m['window'], m['model']): m for m in old_models}
            old_prior_lookup = {r['image_name']: r for r in old_rows}
            for r in read_csv(MAP_OUT / 'fixed_shape_scale_comparison.csv'):
                if r['old_accepted'] != 'True' or r['wall_period'] != 'True':
                    continue
                item = old_lookup[(int(r['window']), int(r['model']))]
                model = colmap.Reconstruction(ROOT / item['folder'])
                anchors = [im for im in model.images.values() if im.has_pose and lookup[Path(im.name).with_suffix('.jpg').name]['optimizer_prior'] == 'True']
                anchors.sort(key=lambda im: im.name)
                a = np.array([im.projection_center() for im in anchors])
                old_b = np.array([[float(old_prior_lookup[im.name][k]) for k in ['east_m', 'north_m', 'up_m']] for im in anchors])
                old_fit = position_registration(a, old_b)
                check(f"fixed shape {r['window']}/{r['model']}: original fit reproduced", old_fit['accepted'] and
                      np.isclose(old_fit['scale'], item['position_fit']['scale'], atol=1e-10) and
                      np.isclose(old_fit['fit_rms_m'], item['position_fit']['fit_rms_m'], atol=1e-10))
                b = np.array([[float(lookup[Path(im.name).with_suffix('.jpg').name][k]) for k in ['east_m', 'north_m', 'up_m']] for im in anchors])
                fit = position_registration(a, b)
                check(f"fixed shape {r['window']}/{r['model']}: ratio reproduced", fit['accepted'] and np.isclose(
                    fit['scale'] / item['position_fit']['scale'], float(r['scale_ratio_corrected_over_old']), atol=1e-10))
            names = ['trajectories_corrected', 'position_displacement', 'static_relative_positions', 'parent_principal_point',
                     'mapping_coverage_comparison', 'fixed_shape_scale_ratios', 'corrected_wall_overview'] + [
                         f'case_M{i}_reassessment' for i in range(1, 6)]
            for name in names:
                check(name + ': editable companions', all((OUT / (name + suffix)).exists() for suffix in
                      ['.png', '.svg', '.figure.pickle', '.plot.json']))
            with TemporaryDirectory(prefix='verify_editor_', dir=OUT) as temp:
                path = Path(temp) / 'comparison.figure.pickle'
                shutil.copy2(OUT / 'position_displacement.figure.pickle', path)
                original_hash = sha256(path)
                document = PlotDocument(path)
                values = [line.get_ydata().copy() for line in document.figure.axes[0].lines]
                document.update({'figure': {'width': 11, 'height': 6},
                    'axes': {'0': {'xmin': 1050, 'xmax': 1110, 'ymin': 0, 'ymax': 1}}})
                document.save()
                restored = PlotDocument(path)
                check('editor dimension/range save and reload', np.allclose(restored.figure.get_size_inches(), [11, 6]) and
                      restored.figure.axes[0].get_xlim() == (1050., 1110.) and restored.figure.axes[0].get_ylim() == (0., 1.))
                check('editor retains original data and master', sha256(path) == original_hash and all(np.array_equal(v, line.get_ydata(), equal_nan=True)
                      for v, line in zip(values, restored.figure.axes[0].lines, strict=True)))
                check('editor PNG SVG PDF output', all((Path(temp) / ('comparison.edited.' + ext)).exists() for ext in ['png', 'svg', 'pdf']))
            for doc in [OUT / 'report.md', OUT / 'media_reassessment.md', MAP_OUT / 'report.md']:
                for link in re.findall(r'\]\(([^)]+)\)', doc.read_text(encoding='utf-8')):
                    if '://' in link or link.startswith('#') or link == 'validation.md':
                        continue
                    check(doc.name + ': ' + link, (doc.parent / unquote(link.split('#')[0])).exists())
            blender = load(MAP_OUT / 'blender.process.json')
            check('Blender saved and reopened', blender['status'] == 'completed_and_reopened_verified')
            check('Blender input and code hashes', blender['input_sha256'] == sha256(MAP_OUT / 'map_data.json') and
                  blender['generator_sha256'] == sha256(ROOT / 'blender/create_ngt_restart_map.py'))
        record['status'] = 'completed'
    except BaseException as error:
        record.update(status='failed', error=str(error))
        raise
    finally:
        record['ended_utc'] = datetime.now(UTC).isoformat()
        write_json(process_path, record)
        write_json(OUT / f'verification_{args.phase}.json', {'status': record['status'], 'checks': checks})
        shutil.copy2(__file__, OUT / 'verify.py')
    print(f'{len(checks)} checks PASS ({args.phase})', flush=True)


if __name__ == '__main__':
    main()
