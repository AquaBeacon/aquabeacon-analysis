"""Evaluate NGT visual SLAM against child attitude only, preserving all failures."""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import matplotlib
import numpy as np

matplotlib.use('Agg')
import matplotlib.pyplot as plt
from run_ngt_child_vslam import BASE, CAMERA, FPS, ROOT, RUN, STAMP_ORIGIN, sha, write_json

from aquabeacon_analysis.child_vslam import (
    rotation_angle_difference,
    sample_rotations,
    spans,
    statistics,
)
from aquabeacon_analysis.editable_plots import save_editable_figure
from aquabeacon_analysis.ngt_sync import numeric_csv


def reference():
    clock = json.loads((BASE/'child_clock.json').read_text(encoding='utf-8'))
    source = ROOT/'data/processed/20261028_ngt_000/child_magnetic_attitude/attitude.csv'
    with source.open(encoding='utf-8') as handle:
        rows = list(csv.DictReader(handle))
    times = np.array([float(r['acquired_us'])/1e6 for r in rows])
    # Ignore UTC and parent_video_time_s: numeric attitude depends only on child sensors.
    result = {}
    for mode, prefix, flag in [('magnetic', 'magnetic_ned_', 'absolute_attitude_available'),
                               ('local', 'local_', 'attitude_usable')]:
        q = np.array([[float(r[prefix+k]) if r[prefix+k] else np.nan
                       for k in ['qx', 'qy', 'qz', 'qw']] for r in rows])
        q[[r[flag].lower() != 'true' for r in rows]] = np.nan
        result[mode] = (times, q)
    camera = numeric_csv(CAMERA, ['video_time_s', 'qx', 'qy', 'qz', 'qw'])
    result['camera_imu'] = (camera[:, 0], camera[:, 1:])
    return clock, result, source


