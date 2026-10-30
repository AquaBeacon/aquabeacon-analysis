"""Verify report evidence, interval accounting, camera provenance and editable outputs."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import unquote

import cv2
import numpy as np
from prepare_ngt_research_report import CONFIG_PATH, MAP, OUT, ROOT, digest, read_csv, write_json

from aquabeacon_analysis.editable_plots import PlotDocument
from aquabeacon_analysis.ngt_research_review import position_state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment', required=True, choices=['20261028_ngt_000'])
    parser.parse_args()
    cv2.setNumThreads(2)
    process = {'pid':os.getpid(), 'owner':'ngt_research_report', 'purpose':'Validate evidence and deliverables',
        'started_utc':datetime.now(UTC).isoformat(), 'status':'running'}
    write_json(OUT / 'verification.process.json', process)
    checks = []

    def check(name, condition, detail=''):
        checks.append({'check':name, 'passed':bool(condition), 'detail':detail})
        if not condition:
            raise AssertionError(f'{name}: {detail}')

    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
        for name in ['manifest.json','report_manifest.json','map_example_metadata.json']:
            info = json.loads((OUT / name).read_text(encoding='utf-8'))
            for path, sha in info['inputs_sha256'].items():
                check(f'{name}: {path}', digest(ROOT / path) == sha)
            for path, sha in info.get('outputs_sha256',{}).items():
                check(f'{name}: output {path}', digest(OUT / path) == sha)
        intervals = read_csv(OUT / 'position_intervals.csv')
        for exp, duration in [('002',60),('003',150),('004',280)]:
            source = read_csv(ROOT / f'results/20261028_ngt_{exp}/position_evaluation/positions_5hz.csv')
            runs = [r for r in intervals if r['experiment'] == exp]
            restored = [r['state'] for r in runs for _ in range(int(r['samples']))]
            check(f'{exp}: all position states preserved',restored == [position_state(r) for r in source])
            check(f'{exp}: duration partition',abs(sum(float(r['duration_s']) for r in runs)-duration)<1e-7)
            check(f'{exp}: continuous cell edges',all(abs(float(a['end_s'])-float(b['start_s']))<1e-7 for a,b in pairwise(runs)))
        mapping = read_csv(MAP / 'frame_evaluation.csv')
        runs = read_csv(OUT / 'mapping_intervals.csv')
        restored = [r['state'] for r in runs for _ in range(int(r['samples']))]
        original = ['positioned' if r['position_registered']=='True' else 'local' if r['registered_structural']=='True' else 'unregistered' for r in mapping]
        check('all 33564 mapping states preserved',len(restored)==33564 and restored==original)
        bins = read_csv(OUT / 'movement_10s_bins.csv')
        boundary = read_csv(OUT / 'movement_boundary_frames.csv')
        check('10-second bins and explicit boundary spill preserve all denominators',sum(int(r['mapping_frames']) for r in bins)+len(boundary)==33564 and sum(int(r['position_cells']) for r in bins)==1400)
        check('boundary spill matches native source rows', [r['source_frame'] for r in boundary]==[r['source_frame'] for r in mapping if not 1260 <= float(r['parent_video_s']) <1540])
        stats = json.loads((OUT / 'statistics.json').read_text(encoding='utf-8'))
        check('mapping summary agrees with row flags',sum(v=='positioned' for v in original)==stats['mapping']['position_registered_unique'] and sum(v!='unregistered' for v in original)==stats['mapping']['registered_structural_unique'])
        audit = json.loads((OUT / 'orientation_audit.json').read_text(encoding='utf-8'))
        check('rotation pixel audit recorded',len(audit['pixel_checks'])==3 and all(r['clockwise_pixel_equality'] for r in audit['pixel_checks']))
        evidence = read_csv(OUT / 'frame_evidence.csv')
        check('72 frame records',len(evidence)==72)
        caps = {}
        try:
            for r in evidence:
                path = OUT / r['file']
                saved = cv2.imread(str(path))
                check(f"{r['file']}: hash",digest(path)==r['sha256'])
                check(f"{r['file']}: aspect",saved.shape[1::-1] == ((720,1280) if r['role']=='parent' else (1280,720)))
                check(f"{r['file']}: time alignment",abs(float(r['actual_parent_s'])-float(r['requested_parent_s'])) < .018)
                src = ROOT / r['source']
                if src.suffix.lower()=='.jpg':
                    frame = cv2.imread(str(src))
                else:
                    if str(src) not in caps:
                        caps[str(src)] = cv2.VideoCapture(str(src),cv2.CAP_FFMPEG,[cv2.CAP_PROP_N_THREADS,2])
                    cap = caps[str(src)]
                    cap.set(cv2.CAP_PROP_POS_FRAMES,int(r['source_frame']))
                    ok, frame = cap.read()
                    check(f"{r['file']}: decode source",ok)
                check(f"{r['file']}: exact source pixels",np.array_equal(saved,frame))
        finally:
            for cap in caps.values():
                cap.release()
        frame_rows = read_csv(OUT / 'clip_frames.csv')
        check('1932 review frames',len(frame_rows)==1932)
        for case in cfg['cases']:
            expected = round((case['clip_s'][1]-case['clip_s'][0])*cfg['clip_fps'])
            selected = [r for r in frame_rows if r['case']==case['id']]
            check(f"{case['id']}: output frame accounting",len(selected)==expected and [int(r['clip_frame']) for r in selected]==list(range(expected)))
            check(f"{case['id']}: paired time error below one source frame",all(abs(float(r['parent_pts_s'])-float(r['child_actual_parent_s']))<.034 for r in selected))
            cap = cv2.VideoCapture(str(OUT / f"clips/{case['id']}.mp4"),cv2.CAP_FFMPEG,[cv2.CAP_PROP_N_THREADS,2])
            try:
                check(f"{case['id']}: fps, duration and dimensions",cap.get(cv2.CAP_PROP_FRAME_COUNT)==expected and cap.get(cv2.CAP_PROP_FPS)==12 and cap.get(cv2.CAP_PROP_FRAME_WIDTH)==1344 and cap.get(cv2.CAP_PROP_FRAME_HEIGHT)==768)
                for ix in [0,expected//2,expected-1]:
                    cap.set(cv2.CAP_PROP_POS_FRAMES,ix)
                    ok,_=cap.read()
                    check(f"{case['id']}: decode {ix}",ok)
            finally:
                cap.release()
        for path, stat in json.loads((OUT / 'source_video_stat.json').read_text(encoding='utf-8')).items():
            now=Path(path).stat()
            check('raw unchanged: '+Path(path).name,(now.st_size,now.st_mtime_ns)==(stat['size'],stat['mtime_ns']))
        names=['position_timeline','movement_mapping_timeline','orientation_audit','map_example_local']+[f"case_{c['id']}" for c in cfg['cases']]
        for name in names:
            check(f'{name}: editable companions',all((OUT / (name+ext)).is_file() for ext in ['.png','.svg','.figure.pickle','.plot.json']))
        # Exercise the real output through the same editor backend; no temporary server is needed.
        with TemporaryDirectory(prefix='verify_editor_',dir=OUT) as temp:
            path=Path(temp)/'timeline.figure.pickle'
            original_path=OUT/'movement_mapping_timeline.figure.pickle'
            shutil.copy2(original_path,path)
            master_hash=digest(path)
            document=PlotDocument(path)
            data_before=[line.get_ydata().copy() for line in document.figure.axes[2].lines]
            document.update({'figure':{'width':10,'height':6},'axes':{'2':{'xlabel':'確認用時間 [s]', 'xmin':1260,'xmax':1540,'ymin':-5,'ymax':105}}})
            document.save()
            restored_doc=PlotDocument(path)
            check('editor dimensions and ranges round trip',np.allclose(restored_doc.figure.get_size_inches(),[10,6]) and restored_doc.figure.axes[2].get_xlim()==(1260.,1540.) and restored_doc.figure.axes[2].get_ylim()==(-5.,105.))
            check('editor data and master preserved',digest(path)==master_hash and all(np.array_equal(a,b.get_ydata(),equal_nan=True) for a,b in zip(data_before,restored_doc.figure.axes[2].lines,strict=True)))
            check('editor PNG SVG PDF exports',all((Path(temp)/('timeline.edited.'+ext)).is_file() for ext in ['png','svg','pdf']))
        # validation.md is the only file produced after this link pass.
        for doc in [OUT/'report.md', OUT/'media_review.md']:
            text=doc.read_text(encoding='utf-8')
            for target in re.findall(r'\]\(([^)]+)\)',text):
                if '://' in target or target.startswith('#') or target=='validation.md':
                    continue
                path=doc.parent/unquote(target.split('#')[0])
                check(f'{doc.name}: link {target}',path.exists())
        check('report explicitly withholds legacy coordinate accuracy','座標不整合' in (OUT/'report.md').read_text(encoding='utf-8'))
        process['status']='completed'
    except BaseException as error:
        process.update(status='failed',error=str(error))
        raise
    finally:
        process['ended_utc']=datetime.now(UTC).isoformat()
        write_json(OUT/'verification.process.json',process)
        write_json(OUT/'verification.json',{'status':process['status'],'checks':checks,'created_utc':datetime.now(UTC).isoformat()})
        shutil.copy2(__file__,OUT/'verify.py')
    lines=['# 研究発表用レポートの検証', '',
        f"自動照合：{len(checks)}項目 PASS。詳細は[verification.json](verification.json)。", '',
        '- 全5 Hzセルの状態・時間と33,564画像の状態を区間台帳から復元し、分母と末尾を確認。',
        '- 72枚について出典フレームを再デコードし、抽出PNGとの全画素一致、縦横比、時刻を確認。',
        '- 12短編・1,932表示フレームの対応を確認。先頭・中間・末尾をデコードし、12 fps・寸法・枚数を確認。',
        '- 入力CSV・校正・設定・生成コードのSHA256、参照したraw動画のサイズ・mtime不変を確認。raw全体の再ハッシュは行っていない。',
        '- 16図のPNG/SVG/figure.pickle/plot.jsonを確認。実際のタイムラインを共通エディタのバックエンドで編集し、寸法・軸範囲の保存再読込とPNG/SVG/PDF出力、元値・欠測の保持を確認。ブラウザUIは今回実操作していない。',
        '- Markdown内のローカルリンクを確認。', '', '## 手動確認', '',
        '親機の既存5秒刻みのレビュー画像と子機の既存レビュー画像を参照し、新規P1〜P7・M1〜M5の代表画像、回転監査図、成立範囲図、局所点群例を目視した。ライトの画角離脱・前景の重なり・壁の可視性を所見JSONへ記録した。原動画全体の連続視聴や個々の特徴対応の手動正解付けは行っていない。', '',
        '映像・ログの対応の検証と位置座標の妥当性は別である。旧親機画像の回転不整合は未解消のため、旧座標の精度・地図尺度は確定しない。', '',
        '## テスト・lint・後片付け', '',
        '実行結果は[品質確認記録](quality_checks.json)と[プロセス確認](process_cleanup.json)へ保存する。']
    (OUT/'validation.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print(f'{len(checks)} evidence checks PASS')


if __name__=='__main__':
    main()
