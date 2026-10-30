"""Run explicitly selected child-only VI-SLAM trials with bounded resources."""
from __future__ import annotations

import argparse
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from prepare_ngt_child_fusion import OUTPUT
from run_ngt_child_vslam import DOCKER, IMAGE, ROOT, owned, sha, write_json


def build():
    binary_dir = ROOT/'data/interim/orbslam3_bin'
    command = [DOCKER, 'run', '--rm', '--name', 'aquabeacon-child-vi-build',
               '--cpus', '2', '--memory', '3g', '--network', 'none',
               '--mount', f'type=bind,source={binary_dir},target=/input',
               '--mount', (f'type=bind,source={ROOT / "docker/orbslam3/prepare_child_vi_adapter.py"},'
                           'target=/patch.py,readonly'), IMAGE, 'bash', '-lc',
               ('python3 /patch.py && cmake --build /opt/ORB_SLAM3/build --target mono_euroc '
               '--parallel 2 && cp /opt/ORB_SLAM3/Examples/Monocular/mono_euroc '
               '/input/child_vi_euroc_bracketed && cp /opt/ORB_SLAM3/Examples/Monocular/mono_euroc.cc '
                '/input/child_vi_euroc_bracketed.cc')]
    result = owned(command, OUTPUT/'vi_bracketed_build.log', 300,
                   'child-only VI diagnostic adapter with real IMU bracket')
    if result['returncode']:
        raise RuntimeError('VI adapter build failed')


def run(experiment, camera_imu=False):
    dataset_name = 'camera_vi30' if camera_imu else 'vi30'
    run_name = 'camera_vi30_bracketed' if camera_imu else 'vi30_bracketed'
    dataset = ROOT/f'data/interim/{experiment}/child_sensor_fusion/{dataset_name}'
    output = ROOT/f'results/{experiment}/child_sensor_fusion/{run_name}'
    output.mkdir(parents=True, exist_ok=True)
    manifest_path = output/'run_manifest.json'
    if manifest_path.exists():
        print('Preserving existing run '+str(manifest_path), flush=True)
        return
    export = json.loads((dataset/'export_manifest.json').read_text())
    images = ROOT/export['image_source']/'mav0/cam0/data'
    binary = ROOT/'data/interim/orbslam3_bin/child_vi_euroc_bracketed'
    name = 'aquabeacon-child-vi-'+experiment.replace('_', '-')+('-camera' if camera_imu else '')
    command = [DOCKER, 'run', '--rm', '--name', name, '--cpus', '4', '--memory', '4g',
               '--pids-limit', '128', '--network', 'none', '-e', 'OMP_NUM_THREADS=2',
               '-e', 'OPENBLAS_NUM_THREADS=1', '-e', 'ORB_IMAGE_EXTENSION=.jpg',
               '--mount', f'type=bind,source={dataset},target=/dataset,readonly',
               '--mount', f'type=bind,source={images},target=/dataset/mav0/cam0/data,readonly',
               '--mount', f'type=bind,source={binary},target=/child_vi,readonly',
               '--mount', f'type=bind,source={output},target=/work', IMAGE,
               '/child_vi', '/opt/ORB_SLAM3/Vocabulary/ORBvoc.txt', '/dataset/settings.yaml',
               '/dataset', '/dataset/timestamps.txt']
    manifest = {'experiment_id': experiment, 'sensor_mode': 'IMU_MONOCULAR', 'export': export,
                'parent_observations_used': False, 'pressure_used_in_slam': False,
                'imu_packet_policy': 'includes first real sample after each image timestamp; '
                                     'offline lookahead, no interpolation or duplication',
                'container_name': name, 'started_utc': datetime.now(UTC).isoformat(),
                'trajectory_frame': 'SaveTrajectoryEuRoC exports IMU body, not optical camera; '
                                    'KeyFrameMapPoses.csv explicitly exports optical camera poses',
                'git_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'],
                                                     cwd=ROOT, text=True).strip(),
                'inputs_sha256': {str(p.relative_to(ROOT)): sha(p) for p in
                                 [binary, dataset/'export_manifest.json', Path(__file__),
                                  ROOT/'docker/orbslam3/prepare_child_vi_adapter.py']}}
    write_json(manifest_path, manifest)
    try:
        result = owned(command, output/'run.log', 2400, experiment+' child-only VI-SLAM')
        manifest.update(exit_code=result['returncode'], process=result)
    finally:
        inspection = subprocess.run([DOCKER, 'inspect', name], capture_output=True,
                                    text=True, timeout=15, check=False)
        if inspection.returncode == 0:
            info = json.loads(inspection.stdout)[0]
            if info['Config']['Image'] == IMAGE:
                subprocess.run([DOCKER, 'stop', '--time', '10', name],
                               capture_output=True, timeout=30, check=False)
        manifest['ended_utc'] = datetime.now(UTC).isoformat()
        write_json(manifest_path, manifest)
    print(json.dumps({'experiment': experiment, 'exit_code': manifest.get('exit_code')}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment', action='append',
                        choices=[f'20261028_ngt_{i:03}' for i in range(1, 5)])
    parser.add_argument('--build', action='store_true')
    parser.add_argument('--camera-imu', action='store_true')
    args = parser.parse_args()
    if not args.build and not args.experiment:
        parser.error('Explicit experiments or --build required')
    if args.build:
        build()
    for experiment in args.experiment or []:
        run(experiment, args.camera_imu)


if __name__ == '__main__':
    main()
