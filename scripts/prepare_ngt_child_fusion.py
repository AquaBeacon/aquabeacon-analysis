"""Prepare child-only sensor inputs and provisional optical/IMU rotation."""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import numpy as np
from run_ngt_child_vslam import BASE, CAMERA, IMU, ROOT, sha, write_json
from scipy.spatial.transform import Rotation

from aquabeacon_analysis.child_attitude import estimate_attitude, magnetic_ned_quaternions
from aquabeacon_analysis.child_sensor_fusion import fit_vector_rotation
from aquabeacon_analysis.ngt_sync import angular_signal, numeric_csv, sample_with_gaps

OUTPUT = ROOT/'results/20261028_ngt_000/child_sensor_fusion'
AXES = ROOT/'calibration/20261029_child_axis_003_imu_frd.json'
HEALTH = ROOT/'data/interim/20261028_ngt_000/decoded/child/C_MAIN_HEALTH_RANGE_83304.health.csv'


def prepare():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    existing = OUTPUT/'sensor_manifest.json'
    if existing.exists():
        result = json.loads(existing.read_text(encoding='utf-8'))
        for relative, digest in result['inputs_sha256'].items():
            if Path(relative).suffix in {'.csv', '.json'} and sha(ROOT/relative) != digest:
                raise ValueError('Preserved sensor preparation has changed input: '+relative)
        if not (OUTPUT/'sensors.npz').exists():
            raise ValueError('Preserved preparation is missing its sensor arrays')
        return result
    clock_path = BASE/'child_clock.json'
    clock = json.loads(clock_path.read_text(encoding='utf-8'))
    axis = np.array(json.loads(AXES.read_text())['R_body_from_sensor'])
    raw = numeric_csv(IMU, ['acquired_us', 'fresh_mask', 'qw', 'qx', 'qy', 'qz',
                           'ax', 'ay', 'az', 'gx', 'gy', 'gz', 'mx', 'my', 'mz'])
    t = (raw[:, 0]/1e6-clock['offset_s'])/clock['time_scale']
    mask = raw[:, 1].astype(int)
    local, angles, heading, observable = estimate_attitude(raw[:, 2:6], raw[:, 12:15], axis)
    for bit, columns in [(1, local), (8, heading)]:
        last = np.maximum.accumulate(np.where(mask & bit, t, -np.inf))
        columns[t-last > .1] = np.nan
    heading[~observable] = np.nan
    magnetic = magnetic_ned_quaternions(angles[:, :2], heading)
    magnetic[~np.isfinite(local).all(axis=1)] = np.nan
    # Fresh samples only for each sensor; no held register treated as a new sample.
    acceleration = sample_with_gaps(t[mask & 2 != 0], raw[mask & 2 != 0, 6:9], t)
    gyro = sample_with_gaps(t[mask & 4 != 0], raw[mask & 4 != 0, 9:12], t)
    health = numeric_csv(HEALTH, ['mono_us', 'pressure_mbar', 'temp_c'])
    health[:, 0] = (health[:, 0]/1e6-clock['offset_s'])/clock['time_scale']
    camera = numeric_csv(CAMERA, ['video_time_s', 'qw', 'qx', 'qy', 'qz',
                                 'accel_x_g', 'accel_y_g', 'accel_z_g'])
    camera_t, camera_g = angular_signal(camera[:, :5])
    camera_a = sample_with_gaps(camera[:, 0], camera[:, 5:8]*9.80665, camera_t)

    visual_path = ROOT/'results/20261028_ngt_003/child_only_vslam/frame_evaluation.csv'
    visual = numeric_csv(visual_path, ['source_video_s', 'tracking_state', 'qx', 'qy', 'qz', 'qw'])
    ix = np.arange(0, len(visual)-12, 4)
    good = np.array([np.all(visual[i:i+13, 1] == 2)
                     and np.isfinite(visual[i:i+13, 2:]).all() for i in ix])
    ix = ix[good]
    center = (visual[ix, 0]+visual[ix+12, 0])/2
    dt = visual[ix+12, 0]-visual[ix, 0]
    vg = ((Rotation.from_quat(visual[ix, 2:]).inv()
           * Rotation.from_quat(visual[ix+12, 2:])).as_rotvec()/dt[:, None])
    bg = sample_with_gaps(t, gyro, center)
    cg = sample_with_gaps(camera_t, camera_g, center)
    train = center < 933.
    rotations = {}
    for name, signal in [('bno', bg), ('camera_imu', cg)]:
        matrix, quality = fit_vector_rotation(signal[train], vg[train])
        valid = (~train & np.isfinite(signal).all(1) & np.isfinite(vg).all(1))
        predicted = signal[valid] @ matrix.T
        rotations[name] = {
            'R_optical_from_sensor': matrix.tolist(), 'training': quality,
            'held_out': {'samples': int(valid.sum()),
                         'rms_rad_s': float(np.sqrt(np.mean((predicted-vg[valid])**2))),
                         'correlation': float(np.corrcoef(predicted.ravel(), vg[valid].ravel())[0, 1])}}

    # Explicit static trial 002 start, not automatic zero-velocity assumption during movement.
    bias_window = (t >= 762) & (t < 767) & np.isfinite(acceleration).all(1)
    bias_window &= np.isfinite(local).all(1) & (np.linalg.norm(gyro, axis=1) < .035)
    r_ned_sensor = (Rotation.from_quat(local[bias_window][:, [1, 2, 3, 0]])
                    * Rotation.from_matrix(axis))
    expected = r_ned_sensor.inv().apply(np.tile([0, 0, -9.80665], (bias_window.sum(), 1)))
    bias = np.median(acceleration[bias_window]-expected, axis=0)
    if bias_window.sum() < 100 or np.linalg.norm(bias) > .3:
        raise ValueError('Static bias trial lacks evidence')
    np.savez_compressed(OUTPUT/'sensors.npz', video_s=t, acceleration=acceleration, gyro=gyro,
                        local_xyzw=local[:, [1, 2, 3, 0]], magnetic_xyzw=magnetic[:, [1, 2, 3, 0]],
                        health=health, camera_t=camera_t, camera_gyro=camera_g,
                        camera_acceleration=camera_a, body_from_sensor=axis,
                        accelerometer_bias=bias)
    manifest = {
        'parent_observations_used': False, 'utc_used': False, 'clock': clock,
        'rotations': rotations, 'rotation_training_source_video_s': [906.539, 933.],
        'rotation_validation_source_video_s': [933., 959.442],
        'rotation_scope': 'provisional fitted from child visual rotations in trial 003; '
                          'same trial is not independent validation; underwater intrinsics used in air',
        'translation_sensor_to_optical_m': [0, 0, 0],
        'translation_status': 'unknown; zero lever arm is an explicit trial assumption',
        'static_accelerometer_bias_sensor_m_s2': bias.tolist(),
        'bias_source_video_s': [762, 767], 'bias_samples': int(bias_window.sum()),
        'gravity_m_s2': 9.80665, 'pressure_density_kg_m3': 997.,
        'pressure_reference': 'first health observation within each selected trial; relative down',
        'attitude': 'recomputed from child-only quaternion/magnetometer and FRD axes; magnetic NED',
        'magnetic_accuracy_validated': False,
        'inputs_sha256': {str(p.relative_to(ROOT)): sha(p) for p in
                         [IMU, CAMERA, HEALTH, AXES, clock_path, visual_path, Path(__file__),
                          ROOT/'src/aquabeacon_analysis/child_sensor_fusion.py']}}
    write_json(OUTPUT/'sensor_manifest.json', manifest)
    print(json.dumps({'bias': bias.tolist(), 'rotations': rotations}), flush=True)
    return manifest


