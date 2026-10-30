"""Summarize every VI input frame and child-only fusion without hiding failures."""
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

from aquabeacon_analysis.child_vslam import rotation_angle_difference, sample_rotations, statistics
from aquabeacon_analysis.editable_plots import save_editable_figure
from aquabeacon_analysis.ngt_sync import numeric_csv


def evaluate_vi(experiment, variant='vi30_bracketed'):
    output = ROOT/f'results/{experiment}/child_sensor_fusion'
    run = output/variant
    manifest = json.loads((run/'run_manifest.json').read_text())
    dataset_name = 'camera_vi30' if variant.startswith('camera') else 'vi30'
    frame_path = ROOT/f'data/interim/{experiment}/child_sensor_fusion/{dataset_name}/frames.csv'
    frames = numeric_csv(frame_path, ['synthetic_ns', 'source_video_s'])
    n = len(frames)
    state = np.full(n, -99, int)
    initialized = np.zeros(n, bool)
    map_id = np.full(n, -1, int)
    samples = np.zeros(n, int)
    with (run/'TrackedFrameSummary.csv').open() as handle:
        for row in csv.DictReader(handle):
            if row.get('imu_samples') is None:
                continue
            i = int(row['frame_index'])
            state[i], initialized[i] = int(row['tracking_state']), row['imu_initialized'] == '1'
            map_id[i], samples[i] = int(row['map_id']), int(row['imu_samples'])
    pose = np.full((n, 7), np.nan)
    path = run/'CameraTrajectory.txt'
    if path.exists() and path.stat().st_size:
        trajectory = np.atleast_2d(np.loadtxt(path))
        t = (trajectory[:, 0]-10**18)/1e9
        j = np.clip(np.searchsorted(frames[:, 1], t), 0, n-1)
        prev = np.maximum(0, j-1)
        use = np.abs(frames[prev, 1]-t) < np.abs(frames[j, 1]-t)
        j[use] = prev[use]
        if np.max(np.abs(frames[j, 1]-t)) > .002:
            raise ValueError('VI trajectory timestamp does not match input frame')
        pose[j] = trajectory[:, 1:]
    maps = []
    if (run/'MapPoints_manifest.csv').exists():
        with (run/'MapPoints_manifest.csv').open() as handle:
            maps = list(csv.DictReader(handle))
    log = (run/'run.log').read_text(errors='replace')
    metrics = {}
    sensor = np.load(OUTPUT/'sensors.npz')
    for mode in ['magnetic', 'local']:
        q = sample_rotations(sensor['video_s'], sensor[mode+'_xyzw'], frames[:, 1])
        _, _, error = rotation_angle_difference(pose[:, 3:], q, (state == 2) & initialized, 30)
        metrics[mode] = statistics(error)
    summary = {'experiment_id': experiment, 'variant': variant, 'input_frames': n,
               'processed_frames': int((state != -99).sum()),
               'unprocessed_frames': int((state == -99).sum()),
               'tracked_frames': int((state == 2).sum()),
               'tracked_fraction': float((state == 2).mean()),
               'imu_initialized_frames': int(initialized.sum()),
               'tracked_and_imu_initialized_frames': int(((state == 2) & initialized).sum()),
               'saved_pose_frames': int(np.isfinite(pose).all(1).sum()),
               'surviving_nonempty_maps': sum(int(r['keyframes']) > 0 for r in maps),
               'surviving_imu_initialized_maps': sum(int(r['keyframes']) > 0
                                                    and r.get('imu_initialized') == '1' for r in maps),
               'points': sum(int(r['map_points']) for r in maps),
               'exit_code': manifest.get('exit_code'),
               'active_map_resets': log.count('SYSTEM-> Reseting active map'),
               'empty_preintegration_messages': log.count('Empty IMU measurements vector'),
               'one_second_rotation_angle_difference_deg': metrics,
               'parent_observations_used': False, 'pressure_input_to_slam': False,
               'absolute_position_accuracy': None,
               'inputs_sha256': {str(p.relative_to(ROOT)): sha(p) for p in
                                [run/'run_manifest.json', run/'TrackedFrameSummary.csv',
                                 frame_path, Path(__file__)]}}
    write_json(run/'summary.json', summary)
    with (run/'frame_evaluation.csv').open('w', newline='', encoding='utf-8') as handle:
        writer = csv.writer(handle)
        writer.writerow(['source_video_s', 'state', 'imu_initialized', 'map_id', 'imu_samples',
                         'sensor_x', 'sensor_y', 'sensor_z', 'qx', 'qy', 'qz', 'qw'])
        for i in range(n):
            writer.writerow([frames[i, 1], state[i], int(initialized[i]), map_id[i], samples[i],
                             *[float(x) if np.isfinite(x) else '' for x in pose[i]]])
    fig, axes = plt.subplots(2, 1, figsize=(10, 5), sharex=True)
    axes[0].step(frames[:, 1]-frames[0, 1], state, where='post', lw=.8, label='Tracking state')
    axes[0].set(yticks=[-99, 1, 2, 3, 4] if summary['unprocessed_frames'] else [1, 2, 3, 4],
                ylabel='1:init wait / 2:OK\n3:recently lost / 4:lost')
    axes[1].step(frames[:, 1]-frames[0, 1], initialized.astype(int), where='post',
                 label='Current map IMU initialized')
    axes[1].set(xlabel='Trial time (s)', ylabel='IMU initialized', yticks=[0, 1], ylim=[-.1, 1.1])
    for ax in axes:
        ax.grid(alpha=.25)
        ax.legend()
    axes[0].set_title(experiment+' / '+variant+' / all input frames retained')
    fig.tight_layout()
    save_editable_figure(fig, run/'tracking.png', dpi=150)
    plt.close(fig)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment', action='append', required=True,
                        choices=[f'20261028_ngt_{i:03}' for i in range(1, 5)])
    args = parser.parse_args()
    summaries = []
    camera_summaries = []
    report = ['# 長田高校：子機地図・深度融合・VI-SLAM', '',
              '親機の観測データは使用していない。既存の実験区間とユーザー指定の共通水中内部校正を使用。', '',
              '[全地図の3D表示](maps.html) / [地図データ](maps.json) / [センサと校正の来歴](sensor_manifest.json)', '',
              ('3D表示は地図切替・回転・拡大・移動・表示設定保存/再読込・PNG保存に対応。'
              '地図間の尺度や原点を統合していない。静的グラフはPNG/SVGと編集マスターを保存。'), '',
              '| 実験 | 単眼120 fps追跡率 | VI30 fps追跡率 | VI慣性初期化済みフレーム | 最終VI地図 | 終了 |',
              '|---|---:|---:|---:|---:|---:|']
    for experiment in args.experiment:
        parent = ROOT/f'results/{experiment}/child_sensor_fusion'
        current = evaluate_vi(experiment)
        if (parent/'vi30/run_manifest.json').exists():
            current['initial_trial'] = evaluate_vi(experiment, 'vi30')
        camera_run = parent/'camera_vi30_bracketed/run_manifest.json'
        if camera_run.exists():
            extra = evaluate_vi(experiment, 'camera_vi30_bracketed')
            current['camera_imu_trial'] = extra
            camera_summaries.append(extra)
        mono = json.loads((ROOT/f'results/{experiment}/child_only_vslam/summary.json').read_text())
        fusion = json.loads((parent/'fusion_summary.json').read_text())
        current['fusion'] = fusion
        summaries.append(current)
        report.append(f'| {experiment[-3:]} | {mono["tracked_fraction"]:.2%} | '
                      f'{current["tracked_fraction"]:.2%} | {current["imu_initialized_frames"]:,} | '
                      f'{current["surviving_nonempty_maps"]} | {current["exit_code"]} |')
        text = [f'# {experiment} 子機センサ融合', '',
                '[この実験の3D地図](maps.html) / [全実験レポート](../../20261028_ngt_000/child_sensor_fusion/report.md)', '',
                (f'VI-SLAM入力{current["input_frames"]:,}フレーム、追跡{current["tracked_fraction"]:.2%}。'
                f'慣性初期化済み{current["imu_initialized_frames"]}フレーム、'
                f'最終非空地図{current["surviving_nonempty_maps"]}個。未処理{current["unprocessed_frames"]}フレーム。'), '',
                ('[VI追跡図](vi30_bracketed/tracking.png) / [VI全フレームCSV](vi30_bracketed/frame_evaluation.csv) / '
                '[VI詳細](vi30_bracketed/summary.json)'), '',
                ('初回は実測IMUを画像時刻まで渡す方式。再試行では積分区間を挟む次の実測IMUも渡す。'
                '将来値の予測・架空のIMU観測の追加はしていない。初回の結果と失敗ログも保持。'), '',
                '[融合詳細](fusion_summary.json)']
        if fusion['available']:
            metrics = fusion['attitude_comparison']['magnetic']
            text += ['', ('[DR・深度融合の図](fusion_comparison.png) / [全時系列CSV](dead_reckoning.csv) / '
                     '[相対深度](pressure.csv)'), '',
                     (f'磁北姿勢での最長連続区間{metrics["longest_segment_s"]:.2f}秒。'
                     f'単純DRの終端変位ノルム{metrics["dr_longest_segment_endpoint_norm_m"]:.2f} m。'
                     f'深度融合後も水平終端変位{metrics["pressure_fused_longest_segment_endpoint_xy_norm_m"]:.2f} m。'), '',
                     (f'圧力との上下方向の差RMSE: DR {metrics["dr_pressure_disagreement_rmse_m"]:.3f} m、'
                     f'融合後 {metrics["fused_pressure_residual_rmse_m"]:.4f} m。'
                     'これは使用した圧力観測への残差であり、独立した精度評価ではない。'), '',
                     '映像と圧力からの尺度推定: '+fusion['visual_depth_scale']['reason']+'。'
                     f'採用した視覚速度更新は{fusion["visual_velocity_updates_applied"]}件。']
        else:
            text += ['', ('本体IMUと深度の時間重複なし。カメラ内蔵四元数から算出した角速度と'
                     'カメラ加速度をVI-SLAMに使用。空中・水中内部校正不一致の参考試行。')]
        text += ['', ('原点と初速度0は仮定。磁北方位・カメラ取付回転・レバーアームは独立精度未検証。'
                 '欠損を跨いでDRを接続せず別セグメントとし、各セグメントを0から開始。'
                 '位置の真値はなくATE/RPE[m]は算出していない。')]
        if 'camera_imu_trial' in current:
            extra = current['camera_imu_trial']
            text += ['', ('カメラ内蔵センサだけの追加VI試行：'
                     f'追跡率{extra["tracked_fraction"]:.2%}、慣性初期化済み'
                     f'{extra["imu_initialized_frames"]}フレーム、最終地図'
                     f'{extra["surviving_nonempty_maps"]}個。'
                     '[詳細](camera_vi30_bracketed/summary.json) / '
                     '[追跡図](camera_vi30_bracketed/tracking.png)')]
        (parent/'report.md').write_text('\n'.join(text)+'\n', encoding='utf-8')
    if camera_summaries:
        report += ['', 'カメラ内蔵センサのみを使う追加VI試行（約30 Hzの姿勢差分角速度と加速度）：', '',
                   '| 実験 | 追跡率 | 慣性初期化済みフレーム | 最終地図 | 終了 |', '|---|---:|---:|---:|---:|']
        for row in camera_summaries:
            report.append(f'| {row["experiment_id"][-3:]} | {row["tracked_fraction"]:.2%} | '
                          f'{row["imu_initialized_frames"]} | {row["surviving_nonempty_maps"]} | '
                          f'{row["exit_code"]} |')
    report += ['', ('単眼とVIはそれぞれ約120 fpsと30 fpsであり、追跡率差は方式だけの効果ではない。'
               '001は空中、003の元の追跡成功区間は水面上。水中性能は002・004と003の水中区間を区別する。'), '',
               '| 実験 | 圧力サンプル | 圧力の変動幅 | DRと圧力の差RMSE | 深度融合後の圧力残差RMSE |',
               '|---|---:|---:|---:|---:|']
    for row in summaries:
        f = row['fusion']
        if f['available']:
            m = f['attitude_comparison']['magnetic']
            report.append(f'| {row["experiment_id"][-3:]} | {f["pressure_samples"]} | '
                          f'{f["pressure_span_m"]:.3f} m | {m["dr_pressure_disagreement_rmse_m"]:.3f} m | '
                          f'{m["fused_pressure_residual_rmse_m"]:.4f} m |')
    report += ['', ('深度を融合すると上下方向は圧力観測に拘束されるが、水平位置の積分ドリフトは残る。'
               '上表は同じ圧力を使った推定結果の整合であり、真値誤差ではない。'), '',
               ('単眼地図を磁北NEDへ回転し、実測圧力サンプルで距離尺度を試算した。'
               '時系列の前60%で尺度・オフセットを推定し、後40%で圧力との整合を確認。'
               '正の尺度、上下変動5 cm以上、少なくとも8圧力サンプル（各分割3以上）、'
               '後半RMSE < max(5 cm, 全体上下幅の30%)を暫定採用条件とした。'
               '今回003・004は上下変動が足りず、視覚速度による水平拘束は採用しなかった。'
               '候補値・不採用理由は各fusion_summary.jsonに残す。'), '',
               ('使用センサ：子機映像、カメラ内蔵姿勢・加速度、本体BNO085のジャイロ・加速度・姿勢・磁気、'
               '子機の圧力。温度は圧力CSVへ保存し、密度997 kg/m³・重力9.80665 m/s²を仮定。'
               '音響受信だけでは世界位置の拘束を得られず、親機依存の距離・時計・位置を使っていない。'), '',
               ('圧力は各実験内の最初の観測を基準とした相対深度。DRは20 Hz、初速度0、'
               '静止実験002の762〜767秒で求めた加速度バイアスを明示的に減算。'
               '移動中の「静かなIMU」を速度0とは仮定しない。0.1秒超の欠損は補間しない。'), '',
               ('VI-SLAMは [ORB-SLAM3](https://github.com/UZ-SLAMLab/ORB_SLAM3) のIMU_MONOCULAR。'
               'カメラ取付回転は実験003の906.539〜933秒で推定、933〜959.442秒で整合確認。'
               '取付並進0、IMUノイズ設定は暫定値。圧力はORB-SLAM3内部へ入れず、別のDR融合に使用。'
               'マップの慣性初期化フラグと、処理の正常終了・視覚追跡成功を区別した。'), '',
               '地図のPNG/SVGに加え `.figure.pickle` / `.plot.json` を保存。編集例：', '',
               '```powershell', ('python -m aquabeacon_analysis.editable_plots '
               'results/20261028_ngt_004/child_sensor_fusion/mono_map_overview.figure.pickle'), '```', '',
               ('3D HTMLは外部ライブラリ・ネットワーク不要。アプリ内ブラウザのfile URL制限により'
               'ブラウザ実画面の検証は未実施。描画・設定保存/再読込はJavaScriptロジック試験、'
               '静的図は画像の目視確認で検証。'), '', '[検証記録](validation.md)']
    (OUTPUT/'report.md').write_text('\n'.join(report)+'\n', encoding='utf-8')
    write_json(OUTPUT/'comparison.json', summaries)
    with (OUTPUT/'comparison.csv').open('w', newline='', encoding='utf-8') as handle:
        fields = ['experiment_id', 'input_frames', 'tracked_fraction', 'imu_initialized_frames',
                  'surviving_nonempty_maps', 'exit_code', 'unprocessed_frames']
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows({key: row[key] for key in fields} for row in summaries)
    print(OUTPUT/'report.md')


if __name__ == '__main__':
    main()
