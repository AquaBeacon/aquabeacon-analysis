"""Assemble the Japanese research report from audited counts and recorded visual observations."""
from __future__ import annotations

import argparse
import json
import os
import shutil
from datetime import UTC, datetime

from prepare_ngt_research_report import CONFIG_PATH, MAP, OUT, ROOT, digest, read_csv, write_json


def table(headers, rows):
    return '\n'.join(['| ' + ' | '.join(headers) + ' |', '| ' + ' | '.join(['---']*len(headers)) + ' |']
        + ['| ' + ' | '.join(str(v) for v in row) + ' |' for row in rows])


def annotate_legacy_reports():
    """Add a dated erratum while retaining the original numerical result and figures."""
    paths = ['20261028_ngt_000/position_evaluation', '20261028_ngt_000/position_visualization',
        '20261028_ngt_004/wall_mapping', '20261028_ngt_004/wall_mapping/native120_restart',
        '20261028_ngt_004/map_visualization']
    records = []
    for name in paths:
        path = ROOT / 'results' / name / 'report.md'
        previous = path.read_text(encoding='utf-8')
        before = digest(path)
        marker = '2026-10-30 座標監査注記'
        if marker not in previous:
            title, tail = previous.split('\n', 1)
            link = os.path.relpath(OUT/'report.md',path.parent).replace('\\','/')
            notice = f'> **{marker}：** 親機画像の90°表示回転と内部校正の座標不整合を確認した。'
            notice += '以下の旧座標に基づくばらつき・角度・子機軌跡・地図尺度は再評価が必要。'
            notice += f'成立フラグは観測の可用性として扱う。[監査と実映像の評価]({link})を参照。'
            path.write_text(title+'\n\n'+notice+'\n'+tail,encoding='utf-8')
        records.append({'file':str(path.relative_to(ROOT)), 'before_sha256':before,
            'after_sha256':digest(path), 'action':'notice_only_original_values_retained'})
    write_json(OUT/'legacy_notice_manifest.json',records)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment', required=True, choices=['20261028_ngt_000'])
    args = parser.parse_args()
    cfg = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
    observations_path = ROOT / 'config/20261028_ngt/research_observations.json'
    template = ROOT / 'scripts/templates/ngt_research_report.md'
    observations = json.loads(observations_path.read_text(encoding='utf-8'))
    stats = json.loads((OUT / 'statistics.json').read_text(encoding='utf-8'))
    doc = template.read_text(encoding='utf-8')
    position_rows = []
    for p in stats['positions']:
        position_rows.append([f"{p['experiment']} {p['label']}", f"{p['start_s']}–{p['end_s']}",
            p['duration_s'], f"{p['position_samples']}/{p['samples']}",
            f"{100*p['magnetic_position_available']:.1f}%", f"{(p['samples']-p['position_samples'])*.2:.1f}",
            f"{p['longest_gap_s']:.1f}"])
    doc = doc.replace('{{POSITION_TABLE}}', table(['区間','親動画 [s]','時間 [s]','成立セル/全セル','成立率','欠測時間 [s]','最長欠測 [s]'], position_rows))
    failures = read_csv(OUT / 'position_failure_patterns.csv')
    labels = {'optical':'光学のみ', 'acoustic':'測距のみ', 'attitude':'姿勢のみ',
        'optical+acoustic':'光学・測距', 'acoustic+attitude':'測距・姿勢'}
    failure_rows = []
    for state, label in labels.items():
        failure_rows.append([label, *[f"{sum(float(r['duration_s']) for r in failures if r['experiment']==exp and r['state']==state):.1f}" for exp in ['002','003','004']]])
    doc = doc.replace('{{FAILURE_TABLE}}', table(['不成立の組合せ','002 [s]','003 [s]','004 [s]'], failure_rows))
    m = stats['mapping']; w = m['wall_interval']
    rows = [['入力画像',f"{m['input_frames']:,}",f"{w['input_frames']:,}"],
        ['形状のある地図への登録',f"{m['registered_structural_unique']:,} ({100*m['registered_structural_unique']/m['input_frames']:.2f}%)",f"{w['structural_registered_unique']:,} ({100*w['structural_fraction']:.2f}%)"],
        ['旧条件で共通座標へ配置',f"{m['position_registered_unique']:,} ({100*m['position_registered_unique']/m['input_frames']:.2f}%)",f"{w['position_registered_unique']:,} ({100*w['positioned_fraction']:.2f}%)"]]
    doc = doc.replace('{{MAPPING_TABLE}}', table(['画像数（重複除去）','全移動280秒','壁時間帯125秒'],rows))
    models = json.loads((MAP / 'models.json').read_text())
    rows = [[f"{v['window']:02d}_{v['model']}",f"{v['first_parent_s']:.2f}–{v['last_parent_s']:.2f}",f"{v['registered_images']:,}",f"{v['points']:,}"]
        for v in models if v['position_registered'] and v['wall_period']]
    doc = doc.replace('{{WALL_MODELS_TABLE}}', table(['モデル','登録画像の時刻範囲 [s]','登録画像数','点数'],rows))
    assert '{{' not in doc
    (OUT / 'report.md').write_text(doc, encoding='utf-8')
    lines = ['# 実カメラ映像の照合と考察', '', '[研究レポートへ戻る](report.md)', '',
        '時刻は親機元動画の秒。動画は同じ時刻の親機・子機を横に並べた12 fps表示である。画像の縦横比は保持し、マッピング入力は約119.88 fpsのままとする。表示フラグは旧解析の候補判定で、位置精度の保証ではない。', '',
        '以下は代表フレームの目視とログの照合による事例分析であり、原動画全体の連続視聴判定ではない。事例は成立・欠測の切替と反例を意図的に選んでいる。図の静止画が動画範囲の前後を含む場合は時刻を明記した。', '',
        '## 動画一覧', '', table(['事例','親動画 [s]','同期動画','確認する点'],[[c['id'],f"{c['clip_s'][0]}–{c['clip_s'][1]}",f"[{c['id']}.mp4](clips/{c['id']}.mp4)",c['title']] for c in cfg['cases']])]
    for c in cfg['cases']:
        item = observations['cases'][c['id']]
        lines += ['',f"## {c['id']}　{c['title']}",'',
            f"[同期動画を開く](clips/{c['id']}.mp4)　親動画 {c['clip_s'][0]}〜{c['clip_s'][1]}秒。代表静止画：" + '、'.join(str(t) for t in c['samples_s'])+'秒。', '',
            f"![{c['id']} 実カメラ画像](case_{c['id']}.png)", '',
            f"**映像で確認した事実：** {item['observed']}", '', f"**ログとの対応：** {item['logs']}", '',
            f"**考察：** {item['interpretation']}", '', f"**未確定事項：** {item['unresolved']}"]
    lines += ['', '## 出典と確認範囲', '',
        '[72枚の元フレーム・画像ハッシュ・実時刻](frame_evidence.csv)、[短編1,932フレームの元動画対応](clip_frames.csv)、[数値集計](statistics.json)、[検証結果](validation.md)。各短編は先頭・中間・末尾のデコード、フレーム数、fpsを確認した。映像中の原因を確定するための追加試行は実施していない。']
    (OUT / 'media_review.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    annotate_legacy_reports()
    shutil.copy2(__file__, OUT / 'write_report.py')
    shutil.copy2(template, OUT / 'report_template.md')
    inputs = [CONFIG_PATH, observations_path, template, ROOT / 'scripts/write_ngt_research_report.py',
        OUT / 'statistics.json', OUT / 'orientation_audit.json', OUT / 'manifest.json', MAP / 'models.json']
    write_json(OUT / 'report_manifest.json', {'experiment_id':args.experiment, 'created_utc':datetime.now(UTC).isoformat(),
        'inputs_sha256':{str(p.relative_to(ROOT)):digest(p) for p in inputs},
        'outputs_sha256':{str(p.relative_to(OUT)):digest(p) for p in [OUT/'report.md', OUT/'media_review.md']},
        'observation_scope':observations['review_method']})
    for exp in ['002','003','004']:
        directory = ROOT / f'results/20261028_ngt_{exp}/research_report'
        directory.mkdir(exist_ok=True)
        (directory / 'report.md').write_text(f'# 長田高校 {exp} 研究発表用の評価\n\n'
            '[統合レポート](../../20261028_ngt_000/research_report/report.md) / '
            '[実映像との照合](../../20261028_ngt_000/research_report/media_review.md)。\n\n'
            '親機画像の表示回転と内部校正の不整合を検出。旧座標の精度・尺度は再評価を要する。\n', encoding='utf-8')
    print(f'Written {OUT / "report.md"}')


if __name__ == '__main__':
    main()
