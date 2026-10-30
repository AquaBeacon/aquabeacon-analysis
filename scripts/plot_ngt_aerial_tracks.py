"""Plot explicit NGT segments on cached GSI aerial imagery using existing fusion."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import subprocess
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import matplotlib
import numpy as np
from PIL import Image

matplotlib.use('Agg')
import matplotlib.pyplot as plt

from aquabeacon_analysis.aerial_tracks import broken_track, enu_to_geodetic, mercator
from aquabeacon_analysis.editable_plots import save_editable_figure


def read(path):
    with path.open(encoding='utf-8-sig', newline='') as stream:
        return list(csv.DictReader(stream))


def digest(path):
    return {'path': path.as_posix(), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment', required=True, choices=['20261028_ngt_000'])
    args = parser.parse_args()
    print(json.dumps({'pid': os.getpid(), 'started_utc': datetime.now(UTC).isoformat(),
                      'purpose': 'bounded aerial track rendering; no server'}), flush=True)
    config_path = Path('config/20261028_ngt/analysis.json')
    config = json.loads(config_path.read_text())
    base = Path('results') / args.experiment
    raw_path = Path('data/processed') / args.experiment / 'gnss/rtk_nav_pvt.csv'
    origin_row = next(r for r in read(raw_path) if r['fix_valid'] == 'True'
                      and r['utc_valid'] == 'True' and int(r['fix_type']) >= 3)
    origin = [float(origin_row[k]) for k in
              ['latitude_deg', 'longitude_deg', 'height_ellipsoid_m']]
    offset = mercator(origin[0], origin[1])[0]
    scale = np.cos(np.radians(origin[0]))
    tracks, points = {}, []
    for segment in config['segments']:
        eid = segment['experiment_id']
        tracks[eid] = []
        for source, name in [('RTK', 'parent_position'),
                             ('PPK', 'parent_position_ppk_alternative')]:
            path = Path('results') / eid / 'fusion' / f'{name}.csv'
            rows = read(path)
            times = np.array([float(r['utc_s']) for r in rows])
            valid = np.array([r['input_valid'] == 'True' for r in rows])
            reset = np.array([r['state_reinitialized'] == 'True' for r in rows])
            converted = {}
            for kind in ['raw', 'fused']:
                enu = np.array([[float(r[f'{kind}_{axis}_m']) for axis in
                                 ['east', 'north', 'up']] for r in rows])
                llh = enu_to_geodetic(enu, origin)
                xy = (mercator(llh[:, 0], llh[:, 1])-offset)*scale
                mask = valid if kind == 'raw' else np.isfinite(xy).all(axis=1)
                converted[kind] = (llh, xy, mask)
                points.extend(xy[mask & np.isfinite(xy).all(axis=1)])
            tracks[eid].append((source, path, rows, times, reset, converted))
    points = np.asarray(points)
    center = (points.min(axis=0)+points.max(axis=0))/2
    span = max(65, float(np.ptp(points, axis=0).max())+20)
    lo, hi = center-span/2, center+span/2
    # A common extent makes all four experiments directly comparable.
    low, high = lo/scale+offset, hi/scale+offset
    half = np.pi*6378137
    zoom = 18
    size = 2*half/(2**zoom)
    xmin, xmax = np.floor((np.array([low[0], high[0]])+half)/size).astype(int)
    ymin, ymax = np.floor((half-np.array([high[1], low[1]]))/size).astype(int)
    if (xmax-xmin+1)*(ymax-ymin+1) > 36:
        raise ValueError('Unexpected map extent; refusing unbounded tile download')
    cache = Path('data/interim') / args.experiment / 'aerial/gsi_seamlessphoto'
    cache.mkdir(parents=True, exist_ok=True)
    mosaic = Image.new('RGB', ((xmax-xmin+1)*256, (ymax-ymin+1)*256))
    tile_records = []
    for x in range(xmin, xmax+1):
        for y in range(ymin, ymax+1):
            url = f'https://cyberjapandata.gsi.go.jp/xyz/seamlessphoto/{zoom}/{x}/{y}.jpg'
            tile = cache / f'{zoom}_{x}_{y}.jpg'
            if not tile.exists():
                with urllib.request.urlopen(url, timeout=20) as response:
                    tile.write_bytes(response.read())
            with Image.open(tile) as image:
                mosaic.paste(image.convert('RGB'), ((x-xmin)*256, (y-ymin)*256))
            tile_records.append({**digest(tile), 'url': url})
    extent = [(xmin*size-half-offset[0])*scale,
              ((xmax+1)*size-half-offset[0])*scale,
              (half-(ymax+1)*size-offset[1])*scale,
              (half-ymin*size-offset[1])*scale]
    plt.rcParams['font.family'] = 'Yu Gothic'
    for segment in config['segments']:
        eid = segment['experiment_id']
        out = Path('results') / eid / 'aerial'
        out.mkdir(parents=True, exist_ok=True)
        fig, axes = plt.subplots(1, 2, figsize=(13, 6.8), layout='constrained')
        counts, inputs = {}, [config_path, raw_path, base/'fusion/manifest.json',
                              base/'gnss/manifest.json', Path(__file__),
                              Path('src/aquabeacon_analysis/aerial_tracks.py')]
        exported = []
        for ax, (source, path, rows, times, reset, converted) in zip(axes, tracks[eid]):
            inputs.append(path)
            # Retain actual native pixels; smoothing must not imply new detail.
            ax.imshow(mosaic, extent=extent, origin='upper', interpolation='nearest')
            for kind, color, label in [('raw', '#00e5ff', f'{source} 観測'),
                                        ('fused', '#ff4cce', f'{source}＋親機IMU静穏判定')]:
                llh, xy, mask = converted[kind]
                line = broken_track(times, xy, mask, reset if kind == 'fused' else None)
                ax.plot(*line.T, color=color, lw=1.4, marker='.', ms=2, label=label)
                ids = np.flatnonzero(mask & np.isfinite(xy).all(axis=1))
                if len(ids):
                    ax.plot(*xy[ids[0]], marker='o', ms=7, mfc='white', mec=color)
                    ax.plot(*xy[ids[-1]], marker='s', ms=7, mfc=color, mec='white')
                for i, row in enumerate(rows):
                    exported.append({'source': source, 'kind': kind, 'utc_s': times[i],
                                     'parent_video_s': row['parent_video_s'],
                                     'latitude_deg': llh[i, 0], 'longitude_deg': llh[i, 1],
                                     'display_valid': bool(mask[i]), 'quality': row['quality'],
                                     'measurement_accepted': row['measurement_accepted'],
                                     'predicted_only': row['predicted_only'],
                                     'state_reinitialized': row['state_reinitialized']})
            counts[source] = {'rows': len(rows), 'valid_raw': int(converted['raw'][2].sum()),
                              'valid_fused': int(converted['fused'][2].sum()),
                              'imu_quiet': sum(r['imu_quiet'] == 'True' for r in rows),
                              'zero_velocity': sum(r['zero_velocity_constraint'] == 'True'
                                                   for r in rows)}
            ax.set(xlim=(lo[0], hi[0]), ylim=(lo[1], hi[1]), aspect='equal',
                   title=f'{source} と親機IMU併用軌跡',
                   xlabel='東方向 [m]（原点緯度で縮尺補正）', ylabel='北方向 [m]')
            ax.legend(loc='upper left', fontsize=8, framealpha=.9)
            bar_x, bar_y = lo[0]+span*.07, lo[1]+span*.08
            ax.plot([bar_x, bar_x+10], [bar_y, bar_y], color='black', lw=5)
            ax.plot([bar_x, bar_x+10], [bar_y, bar_y], color='white', lw=3)
            ax.text(bar_x+5, bar_y+span*.025, '10 m', ha='center', color='white',
                    bbox={'facecolor': 'black', 'alpha': .6, 'pad': 2})
            ax.text(.98, .03, '北 ↑\n○ 開始　□ 終了', transform=ax.transAxes,
                    ha='right', color='white', bbox={'facecolor': 'black', 'alpha': .6})
        fig.suptitle(f'{eid}　親動画 {segment["parent_video_interval_s"]} 秒', fontsize=15)
        native_mpp = size*scale/256
        fig.supxlabel(f'背景：国土地理院・全国最新写真 z18（約{native_mpp:.2f} m/画素、撮影日は実験日と異なる）\n'
                      'GNSSアンテナ位置／加速度二重積分ではない／航空写真は真値ではない', fontsize=9)
        save_editable_figure(fig, out/'aerial_tracks.png', dpi=260)
        # Exact original imagery mosaic, rather than an enlarged pseudo-HD image.
        mosaic.save(out/'aerial_native.png')
        plt.close(fig)
        with (out/'tracks.csv').open('w', encoding='utf-8', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=list(exported[0]))
            writer.writeheader()
            writer.writerows(exported)
        manifest = {'experiment_id': eid, 'created_utc': datetime.now(UTC).isoformat(),
                    'git_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'],
                                                           text=True).strip(),
                    'inputs': [digest(p) for p in inputs], 'tiles': tile_records,
                    'origin_wgs84_lat_lon_height': origin, 'counts': counts,
                    'projection': 'WGS84 ENU -> ECEF -> geodetic -> EPSG3857; local cos(lat0) scale',
                    'display_limits_m': [lo.tolist(), hi.tolist()], 'gap_break_s': 3,
                    'imagery': {'native_zoom': zoom, 'tile_pixels': 256,
                                'native_ground_m_per_pixel_at_origin': native_mpp,
                                'official_max_zoom': 18,
                                'source_specification': 'https://maps.gsi.go.jp/help/use.html',
                                'resampling': 'nearest; no synthetic detail or sharpening',
                                'native_mosaic_extent_m': extent},
                    'cad': 'cad_20261028 reviewed; no calibrated lever arm applied',
                    'fusion': 'Existing GNSS CV filter + IMU quietness; no acceleration integration',
                    'attribution': 'https://maps.gsi.go.jp/development/ichiran.html'}
        (out/'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                         encoding='utf-8')
        report = f'# {eid} 航空写真上の親機軌跡\n\n![航空写真と軌跡](aerial_tracks.png)\n\n'
        report += ('左：RTK観測とRTK＋親機IMU静穏判定。右：PPK観測とPPK＋同判定。'
                   '白丸は開始、四角は終了。全区間を同じ範囲で表示。\n\n'
                   '融合は既存の等速モデル・IMU静穏度による雑音調整と、既知静止区間のみの'
                   'ゼロ速度拘束を用いる。加速度積分によるINS軌跡ではない。'
                   '3秒超の時刻ギャップ・状態再初期化は線を切り、無効観測は表示しない。'
                   '棄却された有効GNSS観測は観測系列に残す。予測のみの行はCSVに保持。\n\n'
                   'CAD cad_20261028は設計参照。ENU方位・実測レバーアームが未校正なので、'
                   '機体中心への補正は行わずGNSSアンテナ位置を示す。'
                   'PPSは時刻同期信号であり位置解ではない。RTKとPPKを比較表示した。'
                   '両方式は相関し、どちらも真値ではない。航空写真への見た目合わせもしていない。\n\n'
                   '[背景：地理院タイル・全国最新写真](https://maps.gsi.go.jp/development/ichiran.html)。'
                   '写真の撮影日・位置誤差は実験の基準測量に相当しない。\n\n'
                   f'公式配信上限のz18を使用（原点で約{native_mpp:.3f} m/画素）。'
                   '[配信仕様](https://maps.gsi.go.jp/help/use.html)を確認し、'
                   'z19等への拡大を新しい解像情報として扱わない。PNGは260 dpi、SVGは軌跡の線・文字を保持。'
                   '背景の補間ぼけを避けて原画素表示にしたが、写真自体の細部は増えていない。'
                   '[原解像度の背景](aerial_native.png)も保存。白い10 mスケールと北向きを付記。\n\n'
                   '[全表示データ](tracks.csv)・[設定・入力ハッシュ・背景タイル](manifest.json)。'
                   '元の融合CSVには品質、棄却、欠測、モデル共分散を保持。\n\n'
                   'ブラウザ編集（寸法・軸・系列・設定保存と再読込・PNG/SVG/PDF出力）：\n\n'
                   f'```powershell\npython -m aquabeacon_analysis.editable_plots results/{eid}/aerial/aerial_tracks.figure.pickle\n```\n')
        (out/'report.md').write_text(report, encoding='utf-8')
        print(eid, counts, flush=True)


if __name__ == '__main__':
    main()