def evaluate(experiment, clock, references, attitude_source, run_name=RUN):
    parent = ROOT/f'results/{experiment}/child_only_vslam'
    output = parent if run_name == RUN else parent/(run_name+'_evaluation')
    output.mkdir(parents=True, exist_ok=True)
    run = parent/run_name
    manifest_path = run/'run_manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    dataset = ROOT/manifest['dataset']
    frame = numeric_csv(dataset/'frames.csv', ['synthetic_ns', 'source_frame', 'source_video_s'])
    t = frame[:, 2]
    n = len(t)
    state = np.full(n, -99, int)  # -99 means no engine record, never treated as tracked
    detected, associated = np.full(n, np.nan), np.full(n, np.nan)
    malformed_tracking_rows = 0
    if (run/'TrackedFrameSummary.csv').exists():
        with (run/'TrackedFrameSummary.csv').open() as handle:
            for row in csv.DictReader(handle):
                if any(row.get(k) is None for k in
                       ('frame_index', 'tracking_state', 'detected_features', 'associated_features')):
                    malformed_tracking_rows += 1
                    continue
                i = int(row['frame_index'])
                if not 0 <= i < n:
                    raise ValueError('Tracking summary frame outside input')
                state[i] = int(row['tracking_state'])
                detected[i], associated[i] = int(row['detected_features']), int(row['associated_features'])
    pose = np.full((n, 7), np.nan)
    trajectory = run/'CameraTrajectory.txt'
    if trajectory.exists() and trajectory.stat().st_size:
        rows = np.atleast_2d(np.loadtxt(trajectory))
        query = (rows[:, 0]-STAMP_ORIGIN)/1e9
        index = np.clip(np.searchsorted(t, query), 0, n-1)
        previous = np.maximum(index-1, 0)
        index[np.abs(t[previous]-query) < np.abs(t[index]-query)] = previous[
            np.abs(t[previous]-query) < np.abs(t[index]-query)]
        if np.max(np.abs(t[index]-query)) > .002:
            raise ValueError('Trajectory timestamp mismatch')
        pose[index] = rows[:, 1:]
    saved = np.isfinite(pose).all(axis=1)
    masks = (state == 2) & saved
    metrics, columns = {}, {}
    review_path = ROOT/'config/20261028_ngt/child_vslam_media_review.json'
    review = json.loads(review_path.read_text(encoding='utf-8'))[experiment]
    body_t = clock['offset_s']+clock['time_scale']*t
    for name, (rt, rq) in references.items():
        q = sample_rotations(rt, rq, t if name == 'camera_imu' else body_t)
        for seconds in (1, 5):
            lag = round(seconds*FPS)
            va, ra, error = rotation_angle_difference(pose[:, 3:], q, masks, lag)
            label = f'{name}_{seconds}s'
            metrics[label] = {**statistics(error), 'actual_interval_s': lag/FPS,
                              'reference_available_frames': int(np.isfinite(q).all(axis=1).sum())}
            columns[label+'_visual_angle_deg'] = va
            columns[label+'_reference_angle_deg'] = ra
            columns[label+'_difference_deg'] = error
    with (output/'frame_evaluation.csv').open('w', encoding='utf-8', newline='') as handle:
        writer = csv.writer(handle)
        writer.writerow(['source_video_s', 'source_frame', 'tracking_state', 'saved_largest_map',
                         'detected_features', 'associated_features', 'x_slam', 'y_slam', 'z_slam',
                         'qx', 'qy', 'qz', 'qw', *columns])
        for i in range(n):
            values = [t[i], int(frame[i, 1]), int(state[i]), bool(saved[i]), detected[i],
                      associated[i], *pose[i], *[v[i] for v in columns.values()]]
            writer.writerow(['' if isinstance(v, float) and not np.isfinite(v) else v for v in values])
    maps = []
    if (run/'MapPoints_manifest.csv').exists():
        with (run/'MapPoints_manifest.csv').open() as handle:
            maps = list(csv.DictReader(handle))
    summary = {'experiment_id': experiment, 'run': run_name, 'exit_code': manifest.get('exit_code'),
               'input_frames': n, 'duration_s': n/FPS, 'tracked_frames': int((state == 2).sum()),
               'tracked_fraction': float((state == 2).mean()), 'saved_pose_frames': int(saved.sum()),
               'saved_pose_fraction': float(saved.mean()),
               'unprocessed_frames': int((state == -99).sum()), 'state_spans': spans(state),
               'malformed_tracking_rows': malformed_tracking_rows,
               'median_detected_features': float(np.nanmedian(detected)),
               'tracking_state_counts': {str(int(s)): int((state == s).sum())
                                         for s in np.unique(state)},
               'tracked_duration_s': float((state == 2).sum()/FPS),
               'initialization_wait_s': float((state == 1).sum()/FPS),
               'recently_lost_duration_s': float((state == 3).sum()/FPS),
               'rotation_pair_statistics': 'Overlapping temporal windows; sample count '
                                           'does not represent independent trials',
               'map_count': sum(int(r['keyframes']) > 0 for r in maps),
               'atlas_map_count_including_empty': len(maps),
               'map_points': sum(int(r['map_points']) for r in maps),
               'rotation_angle_magnitude_consistency': metrics,
               'position_ate_m': None, 'position_rpe_m': None, 'absolute_orientation_error': None,
               'reason': 'No independent position GT, monocular scale unknown, optical/body '
                         'extrinsic uncalibrated. Angle magnitude difference is NOT full SO(3) '
                         'error or true-north accuracy. Reference magnetic heading unvalidated.',
               'parent_observations_used': False,
               'reference_policy': 'Child acquired_us and absolute_attitude_available only; '
                                   'no parent-derived time columns; no quality rejection of heading.',
               'inputs_sha256': {str(p.relative_to(ROOT)): sha(p) for p in
                                 (manifest_path, attitude_source, CAMERA, BASE/'child_clock.json',
                                  Path(__file__), ROOT/'src/aquabeacon_analysis/child_vslam.py')},
               'known_missing_video_s': max(0, 240-n/FPS) if experiment.endswith('001') else 0,
               'medium_note': review['note'],
               'medium_review': review,
               'tracking_state_labels': {'-99': 'not processed', '0': 'no images',
                                         '1': 'not initialized', '2': 'OK',
                                         '3': 'recently lost', '4': 'lost'},
               'saved_pose_scope': 'largest Atlas map only; not_saved is not tracking_lost',
               'pair_selection': 'all intermediate frames must be tracked and saved, with '
                                 'finite reference; SLERP gaps <=0.1s; no extrapolation'}
    if review.get('conservative_underwater_windows_s'):
        water = np.zeros(n, bool)
        for start, end in review['conservative_underwater_windows_s']:
            water |= (t >= start) & (t < end)
        summary['reviewed_underwater_windows'] = {
            'frames': int(water.sum()), 'tracked_frames': int(((state == 2) & water).sum()),
            'tracked_fraction': float((state[water] == 2).mean()),
            'policy': 'Sample-supported conservative windows; transition times unclassified'}
    summary['inputs_sha256'][str(review_path.relative_to(ROOT))] = sha(review_path)
    write_json(output/'summary.json', summary)
    fig, axes = plt.subplots(3, 1, figsize=(11, 8), sharex=True)
    axes[0].step(t-t[0], state, where='post', label='ORB-SLAM3 state')
    axes[0].set_yticks([-99, 1, 2, 3, 4] if (state == -99).any() else [1, 2, 3, 4])
    axes[0].set_ylabel('Tracking state')
    axes[1].plot(t-t[0], associated, label='Associated map points', lw=.7)
    axes[1].set_ylabel('Features')
    for name in references:
        axes[2].plot(t-t[0], columns[f'{name}_1s_difference_deg'], label=name, lw=.7)
    if not any(np.isfinite(columns[f'{name}_1s_difference_deg']).any() for name in references):
        axes[2].text(.5, .5, 'No valid paired rotations', ha='center',
                     transform=axes[2].transAxes)
    axes[2].set_ylabel('1 s angle magnitude\ndifference (deg)')
    axes[2].set_xlabel('Time since first available child video frame (s)')
    for ax in axes:
        if review.get('conservative_air_window_s'):
            air_start, air_end = review['conservative_air_window_s']
            ax.axvspan(air_start-t[0], air_end-t[0], color='#e6a54a', alpha=.18,
                       label='Above water (review)')
        ax.grid(alpha=.25)
        ax.legend(loc='upper right')
    axes[0].set_title(experiment+' / child-only monocular SLAM')
    fig.tight_layout()
    save_editable_figure(fig, output/'evaluation.png', dpi=150)
    plt.close(fig)
    if saved.sum():
        fig, axes = plt.subplots(1, 2, figsize=(11, 4))
        xyz = pose[:, :3]
        axes[0].plot(xyz[:, 0], xyz[:, 2], lw=.7)
        axes[0].set(xlabel='SLAM x (arbitrary units)', ylabel='SLAM z (arbitrary units)')
        axes[0].axis('equal')
        for j, name in enumerate('xyz'):
            axes[1].plot(t-t[0], xyz[:, j], label=name, lw=.7)
        axes[1].set(xlabel='Child video interval time (s)', ylabel='SLAM position (arbitrary units)')
        axes[1].legend()
        fig.suptitle(experiment+' / largest map; metric scale unknown')
        fig.tight_layout()
        save_editable_figure(fig, output/'trajectory.png', dpi=150)
        plt.close(fig)
    if maps and max(int(r['map_points']) for r in maps) > 0:
        selected = max(maps, key=lambda r: int(r['keyframes']))
        point_path = run/selected['ply_file']
        text = point_path.read_text().split('end_header\n', 1)[1]
        xyz = np.array([[float(v) for v in line.split()] for line in text.splitlines() if line])
        if len(xyz) != int(selected['map_points']):
            raise ValueError('Map point count mismatch')
        fig, axes = plt.subplots(1, 3, figsize=(12, 4))
        for ax, (a, b) in zip(axes, [(0, 1), (0, 2), (1, 2)], strict=True):
            ax.scatter(xyz[:, a], xyz[:, b], s=3, alpha=.5)
            ax.set(xlabel=f'Map {"xyz"[a]} (arbitrary units)',
                   ylabel=f'Map {"xyz"[b]} (arbitrary units)')
            ax.set_aspect('equal', adjustable='datalim')
            ax.grid(alpha=.2)
        fig.suptitle(f'{experiment} / map {selected["map_id"]} / {len(xyz)} points')
        fig.tight_layout()
        save_editable_figure(fig, output/'map_projections.png', dpi=150)
        plt.close(fig)
    metric = metrics['magnetic_1s']
    lines = [f'# {experiment} 子機のみ単眼VSLAM', '',
             f'ORB-SLAM3 / {run_name}。親機観測データの使用なし。沖縄水中内部校正はユーザー指定の共通校正。',
             '', f'- 入力: {n:,} フレーム、{n/FPS:.3f}秒、120000/1001 fps（間引きなし）。',
             f'- 追跡状態OK: {(state == 2).mean():.2%}。最大地図に保存された姿勢: {saved.mean():.2%}。',
             (f'- キーフレームを持つ地図数: {summary["map_count"]}。'
              f'点数合計: {summary["map_points"]:,}（地図間座標は非連続）。'),
             f'- 未処理フレーム: {summary["unprocessed_frames"]}。終了コード: {manifest.get("exit_code")}。',
             (f'- 磁北基準の子機姿勢との1秒回転角変化量の差: RMSE {metric["rmse_deg"]}度、'
             f'標本数 {metric["samples"]}。'), '',
             ('回転差は2姿勢間の回転角の**大きさ**の差。光学座標と機体座標の固定回転が未校正のため、'
             '全3軸の姿勢誤差や絶対方位精度とは呼ばない。磁北推定自体も独立精度未検証。'
             '位置の真値とメートル尺度がないためATE/RPE[m]は算出しない。'), '',
             ('時刻は子機カメラと子機IMUの角運動で同期。親機時刻列・GNSS・距離・姿勢・画像は評価に使わない。'
             '実験区間の既存選択境界だけを子機テレメトリから引き継ぐ。'), '',
             ('追跡喪失・初期化待ち・最大地図への保存欠損を区別して全入力行を残す。'
             '欠損を跨ぐ回転比較は行わない。'), '',
             ('[評価CSV](frame_evaluation.csv) / [詳細JSON](summary.json) / '
             '[評価図](evaluation.png) / [編集マスター](evaluation.figure.pickle)'), '',
             ('編集: `python -m aquabeacon_analysis.editable_plots '
             f'results/{experiment}/child_only_vslam/evaluation.figure.pickle`。'
             'ブラウザで変更、設定保存・再読込、PNG/SVG/PDF出力が可能。'), '',
             '[全実験比較](../../20261028_ngt_000/child_only_vslam/report.md)']
    if run_name != RUN:
        lines[-1] = '[追加試行の比較](../../../20261028_ngt_000/child_only_vslam/clahe_report.md)'
        lines += ['', ('画像補正: LABのL成分のみCLAHE（clipLimit=3、8×8）、JPEG95。'
                  '元画像と同じフレームと時刻を保持した別試行。')]
    if experiment.endswith('001'):
        lines += ['', (f'001は子機動画が約{summary["known_missing_video_s"]:.1f}秒不足し、'
                  'この撮影区間に83304の子機IMUは重ならない。カメラ内IMUだけの比較も別記する。'
                  '原動画95.96秒の代表画像はプールサイドの空中撮影であり、水中内部校正と'
                  '撮影媒質が一致しない。001は診断用結果として扱う。')]
    if experiment.endswith('003'):
        lines += ['', ('映像確認：追跡区間906.539〜959.442秒は水面上の場面に対応。'
                  'この角度差を水中追跡精度と解釈しない。'
                  '[代表画像](medium_review.png)。保守的な水中確認窓の追跡率は '
                  f'{summary["reviewed_underwater_windows"]["tracked_fraction"]:.1%}。')]
    if maps and max(int(r['map_points']) for r in maps) > 0:
        lines += ['', ('[最大地図の投影図](map_projections.png) / '
                  '[編集マスター](map_projections.figure.pickle)。地図の座標は任意のSLAM単位。')]
    (output/'report.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment', action='append', required=True)
    parser.add_argument('--run', default=RUN, choices=[RUN, RUN+'_clahe3'])
    args = parser.parse_args()
    clock, refs, source = reference()
    summaries = [evaluate(exp, clock, refs, source, args.run) for exp in args.experiment]
    prefix = '' if args.run == RUN else 'clahe_'
    fields = ['experiment_id', 'input_frames', 'duration_s', 'tracked_fraction',
              'saved_pose_fraction', 'map_count', 'map_points', 'unprocessed_frames']
    with (BASE/(prefix+'comparison.csv')).open('w', encoding='utf-8', newline='') as handle:
        writer = csv.writer(handle)
        writer.writerow(fields+['magnetic_1s_rmse_deg', 'magnetic_1s_samples',
                                'local_1s_rmse_deg', 'camera_imu_1s_rmse_deg'])
        for row in summaries:
            metric = row['rotation_angle_magnitude_consistency']
            writer.writerow([row[k] for k in fields]+[metric['magnetic_1s']['rmse_deg'],
                             metric['magnetic_1s']['samples'], metric['local_1s']['rmse_deg'],
                             metric['camera_imu_1s']['rmse_deg']])
    lines = ['# 長田高校プール：子機のみの単眼VSLAM', '',
             ('各実験を独立した地図で処理。画像だけをORB-SLAM3へ入力し、子機姿勢は評価に使用。'
             '長田高校の親機観測データは未使用。沖縄の共有内部校正は2026-10-30ユーザー指定で適用。'), '',
             '| 実験 | 入力枚数 | 追跡OK | 最大地図保存率 | 地図数 | 磁気姿勢との1秒角度変化量差RMSE |',
             '|---|---:|---:|---:|---:|---:|']
    for s in summaries:
        m = s['rotation_angle_magnitude_consistency']['magnetic_1s']['rmse_deg']
        value = f'{m:.3f}°' if m is not None else '評価不可'
        subdir = '' if args.run == RUN else args.run+'_evaluation/'
        lines.append(f'| [{s["experiment_id"]}](../../{s["experiment_id"]}/child_only_vslam/'
                     f'{subdir}report.md) '
                     f'| {s["input_frames"]:,} | {s["tracked_fraction"]:.1%} '
                     f'| {s["saved_pose_fraction"]:.1%} | {s["map_count"]} | {value} |')
    lines += ['', ('この角度差は回転角の大きさの一致性で、全3軸の姿勢誤差・真値誤差ではない。'
              '単眼の尺度と位置のGround Truthがないため位置ATE/RPE[m]は評価不可。'
              '磁北姿勢は使用可能フラグで選び、未検証であることを理由に除外していない。'), '',
              (f'子機だけの時計推定：acquired_s = {clock["offset_s"]:.9f} + '
              f'{clock["time_scale"]:.12f} × 子機動画秒。採用窓残差最大 '
              f'{clock["max_window_residual_s"]*1000:.3f} ms。これは外部時計精度ではない。'), '',
              ('[比較CSV](comparison.csv)。各実験に全フレーム評価CSV、地図別PLY、実行ログ、'
              'manifest、PNG/SVG/ブラウザ編集用figure.pickleとplot.jsonを保存。'), '',
              ('実行基盤: [ORB-SLAM3](https://github.com/UZ-SLAMLab/ORB_SLAM3)、'
              '固定commit 4452a3c。既存の地図・特徴点出力パッチを使用。')]
    lines += ['', ('映像条件：001は空中のプールサイド映像を含み、子機本体IMUと重複しない。'
              '003の追跡区間（子機原動画906.539〜959.442秒）は水面上の確認画像に対応し、'
              '保守的な水中確認窓での追跡率は0%。この2試行の値を水中の精度として扱わない。')]
    if not prefix and (BASE/'clahe_comparison.csv').exists():
        lines += ['', ('[コントラスト補正の追加試行](clahe_report.md)：002の同じ7,192フレームで'
                  'LAB L成分CLAHE（clipLimit=3）を試したが、追跡率0%、地図0個。'
                  '元画像の失敗結果と補正後の失敗結果をともに保存。')]
    if prefix:
        lines += ['', '追加試行: LAB L成分のCLAHE（clipLimit=3、8×8）、JPEG95。元画像の結果を保持。']
        lines = [line.replace('[比較CSV](comparison.csv)', '[比較CSV](clahe_comparison.csv)')
                 for line in lines]
    (BASE/(prefix+'report.md')).write_text('\n'.join(lines)+'\n', encoding='utf-8')
    print(json.dumps([{k: s[k] for k in fields} for s in summaries], indent=2))


if __name__ == '__main__':
    main()
