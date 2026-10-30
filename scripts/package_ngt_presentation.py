"""Index verified NGT presentation outputs without recomputing or modifying measurements."""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment', choices=['20261028_ngt_000'], required=True)
    args = parser.parse_args()
    output = ROOT/'results'/args.experiment/'presentation'
    output.mkdir(parents=True, exist_ok=True)
    links = []

    def url(relative):
        target = ROOT/relative
        if not target.is_file():
            raise FileNotFoundError(target)
        links.append(target)
        return Path(os.path.relpath(target, output)).as_posix()

    def anchor(relative, label, cls=''):
        return f'<a class="{cls}" href="{html.escape(url(relative), quote=True)}">{html.escape(label)}</a>'

    def picture(relative, alt):
        return f'<img loading="lazy" src="{url(relative)}" alt="{html.escape(alt, quote=True)}">'

    prefix = 'results/20261028_ngt_004/'
    mapping = json.loads((ROOT/prefix/'presentation_mapping/manifest.json').read_text('utf-8'))
    sections = []
    replay = prefix+'presentation_v2/'
    has_replay = all((ROOT/replay/f'render/{v}/{v}_1080.mp4').is_file()
                     for v in ('position_attitude', 'map_vehicles', 'map_only'))
    if has_replay:
        def movie(variant, label):
            base = replay+f'render/{variant}/{variant}'
            return ('<video controls preload="none" playsinline poster="'+url(base+'_1080.png')
                +'" aria-label="'+html.escape(label, quote=True)+'"><source src="'
                +url(base+'_1080.mp4')+'" type="video/mp4">'
                +anchor(base+'_1080.mp4', label)+'</video><p class="actions">'
                +anchor(base+'_1080.mp4', 'MP4 / 1080p')+' · '
                +anchor(base+'.blend', 'Blenderで調整')+' · '
                +anchor(base+'_clean_1080.png', '貼り付け用PNG')+' · '
                +anchor(base+'_clean.blend', '静止画のBlender')+'</p>')

        sections.append('<section id="motion"><div><p class="eyebrow">発表用 / 位置と姿勢</p>'
            '<h2>親子の動きと、機体の向きを見る</h2>'
            '<p>推定位置と磁北基準の姿勢を実CADに反映しました。'
            '右側の拡大表示で、それぞれの機体の向きも確認できます。</p>'
            '<p>色・照明・カメラ・文字・再生速度はBlenderで調整できます。'
            '発表映像には2Dグラフの編集UIを組み込まず、完成画の見やすさを優先しました。</p>'
            '<p class="note">方位の精度は未検証です。子機CADと機体前方の対応、CAD原点と測定中心は'
            '暫定です。推定のない時刻は欠測として表示します。</p></div><div>'
            +movie('position_attitude', '親機と子機の位置・姿勢')+'</div></section>')
        sections.append('<section class="wide" id="mapping"><div>'
            '<p class="eyebrow">発表用 / マッピングの進行</p><h2>写真の観測に合わせて、点群が増えていく</h2>'
            '<p>壁を捉えた9区間・94,455点を、元の座標と色のまま再生します。'
            '親子の位置・姿勢が分かる版と、点群に集中できるマップだけの版を用意しました。</p>'
            '<div class="movie-grid"><div><h3>親機・子機と一緒に</h3>'
            +movie('map_vehicles', '地図と親子機の観測順再生')
            +'</div><div><h3>マップだけ</h3>'+movie('map_only', '地図のみの観測順再生')+'</div></div>'
            '<p class="note">既存の位置拘束型COLMAPで得た最終点群を、各点の2回目の画像観測時刻に'
            '合わせて表示します。処理中のSLAM生成順や、当時の未確定形状を再現したものではありません。'
            '断片間の重複・未復元部分も保持しています。</p><p>'
            +anchor(replay+'report.md', 'データ・座標・検証の記録')+' · '
            +anchor(prefix+'wall_mapping/native120_restart/report.md', '元のマッピング解析')+' · '
            +anchor(replay+'render/map_only/original_points_local_clean_1080.png', '点群の局所拡大 PNG')
            +'</p></div></section>')
    sections.append('<section id="photos"><div><p class="eyebrow">写真付きの別モデル / 子機のみの映像</p>'
        '<h2>写真を重ねて、地図を見る</h2>'
        f'<p>元の{mapping["original_points"]:,}点に、校正を使って{mapping["textured_photos"]}枚の写真を投影。'
        '回転・拡大、写真と点群の切替、撮影時刻の再生ができます。</p>'
        '<p class="actions">'+anchor(prefix+'presentation_mapping/textured_map.html', '3Dマップを開く', 'primary')
        +anchor(prefix+'presentation_mapping/textured_map.glb', 'GLBを保存')+'</p>'
        '<p>'+anchor(prefix+'presentation_mapping/colored_points.ply', '色付き点群 PLY')+' · '
        +anchor(prefix+'presentation_mapping/textured_map.obj', 'OBJ')+' · '
        +anchor(prefix+'presentation_mapping/report.md', '方法と出典')+'</p>'
        '<p class="note">疎な点群を結ぶ近似表面です。密な完成モデルではありません。'
        '5つの独立地図は接続しません。最大地図のキーフレームは約280秒中の12.53秒に限られ、'
        '単眼尺度・形状精度は未検証です。</p></div>'
        '<div>'+picture(prefix+'presentation_animation/presentation.png', '写真面・子機地図と実CADの発表用静止画')+'</div></section>')
    dense_report = ROOT/prefix/'dense_photogrammetry/report.md'
    if dense_report.is_file():
        sections.append('<section class="wide"><div><h2>複数写真からの追加復元も検証</h2>'
            '<p>ステレオ対応と第三の写真で確認できた局所点は269点でした。'
            'これを連続した高密度モデルとは扱いません。対応しなかった場所を面で埋めず、'
            '最初の失敗試行も残しています。</p><p>'
            +anchor(prefix+'dense_photogrammetry/report.md', '追加再構成の方法・診断・限界')
            +'</p></div></section>')
    sections.append('<details class="legacy"><summary>以前の発表素材を参照する</summary>'
        '<section id="legacy-motion"><div><p class="eyebrow">004 / 以前の出力</p>'
        '<h2>写真面の再生と、中立姿勢の位置表示</h2>'
        '<p>写真面が撮影順に現れる地図再生と、親子の推定位置を実CADで再生する素材です。'
        'Blenderでカメラ、色、文字、タイミングを編集できます。</p>'
        '<p class="actions">'+anchor(prefix+'presentation_animation/presentation.mp4', '写真地図の動画', 'primary')
        +anchor(prefix+'presentation_animation/presentation.blend', '地図のBlender')+'</p>'
        '<p class="actions">'+anchor(prefix+'presentation_positions/cad_positions_720p.mp4', '親子位置の動画', 'primary')
        +anchor(prefix+'presentation_positions/cad_positions.blend', '位置のBlender')+'</p>'
        '<p class="note">CADはcad_20261028の設計スナップショットです。子機のカメラとライトは'
        '本番の位置が逆で、正確な変換は未保存です。機体の向き・測定中心を校正済みとは扱いません。'
        '点の生成時刻は不明なため、地図動画は写真の観測順の再生です。</p></div>'
        '<div>'+picture(prefix+'presentation_positions/cad_positions_1080.png', '実CADによる親子位置の表示')
        +'<p>'+anchor(prefix+'presentation_positions/report.md', '位置表示の座標・欠測・CADの扱い')+' · '
        +anchor(prefix+'presentation_animation/report.md', '地図動画の再生条件')+'</p></div></section></details>')
    static_base = 'results/20261028_ngt_001/gnss_overhead/static_scatter'
    if (ROOT/'results/20261028_ngt_001/presentation_figures/static_scatter_presentation.png').is_file():
        static_base = 'results/20261028_ngt_001/presentation_figures/static_scatter_presentation'
    sections.append('<section id="gnss"><div><p class="eyebrow">001–004 / 平面で比較</p>'
        '<h2>GPSの水平差と、静止時のばらつき</h2>'
        '<p>東と北を同じ縮尺にして、原点からのRMS円と平均差を表示しました。'
        '静止区間001には、各系列の平均位置を中心にした散布図もあります。</p>'
        '<p class="actions">'+anchor(static_base+'.png', '静止時の俯瞰図', 'primary')
        +anchor(static_base+'.svg', 'SVGを保存')
        +(anchor(static_base+'.pdf', 'PDFを保存') if (ROOT/(static_base+'.pdf')).is_file() else '')+'</p>'
        '<p>'+anchor('results/20261028_ngt_001/gnss_overhead/static_scatter.png', '研究用の並列表示')+'</p>'
        '<p class="note">独立した真値がないため、RTK−PPKの差RMSと静止ばらつきを表示しています。'
        '絶対精度や信頼区間を示す円ではありません。</p></div><div>'
        +picture(static_base+'.png', '静止区間のRTKとPPKの水平散布図')+'</div></section>')
    names = ['静止', '子機静止', '距離・視野限界', '親子同時移動']
    rows = []
    for i, name in enumerate(names, 1):
        eid = f'20261028_ngt_{i:03}'
        base = f'results/{eid}/'
        metrics = json.loads((ROOT/base/'gnss_overhead/metrics.json').read_text('utf-8'))
        diff = metrics['difference_rtk_minus_ppk']
        rows.append(f'<tr><th scope="row">{i:03} {name}</th><td>{diff["count"]:,}</td>'
            f'<td>{diff["horizontal_rms_m"]:.3f} m</td><td>'
            +anchor(base+'gnss_overhead/horizontal_difference.png', '俯瞰図')+' / '
            +anchor(base+'gnss_overhead/horizontal_difference.svg', 'SVG')+'</td><td>'
            +anchor(base+'aerial_hires/aerial_source_comparison.png', '航空写真の比較')+' / '
            +anchor(base+'gnss_overhead/report.md', '根拠')+'</td></tr>')
    sections.append('<section class="wide"><div><h2>区間ごとの図とデータ</h2>'
        '<div class="table"><table><thead><tr><th>区間</th><th>対応数</th><th>水平差RMS</th>'
        '<th>RTK−PPK</th><th>表示・説明</th></tr></thead><tbody>'+''.join(rows)+'</tbody></table></div>'
        '<p class="note">対応許容幅20 ms・一対一の最近傍時刻。元の有効フラグと棄却フラグを保持し、'
        '新たな外れ値除去・補間はしていません。CSVとmanifestは各「根拠」のリンクから参照できます。</p>'
        '<h2 style="margin-top:36px">航空写真の細部も、見やすく</h2>'
        '<p>従来の国土地理院画像に加え、Esri / Vantorの別ソースを比較できます。'
        '右側は2025年11月17日撮影、原画像の解像度は0.46 mです。'
        '同じ座標を使い、画像へ合わせる位置修正はしていません。</p>'
        +picture(prefix+'aerial_hires/aerial_source_comparison.png', '同じGNSS座標でのGSIとEsri画像の比較')
        +'<p>'+anchor(prefix+'aerial_hires/aerial_source_comparison.svg', '背景比較 SVG')+' · '
        +anchor(prefix+'aerial_hires/report.md', '出典・利用条件・解像度')+' · '
        +anchor(prefix+'aerial/aerial_tracks.svg', 'GSI上のIMU併用軌跡 SVG')+'</p>'
        '<p class="note">図中の出典は発表で使う際も残してください。配信画素は約0.246 mですが、'
        '原画像以上の細部や測位の真値を示すものではありません。</p></div></section>')
    position_links = ' · '.join(anchor(f'results/20261028_ngt_{i:03}/position_visualization/viewer.html', f'区間{i:03} 3D時刻表示') for i in (2, 3, 4))
    gallery_links = ' · '.join(anchor(f'results/20261028_ngt_{i:03}/research_figures/index.html',
        f'{i:03}の2Dグラフ一覧') for i in (0, 1, 2, 3, 4)
        if (ROOT/f'results/20261028_ngt_{i:03}/research_figures/index.html').is_file())
    sections.append('<section class="wide" id="editing"><div><p class="eyebrow">研究用 / 2Dグラフを調整</p>'
        '<h2>元の図を保って、必要なところを編集する</h2>'
        '<p>タイトル・軸・範囲・縮尺・目盛り・凡例・フォント・線・点・注釈・パネル配置を調整できます。'
        '数値入力とスライダー、図寸法のドラッグに対応し、設定の保存・再読込とPNG/SVG/PDF出力ができます。</p>'
        '<p class="actions">'+gallery_links+'</p><p>一覧から図を選んで編集するには、'
        'プロジェクトのターミナルで次を実行します。2Dグラフの選択画面がブラウザで開きます。</p>'
        '<pre>python scripts/open_ngt_research_figures.py --experiment 20261028_ngt_001 --experiment 20261028_ngt_004</pre>'
        '<p>一覧ページだけを開いた場合は、各図のコマンドでも編集できます。'
        '初期の配色・凡例・縦横比はそのまま使い、指定した項目だけを変えます。'
        '特殊な図要素は、エディタ内のPython例から元のFigureを編集できます。MATLABの全機能互換ではありません。</p>'
        '<p class="note">元データと元図は上書きしません。見た目の設定は.style.json、'
        '書き出しは.edited.png/.svg/.pdfに保存します。使い終わったら「保存して終了」。'
        '30分無操作でも編集サーバーは終了します。</p>'
        '<h3>3Dは元プロジェクトから調整</h3><p>映像はBlenderでカメラ・光・色・文字・タイミングを調整します。'
        '写真付きGLBはテクスチャを埋め込み済みです。OBJを移す場合はMTLとtexturesフォルダも添えてください。</p>'
        '<p>従来の対話式位置ビュー：'+position_links+'</p><p>'
        +anchor('results/20261028_ngt_000/presentation/report.md', '検証・再生成の記録')+'</p></div></section>')
    style = '''
:root{--bg:#F1F6F8;--ink:#1A2023;--blue:#00739C;--line:#C7D3D8;--muted:#4F5759}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.75 "BIZ UDPGothic","Yu Gothic",sans-serif}
header{background:#fff;border-bottom:8px solid var(--blue);padding:42px max(24px,calc((100vw - 1300px)/2)) 28px}h1{font-size:clamp(26px,3vw,42px);line-height:1.35;margin:8px 0 14px}h2{font-size:clamp(23px,2.1vw,31px);line-height:1.45;margin:0 0 20px}.eyebrow{color:var(--blue);font-weight:700;font-size:14px;letter-spacing:.04em;margin:0 0 7px}
nav{display:flex;gap:20px;flex-wrap:wrap;margin-top:20px}a{color:#00688E;text-underline-offset:4px}a:hover{color:#003825}a:focus-visible{outline:3px solid #d65a19;outline-offset:5px}.primary{background:var(--blue);color:#fff;text-decoration:none;padding:10px 17px;border-radius:4px;display:inline-block}.primary:hover{background:#005c7c;color:#fff}.actions{display:flex;flex-wrap:wrap;gap:12px;align-items:center}
main{max-width:1360px;margin:auto;padding:0 24px}section{display:grid;grid-template-columns:1fr 1.25fr;gap:42px;padding:44px 0;border-bottom:1px solid var(--line);align-items:center}section.wide{grid-template-columns:1fr}img,video{display:block;width:100%;height:auto;border:1px solid var(--line);background:#fff}video{aspect-ratio:16/9}.movie-grid{display:grid;grid-template-columns:1fr 1fr;gap:28px}.legacy{padding:24px 0;border-bottom:1px solid var(--line)}summary{cursor:pointer;min-height:44px;font-weight:bold}.note{color:var(--muted);font-size:14px;border-left:3px solid var(--line);padding-left:14px}.table{overflow:auto}table{border-collapse:collapse;width:100%;background:white}th,td{text-align:left;padding:13px 16px;border-bottom:1px solid var(--line);white-space:nowrap}thead{background:#e0edf2}pre{overflow:auto;padding:20px;background:#fff;border:1px solid var(--line);font:13px/1.6 monospace}li{margin-bottom:12px}footer{max-width:1360px;margin:0 auto;padding:24px;color:var(--muted);font-size:13px}
@media(max-width:820px){header{padding:26px 20px}main{padding:0 20px}section,.movie-grid{grid-template-columns:1fr;gap:20px;padding:32px 0}nav{gap:15px}.actions>a{min-height:44px;display:inline-flex;align-items:center}}@media(prefers-reduced-motion:reduce){html{scroll-behavior:auto}}
'''
    motion_anchor = 'motion' if has_replay else 'legacy-motion'
    mapping_anchor = 'mapping' if has_replay else 'photos'
    document = '<!doctype html><html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>長田高校プール｜解析・発表素材</title><style>'+style+'</style><header><p class="eyebrow">AquaBeacon / 2026.10.28</p><h1>長田高校プールの解析を、見て伝える</h1><p>発表に使う映像と、細かく編集する研究グラフ。用途に合わせて選べます。</p><nav aria-label="ページ内ナビゲーション"><a href="#'+motion_anchor+'">位置と姿勢</a><a href="#'+mapping_anchor+'">マッピング</a><a href="#gnss">GPSの水平差</a><a href="#editing">2Dグラフを編集</a></nav></header><main>'+''.join(sections)+'</main><footer>データの出典・校正・適用条件・除外の扱いは各結果のmanifestとreportに保存。元のraw、位置推定CSV、再構成点は変更していません。</footer></html>'
    (output/'index.html').write_text(document, 'utf-8')
    record = {'generated_utc': datetime.now(UTC).isoformat(), 'experiment_id': args.experiment,
        'git_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        'script_sha256': sha(Path(__file__)), 'index_sha256': sha(output/'index.html'),
        'linked_files': [{'path': p.relative_to(ROOT).as_posix(), 'sha256': sha(p)}
                         for p in sorted(set(links))],
        'data_policy': 'Indexes existing generated outputs; no measurement changes',
        'browser_verification': {'index': 'Not performed: prior browser policy rejected file protocol access',
            'research_editor': 'Standard loopback editor verified with QA copy: ranges, markers, legend, ticks, grid, drag width, save/reload and shutdown'}}
    (output/'manifest.json').write_text(json.dumps(record, indent=2), 'utf-8')
    print(output/'index.html')


if __name__ == '__main__':
    main()
