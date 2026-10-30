"""Bounded static-report comparison with documented local World Imagery coverage."""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import subprocess
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import matplotlib
import numpy as np
from PIL import Image

matplotlib.use('Agg')
import matplotlib.pyplot as plt

from aquabeacon_analysis.aerial_tracks import broken_track, mercator
from aquabeacon_analysis.editable_plots import save_editable_figure

BASE = 'https://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer'
OUT = Path('results/20261028_ngt_004/aerial_hires')
TERMS = 'https://doc.arcgis.com/en/arcgis-online/reference/static-maps.htm'
CONFIG = Path('config/20261028_ngt/aerial_hires.json')


def digest(path):
    return {'path': path.as_posix(), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def load_static_mosaic(request):
    """Reuse the cached composition, otherwise retrieve only the recorded small extent."""
    path = OUT/'world_imagery_static.png'
    if path.exists():
        return
    tiles = request['tiles_for_static_map_only']
    if request['zoom'] != 19 or not 1 <= len(tiles) <= 16:
        raise ValueError('Only the configured bounded z19 static-map extent is supported')
    coordinates = []
    for tile in tiles:
        prefix = BASE+'/tile/19/'
        if not tile['url'].startswith(prefix):
            raise ValueError('Tile URL is outside the recorded official service')
        y, x = map(int, tile['url'][len(prefix):].split('/'))
        coordinates.append((x, y, tile['url']))
    xmin, ymin = min(x for x, _, _ in coordinates), min(y for _, y, _ in coordinates)
    xmax, ymax = max(x for x, _, _ in coordinates), max(y for _, y, _ in coordinates)
    if (xmax-xmin+1)*(ymax-ymin+1) > 16:
        raise ValueError('Refusing unexpectedly large mosaic')
    mosaic = Image.new('RGB', ((xmax-xmin+1)*256, (ymax-ymin+1)*256))
    for x, y, url in coordinates:
        with urllib.request.urlopen(url, timeout=15) as response:
            payload = response.read(2_000_001)
        if len(payload) > 2_000_000:
            raise ValueError('Unexpected tile size')
        with Image.open(io.BytesIO(payload)) as tile:
            if tile.size != (256, 256):
                raise ValueError('Unexpected tile dimensions')
            mosaic.paste(tile.convert('RGB'), ((x-xmin)*256, (y-ymin)*256))
    mosaic.save(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment', required=True, choices=['20261028_ngt_000'])
    parser.add_argument('--refresh-metadata', action='store_true',
                        help='Explicitly request live metadata; changed imagery requires review')
    args = parser.parse_args()
    request = json.loads(CONFIG.read_text())
    OUT.mkdir(parents=True, exist_ok=True)
    load_static_mosaic(request)
    (OUT/'request.json').write_text(json.dumps(request, indent=2), encoding='utf-8')
    params = {'f': 'json', 'geometry': '135.1431,34.6707',
              'geometryType': 'esriGeometryPoint', 'inSR': '4326',
              'spatialRel': 'esriSpatialRelIntersects', 'outFields': '*', 'returnGeometry': 'false'}
    metadata_url = BASE+'/4/query?'+urllib.parse.urlencode(params)
    metadata_path = OUT/'source_metadata.json'
    if metadata_path.exists() and not args.refresh_metadata:
        metadata = json.loads(metadata_path.read_text())
    else:
        with urllib.request.urlopen(metadata_url, timeout=15) as response:
            metadata = json.load(response)
        metadata_path.write_text(json.dumps(metadata, indent=2), encoding='utf-8')
    sources = [f['attributes'] for f in metadata['features']]
    source = max(sources, key=lambda s: s.get('MaxMapLevel', 0))
    # Native-source evidence is local metadata, not the service-wide LOD list.
    if source['MaxMapLevel'] != 19 or source['SRC_RES'] != .46 or source['SRC_DATE'] != 20251117:
        raise ValueError('Source changed from the reviewed frozen metadata; review before presentation')
    plt.rcParams['font.family'] = 'Yu Gothic'
    with Image.open(OUT/'world_imagery_static.png') as image:
        world = np.array(image.convert('RGB'))
    for number in range(1, 5):
        eid = f'20261028_ngt_{number:03d}'
        out = Path('results')/eid/'aerial_hires'
        out.mkdir(exist_ok=True)
        original = Path('results')/eid/'aerial'
        previous = json.loads((original/'manifest.json').read_text())
        origin = previous['origin_wgs84_lat_lon_height']
        offset = mercator(origin[0], origin[1])[0]
        scale = np.cos(np.radians(origin[0]))
        lo, hi = np.array(previous['display_limits_m'])
        gsi_extent = previous['imagery']['native_mosaic_extent_m']
        with Image.open(original/'aerial_native.png') as image:
            gsi = np.array(image.convert('RGB'))
        path = original/'tracks.csv'
        with path.open(encoding='utf-8', newline='') as stream:
            rows = list(csv.DictReader(stream))
        fig, axes = plt.subplots(1, 2, figsize=(13, 7.2), layout='constrained')
        for ax, pixels, extent, title in zip(axes, [gsi, world], [gsi_extent, request['extent_m']],
                ['GSI z18：約0.491 m/配信画素', 'Esri / Vantor z19：原画像0.46 m、配信約0.246 m/画素']):
            ax.imshow(pixels, extent=extent, origin='upper', interpolation='nearest')
            for name, color in [('RTK', '#00ffff'), ('PPK', '#ff3dd2')]:
                selected = [r for r in rows if r['source'] == name and r['kind'] == 'raw']
                times = np.array([float(r['utc_s']) for r in selected])
                ll = np.array([[float(r['latitude_deg']), float(r['longitude_deg'])] for r in selected])
                xy = (mercator(ll[:, 0], ll[:, 1])-offset)*scale
                valid = np.array([r['display_valid'] == 'True' for r in selected])
                line = broken_track(times, xy, valid)
                ax.plot(*line.T, color=color, lw=1.2, label=name+' 元の有効観測')
            ax.set(xlim=(lo[0], hi[0]), ylim=(lo[1], hi[1]), aspect='equal',
                   title=title, xlabel='東方向 [m]', ylabel='北方向 [m]')
            ax.legend(fontsize=8, loc='upper left')
            x, y = lo+[4, 4]
            ax.plot([x, x+10], [y, y], color='white', lw=4)
            ax.text(x+5, y+2, '10 m', color='white', ha='center',
                    bbox={'facecolor': 'black', 'alpha': .6})
            ax.text(.98, .04, '北 ↑', transform=ax.transAxes, ha='right', color='white',
                    bbox={'facecolor': 'black', 'alpha': .6})
        fig.suptitle(eid+'　同一範囲・同一GNSS観測の背景比較', fontsize=14)
        fig.supxlabel('左：国土地理院 全国最新写真／右：Esri, Vantor, Earthstar Geographics, GIS User Community\n'
                      '©2026 Esri and its licensors. All rights reserved. 原画像0.46 mを超える細部は保証しない。', fontsize=8)
        save_editable_figure(fig, out/'aerial_source_comparison.png', dpi=240)
        plt.close(fig)
        manifest = {'experiment_id': eid, 'created_utc': datetime.now(UTC).isoformat(),
                    'git_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
                    'inputs': [digest(p) for p in [path, original/'aerial_native.png',
                               original/'manifest.json', OUT/'world_imagery_static.png',
                               OUT/'request.json', OUT/'source_metadata.json', CONFIG, Path(__file__)]],
                    'metadata_url': metadata_url, 'local_source': source,
                    'terms': TERMS, 'use': 'Static local research/presentation map only; no offline tile package',
                    'sampling_ground_m_per_pixel': .29858214164761665*scale,
                    'native_detail_limit_m': source['SRC_RES'],
                    'comparison_limit': 'GSI tile sampling .491m, original GSI sensor GSD not verified; native detail improvement is modest, not 2x',
                    'alignment': 'Original coordinates and masks unchanged, no manual registration'}
        (out/'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
        (out/'report.md').write_text(f'# {eid} 航空写真の別ソース比較\n\n'
            '![同じ観測の背景比較](aerial_source_comparison.png)\n\n'
            '左は既存GSI、右はEsri World ImageryのVantor Vivid Advanced（GE01、2025-11-17撮影）。'
            '現地メタデータの原画像解像度0.46 m・配信上限19を確認。配信画素は約0.246 mだが原画像以上の細部は増えない。'
            '既存GSIの配信画素約0.491 mに対し別ソースは細部が見やすいが、2倍の原画像解像度を主張しない。'
            '15 cm・7.5 cmメタデータ層には現地を覆うデータがなかった。位置精度SRC_ACC=8.47 mは提供者メタデータで、測位評価の真値ではない。\n\n'
            '4タイルの小範囲だけを静的比較図へ合成し、既存のGNSS選択・数値・GSI画像は変更しない。'
            'RTKとPPK観測の有効マスク・欠測・3秒超ギャップを維持し、画像に合わせた位置修正をしない。\n\n'
            f'[静的レポート・PowerPoint利用条件]({TERMS})は出典併記での利用を明示。'
            'Map image is the intellectual property of Esri and is used herein under license. '
            'Copyright © 2026 Esri and its licensors. All rights reserved.\n\n'
            '[現地メタデータと入力ハッシュ](manifest.json)。PNG/SVG/figure.pickle/plot.jsonを併置し、既存ブラウザ編集経路を使用。\n', encoding='utf-8')
    print('Four static comparisons rendered from four tiles; local metadata .46m, max19 verified.')


if __name__ == '__main__':
    main()
