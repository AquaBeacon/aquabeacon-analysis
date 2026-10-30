"""Run native-rate monocular SLAM for explicit NGT trials using child observations only."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from aquabeacon_analysis.camera_calibration import load_camera_intrinsics
from aquabeacon_analysis.child_vslam import frame_interval
from aquabeacon_analysis.ngt_sync import angular_signal, fit_rotation, numeric_csv, sample_with_gaps

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / 'results/20261028_ngt_000/child_only_vslam'
DOCKER = r'C:\Program Files\Docker\Docker\resources\bin\docker.exe'
IMAGE = 'aquabeacon-orbslam3:4452a3c'
RUN = 'okinawa_native120'
FPS = 120000 / 1001
STAMP_ORIGIN = 10**18  # synthetic offset for the legacy 19-digit filename reader; NOT UTC
VIDEO = ROOT / 'data/raw/ngtHighSchoolExperiment1/child/DJI/DJI_20261028140748_0186_D.MP4'
CAMERA = ROOT / 'data/interim/20261028_ngt_000/cameras/child_telemetry.csv'
IMU = ROOT / 'data/interim/20261028_ngt_000/decoded/child/C_MAIN_IMU_100Hz_83304.imu.csv'


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False), encoding='utf-8')


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda: handle.read(8*1024*1024), b''):
            digest.update(block)
    return digest.hexdigest()


def owned(command, log, timeout, purpose):
    """Journal each bounded task-owned subprocess and wait for its exit."""
    log.parent.mkdir(parents=True, exist_ok=True)
    record = {'owner': 'ngt_child_only_vslam', 'purpose': purpose, 'command': command,
              'started_utc': datetime.now(UTC).isoformat(), 'controller_pid': os.getpid()}
    with log.open('w', encoding='utf-8') as handle:  # noqa: SIM117
        with subprocess.Popen(command, stdout=handle, stderr=subprocess.STDOUT,
                              creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
                              ) as process:
            record['pid'] = process.pid
            write_json(log.with_suffix('.process.json'), record)
            print(json.dumps(record), flush=True)
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                record['timed_out'] = True
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
            finally:
                record.update(returncode=process.returncode, ended_utc=datetime.now(UTC).isoformat())
                write_json(log.with_suffix('.process.json'), record)
    return record


def clock_fit():
    """Fit acquired_us to video PTS from ONLY child angular motion, without UTC."""
    destination = BASE / 'child_clock.json'
    if destination.exists():
        result = json.loads(destination.read_text(encoding='utf-8'))
        if result['inputs_sha256'] == {str(p.relative_to(ROOT)): sha(p) for p in (CAMERA, IMU)}:
            return result
        raise ValueError('Clock source changed')
    ct, cg = angular_signal(numeric_csv(CAMERA, ['video_time_s', 'qw', 'qx', 'qy', 'qz']))
    body = numeric_csv(IMU, ['acquired_us', 'gx', 'gy', 'gz'])
    bt, bg = body[:, 0]/1e6, body[:, 1:]
    cn, bn = np.linalg.norm(cg, axis=1), np.linalg.norm(bg, axis=1)

    def match(window, offsets, rate):
        grid = np.arange(*window, 1/rate)
        cam = np.interp(grid, ct, cn)
        scores = []
        for offset in offsets:
            sample = np.interp(grid+offset, bt, bn, left=np.nan, right=np.nan)
            good = np.isfinite(sample) & np.isfinite(cam)
            score = (np.corrcoef(cam[good], sample[good])[0, 1]
                     if good.mean() >= .8 and np.std(sample[good]) > 1e-5 else -1)
            scores.append(float(score))
        best = int(np.argmax(scores))
        return float(offsets[best]), scores[best]

    seed, _ = match((540, 620), np.arange(-1000, 1000, .5), 5)
    windows = []
    for win in [(540, 620), (700, 850), (950, 1100), (1200, 1400), (1400, 1510)]:
        offset, corr = match(win, np.arange(seed-1, seed+1.001, .002), 30)
        selection = (ct >= win[0]) & (ct < win[1])
        aligned = sample_with_gaps(bt, bg, ct[selection]+offset)
        try:
            _, quality = fit_rotation(aligned, cg[selection])
        except ValueError:
            quality = {'correlation': 0, 'singular_ratio': 0}
        windows.append({'window_s': list(win), 'offset_s': offset, 'correlation': corr,
                        'rotation_check': quality,
                        'accepted': corr >= .9 and quality['correlation'] >= .85
                        and quality['singular_ratio'] >= .01})
    accepted = [r for r in windows if r['accepted']]
    if len(accepted) < 3:
        raise ValueError(f'Insufficient child clock evidence: {windows}')
    centers = np.array([np.mean(r['window_s']) for r in accepted])
    offsets = np.array([r['offset_s'] for r in accepted])
    slope, offset = np.polyfit(centers, offsets, 1)
    result = {'equation': 'child acquired_s = offset_s + time_scale * source_video_s',
              'offset_s': float(offset), 'time_scale': float(1+slope), 'windows': windows,
              'max_window_residual_s': float(np.max(np.abs(offsets-offset-slope*centers))),
              'parent_observations_used': False, 'utc_used': False,
              'inputs_sha256': {str(p.relative_to(ROOT)): sha(p) for p in (CAMERA, IMU)}}
    write_json(destination, result)
    return result


def settings():
    k, d, provenance = load_camera_intrinsics(ROOT, 'C_MAIN_CAM', (1280, 720))
    calibration_path = Path(provenance['source'])
    if provenance['calibrated_size'] != [1920, 1080] or d[4] != 0:
        raise ValueError('Unexpected native camera calibration model')
    write_json(BASE/'camera_intrinsics_applied.json', provenance)
    lines = ['%YAML:1.0', '# Native MP4 resized 2/3; shared Okinawa underwater calibration.',
             '# Calibration SHA256: '+sha(calibration_path), 'File.version: "1.0"',
             'Camera.type: "PinHole"']
    values = {'Camera1.fx': k[0, 0], 'Camera1.fy': k[1, 1], 'Camera1.cx': k[0, 2],
              'Camera1.cy': k[1, 2], **dict(zip(['Camera1.k1', 'Camera1.k2', 'Camera1.p1',
                                            'Camera1.p2'], d[:4], strict=True)),
              'Camera.width': 1280, 'Camera.height': 720, 'Camera.fps': 120, 'Camera.RGB': 0,
              'ORBextractor.nFeatures': 1500, 'ORBextractor.scaleFactor': 1.2,
              'ORBextractor.nLevels': 8, 'ORBextractor.iniThFAST': 12,
              'ORBextractor.minThFAST': 5,
              'Viewer.KeyFrameSize': .05, 'Viewer.KeyFrameLineWidth': 1.,
              'Viewer.GraphLineWidth': .9, 'Viewer.PointSize': 2., 'Viewer.CameraSize': .08,
              'Viewer.CameraLineWidth': 3., 'Viewer.ViewpointX': 0.,
              'Viewer.ViewpointY': -.7, 'Viewer.ViewpointZ': -3.5, 'Viewer.ViewpointF': 500.}
    lines.extend(f'{key}: {value}' for key, value in values.items())
    destination = ROOT / 'config/20261028_ngt/child_only_monocular.yaml'
    destination.write_text('\n'.join(lines)+'\n', encoding='utf-8')
    return destination, calibration_path


def prepare(experiment, calibration_path):
    destination = ROOT / f'data/interim/{experiment}/child_only_vslam/{RUN}'
    manifest_path = destination / 'export_manifest.json'
    if manifest_path.exists():
        return destination, json.loads(manifest_path.read_text(encoding='utf-8'))
    if RUN.endswith('_clahe3'):
        return prepare_clahe(experiment, destination)
    selection = ROOT / f'data/interim/{experiment}/synchronized/child/child_telemetry.csv'
    times = numeric_csv(selection, ['video_time_s'])[:, 0]
    frames = frame_interval(float(times[0]), float(times[-1]+1001/30000), FPS, 183485)
    image_dir = destination / 'mav0/cam0/data'
    image_dir.mkdir(parents=True, exist_ok=True)
    if any(image_dir.iterdir()):
        raise ValueError(f'Partial extraction exists: {image_dir}')
    command = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-threads', '2',
               '-ss', f'{frames[0]/FPS:.12f}', '-i', str(VIDEO), '-map', '0:v:0',
               '-frames:v', str(len(frames)), '-vf', 'scale=1280:720', '-filter_threads', '2',
               '-vsync', '0', '-q:v', '2', '-threads', '2', '-start_number', '0',
               str(image_dir / '%09d.jpg')]
    record = owned(command, destination / 'extract.log', 3600, f'{experiment} native frames')
    if record['returncode']:
        raise RuntimeError('Frame extraction failed; inspect '+str(destination / 'extract.log'))
    images = sorted(image_dir.glob('*.jpg'))
    if len(images) != len(frames):
        raise ValueError('Native frame count mismatch')
    records = []
    hashes = hashlib.sha256()
    for source, index in zip(images, frames, strict=True):
        stamp = STAMP_ORIGIN + round(int(index)*1001*1_000_000_000/120000)
        target = source.with_name(f'{stamp}.jpg')
        source.rename(target)
        digest = sha(target)
        hashes.update((target.name+' '+digest+'\n').encode())
        records.append((stamp, int(index), index/FPS, digest))
    with (destination / 'frames.csv').open('w', newline='', encoding='utf-8') as handle:
        writer = csv.writer(handle)
        writer.writerow(['synthetic_ns', 'source_frame', 'source_video_s', 'image_sha256'])
        writer.writerows(records)
    (destination / 'timestamps.txt').write_text(''.join(f'{r[0]}\n' for r in records),
                                               encoding='utf-8', newline='\n')
    manifest = {'experiment_id': experiment, 'run': RUN, 'native_fps': '120000/1001',
                'input_frames': len(frames), 'source_frame_interval': [int(frames[0]),
                                                                     int(frames[-1]+1)],
                'source_video_interval_s': [float(frames[0]/FPS), float((frames[-1]+1)/FPS)],
                'resize': [1280, 720], 'input_channels': 3, 'image_extension': '.jpg',
                'frame_sampling': 'all native frames in selected interval; no decimation',
                'time_basis': 'synthetic_ns = 1e18 + source_video_pts * 1e9; NOT UTC',
                'selection_note': 'Only source_video_s read from existing child subset; inherited '
                                  'experiment boundaries are administrative selection metadata.',
                'camera_source': str(VIDEO.relative_to(ROOT)),
                'input_sha256': {str(selection.relative_to(ROOT)): sha(selection),
                                 str(calibration_path.relative_to(ROOT)): sha(calibration_path)},
                'image_digest_chain': hashes.hexdigest(), 'parent_observations_used': False,
                'camera_calibration_user_authorized_shared': True}
    write_json(manifest_path, manifest)
    return destination, manifest


def prepare_clahe(experiment, destination):
    """Keep the identical frame/time plan and explicitly record contrast processing."""
    import cv2

    cv2.setNumThreads(2)
    source = ROOT/f'data/interim/{experiment}/child_only_vslam/okinawa_native120'
    original = json.loads((source/'export_manifest.json').read_text(encoding='utf-8'))
    with (source/'frames.csv').open() as handle:
        rows = list(csv.DictReader(handle))
    images = destination/'mav0/cam0/data'
    images.mkdir(parents=True, exist_ok=True)
    if any(images.iterdir()):
        raise ValueError('Partial CLAHE export exists; preserving it')
    clahe = cv2.createCLAHE(clipLimit=3., tileGridSize=(8, 8))
    record = {'pid': os.getpid(), 'started_utc': datetime.now(UTC).isoformat(),
              'owner': 'ngt_child_only_vslam', 'purpose': f'{experiment} CLAHE frame preparation'}
    write_json(destination/'prepare.process.json', record)
    chain = hashlib.sha256()
    for row in rows:
        name = row['synthetic_ns']+'.jpg'
        bgr = cv2.imread(str(source/'mav0/cam0/data'/name))
        if bgr is None:
            raise ValueError('Missing source image '+name)
        lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)
        lab[:, :, 0] = clahe.apply(lab[:, :, 0])
        enhanced = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)
        if not cv2.imwrite(str(images/name), enhanced, [cv2.IMWRITE_JPEG_QUALITY, 95]):
            raise ValueError('Cannot save CLAHE image')
        row['image_sha256'] = sha(images/name)
        chain.update((name+' '+row['image_sha256']+'\n').encode())
    with (destination/'frames.csv').open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    (destination/'timestamps.txt').write_bytes((source/'timestamps.txt').read_bytes()
                                              .replace(b'\r\n', b'\n'))
    manifest = {**original, 'run': RUN, 'image_digest_chain': chain.hexdigest(),
                'image_processing': 'OpenCV LAB L-channel CLAHE clipLimit=3 tileGrid=8x8; JPEG95',
                'original_export': str(source.relative_to(ROOT)),
                'original_export_sha256': sha(source/'export_manifest.json')}
    write_json(destination/'export_manifest.json', manifest)
    record.update(ended_utc=datetime.now(UTC).isoformat(), returncode=0)
    write_json(destination/'prepare.process.json', record)
    return destination, manifest


def run_slam(experiment, dataset, export, config, calibration):
    output = ROOT / f'results/{experiment}/child_only_vslam/{RUN}'
    path = output / 'run_manifest.json'
    if path.exists():
        print('Preserving existing run '+str(path), flush=True)
        return
    output.mkdir(parents=True, exist_ok=True)
    timestamps = dataset/'timestamps.txt'
    timestamps.write_bytes(timestamps.read_bytes().replace(b'\r\n', b'\n'))
    name = f'aquabeacon-child-only-{experiment}-{RUN}'.replace('_', '-')
    binary = ROOT / 'data/interim/orbslam3_bin/mono_euroc_empty_safe'
    command = [DOCKER, 'run', '--rm', '--name', name, '--cpus', '4', '--memory', '4g',
               '--pids-limit', '128', '--network', 'none', '-e', 'OMP_NUM_THREADS=2',
               '-e', 'OPENBLAS_NUM_THREADS=1', '-e', 'OPENCV_FOR_THREADS_NUM=2',
               '-e', 'ORB_IMAGE_EXTENSION=.jpg',
               '--mount', f'type=bind,source={dataset},target=/dataset,readonly',
               '--mount', f'type=bind,source={config},target=/settings.yaml,readonly',
               '--mount', f'type=bind,source={binary},target=/mono_euroc,readonly',
               '--mount', f'type=bind,source={output},target=/work', IMAGE,
               '/mono_euroc', '/opt/ORB_SLAM3/Vocabulary/ORBvoc.txt', '/settings.yaml',
               '/dataset', '/dataset/timestamps.txt']
    manifest = {'experiment_id': experiment, 'run': RUN, 'sensor_mode': 'Monocular',
                'dataset': str(dataset.relative_to(ROOT)), 'export': export,
                'container_name': name, 'parent_observations_used': False,
                'imu_input_to_slam': False, 'absolute_attitude_input_to_slam': False,
                'git_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'],
                                                     cwd=ROOT, text=True).strip(),
                'worktree_dirty': True,
                'inputs_sha256': {str(p.relative_to(ROOT)): sha(p) for p in
                                  (config, calibration, binary, Path(__file__),
                                   dataset/'export_manifest.json', dataset/'frames.csv', timestamps,
                                   ROOT/'docker/orbslam3/finish_empty_map.py')},
                'started_utc': datetime.now(UTC).isoformat()}
    write_json(path, manifest)
    try:
        process = owned(command, output/'run.log', 7200, f'{experiment} ORB-SLAM3 monocular')
        manifest['process'] = process
        manifest['exit_code'] = process['returncode']
    finally:
        # Target only the name created by this run; stop it if the client timed out.
        inspected = subprocess.run([DOCKER, 'inspect', name], capture_output=True,
                                   text=True, timeout=15, check=False)
        if inspected.returncode == 0:
            info = json.loads(inspected.stdout)[0]
            if info['Config']['Image'] == IMAGE:
                subprocess.run([DOCKER, 'stop', '--time', '10', name], capture_output=True,
                               timeout=30, check=False)
        manifest['ended_utc'] = datetime.now(UTC).isoformat()
        write_json(path, manifest)
    if manifest['exit_code']:
        raise RuntimeError(f'{experiment}: ORB-SLAM3 failed; see {output / "run.log"}')


def main():
    global RUN
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment', action='append', required=True,
                        choices=[f'20261028_ngt_{i:03}' for i in range(1, 5)])
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--clock-only', action='store_true')
    parser.add_argument('--clahe', action='store_true', help='Separate contrast-enhanced trial')
    args = parser.parse_args()
    if args.clahe:
        RUN = 'okinawa_native120_clahe3'
    BASE.mkdir(parents=True, exist_ok=True)
    print(json.dumps({'pid': os.getpid(), 'started_utc': datetime.now(UTC).isoformat(),
                      'owner': 'ngt_child_only_vslam', 'experiments': args.experiment}), flush=True)
    print(json.dumps(clock_fit()), flush=True)
    config, calibration = settings()
    if args.clock_only:
        return
    if not args.prepare_only:
        subprocess.run([DOCKER, 'image', 'inspect', IMAGE], capture_output=True,
                       check=True, timeout=30)
    for experiment in args.experiment:
        dataset, export = prepare(experiment, calibration)
        if not args.prepare_only:
            run_slam(experiment, dataset, export, config, calibration)
        print(f'Finished {experiment}', flush=True)


if __name__ == '__main__':
    main()
