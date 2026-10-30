"""Export calibrated child attitude for explicitly selected NGT experiments."""
from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import matplotlib
import numpy as np

matplotlib.use('Agg')
import matplotlib.pyplot as plt

from aquabeacon_analysis.child_attitude import estimate_attitude, magnetic_ned_quaternions
from aquabeacon_analysis.editable_plots import save_editable_figure
from aquabeacon_analysis.experiments import load_experiment, repository_path
from aquabeacon_analysis.ngt_binary import sha256


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment', action='append', required=True)
    parser.add_argument('--calibration', required=True)
    parser.add_argument('--magnetic-heading', action='store_true',
                        help='Export provisional magnetic-north absolute attitude separately')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    started = datetime.now(UTC).isoformat()
    print(json.dumps({'pid': os.getpid(), 'started_utc': started,
                      'purpose': 'offline NGT child attitude', 'owner': args.experiment}), flush=True)
    acquisition = load_experiment(root, '20261028_ngt_000')
    calibration = repository_path(root, args.calibration)
    solution = json.loads(calibration.read_text(encoding='utf-8'))
    matrix = np.array(solution['R_body_from_sensor'])
    source = acquisition.stage_path('interim') / 'decoded/child/C_MAIN_IMU_100Hz_83304.imu.csv'
    decode = source.with_name('C_MAIN_IMU_100Hz_83304.bin.decode.json')
    provenance = json.loads(decode.read_text(encoding='utf-8'))
    raw = Path(provenance['source'])
    if (sha256(source) != provenance['streams']['imu']['csv_sha256']
            or sha256(raw) != provenance['source_sha256']):
        raise ValueError('Decoded CSV or raw source changed; decode again')
    sync_path = acquisition.stage_path('results') / 'synchronization/manifest.json'
    sync = json.loads(sync_path.read_text(encoding='utf-8'))['cameras']['parent']
    with source.open(encoding='utf-8', newline='') as handle:
        rows = list(csv.DictReader(handle))
    def array(keys):
        return np.array([[float(row[k]) for k in keys] for row in rows])
    q, angles, heading, observable = estimate_attitude(
        array(['qw', 'qx', 'qy', 'qz']), array(['mx', 'my', 'mz']), matrix)
    mono = array(['acquired_us'])[:, 0] / 1e6
    utc = array(['utc_us'])[:, 0]
    mask = array(['fresh_mask'])[:, 0].astype(int)
    clock_ok = utc > 1e15
    video = np.where(clock_ok, (utc / 1e6 - sync['utc_at_video_zero_s']) / sync['time_scale'],
                     np.nan)
    # Export ages rather than silently presenting held register values as fresh samples.
    ages = []
    for bit in (1, 8):
        last = np.maximum.accumulate(np.where(mask & bit, mono, -np.inf))
        ages.append(np.where(np.isfinite(last), mono - last, np.nan))
    q_age, mag_age = ages
    usable = np.isfinite(q).all(axis=1) & (q_age <= .1)
    magnetic_usable = observable & usable & (mag_age <= .1)
    heading[~magnetic_usable] = np.nan
    magnetic_q = magnetic_ned_quaternions(angles[:, :2], heading)
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
    inputs = [source, raw, decode, calibration, sync_path,
              root / 'results/20261029_child_axis_003/calibration_clip/manifest.json',
              Path(__file__), root / 'src/aquabeacon_analysis/child_attitude.py',
              root / 'src/aquabeacon_analysis/child_axis_calibration.py',
              root / 'src/aquabeacon_analysis/magnetic_heading.py']
    hashes = {p.relative_to(root).as_posix(): sha256(p) for p in inputs}
    for experiment_id in args.experiment:
        exp = load_experiment(root, experiment_id)
        if (experiment_id not in {f'20261028_ngt_{i:03}' for i in range(5)}
                or args.calibration not in exp.metadata['calibration_paths']):
            raise ValueError('Select registered NGT experiment with explicit calibration reference')
        interval = exp.metadata.get('parent_video_interval_s')
        selected = np.ones(len(rows), dtype=bool)
        start_utc = sync['utc_at_video_zero_s']
        if interval:
            start_utc += sync['time_scale'] * interval[0]
            lo, hi = [round((sync['utc_at_video_zero_s'] + sync['time_scale'] * t) * 1e6)
                      for t in interval]
            selected = clock_ok & (utc >= lo) & (utc < hi)
        ids = np.flatnonzero(selected)
        analysis = 'child_magnetic_attitude' if args.magnetic_heading else 'child_attitude'
        output = exp.stage_path('processed') / analysis
        result = exp.stage_path('results') / analysis
        output.mkdir(parents=True, exist_ok=True)
        result.mkdir(parents=True, exist_ok=True)
        path = output / 'attitude.csv'
        fields = ['index', 'acquired_us', 'utc_us', 'utc_source', 'fresh_mask',
                  'parent_video_time_s', 'experiment_time_s', 'clock_valid',
                  'local_qw', 'local_qx', 'local_qy', 'local_qz',
                  'roll_rad', 'pitch_rad', 'yaw_local_rad', 'quaternion_valid',
                  'attitude_usable', 'quaternion_age_s', 'magnetic_age_s',
                  'magnetic_heading_candidate_deg', 'magnetic_candidate_observable',
                  'absolute_heading_valid', 'true_heading_deg']
        if args.magnetic_heading:
            fields += ['heading_magnetic_deg', 'magnetic_ned_qw', 'magnetic_ned_qx',
                       'magnetic_ned_qy', 'magnetic_ned_qz',
                       'absolute_attitude_available', 'heading_accuracy_validated']
        with path.open('w', encoding='utf-8', newline='') as handle:
            writer = csv.writer(handle)
            writer.writerow(fields)
            for i in ids:
                values = [*[rows[i][k] for k in fields[:5]], video[i],
                          (utc[i] / 1e6 - start_utc) if clock_ok[i] else np.nan,
                          bool(clock_ok[i]), *q[i], *angles[i],
                          bool(np.isfinite(q[i]).all()), bool(usable[i]), q_age[i], mag_age[i],
                          heading[i], bool(magnetic_usable[i]), False, '']
                if args.magnetic_heading:
                    values += [heading[i], *magnetic_q[i],
                               bool(np.isfinite(magnetic_q[i]).all()), False]
                writer.writerow(['' if isinstance(v, (float, np.floating)) and not np.isfinite(v)
                                 else v for v in values])
        stats = {'rows': len(ids), 'usable_attitudes': int(usable[ids].sum()),
                 'invalid_quaternions': int((~np.isfinite(q[ids]).all(axis=1)).sum()),
                 'unclocked_rows': int((~clock_ok[ids]).sum()),
                 'magnetic_candidates': int(np.isfinite(heading[ids]).sum()),
                 'absolute_heading_valid_rows': 0}
        manifest = {'schema_version': 1, 'experiment_id': experiment_id,
                    'created_utc': started, 'pid': os.getpid(), 'git_commit': revision,
                    'worktree_dirty': bool(subprocess.check_output(
                        ['git', 'status', '--porcelain'], cwd=root, text=True)),
                    'inputs_sha256': hashes,
                    'registry_sha256': sha256(root / 'experiments' / experiment_id / 'experiment.json'),
                    'output': path.relative_to(root).as_posix(), 'output_sha256': sha256(path),
                    'columns': fields, 'statistics': stats,
                    'frame': 'FRD body to local_down; local x=game x, y=-game y, z=-game z',
                    'quaternion_order': 'wxyz',
                    'equation': 'R_local_body = diag(1,-1,-1) @ R_game_sensor @ R_body_sensor.T',
                    'angles': 'intrinsic ZYX: Rz(yaw) Ry(pitch) Rx(roll); radians; roll right-down, pitch nose-up',
                    'heading': 'Local yaw arbitrary. Magnetic candidate clockwise from magnetic north; unvalidated. True heading absent.',
                    'absolute_heading_valid': False,
                    'heading_evidence': 'Calibration held-out pose failed: field residual 10.95%, horizontal angle 19.11 deg.',
                    'mounting_basis': 'User confirms same sensor mounting during calibration and pool run.',
                    'policy': {'max_register_age_s': .1, 'interpolation': False,
                               'magnetic_bias_applied': False, 'camera_fusion': False,
                               'interval': interval, 'interval_policy': 'half-open UTC [start,end)',
                               'time_basis': 'recorded UTC only; unmapped full-session rows retained',
                               'singularity': 'horizontal forward norm <1e-6: roll/yaw blank'},
                    'note': 'Usable means numerical and age gates only, not independent attitude accuracy validation.'}
        if args.magnetic_heading:
            manifest.update({
                'schema_version': 2,
                'absolute_attitude': {
                    'frame': 'body FRD to magnetic NED (north,east,down), wxyz',
                    'equation': 'R_magNED_body = Rz(heading_magnetic) @ Ry(pitch) @ Rx(roll)',
                    'heading_unit': 'degrees clockwise; 0=magnetic north, 90=east, range [0,360)',
                    'accuracy_validated': False,
                    'user_policy': '2026-10-30: estimate absolute heading regardless of accuracy',
                    'availability_column': 'absolute_attitude_available',
                    'legacy_absolute_heading_valid': 'independent validation only, remains false',
                    'declination_applied': False,
                    'filtering': 'none; per-sample tilt-compensated recorded magnetic vector',
                },
            })
        (result / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
        (output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
        fig, axes = plt.subplots(3, 1, figsize=(11, 7), sharex=True, layout='constrained')
        t = mono[ids] - mono[0]
        yaw_label = 'Magnetic heading (provisional)' if args.magnetic_heading else 'Local yaw (not north)'
        for axis, col, label in zip(axes, range(3), ['Roll', 'Pitch', yaw_label], strict=True):
            value = np.degrees(angles[ids, col]).copy()
            if args.magnetic_heading and col == 2:
                value = heading[ids].copy()
                axis.set_ylim(0, 360)
                axis.set_yticks([0, 90, 180, 270, 360])
            value[~usable[ids]] = np.nan
            if len(ids):
                breaks = np.r_[False, (np.diff(mono[ids]) > .1) | (np.abs(np.diff(value)) > 180)]
                value[breaks] = np.nan
            else:
                axis.text(.5, .5, 'No child IMU samples in this UTC interval',
                          ha='center', transform=axis.transAxes)
            axis.plot(t, value, linewidth=.7, label=label)
            axis.set_ylabel('deg')
            axis.legend(loc='upper right')
            axis.grid(alpha=.25)
        axes[-1].set_xlabel('Child monotonic seconds since first recorded sample')
        title = ('provisional magnetic-north attitude' if args.magnetic_heading
                 else 'calibrated FRD; absolute heading unavailable')
        axes[0].set_title(f'{experiment_id}: {title}')
        save_editable_figure(fig, result / 'attitude.png', dpi=150)
        plt.close(fig)
        report = (f'# 長田高校プール・子機姿勢 {experiment_id}\n\n'
                  '軸校正を適用した重力基準のロール・ピッチと、北を基準としないヨー。'
                  '磁気方位は独立検証不合格のため、完全な絶対姿勢は未確定。\n\n'
                  f'- CSV: `{path.relative_to(root).as_posix()}`\n'
                  f'- 全{len(ids):,}行、数値・鮮度条件を満たす姿勢{stats["usable_attitudes"]:,}行。\n'
                  f'- UTC未確定{stats["unclocked_rows"]:,}行、無効四元数{stats["invalid_quaternions"]:,}行。\n'
                  '- `local_q*`は機体FRDからlocal_downへのwxyz四元数。local xは任意、zは下。\n'
                  '- `roll_rad`は右下がり正、`pitch_rad`は機首上げ正、`yaw_local_rad`は任意基準。\n'
                  '- 後続解析は`attitude_usable=True`を確認。新規観測のみなら`fresh_mask & 1`も確認。\n'
                  '- 時間同期には`clock_valid=True`の`utc_us`を使用。元の単調時刻・鮮度も保持。\n'
                  '- `magnetic_heading_candidate_deg`は傾斜補正した未検証の磁北候補。'
                  '`absolute_heading_valid=False`、`true_heading_deg`は空欄。北基準融合には使わない。\n'
                  '- 磁気補正・補間・カメラ融合なし。0.1秒を超える保持値をusableにしない。\n'
                  '- 軸校正の方向残差最大2.612°は本番の絶対姿勢精度ではない。\n\n'
                  '![姿勢](attitude.png)\n\nPNG・SVG・figure.pickle・plot.jsonを保存。'
                  'ブラウザ編集は以下で起動し、設定保存・再読込とPNG/SVG/PDF出力が可能。\n\n'
                  f'`python -m aquabeacon_analysis.editable_plots {result.relative_to(root).as_posix()}/attitude.figure.pickle`\n\n'
                  '入力・校正・コードのハッシュ、時刻・座標・欠損の規約はmanifest.json。'
                  '完全な絶対姿勢には独立検証を通過した磁気方位、または同期した既知の機首方位が必要。\n')
        if args.magnetic_heading:
            report = (f'# 長田高校プール・磁北基準の子機絶対姿勢 {experiment_id}\n\n'
                      'ユーザー指定により精度の検証合格を条件とせず、磁気データから絶対方位を推定した。'
                      '機体軸校正後の傾きで磁場を水平面へ戻し、磁北基準の方位と姿勢四元数を保存。\n\n'
                      f'- CSV: `{path.relative_to(root).as_posix()}`\n'
                      f'- 記録{len(ids):,}行、方位・絶対姿勢を算出できた行は'
                      f'{int(np.isfinite(magnetic_q[ids]).all(axis=1).sum()):,}行。\n'
                      '- `heading_magnetic_deg`: 磁北0°・東90°・南180°・西270°、時計回り。\n'
                      '- `magnetic_ned_qw/qx/qy/qz`: 機体FRD→磁北NEDのwxyz四元数。\n'
                      '- 後続解析は`absolute_attitude_available=True`の行で上記四元数を使用する。'
                      'UTCとの結合には`clock_valid=True`を確認。\n'
                      '- `heading_accuracy_validated=False`は精度未検証の表示。'
                      '既存互換列`absolute_heading_valid=False`も検証状態であり、今回の利用可否ではない。\n'
                      '- 真北への偏角補正は未適用。`true_heading_deg`は空欄。'
                      '磁気bias補正・平滑化・補間なし。元の時刻・鮮度・相対姿勢を併記。\n'
                      '- 数値不能・鉛直前方・更新後0.1秒超は欠損のまま保存。'
                      '磁気の精度評価による除外はしない。\n\n'
                      '![磁北基準の姿勢](attitude.png)\n\n'
                      '図はPNG/SVGとブラウザ編集用マスターを保存。編集コマンド：\n\n'
                      f'`python -m aquabeacon_analysis.editable_plots {result.relative_to(root).as_posix()}/attitude.figure.pickle`\n\n'
                      '入力・校正・コードのSHA256、座標系と計算条件はmanifest.json。\n')
        (result / 'report.md').write_text(report, encoding='utf-8')
        print(json.dumps({'experiment_id': experiment_id, **stats}), flush=True)


if __name__ == '__main__':
    main()
