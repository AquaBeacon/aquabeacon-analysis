"""Preserve all independent child SLAM maps in an offline interactive viewer."""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import matplotlib
import numpy as np

matplotlib.use('Agg')
import matplotlib.pyplot as plt
from prepare_ngt_child_fusion import OUTPUT
from run_ngt_child_vslam import ROOT, sha, write_json

from aquabeacon_analysis.editable_plots import save_editable_figure


def read_maps(experiment, mode):
    directory = ROOT/f'results/{experiment}'
    folders = {'mono': 'child_only_vslam/okinawa_native120',
               'vi': 'child_sensor_fusion/vi30_bracketed',
               'camera_vi': 'child_sensor_fusion/camera_vi30_bracketed'}
    run = directory/folders[mode]
    manifest = run/'MapPoints_manifest.csv'
    if not manifest.exists():
        return []
    with manifest.open() as handle:
        records = list(csv.DictReader(handle))
    nonempty = [r for r in records if int(r['keyframes']) > 0]
    largest = max(nonempty, key=lambda r: int(r['keyframes'])) if nonempty else None
    result = []
    inputs = {}
    for record in nonempty:
        path = run/record['ply_file']
        contents = path.read_text().split('end_header\n', 1)[1]
        points = np.array([[float(v) for v in row.split()] for row in contents.splitlines() if row])
        if len(points) != int(record['map_points']):
            raise ValueError('Point count mismatch')
        inputs[str(path.relative_to(ROOT))] = sha(path)
        trajectory = []
        if mode != 'mono':
            with (run/'KeyFrameMapPoses.csv').open() as handle:
                poses = [r for r in csv.DictReader(handle) if r['map_id'] == record['map_id']]
            poses.sort(key=lambda r: float(r['synthetic_ns']))
            for i, row in enumerate(poses):
                if i and float(row['synthetic_ns'])-float(poses[i-1]['synthetic_ns']) > 1e9:
                    trajectory.append(None)
                trajectory.append([float(row[k]) for k in 'xyz'])
        elif record == largest and (run/'KeyFrameTrajectory.txt').stat().st_size:
            # ORB's keyframe export is in the original map frame, unlike frame export.
            poses = np.atleast_2d(np.loadtxt(run/'KeyFrameTrajectory.txt'))
            for i, pose in enumerate(poses):
                if i and pose[0]-poses[i-1, 0] > 1e9:
                    trajectory.append(None)
                trajectory.append(pose[1:4].tolist())
        initialized = mode != 'mono' and record.get('imu_initialized') == '1'
        note = ('独立した地図です。他の地図と同じ位置・尺度には揃えていません。'
                + ('慣性初期化済みですが、位置精度・尺度は独立に未検証です。' if initialized
                   else '距離尺度は未確定です。'))
        if experiment.endswith('001'):
            note += ' 空中映像の参考試行です。'
        if experiment.endswith('003'):
            note += ' 元の単眼追跡成功区間は主に水面上です。'
        label = {'mono': '単眼', 'vi': 'VI-SLAM', 'camera_vi': 'カメラIMU VI-SLAM'}[mode]
        result.append({'label': experiment[-3:]+' / '+label
                       +' / 地図 '+record['map_id'], 'experiment_id': experiment,
                       'mode': mode, 'map_id': record['map_id'], 'points': points.tolist(),
                       'trajectory': trajectory, 'unit': '推定メートル尺度（未検証）' if initialized
                       else '任意SLAM単位（mではありません）', 'note': note,
                       'source': str(path.relative_to(ROOT)).replace('\\', '/'),
                       'default': experiment.endswith('004') and mode == 'mono' and record == largest})
    if not nonempty:
        result.append({'label': experiment[-3:]+' / '+mode+' / 地図なし', 'points': [],
                       'trajectory': [], 'unit': '初期化不成立', 'note': '欠損や失敗を保持しています。',
                       'source': str(run.relative_to(ROOT)), 'experiment_id': experiment,
                       'mode': mode, 'map_id': None})
    output = directory/'child_sensor_fusion'
    output.mkdir(parents=True, exist_ok=True)
    if result and nonempty:
        selected = max(result, key=lambda r: len(r['points']))
        points = np.asarray(selected['points'])
        trajectory = np.array([p if p is not None else [np.nan]*3 for p in selected['trajectory']])
        fig, axes = plt.subplots(1, 3, figsize=(12, 4))
        for ax, (a, b) in zip(axes, [(0, 1), (0, 2), (1, 2)], strict=True):
            ax.scatter(points[:, a], points[:, b], s=2, alpha=.6, color='#286b91', label='Map points')
            if len(trajectory):
                ax.plot(trajectory[:, a], trajectory[:, b], color='#df7c32', lw=.8,
                        label='Keyframe camera path (gaps >1 s broken)')
            units = 'm, unvalidated' if '推定メートル' in selected['unit'] else 'SLAM units'
            ax.set(xlabel=f'Map {"xyz"[a]} ({units})', ylabel=f'Map {"xyz"[b]} ({units})')
            ax.set_aspect('equal', adjustable='datalim')
            ax.grid(alpha=.2)
        axes[0].legend(fontsize=7)
        fig.suptitle(experiment+' / '+mode+' map '+selected['map_id']+' / unverified scale')
        fig.tight_layout()
        save_editable_figure(fig, output/f'{mode}_map_overview.png', dpi=150)
        plt.close(fig)
    write_json(output/f'{mode}_map_inputs.json', inputs)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment', action='append', required=True,
                        choices=[f'20261028_ngt_{i:03}' for i in range(1, 5)])
    args = parser.parse_args()
    data = [item for exp in args.experiment for mode in ['mono', 'vi', 'camera_vi']
            for item in read_maps(exp, mode)]
    template = ROOT/'src/aquabeacon_analysis/child_map_viewer.html'
    html = template.read_text(encoding='utf-8').replace('__MAP_DATA__', json.dumps(data, ensure_ascii=False))
    (OUTPUT/'maps.html').write_text(html, encoding='utf-8')
    write_json(OUTPUT/'maps.json', data)
    for experiment in args.experiment:
        per = [item for item in data if item['experiment_id'] == experiment]
        (ROOT/f'results/{experiment}/child_sensor_fusion/maps.html').write_text(
            template.read_text(encoding='utf-8').replace('__MAP_DATA__', json.dumps(per, ensure_ascii=False)),
            encoding='utf-8')
    write_json(OUTPUT/'map_viewer_manifest.json', {
        'parent_observations_used': False, 'no_map_registration': True,
        'map_entries': len(data), 'points': sum(len(r['points']) for r in data),
        'script_sha256': sha(Path(__file__)), 'template_sha256': sha(template)})
    print(OUTPUT/'maps.html')


if __name__ == '__main__':
    main()
