"""Apply conservative PPK screening and publish editable comparison evidence."""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import matplotlib
import numpy as np
from compare_ngt_ppk_bases import metrics
from ppk_ngt_geonet import dump, hashed

matplotlib.use('Agg')
import matplotlib.pyplot as plt

from aquabeacon_analysis.editable_plots import save_editable_figure


def read(path):
    with path.open(encoding='utf-8-sig', newline='') as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        for key in ['utc_s', 'parent_video_s', 'east_m', 'north_m', 'up_m',
                    'latitude_deg', 'longitude_deg', 'height_ellipsoid_m']:
            row[key] = float(row[key])
        row['quality'] = int(row['quality'])
    return rows


def assess(candidate, reference, agreement=None):
    """Screen repeatability/coverage and bidirectional ambiguity corroboration."""
    reasons = []
    static = candidate['static']
    if static is None or static['count'] < 228:
        reasons.append('静止区間の観測不足')
    if static:
        if static['horizontal_rms_m'] > reference['static']['horizontal_rms_m']:
            reasons.append('静止水平RMSが従来より大きい')
        if static['three_d_rms_m'] > reference['static']['three_d_rms_m']:
            reasons.append('静止3D RMSが従来より大きい')
    if agreement is not None:
        both = agreement['both_fixed']
        if both['count'] < 30:
            reasons.append('前後両方向Fixedの共通時刻が30点未満')
        if both.get('within_h02_v04_fraction', 0) < .9:
            reasons.append('両方向Fixedの水平0.2m・鉛直0.4m以内一致が90%未満')
    return reasons


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment', required=True, choices=['20261028_ngt_000'])
    args = parser.parse_args()
    eid = args.experiment
    out = Path('results')/eid/'ppk_base_comparison'
    manifest_path = out/'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if len(manifest['runs']) != 40 or manifest['failures']:
        raise RuntimeError('Complete and inspect all 40 runs before selecting')
    old_path = Path('data/processed')/eid/'gnss/ppk_positions.csv'
    old = read(old_path)
    reference = metrics(old)
    candidates = {}
    all_rows = {'existing': old}
    for key, run in manifest['runs'].items():
        rows = read(Path(run['positions']['path']))
        all_rows[key] = rows
        if 'combined' not in key:
            continue
        reasons = assess(run['metrics'], reference,
                         run['direction_agreement'] if key.endswith('_ar') else None)
        candidates[key] = {'rejection_reasons': reasons, 'eligible': not reasons,
                           'metrics': run['metrics'], 'position_file': run['positions']}
    eligible = [key for key, value in candidates.items() if value['eligible']]
    chosen = min(eligible, key=lambda key: (candidates[key]['metrics']['static']['horizontal_rms_m'],
                                          candidates[key]['metrics']['static']['three_d_rms_m'])) if eligible else 'existing'
    # A non-existing choice needs downstream propagation; never silently mark it applied.
    decision = {'selected_variant': chosen, 'application_status': 'existing_retained' if chosen == 'existing' else 'candidate_requires_propagation',
                'reference': hashed(old_path), 'reference_metrics': reference, 'candidates': candidates,
                'inputs': [hashed(manifest_path), hashed(Path(__file__)),
                           hashed(Path('scripts/compare_ngt_ppk_bases.py'))],
                'scope': 'Best supported stability among tested candidates, not demonstrated absolute accuracy',
                'criterion': manifest['predeclared_screen'],
                'calibration': 'No change to raw observations, reference frame, antenna correction or CAD'}
    dump(out/'adoption.json', decision)
    dump(Path('config/20261028_ngt/ppk_adoption.json'), decision)
    plt.rcParams['font.family'] = 'Yu Gothic'
    keys = ['existing']+[key for key in candidates if key.endswith('_ar')]
    labels = ['従来PPK（採用維持）' if chosen == 'existing' else '従来PPK']
    labels += [manifest['runs'][k]['station_name']+(' 補間ON' if '/interp_' in k else ' 補間OFF') for k in keys[1:]]
    values = [reference]+[candidates[k]['metrics'] for k in keys[1:]]
    fig, axes = plt.subplots(1, 3, figsize=(14, 7.8), layout='constrained')
    y = np.arange(len(keys))
    axes[0].barh(y, [v['static']['horizontal_rms_m'] for v in values], color='#277a92')
    axes[1].barh(y, [v['static']['three_d_rms_m'] for v in values], color='#b57239')
    axes[2].barh(y, [100*v['experiment_fixed']/v['experiment_rows'] for v in values], color='#468c4b')
    for ax, title in zip(axes, ['静止水平RMS [m]', '静止3D RMS [m]', '240–1540秒のFixed割合 [%]'], strict=True):
        ax.set(yticks=y, yticklabels=labels, xlabel=title)
        ax.invert_yaxis()
        ax.grid(axis='x', alpha=.2)
    fig.suptitle('近隣5基準局のPPK比較（AR有効／元の全解を評価）')
    fig.supxlabel('RMSは240–480秒の各平均まわり。Fixed点だけへの絞り込みなし。真値誤差ではない。\n'
                  '補間ONはRTKLIB内の基準局残差時刻補間（age上限30秒）、OFFは上限15秒。', fontsize=9)
    save_editable_figure(fig, out/'base_comparison.png', dpi=150)
    plt.close(fig)
    with (out/'comparison.csv').open('w', encoding='utf-8', newline='') as handle:
        fields = ['variant', 'station', 'distance_km', 'fixed_total', 'rows', 'static_h_rms_m',
                  'static_3d_rms_m', 'static_rows', 'both_fixed_epochs', 'direction_agreement_fraction',
                  'max_step_m', 'step_over_2m_count', 'eligible', 'rejection_reasons']
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for key in ['existing']+list(candidates):
            run = manifest['runs'].get(key, {})
            m = reference if key == 'existing' else candidates[key]['metrics']
            both = run.get('direction_agreement', {}).get('both_fixed', {})
            writer.writerow(dict(zip(fields, [key, run.get('station_name', '従来RTCM基準局'),
                run.get('distance_km', ''), m['fixed'], m['rows'], m['static']['horizontal_rms_m'],
                m['static']['three_d_rms_m'], m['static']['count'], both.get('count', ''),
                both.get('within_h02_v04_fraction', ''), m['max_step_m'], m['step_over_2m_count'],
                key == chosen, '; '.join(candidates.get(key, {}).get('rejection_reasons', []))], strict=True)))
    report = '# 近隣基準局PPKの比較と採用判断\n\n'
    report += ('**従来のMSM7・AR off PPKを採用維持。**' if chosen == 'existing' else f'候補選択：{chosen}（適用前）。')
    report += (' 近隣5局の観測を取得し、各局でcombined/forward/backwardのAR有効とcombined Floatを、'
               '基準局残差補間OFF/ONで比較した（計40条件）。Fixed数のみで採用しなかった。\n\n'
               '[国土地理院の公式データ提供](https://terras.gsi.go.jp/)から2026-10-28 05時台UTCの'
               'RINEX3.02・GRJE放送暦を取得。神戸中央の保存済み取得物は再利用。'
               '兵庫県の稼働局を実験地点からの概算距離で並べ、近い5局を対象とした。\n\n'
               '| 基準局 | 概算距離 | Fixed OFF→ON | 静止水平RMS OFF→ON | 静止3D RMS OFF→ON |\n'
               '|---|---:|---:|---:|---:|\n')
    for station in manifest['stations']:
        code = station['observation_cd']
        a, b = [candidates[f'{code}/{prefix}combined_ar']['metrics'] for prefix in ['', 'interp_']]
        report += (f'| {station["observation_name"]} {code} | {station["distance_km"]:.1f} km | '
                   f'{a["fixed"]}→{b["fixed"]} | {a["static"]["horizontal_rms_m"]:.3f}→{b["static"]["horizontal_rms_m"]:.3f} m | '
                   f'{a["static"]["three_d_rms_m"]:.3f}→{b["static"]["three_d_rms_m"]:.3f} m |\n')
    report += (f'\n表のOFF/ONは残差時刻補間で、どちらも整数固定ARは有効。Fixedは全収録の点数。'
               f'従来PPKの静止240点：水平RMS {reference["static"]["horizontal_rms_m"]:.3f} m、'
               f'3D RMS {reference["static"]["three_d_rms_m"]:.3f} m。\n\n'
               '![基準局比較](base_comparison.png)\n\n'
               '## 採用基準と結果\n\n'
               '事前に、静止区間の228点以上を保持し、水平・3D RMSがともに従来以下、'
               'AR候補は前後両方向Fixedの共通時刻30点以上かつ水平0.2m・鉛直0.4m以内の一致が90%以上、'
               'という保守的な採用条件を固定した。これは整合性の検査であり絶対精度の証明ではない。'
               '各候補の不採用理由は[採用記録](adoption.json)と[比較CSV](comparison.csv)に保存。'
               '同じ観測を使う前後解は独立した真値ではない。全静止点と前半・後半のばらつきも残した。\n\n'
               '30秒間隔の基準局を使うため、[RTKLIBの残差時刻補間](https://github.com/tomojitakasu/RTKLIB/blob/master/src/rtkpos.c)'
               'を追加検証した。ONでは基準局観測との時刻差上限を30秒、OFFでは15秒とし、差分を設定ファイルに記録。'
               '観測RINEXの内容を補間・上書きしていない。補間の有無は測定データの増加を意味しない。\n\n'
               'RINEXヘッダー座標はモデル概略座標で、当日の高精度座標ではない。'
               '外部ANTEX・受信アンテナ測定中心・座標エポック補正は未適用。'
               'この条件では「最も絶対精度が高い」と認定できない。'
               '[国土地理院FAQ](https://www.gsi.go.jp/denshi/denshi_faq.html)。\n\n'
               '[全40実行の入力ハッシュ・設定・指標](manifest.json)。'
               '生RINEX・ログ・POS・CSVは `data/interim/20261028_ngt_000/ppk_base_comparison/`、'
               '神戸中央の取得物は既存 `gnss_geonet_950356/`。解析失敗・未採用結果も保持する。\n\n')
    config = json.loads(Path('config/20261028_ngt/analysis.json').read_text())
    segments = [{'experiment_id': eid, 'parent_video_interval_s': [old[0]['parent_video_s'], old[-1]['parent_video_s']+1]}]+config['segments']
    for segment in segments:
        sub_id = segment['experiment_id']
        sub = Path('results')/sub_id/'ppk_base_comparison'
        sub.mkdir(parents=True, exist_ok=True)
        lo, hi = segment['parent_video_interval_s']
        fig, axes = plt.subplots(6, 1, figsize=(12, 11), sharex=True, layout='constrained')
        for ax, code in zip(axes, ['existing']+[s['observation_cd'] for s in manifest['stations']], strict=True):
            run_keys = ['existing'] if code == 'existing' else [code+'/combined_ar', code+'/interp_combined_ar']
            for key in run_keys:
                records = [r for r in all_rows[key] if lo <= r['parent_video_s'] < hi]
                ax.plot([r['parent_video_s'] for r in records],
                        [{1: 3, 2: 2, 4: 1, 5: 0}.get(r['quality'], -1) for r in records],
                        '.', ms=3, alpha=.7, label='従来採用解' if key == 'existing' else
                        ('補間ON' if '/interp_' in key else '補間OFF'))
            ax.set(yticks=[0, 1, 2, 3], yticklabels=['Single', 'DGPS', 'Float', 'Fixed'], ylim=(-.3, 3.4),
                   ylabel='従来PPK' if code == 'existing' else next(s['observation_name'] for s in manifest['stations'] if s['observation_cd'] == code))
            ax.grid(alpha=.2)
            ax.legend(loc='upper right', fontsize=8)
        axes[-1].set(xlim=(lo, hi), xlabel='親機原動画時刻 [s]')
        fig.suptitle(sub_id+'　各基準局PPKの品質（点のみ、欠測を接続しない）')
        save_editable_figure(fig, sub/'base_quality.png', dpi=140)
        plt.close(fig)
        selected = [r for r in all_rows[chosen] if lo <= r['parent_video_s'] < hi]
        with (sub/'adopted_ppk_positions.csv').open('w', newline='', encoding='utf-8') as handle:
            writer = csv.DictWriter(handle, fieldnames=list(all_rows[chosen][0]))
            writer.writeheader()
            writer.writerows(selected)
        if sub_id == eid:
            report += '![全収録の各局品質](base_quality.png)\n\n'
        else:
            sub_report = (f'# {sub_id} 近隣基準局PPK比較\n\n'
                          f'採用：{chosen}。従来PPKを維持したため既存RTK/PPK選択・IMU併用軌跡は変更なし。\n\n'
                          '[採用判断と全局比較](../../20261028_ngt_000/ppk_base_comparison/report.md)・'
                          '[この区間の採用PPK](adopted_ppk_positions.csv)・'
                          '[採用中のRTK/PPK品質選択と航空写真](../gnss_selection/report.md)。\n\n'
                          '![各基準局の品質](base_quality.png)\n\n'
                          'PNG/SVG/figure.pickle/plot.jsonを保存。共通ブラウザエディタで編集可能。\n')
            (sub/'report.md').write_text(sub_report, encoding='utf-8')
            report += f'- [{sub_id} の品質と採用結果](../../{sub_id}/ppk_base_comparison/report.md)\n'
    report += ('\n図は編集可能。例：\n\n```powershell\n'
               f'python -m aquabeacon_analysis.editable_plots results/{eid}/ppk_base_comparison/base_comparison.figure.pickle\n```\n')
    (out/'report.md').write_text(report, encoding='utf-8')
    print('selected:', chosen, 'eligible:', eligible)


if __name__ == '__main__':
    main()
