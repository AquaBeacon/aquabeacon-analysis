"""Verify saved synchronization outputs and write the experiment report."""
import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from aquabeacon_analysis.experiments import load_experiment
from aquabeacon_analysis.ngt_binary import sha256
from aquabeacon_analysis.ngt_sync import numeric_csv
from aquabeacon_analysis.time_sync import probe_media, resolve_ffmpeg


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment', required=True)
    args = parser.parse_args()
    exp = load_experiment(Path.cwd(), args.experiment)
    output = exp.stage_path('results') / 'synchronization'
    manifest = json.loads((output / 'manifest.json').read_text())
    checks = []
    lines = ['# Nanyo海上実験：姿勢による時刻同期', '',
             ('長田高校と同じDJI四元数の微分角速度と機体ジャイロの照合で、'
             f"全{len(manifest['cameras'])}本を個別に同期した。原本は移動・変更していない。"), '',
             '## 結果', '',
             ('| 動画 | 推定開始時刻 JST | 作成時刻への補正 s | drift ppm | '
             'RMS残差 ms | 最大残差 ms | 採用窓 | 検証範囲（動画秒） |'),
             '|---|---|---:|---:|---:|---:|---:|---|']
    for name, result in manifest['cameras'].items():
        for entry in result['inputs']:
            assert sha256(Path(entry['path'])) == entry['sha256']
        values = numeric_csv(exp.stage_path('processed') / 'synchronization' /
                             f'{name}_attitude.csv',
                             ['video_time_s', 'sync_utc_s', 'clock_interpolated'])
        expected = result['utc_at_video_zero_s'] + result['time_scale']*values[:, 0]
        assert np.max(np.abs(expected-values[:, 1])) < 1e-6
        assert np.all(np.diff(values[:, 1]) > 0)
        low, high = result['validated_interval_s']
        assert np.array_equal(values[:, 2], (values[:, 0] >= low) & (values[:, 0] <= high))
        for suffix in ('.png', '.svg', '.figure.pickle', '.plot.json'):
            assert (output / f'{name}{suffix}').stat().st_size > 0
        video = Path(result['source_video']['path'])
        lrf = video.with_suffix('.LRF')
        media = probe_media(lrf, resolve_ffmpeg())
        # Same acquisition and timeline required before applying LRF PTS to MP4.
        assert abs(media.start_utc_us-result['source_video']['start_utc_us']) <= 1e6
        assert abs(media.duration_s-result['source_video']['duration_s']) < .2
        accepted = [w for w in result['windows'] if w['accepted']]
        assert len(accepted) >= 3
        checks.append({'camera': name, 'rows': len(values),
                       'lrf_path': str(lrf), 'lrf_sha256': sha256(lrf),
                       'mp4_size_bytes': video.stat().st_size,
                       'duration_difference_s': media.duration_s-result['source_video']['duration_s'],
                       'minimum_peak_margin': min(w['correlation']-w['coarse_competitor']
                                                  for w in accepted),
                       'leave_one_window_out_max_ms': result['leave_one_window_out_max_s']*1000})
        start = datetime.fromisoformat(result['utc_at_video_zero_iso']).astimezone(
            timezone(timedelta(hours=9))).strftime('%H:%M:%S.%f')
        lines.append(f"| {name} | {start} | {result['offset_from_creation_time_s']:+.6f} | "
                     f"{result['drift_ppm']:.3f} | {result['residual_rms_s']*1000:.2f} | "
                     f"{result['residual_max_s']*1000:.2f} | {len(accepted)} | {low:g}–{high:g} |")
    for path, digest in manifest['code_hashes'].items():
        assert sha256(Path(path)) == digest
    lines += ['', '## 適用方法と限界', '',
              ('`UTC秒 = utc_at_video_zero_s + time_scale × 動画PTS秒`。'
              f"係数は[manifest.json](manifest.json)を参照。実験IDは{args.experiment}。"), '',
              ('親機の時計基準はSpresenseのGPS PPS quality=2の記録UTC。'
              '子機はC_MAIN_IMUのgps_pps_candidate記録UTC。入力ファイルはmanifestに列挙。'
              '親子間のUTC伝達の絶対誤差は今回独立に検証していない。'
              'IMU配信遅延も経験的補正に含む。'), '',
              ('元姿勢の四元数を維持した同期CSVは '
              f'`data/processed/{args.experiment}/synchronization/*_attitude.csv`。'
              '`clock_interpolated=1` は採用窓の最初から最後までの内側である。'
              'それ以外は外挿。内部の低相関区間も除外理由とともにmanifestに残した。'
              '検証範囲は動画ごとに上表を参照。再起動の前後へ別動画の時計を流用しない。'), '',
              ('残差は内部整合性であり、絶対時刻精度や機体の絶対方位精度ではない。'
              '探索刻みはmanifestの設定、leave-one-window-out最大誤差は検証JSONを参照。'
              '短い収録のドリフトは限られた窓からの推定。'), '',
              '## 診断図', '']
    for name in manifest['cameras']:
        lines += [f'![{name}]({name}.png)', '',
                  (f'[SVG]({name}.svg) / [窓ごとの数値]({name}_windows.csv)。'
                  f'編集元 `{name}.figure.pickle` と `.plot.json` も同じフォルダ。'), '']
    lines += ['## 再現と検証', '',
              ('再現コード：`scripts/prepare_nanyo_sync.py`、`scripts/synchronize_nanyo.py`。'
              f'設定：`config/{args.experiment}/synchronization.json`。'
              'CRC検証と機体原本ハッシュはinterimのdecode.jsonとparent_100hz.jsonに保存。'
              'LRF原本ハッシュは[verification.json](verification.json)に保存。'), '',
              ('本スクリプトで同期成果物の数値を検証。'
              '同期CSVの再計算一致、単調性、外挿フラグ、図の編集元と入力ハッシュを検証。'
              '図の目視確認とコードの試験結果は別途記録する。'), '']
    (output / 'report.md').write_text('\n'.join(lines), encoding='utf-8')
    (output / 'verification.json').write_text(json.dumps(
        {'status': 'passed', 'cameras': checks, 'report_script_sha256': sha256(Path(__file__))},
        indent=2))
    print(json.dumps(checks, indent=2))


if __name__ == '__main__':
    main()
