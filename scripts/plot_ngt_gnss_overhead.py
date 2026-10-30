"""Explicit NGT RTK/adopted-PPK horizontal difference and static-scatter figures."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import matplotlib
import numpy as np

matplotlib.use('Agg')
import matplotlib.pyplot as plt

from aquabeacon_analysis.editable_plots import save_editable_figure
from aquabeacon_analysis.gnss_overhead import horizontal_metrics, nearest_pairs


def read(path):
    with path.open(encoding='utf-8-sig', newline='') as stream:
        rows = list(csv.DictReader(stream))
    times = np.array([float(r['utc_s']) for r in rows])
    xy = np.array([[float(r[f'raw_{a}_m']) for a in ['east', 'north']] for r in rows])
    valid = np.array([r['input_valid'] == 'True' for r in rows]) & np.isfinite(xy).all(axis=1)
    return rows, times, xy, valid


def digest(path):
    return {'path': path.as_posix(), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def circle(ax, center, radius, label, color):
    angle = np.linspace(0, 2*np.pi, 241)
    ax.plot(center[0]+radius*np.cos(angle), center[1]+radius*np.sin(angle),
            color=color, lw=1.4, label=label)


def format_axis(ax, label):
    ax.axhline(0, color='#adb5bd', lw=.7)
    ax.axvline(0, color='#adb5bd', lw=.7)
    ax.set(xlabel='東方向 [m]', ylabel='北方向 [m]', title=label, aspect='equal')
    ax.grid(alpha=.2)
    ax.legend(fontsize=8, loc='best')


def render(eid, max_gap_s):
    out = Path('results')/eid/'gnss_overhead'
    out.mkdir(parents=True, exist_ok=True)
    config = Path('config/20261028_ngt/analysis.json')
    adoption = Path('config/20261028_ngt/ppk_adoption.json')
    chosen = json.loads(adoption.read_text(encoding='utf-8'))
    if chosen['selected_variant'] != 'existing' or chosen['application_status'] != 'existing_retained':
        raise ValueError('Recheck downstream propagation before plotting changed PPK adoption')
    paths = [Path('results')/eid/'fusion'/f'{name}.csv' for name in
             ['parent_position', 'parent_position_ppk_alternative']]
    data = [read(p) for p in paths]
    pairs = nearest_pairs(data[0][1], data[1][1], data[0][3], data[1][3], max_gap_s)
    residual = data[0][2][pairs[:, 0]]-data[1][2][pairs[:, 1]]
    metrics = horizontal_metrics(residual)
    counts = {}
    for name, (rows, times, xy, valid) in zip(['RTK', 'PPK'], data):
        counts[name] = {'rows': len(rows), 'input_valid': sum(r['input_valid'] == 'True' for r in rows),
                        'finite_and_input_valid': int(valid.sum()),
                        'invalid_or_nonfinite': int((~valid).sum()),
                        'valid_unpaired': int(valid.sum()-len(pairs)),
                        'quality_counts': {q: sum(r['quality'] == q for r in rows)
                                           for q in sorted({r['quality'] for r in rows})},
                        'measurement_rejected_retained': int(sum(r['measurement_rejected'] == 'True'
                                                            and v for r, v in zip(rows, valid)))}
    plt.rcParams['font.family'] = 'Yu Gothic'
    fig, ax = plt.subplots(figsize=(8, 7.5))
    fig.subplots_adjust(left=.12, right=.98, bottom=.15, top=.91)
    ax.plot(*residual.T, '.', ms=4, alpha=.55, color='#176b96', label=f'同時刻差 ({len(pairs)}組)')
    if len(residual):
        mean = metrics['mean_east_north_m']
        ax.plot(*mean, '+', ms=15, mew=2, color='#d65c21', label=f'平均差 E={mean[0]:.3f}, N={mean[1]:.3f} m')
        circle(ax, [0, 0], metrics['horizontal_rms_m'],
               f'原点からの水平差RMS {metrics["horizontal_rms_m"]:.3f} m', '#d65c21')
        circle(ax, mean, metrics['horizontal_scatter_m'],
               f'平均差まわりRMS {metrics["horizontal_scatter_m"]:.3f} m', '#208769')
    format_axis(ax, f'{eid}　RTK − 採用PPK の水平差')
    fig.supxlabel('独立した真値なし：RMSE精度ではなく方式間差。\n円はRMS半径（信頼区間ではない）。',
                  fontsize=9, y=.025)
    save_editable_figure(fig, out/'horizontal_difference.png', dpi=220)
    plt.close(fig)
    static = {}
    if eid.endswith('_001'):
        fig, axes = plt.subplots(1, 2, figsize=(12, 6.5), layout='constrained')
        limits = []
        for ax, name, (_, _, xy, valid) in zip(axes, ['RTK', 'PPK'], data):
            centered = xy[valid]-xy[valid].mean(axis=0)
            m = horizontal_metrics(centered)
            static[name] = {**m, 'reference_mean_enu_m': xy[valid].mean(axis=0).tolist()}
            ax.plot(*centered.T, '.', ms=4, alpha=.6, label=f'{name} 有効観測 ({len(centered)}点)')
            ax.plot(0, 0, '+', ms=12, color='black', label='自身の平均位置')
            circle(ax, [0, 0], m['horizontal_rms_m'], f'水平散らばりRMS {m["horizontal_rms_m"]:.3f} m', '#d65c21')
            format_axis(ax, f'{name} 静止区間：自身の平均からの差')
            limits.append(np.max(np.abs(centered))*1.1)
        for ax in axes:
            ax.set(xlim=(-max(limits), max(limits)), ylim=(-max(limits), max(limits)))
        fig.suptitle(f'{eid}　親動画240–480秒：水平静止散らばり', fontsize=14)
        fig.supxlabel('両方式はそれぞれ自身の平均を原点に表示。平均の差・絶対位置の正しさは評価しない。', fontsize=9)
        save_editable_figure(fig, out/'static_scatter.png', dpi=220)
        plt.close(fig)
    exported = []
    for i, j in pairs:
        exported.append({'rtk_row': int(i), 'ppk_row': int(j), 'rtk_utc_s': data[0][1][i],
                         'ppk_utc_s': data[1][1][j], 'time_difference_s': data[0][1][i]-data[1][1][j],
                         'rtk_quality': data[0][0][i]['quality'], 'ppk_quality': data[1][0][j]['quality'],
                         'east_difference_m': data[0][2][i, 0]-data[1][2][j, 0],
                         'north_difference_m': data[0][2][i, 1]-data[1][2][j, 1]})
    with (out/'matched_differences.csv').open('w', encoding='utf-8', newline='') as stream:
        fields = ['rtk_row', 'ppk_row', 'rtk_utc_s', 'ppk_utc_s', 'time_difference_s',
                  'rtk_quality', 'ppk_quality', 'east_difference_m', 'north_difference_m']
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(exported)
    # Export every original row, including excluded samples and upstream flags.
    for source, path in zip(['rtk', 'ppk'], paths):
        (out/f'{source}_observations.csv').write_bytes(path.read_bytes())
    result = {'experiment_id': eid, 'difference_rtk_minus_ppk': metrics, 'counts': counts,
              'static_scatter': static, 'max_abs_pair_dt_s': max((abs(r['time_difference_s']) for r in exported), default=None)}
    (out/'metrics.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    manifest = {'created_utc': datetime.now(UTC).isoformat(), 'experiment_id': eid,
                'git_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
                'inputs': [digest(p) for p in [*paths, config, adoption,
                           Path('results/20261028_ngt_000/fusion/manifest.json'), Path(__file__),
                           Path('src/aquabeacon_analysis/gnss_overhead.py')]],
                'frame': 'Original shared WGS84 local ENU; horizontal East/North, metres',
                'selection': 'input_valid and finite raw East/North; rejected valid measurements retained; no outlier removal',
                'matching': {'method': 'nearest one-to-one epoch, no interpolation', 'max_gap_s': max_gap_s},
                'reference': 'RTK minus adopted PPK; shared rover, correlated solutions, no independent truth',
                'calibration': 'Original GNSS antenna positions; no new lever arm or absolute alignment',
                'static_interval_parent_video_s': [240, 480] if static else None}
    (out/'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    report = (f'# {eid} GNSS水平差\n\n![水平差](horizontal_difference.png)\n\n'
              f'有効ペア {len(pairs)}組。RTK−採用PPKの水平差RMS {metrics["horizontal_rms_m"]:.4f} m、'
              f'平均差まわりRMS {metrics["horizontal_scatter_m"]:.4f} m。\n\n'
              '独立した基準測量はなく、これは真値に対するRMSE・絶対精度ではない。'
              '原点中心の円は水平差RMS、平均差中心の円は散らばりRMSの半径を示す。信頼区間ではない。'
              'RTKとPPKは同じ移動局を使用し相関する。採用PPKは既存解を維持する選定結果を確認した。\n\n'
              f'元CSVのinput_validと有限値条件を保持。最近傍の時刻差{max_gap_s}秒以下を一対一対応させ、'
              '補間・外れ値除去はしない。有効でも融合で棄却された観測を残す。'
              '全元行・品質・棄却フラグはrtk_observations.csv、ppk_observations.csvに保持。'
              '移動区間002–004の平均まわりの軌跡散らばりを静止精度と扱わない。\n\n'
              '[対応データ](matched_differences.csv)・[指標と除外件数](metrics.json)・[入力ハッシュと条件](manifest.json)。\n\n'
              'ブラウザ編集：\n\n```powershell\n'
              f'python -m aquabeacon_analysis.editable_plots results/{eid}/gnss_overhead/horizontal_difference.figure.pickle\n```\n')
    if static:
        report += '\n![静止散らばり](static_scatter.png)\n\n静止001のみ自身の平均からの散らばりを同じ軸範囲で比較。平均位置の正しさは示さない。\n'
    (out/'report.md').write_text(report, encoding='utf-8')
    print(json.dumps(result), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment', action='append', required=True,
                        choices=[f'20261028_ngt_{i:03d}' for i in range(1, 5)])
    parser.add_argument('--max-gap-s', type=float, default=.02)
    args = parser.parse_args()
    print(json.dumps({'pid': os.getpid(), 'started_utc': datetime.now(UTC).isoformat(),
                      'purpose': 'bounded GNSS figures, no server'}), flush=True)
    for eid in dict.fromkeys(args.experiment):
        render(eid, args.max_gap_s)


if __name__ == '__main__':
    main()