def prepare_vi(experiment, manifest, camera_imu=False):
    source = ROOT/f'data/interim/{experiment}/child_only_vslam/okinawa_native120'
    dataset_name = 'camera_vi30' if camera_imu else 'vi30'
    dataset = ROOT/f'data/interim/{experiment}/child_sensor_fusion/{dataset_name}'
    if (dataset/'export_manifest.json').exists():
        previous = json.loads((dataset/'export_manifest.json').read_text())
        for relative, digest in previous['inputs_sha256'].items():
            if sha(ROOT/relative) != digest:
                raise ValueError('Preserved VI export input changed: '+relative)
        return dataset
    dataset.mkdir(parents=True, exist_ok=True)
    (dataset/'mav0/cam0/data').mkdir(parents=True, exist_ok=True)
    with (source/'frames.csv').open() as handle:
        frames = list(csv.DictReader(handle))[::4]
    times = np.array([float(r['source_video_s']) for r in frames])
    sensors = np.load(OUTPUT/'sensors.npz')
    mode = 'camera_imu' if camera_imu or experiment.endswith('001') else 'bno'
    if mode == 'bno':
        t, gyro = sensors['video_s'], sensors['gyro']
        acc = sensors['acceleration']-sensors['accelerometer_bias']
    else:
        t, gyro, acc = sensors['camera_t'], sensors['camera_gyro'], sensors['camera_acceleration']
    valid = (t >= times[0]-.2) & (t <= times[-1]+.2)
    valid &= np.isfinite(gyro).all(1) & np.isfinite(acc).all(1)
    imu_t = t[valid]
    imu_path = dataset/'mav0/imu0/data.csv'
    imu_path.parent.mkdir(parents=True, exist_ok=True)
    with imu_path.open('w', newline='', encoding='utf-8') as handle:
        writer = csv.writer(handle, lineterminator='\n')
        writer.writerow(['#timestamp [ns]', 'wx', 'wy', 'wz', 'ax', 'ay', 'az'])
        for ti, gi, ai in zip(imu_t, gyro[valid], acc[valid], strict=True):
            writer.writerow([10**18+round(ti*1e9), *gi, *ai])
    (dataset/'timestamps.txt').write_text(''.join(r['synthetic_ns']+'\n' for r in frames),
                                         encoding='utf-8', newline='\n')
    with (dataset/'frames.csv').open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(frames[0]))
        writer.writeheader()
        writer.writerows(frames)
    rotation = np.array(manifest['rotations'][mode]['R_optical_from_sensor']).T
    transform = np.eye(4)
    transform[:3, :3] = rotation
    settings = (ROOT/'config/20261028_ngt/child_only_monocular.yaml').read_text()
    settings = settings.replace('Camera.fps: 120', 'Camera.fps: 30')
    settings += '\n# Child-only provisional inertial trial; zero unmeasured lever arm.\n'
    settings += 'IMU.T_b_c1: !!opencv-matrix\n   rows: 4\n   cols: 4\n   dt: f\n   data: ['
    settings += ', '.join(f'{v:.12g}' for v in transform.ravel())+']\n'
    settings += ('IMU.NoiseGyro: 0.02\nIMU.NoiseAcc: 0.2\nIMU.GyroWalk: 0.001\n'
                 'IMU.AccWalk: 0.01\nIMU.Frequency: '+('100.0' if mode == 'bno' else '29.97003')+'\n')
    (dataset/'settings.yaml').write_text(settings, encoding='utf-8')
    export = {'experiment_id': experiment, 'image_source': str(source.relative_to(ROOT)),
              'source_video_interval_s': [float(times[0]), float(times[-1]+1001/30000)],
              'input_frames': len(times), 'sampling': 'every 4th native frame, 30000/1001 Hz',
              'imu_mode': mode, 'imu_rows': int(valid.sum()),
              'imu_max_gap_s': float(np.max(np.diff(imu_t))),
              'imu_gaps_over_100ms': int((np.diff(imu_t) > .1).sum()),
              'noise_model': 'trial assumptions; not Allan-calibrated',
              'time_basis': 'synthetic nanoseconds 1e18 + child source video seconds * 1e9',
              'parent_observations_used': False, 'pressure_input_to_orbslam': False,
              'inputs_sha256': {str(p.relative_to(ROOT)): sha(p) for p in
                               [source/'frames.csv', OUTPUT/'sensor_manifest.json',
                                dataset/'frames.csv', dataset/'settings.yaml', imu_path]}}
    write_json(dataset/'export_manifest.json', export)
    return dataset


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment', action='append', required=True,
                        choices=[f'20261028_ngt_{i:03}' for i in range(1, 5)])
    parser.add_argument('--camera-imu', action='store_true', help='Separate camera-only inertial trial')
    args = parser.parse_args()
    manifest = prepare()
    for experiment in args.experiment:
        print(prepare_vi(experiment, manifest, args.camera_imu), flush=True)


if __name__ == '__main__':
    main()
