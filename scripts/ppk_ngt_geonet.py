"""Download official nearby GEONET observations and run isolated NGT PPK variants."""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import math
import os
import pickle
import re
import subprocess
import tarfile
from collections import Counter
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import matplotlib
import numpy as np

matplotlib.use('Agg')
import matplotlib.pyplot as plt

from aquabeacon_analysis.aerial_tracks import broken_track, mercator
from aquabeacon_analysis.editable_plots import save_editable_figure
from aquabeacon_analysis.gnss_selection import STATUS, select_positions
from aquabeacon_analysis.gps_parent import geodetic_to_enu


class Inputs(HTMLParser):
    def __init__(self):
        super().__init__()
        self.fields = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'input' and 'name' in attrs:
            self.fields[attrs['name']] = attrs.get('value', '')


def get(url, fields=None):
    request = Request(url, data=urlencode(fields).encode() if fields else None)
    with urlopen(request, timeout=45) as response:
        data = response.read(30_000_001)
        if len(data) > 30_000_000:
            raise ValueError('Unexpected response larger than 30 MB')
        return data


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def hashed(path):
    return {'path': path.as_posix(), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def observations(path):
    header, epochs = [], []
    with path.open(encoding='ascii', errors='replace') as handle:
        for line in handle:
            header.append(line.rstrip())
            if 'END OF HEADER' in line:
                break
        for line in handle:
            if line.startswith('>'):
                f = line.split()
                epochs.append(datetime(*map(int, f[1:6]), tzinfo=UTC).timestamp()+float(f[6])-18)
    if not epochs:
        raise ValueError('No RINEX3 epochs')
    return {'header': header, 'epochs': len(epochs), 'first_utc_s': epochs[0],
            'last_utc_s': epochs[-1], 'interval_s': float(np.median(np.diff(epochs))),
            'xyz_m': [float(v) for v in next(l for l in header if 'APPROX POSITION XYZ' in l)[:60].split()]}


def read_positions(path, origin, sync):
    rows = []
    for line in path.read_text().splitlines():
        if not line.strip() or line.startswith('%'):
            continue
        f = line.split()
        stamp = datetime.strptime(' '.join(f[:2])+'+0000', '%Y/%m/%d %H:%M:%S.%f%z')
        rows.append({'utc_s': stamp.timestamp(), 'latitude_deg': float(f[2]),
                     'longitude_deg': float(f[3]), 'height_ellipsoid_m': float(f[4]),
                     'quality': int(f[5]), 'satellites': int(f[6]),
                     'sd_n_m': float(f[7]), 'sd_e_m': float(f[8]), 'sd_u_m': float(f[9]),
                     'age_s': float(f[13]), 'ratio': float(f[14])})
    if not rows:
        raise ValueError('PPK produced no positions')
    llh = np.array([[r[k] for k in ['latitude_deg', 'longitude_deg', 'height_ellipsoid_m']]
                    for r in rows])
    xyz = geodetic_to_enu(*llh.T, *origin)
    for r, point in zip(rows, xyz, strict=True):
        r['parent_video_s'] = (r['utc_s']-sync['utc_at_video_zero_s'])/sync['time_scale']
        r.update(zip(['east_m', 'north_m', 'up_m'], point.tolist(), strict=True))
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment', required=True, choices=['20261028_ngt_000'])
    args = parser.parse_args()
    acquisition = args.experiment
    root = Path.cwd()
    print(json.dumps({'pid': os.getpid(), 'start_utc': datetime.now(UTC).isoformat(),
                      'purpose': 'isolated GEONET download and bounded PPK'}), flush=True)
    source = Path('data/interim')/acquisition/'gnss'
    target = Path('data/interim')/acquisition/'gnss_geonet_950356'
    target.mkdir(parents=True, exist_ok=True)
    url = 'https://terras.gsi.go.jp/api/observation.php?'+urlencode(
        {'category': 'gis', 'site': '', 'name': '', 'ken': '兵庫県', 'sstatus': ''})
    stations = json.loads(get(url))
    for r in stations:
        r['distance_km'] = math.hypot((float(r['latitude'])-34.6707714)*111.1,
                                      (float(r['longitude'])-135.1432009)*91.4)
    stations.sort(key=lambda r: r['distance_km'])
    dump(target/'ranked_stations.json', stations)
    station = next(r for r in stations if r['observation_cd'] == '950356')
    if station['sstatus'] != 0 or station['abolition_date']:
        raise ValueError('Selected station is not active')
    dump(target/'station.json', station)
    payload = {'category': 'gis', 'sel_obs[0]': '950356', 'hour_time': 'UTC',
               'hour_st_year': '2026', 'hour_st_mon': '10', 'hour_st_day': '28',
               'hour_st_time': '05', 'hour_ed_year': '2026', 'hour_ed_mon': '10',
               'hour_ed_day': '28', 'hour_ed_time': '05', 'hour_satellite': 'GRJE',
               'hour_rinex_ver': '3.02', 'hour_filename': 'normal', 'hour_navfile': 'normal'}
    dump(target/'request.json', payload)
    download_manifest = target/'download_manifest.json'
    if not download_manifest.exists():
        page = get('https://terras.gsi.go.jp/hour_download.php', payload).decode()
        (target/'hour_response.html').write_text(page, encoding='utf-8')
        fields = Inputs()
        fields.feed(page)
        files = re.findall(r"one_file_download\('([^']+)','([^']+)','([^']+)','([^']+)'\)", page)
        if not files:
            raise RuntimeError('No official downloadable files for requested station/hour')
        records = []
        for code, filename, kind, tmp in files:
            if code != '950356':
                raise ValueError('Unexpected station in response')
            form = dict(fields.fields)
            form.update(satellite_type='GRJE', data_type=kind, observation_date='2026-10-28',
                        d_mode='[hour] GRJE 3.02 2026/10/28.05 2026/10/28.06',
                        d_obs_cd=code, d_file=filename, d_tmp_dir=tmp,
                        d_satellite='DOWNLOAD', d_rv='3.02')
            content = get('https://terras.gsi.go.jp/download.php', form)
            archive = target/Path(filename).name
            archive.write_bytes(content)
            records.append({**hashed(archive), 'endpoint': 'https://terras.gsi.go.jp/download.php',
                            'form': form, 'retrieved_utc': datetime.now(UTC).isoformat()})
            if filename.endswith('.tar.gz'):
                with tarfile.open(fileobj=io.BytesIO(content), mode='r:gz') as tar:
                    members = [m for m in tar.getmembers() if m.isfile()]
                    if sum(m.size for m in members) > 60_000_000:
                        raise ValueError('Unexpected expanded archive size')
                    for member in members:
                        (target/Path(member.name).name).write_bytes(tar.extractfile(member).read())
            else:
                (target/Path(filename[:-3]).name).write_bytes(gzip.decompress(content))
        dump(download_manifest, records)
    base = next(target.glob('*.26o'))
    base_info = observations(base)
    if not (base_info['first_utc_s'] < datetime(2026, 10, 28, 5, 4, tzinfo=UTC).timestamp()
            and base_info['last_utc_s'] > datetime(2026, 10, 28, 5, 43, tzinfo=UTC).timestamp()):
        raise ValueError('Official base does not cover acquisition')
    nav = sorted(p for p in target.glob('*.26*') if p.suffix in ['.26n', '.26g', '.26l', '.26q'])
    if not nav:
        raise ValueError('No official broadcast navigation files')
    print(json.dumps({'station': station['observation_roman'], 'base': base_info},
                     ensure_ascii=True), flush=True)
    sync_path = Path('results')/acquisition/'synchronization/manifest.json'
    sync = json.loads(sync_path.read_text())['cameras']['parent']
    rtk_path = Path('data/processed')/acquisition/'gnss/rtk_nav_pvt.csv'
    with rtk_path.open(newline='') as handle:
        rtk_rows = list(csv.DictReader(handle))
        origin_row = next(r for r in rtk_rows if r['fix_valid'] == 'True'
                          and r['utc_valid'] == 'True' and int(r['fix_type']) >= 3)
    origin = [float(origin_row[k]) for k in ['latitude_deg', 'longitude_deg', 'height_ellipsoid_m']]
    binary = Path('../gnss-tools/tools/rtklib/RTKLIB_EX_2.5.1/rnx2rtkp.exe').resolve()
    config_path = Path('config/20261028_ngt/geonet_950356.json')
    config = {'station_id': '950356', 'station_name': station['observation_name'],
              'base_obs': base.as_posix(), 'base_coordinates': 'RINEX header model coordinates',
              'primary_run': 'ar_continuous', 'systems': 'GPS/Galileo/QZSS', 'navsys': 73,
              'max_base_age_s': 15, 'base_interval_s': base_info['interval_s'],
              'warning': '30s base not interpolated into 1s observations; output age is retained',
              'ar_ratio_threshold': 3, 'minimum_lock_epochs': 5, 'elevation_deg': 15,
              'gravity_or_cad_correction': None}
    dump(config_path, config)
    manifest = {'created_utc': datetime.now(UTC).isoformat(), 'experiment_id': acquisition,
                'station': station, 'base': base_info, 'config': config, 'runs': {},
                'git_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
                'source': '国土地理院 電子基準点データ提供サービス https://terras.gsi.go.jp/',
                'coordinate_limit': 'RINEX header model coordinates; not surveyed result or daily coordinate',
                'frame_limit': 'WGS84-labelled solver output; GEONET coordinate model epoch not transformed',
                'utc_leap_seconds': 18,
                'inputs': [hashed(p) for p in [source/'rover.obs', source/'rover.nav', base,
                           *nav, sync_path, rtk_path, config_path, binary, Path(__file__)]]}
    all_runs = {}
    for run, ar, interval in [('ar_continuous', 'continuous', None), ('float', 'off', None),
                              ('ar_30s', 'continuous', 30)]:
        conf = target/f'{run}.conf'
        conf.write_text('pos1-posmode =kinematic\npos1-frequency =l1+l2+l5\n'
                        'pos1-soltype =combined\npos1-navsys =73\npos1-elmask =15\n'
                        f'pos2-armode ={ar}\npos2-arthres =3\npos2-arlockcnt =5\n'
                        'pos2-maxage =15\nant2-postype =rinexhead\n'
                        'out-solformat =llh\nout-timesys =utc\nout-timeform =hms\n'
                        'out-height =ellipsoidal\n', encoding='ascii')
        pos = target/f'{run}.pos'
        command = [str(binary), '-k', str(conf.resolve()), '-c', '-t', '-u']
        if interval:
            command += ['-ti', str(interval)]
        command += ['-o', str(pos.resolve()), str((source/'rover.obs').resolve()),
                    str(base.resolve()), *[str(p.resolve()) for p in nav]]
        dump(target/f'{run}_command.json', command)
        with (target/f'{run}.log').open('w') as log:
            process = subprocess.Popen(command, stdout=log, stderr=log, cwd=root)
            print(json.dumps({'pid': process.pid, 'start_utc': datetime.now(UTC).isoformat(),
                              'purpose': run, 'owner': 'ppk_ngt_geonet', 'timeout_s': 120}), flush=True)
            try:
                code = process.wait(timeout=120)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                raise
        if code:
            raise RuntimeError(f'RTKLIB failed: {run}: {code}')
        rows = read_positions(pos, origin, sync)
        all_runs[run] = rows
        manifest['runs'][run] = {'quality_counts': dict(Counter(r['quality'] for r in rows)),
                                'positions': len(rows), 'max_abs_age_s': max(abs(r['age_s']) for r in rows),
                                'command': command, 'config': hashed(conf), 'positions_file': hashed(pos)}
        for eid, interval in [(acquisition, None)]+[(s['experiment_id'], s['parent_video_interval_s'])
                                                   for s in json.loads(Path('config/20261028_ngt/analysis.json').read_text())['segments']]:
            out = Path('results')/eid/'ppk_geonet_950356'
            out.mkdir(parents=True, exist_ok=True)
            selected = rows if interval is None else [r for r in rows if interval[0] <= r['parent_video_s'] < interval[1]]
            with (out/f'{run}_positions.csv').open('w', newline='', encoding='utf-8') as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(selected)
        print(run, manifest['runs'][run]['quality_counts'], flush=True)
    dump(Path('results')/acquisition/'ppk_geonet_950356/manifest.json', manifest)
    plot_and_report(acquisition, all_runs, rtk_rows, origin, manifest)


def plot_and_report(acquisition, runs, rtk, origin, manifest):
    """Preserve new PPK and quality-selected tracks without overwriting old solutions."""
    selection = select_positions(rtk, runs['ar_continuous'])
    cfg = json.loads(Path('config/20261028_ngt/analysis.json').read_text())
    intervals = [{'experiment_id': acquisition, 'parent_video_interval_s':
                  [selection[0]['parent_video_s'], selection[-1]['parent_video_s']+1]}]+cfg['segments']
    plt.rcParams['font.family'] = 'Yu Gothic'
    for segment in intervals:
        eid = segment['experiment_id']
        lo, hi = segment['parent_video_interval_s']
        out = Path('results')/eid/'ppk_geonet_950356'
        chosen = [r for r in selection if lo <= r['parent_video_s'] < hi]
        with (out/'selected_positions.csv').open('w', newline='', encoding='utf-8') as handle:
            writer = csv.DictWriter(handle, fieldnames=list(selection[0]))
            writer.writeheader()
            writer.writerows(chosen)
        fig, axes = plt.subplots(3, 1, figsize=(12, 7.5), sharex=True, layout='constrained')
        for ax, source, color in zip(axes[:2], ['RTK', 'PPK'], ['#007eaf', '#bd4b00']):
            t = np.array([r['parent_video_s'] for r in chosen])
            q = np.array([r[source.lower()+'_rank'] for r in chosen])
            xy = broken_track([r['utc_s'] for r in chosen], np.column_stack([t, q]),
                              np.ones(len(chosen), bool))
            ax.plot(*xy.T, color=color, marker='.', ms=3, lw=1, label=source)
            ax.set(yticks=list(STATUS), yticklabels=list(STATUS.values()), ylim=(-1.4, 4.4),
                   ylabel=source+' 判定')
            ax.legend(loc='upper right')
            ax.grid(alpha=.25)
        axes[2].plot([r['parent_video_s'] for r in chosen],
                     [{'None': 0, 'RTK': 1, 'PPK': 2}[r['selected_source']] for r in chosen],
                     marker='.', ms=3, color='#287a39', lw=1)
        axes[2].set(yticks=[0, 1, 2], yticklabels=['採用なし', 'RTK', '神戸中央PPK'],
                    ylim=(-.3, 2.3), xlabel='親動画時刻 [s]', ylabel='採用元', xlim=(lo, hi))
        axes[2].grid(alpha=.25)
        fig.suptitle(f'{eid}　神戸中央950356を使ったPPK（整数固定有効）')
        fig.supxlabel('基準局30秒値／解の時刻差は最大15秒／Fixedは検証済み精度を意味しない\n'
                      'Fixed > Float > DGPS > Single、同順位は新PPKを選択', fontsize=9)
        save_editable_figure(fig, out/'quality_status.png', dpi=150)
        plt.close(fig)
        primary = [r for r in runs['ar_continuous'] if lo <= r['parent_video_s'] < hi]
        summary = {'experiment_id': eid, 'primary_quality_counts': dict(Counter(r['quality'] for r in primary)),
                   'selected_source': dict(Counter(r['selected_source'] for r in chosen)),
                   'selected_status': dict(Counter(r['selected_status'] for r in chosen)),
                   'selection_policy': 'Fixed > Float > DGPS > Single; ties prefer new GEONET PPK',
                   'acquisition_manifest': '../../20261028_ngt_000/ppk_geonet_950356/manifest.json',
                   'interval_s': [lo, hi]}
        if eid != acquisition:
            old_master = Path('results')/eid/'aerial/aerial_tracks.figure.pickle'
            original = pickle.loads(old_master.read_bytes())  # trusted local figure only
            ref = original.axes[0]
            fig, axes = plt.subplots(1, 2, figsize=(13, 6.8), layout='constrained')
            origin_xy = mercator(origin[0], origin[1])[0]

            def xy(rows, origin_xy=origin_xy):
                return (mercator(np.array([r['latitude_deg'] for r in rows]),
                                 np.array([r['longitude_deg'] for r in rows]))-origin_xy)*np.cos(np.radians(origin[0]))

            for ax in axes:
                ax.imshow(ref.images[0].get_array(), extent=ref.images[0].get_extent(), origin='upper')
                ax.set(xlim=ref.get_xlim(), ylim=ref.get_ylim(), aspect='equal',
                       xlabel=ref.get_xlabel(), ylabel=ref.get_ylabel())
            pp = xy(primary)
            line = broken_track([r['utc_s'] for r in primary], pp, np.ones(len(primary), bool))
            axes[0].plot(*line.T, color='#ff4cce', lw=1.5, marker='.', ms=3, label='神戸中央PPK')
            fixed = np.array([r['quality'] == 1 for r in primary])
            axes[0].plot(*pp[fixed].T, ls='none', marker='o', ms=5, mfc='none',
                         mec='#fff000', label='Fixed判定候補')
            axes[0].set_title('神戸中央PPK（未平滑化）')
            pick_xy = xy(chosen)
            for source, color in [('RTK', '#00e5ff'), ('PPK', '#ff4cce')]:
                mask = np.array([r['selected_source'] == source for r in chosen])
                line = broken_track([r['utc_s'] for r in chosen], pick_xy, mask,
                                    [r['source_switch'] for r in chosen])
                axes[1].plot(*line.T, color=color, lw=1.5, marker='.', ms=3, label=source+'採用')
            axes[1].set_title('RTKと神戸中央PPKの品質優先選択')
            for ax in axes:
                ax.legend(loc='upper left', fontsize=8)
            fig.suptitle(eid+'　新基準局：神戸中央（約3.2km）')
            fig.supxlabel('背景：国土地理院・全国最新写真／北が上／GNSSアンテナ位置\n'
                          '品質優先選択はIMU融合ではない。切替・欠測で線を分割。', fontsize=9)
            save_editable_figure(fig, out/'aerial_tracks.png', dpi=150)
            plt.close(fig)
            plt.close(original)
            summary['background_master'] = hashed(old_master)
        dump(out/'summary.json', summary)
        report = f'# {eid} 神戸中央基準局を用いたPPK\n\n'
        report += ('今回の新しい基準局は **国土地理院「神戸中央」950356 / KOBECHUUOU**。'
                   f'API上の設置施設は{manifest["station"]["facility"]}、実験地点から概算3.2km。'
                   '対象日2026-10-28の05時台UTCのRINEX3.02観測とGRJE放送暦を公式配布元から取得した。'
                   'このフォルダは神戸中央を入力とする新解析。従来の受信RTCM由来PPKは比較用に保持した。\n\n'
                   '[出典：国土地理院電子基準点データ提供サービス](https://terras.gsi.go.jp/)・'
                   '[局情報](https://terras.gsi.go.jp/geo_info/php/950356.php)。\n\n'
                   '基準局30秒値121エポック、移動局約1秒値。30秒観測を1秒観測に水増しせず、'
                   'RTKLIBが使った基準局観測との時刻差ageを保存した（最大15秒）。'
                   '出力が1秒間隔でも基準局が1秒観測であることを意味しない。\n\n'
                   '主表示はAR continuous、ratio閾値3、最低lock 5エポック、仰角15°、'
                   'GPS/Galileo/QZSS、kinematic combined。Float比較と30秒出力の診断も同じ基準局で保存。'
                   '**主表示のFixedは整数固定の判定候補であり、検証済みの真値精度ではない。**\n\n')
        report += '| 実行 | 全収録点数 | Fixed | Float | DGPS | 最大絶対age [s] |\n|---|---:|---:|---:|---:|---:|\n'
        for name, r in manifest['runs'].items():
            c = {int(k): v for k, v in r['quality_counts'].items()}
            report += f'| {name} | {r["positions"]} | {c.get(1,0)} | {c.get(2,0)} | {c.get(4,0)} | {r["max_abs_age_s"]} |\n'
        report += ('\n30秒出力診断ではFixedが出ず、固定判定の設定・時刻条件への依存が残る。'
                   '新基準局だから精度が改善したとは断定しない。静止区間の新PPK主表示は'
                   '水平RMS約0.606m・3D RMS約1.958m（各平均まわり）で、従来PPKの0.217m・0.916mより大きい。'
                   '値は絶対精度ではない。\n\n'
                   '基準局ECEFはRINEXヘッダー(-3723694.9786,3701367.9415,3609416.0055)mを使用。'
                   '[地理院FAQ](https://www.gsi.go.jp/denshi/denshi_faq.html)のとおりヘッダーはモデル概略座標。'
                   '測量成果や当日の日々座標として扱わない。外部ANTEX位相中心補正・地殻変動補正・'
                   'CADレバーアーム補正は適用していない。\n\n')
        report += f'この区間のPPK Q内訳：{summary["primary_quality_counts"]}。採用元：{summary["selected_source"]}。\n\n'
        report += '![RTKと新PPKの品質](quality_status.png)\n\n'
        if eid != acquisition:
            report += '![新PPKと品質優先選択](aerial_tracks.png)\n\n'
        report += ('[新PPK主表示CSV](ar_continuous_positions.csv)・[Float比較](float_positions.csv)・'
                   '[30秒診断](ar_30s_positions.csv)・[RTKとの品質優先選択CSV](selected_positions.csv)・'
                   '[区間集計](summary.json)。選択はFixed > Float > DGPS > Single、同順位は新PPK、'
                   '両方無効はNaN。元座標をそのまま採用し切替オフセット補正はしない。'
                   'この選択は既存IMU融合への入力差し替えまでは含まない。\n\n'
                   '[全体の入力ハッシュ・処理設定](../../20261028_ngt_000/ppk_geonet_950356/manifest.json)。'
                   '取得履歴・元RINEX・コマンド・設定は `data/interim/20261028_ngt_000/gnss_geonet_950356/`。'
                   'PNG/SVG/master/plot.jsonを併置し、既存ブラウザエディタで保存・再読込・PNG/SVG/PDF出力ができる。\n\n'
                   f'```powershell\npython -m aquabeacon_analysis.editable_plots results/{eid}/ppk_geonet_950356/quality_status.figure.pickle\n```\n')
        (out/'report.md').write_text(report, encoding='utf-8')


if __name__ == '__main__':
    main()
