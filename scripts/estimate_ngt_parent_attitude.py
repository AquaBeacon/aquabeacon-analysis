"""Estimate magnetic-north parent attitude with separate CAD-aligned LIS3MDL axes."""
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
from scipy.spatial.transform import Rotation

matplotlib.use('Agg')
import matplotlib.pyplot as plt

from aquabeacon_analysis.child_axis_calibration import body_quaternions
from aquabeacon_analysis.editable_plots import save_editable_figure
from aquabeacon_analysis.experiments import load_experiment, repository_path
from aquabeacon_analysis.ngt_binary import sha256
from aquabeacon_analysis.ngt_sync import numeric_csv, sample_with_gaps
from aquabeacon_analysis.parent_magnetic_attitude import (
    absolute_from_separate_mag,
    gravity_tilt,
    sample_rotations,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment', required=True, action='append',
                        choices=[f'20261028_ngt_{i:03}' for i in range(5)])
    parser.add_argument('--calibration', required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    started = datetime.now(UTC).isoformat()
    print(json.dumps({'pid': os.getpid(), 'started_utc': started,
                      'owner': args.experiment, 'purpose': 'offline parent magnetic attitude'}),
          flush=True)
    cfg_path = repository_path(root, args.calibration)
    cfg = json.loads(cfg_path.read_text(encoding='utf-8'))
    axis_path = root / cfg['imu_axes']
    axis = json.loads(axis_path.read_text(encoding='utf-8'))
    rig_path = root / cfg['rig']
    rig = json.loads(rig_path.read_text(encoding='utf-8'))
    vendor = (root / rig['source_root_hint'] / cfg['vendor_board_file']).resolve()
    if sha256(vendor) != rig['sources'][cfg['vendor_board_file']]['sha256']:
        raise ValueError('Vendor board changed from CAD snapshot')
    base = root / 'data/interim/20261028_ngt_000'
    mag_path = base / 'decoded/parent/P_MAIN_HEALTH_RANGE_11071.mag_observation.csv'
    decode_path = mag_path.with_name('P_MAIN_HEALTH_RANGE_11071.bin.decode.json')
    decode = json.loads(decode_path.read_text(encoding='utf-8'))
    raw = Path(decode['source'])
    if (sha256(raw) != decode['source_sha256']
            or sha256(mag_path) != decode['streams']['mag_observation']['csv_sha256']):
        raise ValueError('Magnetic source hash mismatch')
    camera_path = base / 'cameras/parent_telemetry.csv'
    accel_path = base / 'decoded/spresence/parent_100hz_bin_means.csv'
    accel_manifest_path = accel_path.with_suffix('.json')
    accel_manifest = json.loads(accel_manifest_path.read_text(encoding='utf-8'))
    if sha256(accel_path) != accel_manifest['csv_sha256']:
        raise ValueError('Spresense averaged CSV hash mismatch')
    camera_metadata_path = camera_path.with_suffix('.json')
    sync_path = root / 'results/20261028_ngt_000/synchronization/manifest.json'
    sync = json.loads(sync_path.read_text(encoding='utf-8'))['cameras']['parent']
    with mag_path.open(encoding='utf-8', newline='') as f:
        rows = list(csv.DictReader(f))
    utc = np.array([int(r['utc_read_us']) for r in rows], dtype=np.int64)
    epoch = sync['utc_at_video_zero_s']
    t = utc / 1e6 - epoch
    camera = numeric_csv(camera_path, ['video_time_s', 'qw', 'qx', 'qy', 'qz'])
    accel = numeric_csv(accel_path, ['sample_utc_us', 'corrected_ax', 'corrected_ay', 'corrected_az'])
    body_from_imu = np.array(axis['R_body_from_spresense'])
    body_from_camera = body_from_imu @ np.array(sync['R_camera_imu']).T
    qc = body_quaternions(camera[:, 1:], body_from_camera)
    camera_t = sync['time_scale'] * camera[:, 0]
    sampled = sample_rotations(camera_t, qc, t)
    camera_ok = np.isfinite(sampled).all(axis=1)
    acceleration = sample_with_gaps(accel[:, 0]/1e6-epoch, accel[:, 1:], t) @ body_from_imu.T
    fallback = gravity_tilt(acceleration)
    sampled[~camera_ok] = fallback[~camera_ok]
    tilt_source = np.where(camera_ok, 'camera_slerp',
                           np.where(np.isfinite(fallback).all(axis=1),
                                    'accelerometer_instantaneous', 'unavailable'))
    # Check the down-positive camera-world convention against actual run acceleration.
    ca = sample_with_gaps(accel[:, 0]/1e6-epoch, accel[:, 1:], camera_t) @ body_from_imu.T
    check = np.isfinite(ca).all(axis=1) & np.isfinite(qc).all(axis=1)
    world_force = Rotation.from_quat(qc[check][:, [1, 2, 3, 0]]).apply(ca[check])
    force_median = np.median(world_force, axis=0)
    if force_median[2] > -8 or np.linalg.norm(force_median[:2]) > 2:
        raise ValueError('Camera world +Z-down convention check failed')
    mag = np.array([[float(r['corrected_'+k]) for k in 'xyz'] for r in rows])
    data_valid = np.array([r['valid'] == 'True' and r['hardware_overrun'] == 'False'
                           and int(r['calibration_generation']) == 1 for r in rows])
    clock_ok = utc > 10**15
    mag[~(data_valid & clock_ok)] = np.nan
    q, heading, rp = absolute_from_separate_mag(sampled, mag, cfg['R_frd_from_mag'])
    available = np.isfinite(q).all(axis=1)
    body_mag = mag @ np.array(cfg['R_frd_from_mag']).T
    inputs = [cfg_path, axis_path, rig_path, vendor, raw, decode_path, mag_path,
              camera_path, camera_metadata_path, accel_path, accel_manifest_path,
              sync_path, Path(__file__),
              root / 'src/aquabeacon_analysis/parent_magnetic_attitude.py',
              root / 'src/aquabeacon_analysis/child_axis_calibration.py',
              root / 'src/aquabeacon_analysis/ngt_sync.py']
    hashes = {str(p.resolve()): sha256(p) for p in inputs}
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
    for eid in args.experiment:
        exp = load_experiment(root, eid)
        if args.calibration not in exp.metadata['calibration_paths']:
            raise ValueError('Register selected calibration before use')
        interval = exp.metadata.get('parent_video_interval_s')
        select = np.ones(len(rows), dtype=bool)
        zero = epoch
        if interval:
            lo, hi = [round((epoch + sync['time_scale']*x)*1e6) for x in interval]
            select = clock_ok & (utc >= lo) & (utc < hi)
            zero = lo/1e6
        ids = np.flatnonzero(select)
        out = exp.stage_path('processed') / 'parent_magnetic_attitude'
        result = exp.stage_path('results') / 'parent_magnetic_attitude'
        out.mkdir(parents=True, exist_ok=True)
        result.mkdir(parents=True, exist_ok=True)
        path = out / 'attitude.csv'
        extra = ['parent_video_time_s', 'experiment_time_s', 'clock_valid', 'tilt_source',
                 'mag_body_x_uT', 'mag_body_y_uT', 'mag_body_z_uT',
                 'roll_rad', 'pitch_rad', 'heading_magnetic_deg',
                 'magnetic_ned_qw', 'magnetic_ned_qx', 'magnetic_ned_qy', 'magnetic_ned_qz',
                 'absolute_attitude_available', 'heading_accuracy_validated', 'true_heading_deg']
        fields = list(rows[0]) + extra
        with path.open('w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(fields)
            for i in ids:
                added = [t[i]/sync['time_scale'] if clock_ok[i] else np.nan,
                         utc[i]/1e6-zero if clock_ok[i] else np.nan, bool(clock_ok[i]),
                         tilt_source[i], *body_mag[i], *rp[i], heading[i], *q[i],
                         bool(available[i]), False, '']
                writer.writerow([*rows[i].values(),
                                 *['' if isinstance(v, (float, np.floating)) and not np.isfinite(v)
                                   else v for v in added]])
        stats = {'rows': len(ids), 'absolute_attitude_available': int(available[ids].sum()),
                 'camera_tilt_rows': int((camera_ok[ids] & available[ids]).sum()),
                 'accelerometer_tilt_rows': int((~camera_ok[ids] & available[ids]).sum()),
                 'missing_tilt': int((tilt_source[ids] == 'unavailable').sum()),
                 'magnetic_data_flag_rejected': int((~data_valid[ids]).sum()),
                 'unobservable_or_unclocked': int((~available[ids] & data_valid[ids]
                                                  & (tilt_source[ids] != 'unavailable')).sum())}
        manifest = {'schema_version': 1, 'experiment_id': eid, 'created_utc': started,
                    'pid': os.getpid(), 'git_commit': revision, 'worktree_dirty': True,
                    'inputs_sha256': hashes, 'configuration': cfg,
                    'spresense_upstream_provenance': accel_manifest,
                    'registry_sha256': sha256(root/'experiments'/eid/'experiment.json'),
                    'output': path.relative_to(root).as_posix(), 'output_sha256': sha256(path),
                    'statistics': stats, 'columns': fields,
                    'R_body_from_camera_imu_applied': body_from_camera.tolist(),
                    'camera_world_specific_force_median_m_s2': force_median.tolist(),
                    'frame': 'body FRD to magnetic NED; scalar-first wxyz',
                    'heading': 'clockwise magnetic north, degrees [0,360); true north not applied',
                    'formula': 'm_world=R_world_body R_body_mag m_sensor; R_NED_body=Rz(-atan2(m_world_y,m_world_x)) R_world_body',
                    'time_basis': 'recorded utc_read_us; original utc_source and time_uncertainty_us retained; no new UTC accuracy claim',
                    'interval_parent_video_s': interval, 'interval_policy': 'half-open UTC [start,end)',
                    'missing_policy': 'no extrapolation; SLERP or acceleration interpolation <=0.1s; invalid/overrun or non-generation1 mag missing; horizontal field <=1uT or forward projection <=0.1 missing',
                    'accuracy_validated': False, 'new_bias_correction': False,
                    'tilt_limit': 'accelerometer fallback includes dynamic acceleration; camera/world and mounting are provisional',
                    'same_mount_assumption': 'Oct29 Spresense body axes reused for Oct28 same mounting; Oct28 camera alignment used, not Oct29 clock'}
        for folder in [result, out]:
            (folder/'manifest.json').write_text(json.dumps(manifest, indent=2)+'\n', encoding='utf-8')
        fig, axes = plt.subplots(3, 1, sharex=True, figsize=(11, 7), layout='constrained')
        for ax, values, label in zip(axes, [np.degrees(rp[:, 0]), np.degrees(rp[:, 1]), heading],
                                     ['Roll', 'Pitch', 'Magnetic heading (provisional)'], strict=True):
            values = values[ids].copy()
            if len(ids):
                breaks = np.r_[False, (np.diff(t[ids]) > .2) | (np.abs(np.diff(values)) > 180)]
                values[breaks] = np.nan
            ax.plot(t[ids]/sync['time_scale'], values, linewidth=.7, label=label)
            ax.set_ylabel('deg')
            ax.legend(loc='upper right')
            ax.grid(alpha=.25)
        axes[-1].set_ylim(0, 360)
        axes[-1].set_yticks([0, 90, 180, 270, 360])
        axes[-1].set_xlabel('Parent source-video time (s)')
        axes[0].set_title(f'{eid}: parent magnetic NED; CAD + user mounting')
        save_editable_figure(fig, result/'attitude.png', dpi=150)
        plt.close(fig)
        report = (f'# 親機の磁北基準絶対姿勢 {eid}\n\n'
                  f'全{len(ids):,}行中{stats["absolute_attitude_available"]:,}行で算出。'
                  f'傾きはカメラ{stats["camera_tilt_rows"]:,}行、加速度からの暫定推定'
                  f'{stats["accelerometer_tilt_rows"]:,}行。精度合否で除外しない。\n\n'
                  f'欠損内訳：傾きなし{stats["missing_tilt"]:,}行、磁気データフラグ不適合'
                  f'{stats["magnetic_data_flag_rejected"]:,}行、方位可観測性または時計条件不成立'
                  f'{stats["unobservable_or_unclocked"]:,}行（条件は重なる場合あり）。元の全行を保持。\n\n'
                  f'CSV: `{path.relative_to(root).as_posix()}`\n\n'
                  '`heading_magnetic_deg`は磁北0°・東90°・南180°・西270°。'
                  '`magnetic_ned_qw/qx/qy/qz`は機体FRD→磁北NEDの四元数。'
                  '`absolute_attitude_available=True`を選び、UTC結合は`clock_valid`も確認する。\n\n'
                  '## 軸の対応\n\n'
                  '前方はユーザー指定のGNSS受信基板→Spresense。CADの直交軸では+Xを前、-Yを右、-Zを下とする。'
                  '磁気センサの+XはGNSS受信基板側、基板上面はCADどおり上向きと仮定。'
                  '`m_FRD = [mx, -my, -mz]`。基板間の中心を結ぶ斜めの線を実測軸とは扱わず、CAD直交軸へ対応付けた暫定取付変換。\n\n'
                  '公式EAGLEのX矢印は基板+Y、Y矢印は基板-X。元CAD配置のままではXがGNSSと逆を向くため、'
                  '実機情報を優先して基板の面内向きを180°反転した軸対応を使用。CAD位置や測定中心は変更しない。'
                  'Spresenseの軸変換を磁気へ流用していない。\n\n'
                  '## 姿勢と時間\n\n'
                  'Spresense→FRDは軸校正011、本番カメラ→Spresenseは10月28日の動作同期結果を使用。'
                  '親機カメラ世界の+Zが下であることを本番加速度で確認。'
                  f'世界座標の比力中央値は{force_median.round(4).tolist()} m/s²。'
                  '磁気サンプル時刻へ0.1秒以内のSLERPで姿勢を合わせる。カメラ範囲外はSpresense加速度の'
                  '傾きへ切替え、tilt_sourceに記録する。加速度fallbackは運動加速度の影響を含む。'
                  'カメラも加速度もない範囲は欠損。元のUTC品質・不確かさ・磁気生値/補正値も残す。\n\n'
                  '磁気は記録済みcalibration_generation=1の補正値を使用し、biasを二重適用しない。'
                  '真北偏角未適用、heading_accuracy_validated=False。機体の同一取付けと基板面の上下は前提としてmanifestに保存。\n\n'
                  '![親機姿勢](attitude.png)\n\nPNG・SVG・ブラウザ編集マスターを保存。\n\n'
                  f'`python -m aquabeacon_analysis.editable_plots {result.relative_to(root).as_posix()}/attitude.figure.pickle`\n\n'
                  '部品軸の出典：[Adafruit製品](https://www.adafruit.com/product/4479)、'
                  '[公式基板資料](https://learn.adafruit.com/lis3mdl-triple-axis-magnetometer/downloads)、'
                  '[STデータシート](https://www.st.com/resource/en/datasheet/lis3mdl.pdf)。'
                  '使用したCAD・校正・入力・コードのハッシュはmanifest.json。\n')
        (result/'report.md').write_text(report, encoding='utf-8')
        print(json.dumps({'experiment_id': eid, **stats}), flush=True)


if __name__ == '__main__':
    main()
