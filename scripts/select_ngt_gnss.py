"""Show GNSS quality and save a separate fixed-priority position selection."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import pickle
import subprocess
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import matplotlib
import numpy as np

matplotlib.use('Agg')
import matplotlib.pyplot as plt

from aquabeacon_analysis.aerial_tracks import broken_track, mercator
from aquabeacon_analysis.editable_plots import save_editable_figure
from aquabeacon_analysis.gnss_selection import STATUS, select_positions


def read(path):
    with path.open(encoding='utf-8-sig', newline='') as handle:
        return list(csv.DictReader(handle))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment', required=True, choices=['20261028_ngt_000'])
    args = parser.parse_args()
    print(json.dumps({'pid': os.getpid(), 'started_utc': datetime.now(UTC).isoformat(),
                      'purpose': 'bounded quality and selection rendering; no server'}), flush=True)
    acquisition = args.experiment
    data = Path('data/processed')/acquisition/'gnss'
    inputs = [data/'rtk_nav_pvt.csv', data/'ppk_positions.csv',
              Path('config/20261028_ngt/analysis.json'),
              Path('results')/acquisition/'gnss/manifest.json', Path(__file__),
              Path('src/aquabeacon_analysis/gnss_selection.py'),
              Path('src/aquabeacon_analysis/aerial_tracks.py')]
    rtk, ppk = read(inputs[0]), read(inputs[1])
    rows = select_positions(rtk, ppk)
    config = json.loads(inputs[2].read_text())
    segments = [{'experiment_id': acquisition, 'label': '全収録',
                 'parent_video_interval_s': [rows[0]['parent_video_s'],
                                              rows[-1]['parent_video_s']+1]}]+config['segments']
    plt.rcParams['font.family'] = 'Yu Gothic'
    for segment in segments:
        eid = segment['experiment_id']
        start, end = segment['parent_video_interval_s']
        subset = [r for r in rows if start <= r['parent_video_s'] < end]
        out = Path('results')/eid/'gnss_selection'
        out.mkdir(parents=True, exist_ok=True)
        with (out/'selected_positions.csv').open('w', encoding='utf-8', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(subset)
        t = np.array([r['parent_video_s'] for r in subset])
        utc = np.array([r['utc_s'] for r in subset])
        fig, axes = plt.subplots(3, 1, figsize=(12, 7.5), sharex=True, layout='constrained')
        for ax, source, color in zip(axes[:2], ['RTK', 'PPK'], ['#007eaf', '#bd4b00']):
            ranks = np.array([r[f'{source.lower()}_rank'] for r in subset])
            series = broken_track(utc, np.column_stack([t, ranks]), np.ones(len(t), bool))
            ax.plot(*series.T, marker='.', ms=3, lw=1, color=color, label=source)
            ax.set(yticks=list(STATUS), yticklabels=list(STATUS.values()), ylim=(-1.4, 4.4),
                   ylabel=source+' 判定')
            ax.grid(alpha=.25)
            ax.legend(loc='upper right')
        selected = np.array([{'None': 0, 'RTK': 1, 'PPK': 2}[r['selected_source']] for r in subset])
        series = broken_track(utc, np.column_stack([t, selected]), np.ones(len(t), bool))
        axes[2].plot(*series.T, marker='.', ms=3, color='#287a39', lw=1)
        axes[2].set(yticks=[0, 1, 2], yticklabels=['採用なし', 'RTK', 'PPK'], ylim=(-.3, 2.3),
                    xlabel='親機原動画の時刻 [s]', ylabel='採用元', xlim=(start, end))
        axes[2].grid(alpha=.25)
        fig.suptitle(f'{eid}　GNSS解の判定と採用元')
        fig.supxlabel('Fixed > Float > DGPS > Single／同順位はPPK／Missingは対応解なし\n'
                      'PPKは整数固定offの最終解。判定は各観測時刻の値（線は表示用）。', fontsize=9)
        save_editable_figure(fig, out/'quality_status.png', dpi=150)
        plt.close(fig)
        extra_inputs = []
        if eid != acquisition:
            aerial = Path('results')/eid/'aerial'
            master = aerial/'aerial_tracks.figure.pickle'
            meta_path = aerial/'manifest.json'
            extra_inputs = [master, meta_path]
            # Trusted, locally generated master only; preserve its cached aerial pixels.
            old = pickle.loads(master.read_bytes())
            ref = old.axes[0]
            meta = json.loads(meta_path.read_text())
            origin = meta['origin_wgs84_lat_lon_height']
            xy = (mercator(np.array([r['latitude_deg'] for r in subset]),
                           np.array([r['longitude_deg'] for r in subset]))-
                  mercator(origin[0], origin[1])[0])*np.cos(np.radians(origin[0]))
            fig, ax = plt.subplots(figsize=(8, 8), layout='constrained')
            ax.imshow(ref.images[0].get_array(), extent=ref.images[0].get_extent(), origin='upper')
            switches = np.array([r['source_switch'] for r in subset])
            colors = {'RTK': '#00e5ff', 'PPK': '#ff4cce'}
            for source, color in colors.items():
                mask = np.array([r['selected_source'] == source for r in subset])
                line = broken_track(utc, xy, mask, switches)
                ax.plot(*line.T, color=color, marker='.', ms=4, lw=1.5, label=source+' 採用')
            fixed = np.array([r['selected_status'] == 'Fixed' for r in subset])
            ax.plot(*xy[fixed].T, linestyle='none', marker='o', ms=5,
                    mfc='none', mec='#fff000', label='Fixed採用')
            ax.plot(*xy[switches].T, linestyle='none', marker='x', ms=8,
                    color='white', label='採用元切替（線を分割）')
            ax.set(xlim=ref.get_xlim(), ylim=ref.get_ylim(), aspect='equal',
                   xlabel=ref.get_xlabel(), ylabel=ref.get_ylabel(),
                   title=f'{eid}　Fixed優先の選択軌跡（GNSS観測）')
            ax.legend(loc='upper left', fontsize=9)
            fig.supxlabel('背景：国土地理院・全国最新写真／北が上\n'
                          'アンテナ位置・未平滑化／Fixedは真値保証ではない', fontsize=9)
            save_editable_figure(fig, out/'selected_aerial.png', dpi=150)
            plt.close(fig)
            plt.close(old)
        summary = {key: dict(Counter(r[key] for r in subset)) for key in
                   ['rtk_status', 'ppk_status', 'selected_status', 'selected_source', 'reason']}
        steps = [r['switch_step_horizontal_m'] for r in subset if r['source_switch']]
        summary['source_switches'] = len(steps)
        summary['max_switch_step_horizontal_m'] = max(steps) if steps else None
        summary['paired_epochs'] = sum(np.isfinite(r['match_delta_s']) for r in subset).item()
        manifest = {'experiment_id': eid, 'created_utc': datetime.now(UTC).isoformat(),
                    'git_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'],
                                                           text=True).strip(),
                    'inputs': [{'path': p.as_posix(), 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
                               for p in inputs+extra_inputs],
                    'summary': summary, 'parent_video_interval_s': [start, end],
                    'matching': 'one-to-one union of UTC epochs; tolerance 0.02 s; no interpolation',
                    'epoch_time': 'RTK UTC if present, otherwise PPK; selected_utc_s retains actual UTC',
                    'priority': ['Fixed', 'Float', 'DGPS', 'Single'], 'tie_break': 'final PPK',
                    'invalid': 'No selection for missing, nonfinite, invalid or unsupported quality',
                    'rtk_mapping': 'valid fix_type 3/4; carrier 2 Fixed, 1 Float, 0 differential DGPS else Single',
                    'ppk_mapping': 'Q 1 Fixed, 2 Float, 4 DGPS, 5 Single; others unsupported',
                    'switch_policy': 'Retain actual source coordinates, no offset correction; break map line',
                    'fusion': 'GNSS selection only; previous IMU-aided outputs unchanged',
                    'limitations': ['Fixed is not ground truth', 'Final PPK ambiguity resolution is off',
                                    'Same-quality PPK preference is a declared policy, not proven accuracy',
                                    'Switch step includes motion between epochs, not pure method offset'],
                    'quality_sources': ['https://www.rtklib.com/prog/manual_2.4.2.pdf',
                                        'https://content.u-blox.com/sites/default/files/ZED-F9P_IntegrationManual_UBX-18010802.pdf']}
        (out/'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        report = f'# {eid} GNSS品質とFixed優先選択\n\n![品質判定](quality_status.png)\n\n'
        report += ('UTC差20ms以内のRTK・PPKを1対1で対応させ、補間せず選択した。'
                   '片方だけ有効なFixedならその解を採用。Fixed > Float > DGPS > Singleの順で、'
                   '両方Fixedを含む同順位は最終PPKを優先する。両方無効なら座標はNaN。'
                   '同順位PPK優先は明示した運用規則で、精度が高いと証明したものではない。\n\n'
                   '**最終PPKは整数固定offのためFixed 0点。** 過去の問題のあるFixed解へ戻していない。'
                   '採用元の位置をそのまま使い、平均化・オフセット補正はしない。'
                   'これはGNSS観測の選択結果で、既存の親機IMU併用軌跡は変更していない。\n\n')
        report += '| 判定 | RTK | PPK | 採用結果 |\n|---|---:|---:|---:|\n'
        for status in STATUS.values():
            report += f'| {status} | {summary["rtk_status"].get(status, 0)} | {summary["ppk_status"].get(status, 0)} | {summary["selected_status"].get(status, 0)} |\n'
        report += f'| 採用なし（欠測・無効） | — | — | {summary["selected_source"].get("None", 0)} |\n'
        report += (f'\n採用元：{summary["selected_source"]}。切替{len(steps)}回。'
                   f'切替時の前後点の最大水平差：{summary["max_switch_step_horizontal_m"]} m。'
                   'この差は実際の移動も含み、純粋な方式間差ではない。'
                   '方式間水平差もCSVへ保存した。航空写真の線は切替で分割。\n\n')
        if eid != acquisition:
            report += '![Fixed優先の航空写真軌跡](selected_aerial.png)\n\n'
        report += ('[採用データ・品質・時刻差・選択理由](selected_positions.csv)・'
                   '[設定・ハッシュ・集計](manifest.json)。Missingは対応解なし、Invalidは無効または未対応コード。'
                   'グラフの線は状態の補間推定ではなく、各観測点を結んだ表示。\n\n'
                   '[u-bloxのcarrier判定](https://content.u-blox.com/sites/default/files/ZED-F9P_IntegrationManual_UBX-18010802.pdf)・'
                   '[RTKLIBのQ判定](https://www.rtklib.com/prog/manual_2.4.2.pdf)。Fixed表示は正しい整数解や絶対精度を保証しない。\n\n'
                   '各図にPNG/SVG/figure.pickle/plot.jsonを併置。ブラウザ編集：\n\n'
                   f'```powershell\npython -m aquabeacon_analysis.editable_plots results/{eid}/gnss_selection/quality_status.figure.pickle\n```\n'
                   '航空写真図はファイル名を `selected_aerial.figure.pickle` に変更。\n')
        (out/'report.md').write_text(report, encoding='utf-8')
        print(eid, summary, flush=True)


if __name__ == '__main__':
    main()
