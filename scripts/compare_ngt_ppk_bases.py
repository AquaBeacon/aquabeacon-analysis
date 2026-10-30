"""Bounded, reproducible multi-base PPK experiment; never overwrite adopted data."""
from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import math
import os
import re
import subprocess
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode

import numpy as np
from ppk_ngt_geonet import Inputs, dump, get, hashed, observations, read_positions


def download_station(station, destination):
    code = station['observation_cd']
    destination.mkdir(parents=True, exist_ok=True)
    record = destination/'download_manifest.json'
    if record.exists():
        return
    payload = {'category': 'gis', 'sel_obs[0]': code, 'hour_time': 'UTC',
               'hour_st_year': '2026', 'hour_st_mon': '10', 'hour_st_day': '28',
               'hour_st_time': '05', 'hour_ed_year': '2026', 'hour_ed_mon': '10',
               'hour_ed_day': '28', 'hour_ed_time': '05', 'hour_satellite': 'GRJE',
               'hour_rinex_ver': '3.02', 'hour_filename': 'normal', 'hour_navfile': 'normal'}
    dump(destination/'request.json', payload)
    page = get('https://terras.gsi.go.jp/hour_download.php', payload).decode()
    (destination/'hour_response.html').write_text(page, encoding='utf-8')
    fields = Inputs()
    fields.feed(page)
    files = re.findall(r"one_file_download\('([^']+)','([^']+)','([^']+)','([^']+)'\)", page)
    if not files:
        raise RuntimeError('No downloadable station/hour files')
    records = []
    for returned_code, filename, kind, tmp in files:
        if returned_code != code:
            raise ValueError('Unexpected station')
        form = dict(fields.fields)
        form.update(satellite_type='GRJE', data_type=kind, observation_date='2026-10-28',
                    d_mode='[hour] GRJE 3.02 2026/10/28.05 2026/10/28.06',
                    d_obs_cd=code, d_file=filename, d_tmp_dir=tmp,
                    d_satellite='DOWNLOAD', d_rv='3.02')
        content = get('https://terras.gsi.go.jp/download.php', form)
        archive = destination/Path(filename).name
        archive.write_bytes(content)
        records.append({**hashed(archive), 'endpoint': 'https://terras.gsi.go.jp/download.php',
                        'form': form, 'retrieved_utc': datetime.now(UTC).isoformat()})
        if filename.endswith('.tar.gz'):
            with tarfile.open(fileobj=io.BytesIO(content), mode='r:gz') as tar:
                members = [m for m in tar.getmembers() if m.isfile()]
                if sum(m.size for m in members) > 60_000_000:
                    raise ValueError('Archive too large')
                for member in members:
                    (destination/Path(member.name).name).write_bytes(tar.extractfile(member).read())
        else:
            expanded = gzip.decompress(content)
            if len(expanded) > 60_000_000:
                raise ValueError('Observation file too large')
            (destination/Path(filename[:-3]).name).write_bytes(expanded)
    dump(record, records)


def metrics(rows):
    rows = sorted(rows, key=lambda r: r['utc_s'])
    def scatter(selected):
        if not selected:
            return None
        xyz = np.array([[r[k] for k in ['east_m', 'north_m', 'up_m']] for r in selected])
        delta = xyz-xyz.mean(axis=0)
        return {'count': len(selected), 'horizontal_rms_m': float(np.sqrt(np.mean(np.sum(delta[:, :2]**2, axis=1)))),
                'vertical_rms_m': float(np.sqrt(np.mean(delta[:, 2]**2))),
                'three_d_rms_m': float(np.sqrt(np.mean(np.sum(delta**2, axis=1)))),
                'mean_enu_m': xyz.mean(axis=0).tolist()}
    selected = [r for r in rows if 240 <= r['parent_video_s'] < 480]
    experiment = [r for r in rows if 240 <= r['parent_video_s'] < 1540]
    times = np.array([r['utc_s'] for r in experiment])
    xy = np.array([[r['east_m'], r['north_m']] for r in experiment])
    steps = np.linalg.norm(np.diff(xy, axis=0), axis=1)
    near = np.diff(times) <= 1.1
    return {'rows': len(rows), 'fixed': sum(r['quality'] == 1 for r in rows),
            'experiment_rows': len(experiment), 'experiment_fixed': sum(r['quality'] == 1 for r in experiment),
            'static': scatter(selected),
            'static_first_half': scatter([r for r in selected if r['parent_video_s'] < 360]),
            'static_second_half': scatter([r for r in selected if r['parent_video_s'] >= 360]),
            'static_fixed': scatter([r for r in selected if r['quality'] == 1]),
            'step_over_2m_count': int(np.sum(steps[near] > 2)),
            'max_step_m': float(steps[near].max()) if near.any() else None}


def direction_agreement(forward, backward):
    b = {round(r['utc_s'], 3): r for r in backward}
    pairs = [(r, b[round(r['utc_s'], 3)]) for r in forward
             if round(r['utc_s'], 3) in b and 240 <= r['parent_video_s'] < 1540]
    fixed = [(a, b) for a, b in pairs if a['quality'] == b['quality'] == 1]
    def evaluate(pairs):
        if not pairs:
            return {'count': 0}
        delta = np.array([[a[k]-b[k] for k in ['east_m', 'north_m', 'up_m']] for a, b in pairs])
        h = np.linalg.norm(delta[:, :2], axis=1)
        return {'count': len(pairs), 'horizontal_median_m': float(np.median(h)),
                'horizontal_p95_m': float(np.percentile(h, 95)),
                'vertical_abs_p95_m': float(np.percentile(np.abs(delta[:, 2]), 95)),
                'within_h02_v04_fraction': float(np.mean((h <= .2) & (np.abs(delta[:, 2]) <= .4)))}
    return {'all': evaluate(pairs), 'both_fixed': evaluate(fixed)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment', required=True, choices=['20261028_ngt_000'])
    args = parser.parse_args()
    print(json.dumps({'pid': os.getpid(), 'start_utc': datetime.now(UTC).isoformat(),
                      'purpose': 'five nearby bases, eight PPK modes, sequential, 120s per run'}), flush=True)
    eid = args.experiment
    root = Path('data/interim')/eid/'ppk_base_comparison'
    root.mkdir(parents=True, exist_ok=True)
    out = Path('results')/eid/'ppk_base_comparison'
    out.mkdir(parents=True, exist_ok=True)
    src = Path('data/interim')/eid/'gnss'
    original_station = Path('data/interim')/eid/'gnss_geonet_950356'
    url = 'https://terras.gsi.go.jp/api/observation.php?'+urlencode(
        {'category': 'gis', 'site': '', 'name': '', 'ken': '兵庫県', 'sstatus': ''})
    stations = json.loads(get(url))
    for station in stations:
        station['distance_km'] = math.hypot((float(station['latitude'])-34.6707714)*111.1,
                                            (float(station['longitude'])-135.1432009)*91.4)
    stations.sort(key=lambda r: r['distance_km'])
    dump(root/'ranked_stations.json', stations)
    selected_stations = [r for r in stations if r['sstatus'] == 0 and not r['abolition_date']][:5]
    binary = Path('../gnss-tools/tools/rtklib/RTKLIB_EX_2.5.1/rnx2rtkp.exe').resolve()
    sync_path = Path('results')/eid/'synchronization/manifest.json'
    sync = json.loads(sync_path.read_text())['cameras']['parent']
    origin = [34.6707714, 135.1432009, 50.198]
    manifest = {'experiment_id': eid, 'created_utc': datetime.now(UTC).isoformat(),
                'station_search_url': url, 'stations': selected_stations, 'runs': {}, 'failures': [],
                'git_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
                'origin_lat_lon_ellipsoid_m': origin,
                'inputs': [hashed(p) for p in [src/'rover.obs', src/'rover.nav', binary, sync_path,
                                              Path(__file__), Path('scripts/ppk_ngt_geonet.py')]],
                'predeclared_screen': {'static_interval_s': [240, 480], 'evaluation_interval_s': [240, 1540],
                    'require_static_rows': 228, 'require_static_h_rms_le_existing': True,
                    'require_static_3d_rms_le_existing': True,
                    'fixed_candidate_min_direction_overlap': 30,
                    'fixed_candidate_min_agreement_fraction': .9,
                    'agreement_horizontal_m': .2, 'agreement_vertical_m': .4,
                    'note': 'Conservative screening only, not proof of accuracy; no ratio tuning to force fix'},
                'limitations': ['GEONET header approximate coordinates, not surveyed/daily coordinates',
                                'No ANTEX/rover antenna calibration; no geodetic epoch correction',
                                '30s base, 1s rover: original age15; explicit timeinterp variants age30',
                                'misc-timeinterp interpolates residuals inside RTKLIB; raw observations unchanged']}
    dump(out/'manifest.json', manifest)
    for station in selected_stations:
        code = station['observation_cd']
        destination = original_station if code == '950356' else root/code
        try:
            download_station(station, destination)
            base = next(destination.glob('*.26o'))
            info = observations(base)
            if info['first_utc_s'] > 1793163883 or info['last_utc_s'] < 1793166196:
                raise ValueError('Base lacks acquisition coverage')
            nav = sorted(p for p in destination.glob('*.26*') if p.suffix in ['.26n', '.26g', '.26l', '.26q'])
            if not nav:
                raise ValueError('No navigation files')
            dump(root/f'{code}_base.json', info)
            runs = {}
            for name, direction, ar in [('combined_ar', 'combined', 'continuous'),
                                         ('forward_ar', 'forward', 'continuous'),
                                         ('backward_ar', 'backward', 'continuous'),
                                         ('combined_float', 'combined', 'off'),
                                         ('interp_combined_ar', 'combined', 'continuous'),
                                         ('interp_forward_ar', 'forward', 'continuous'),
                                         ('interp_backward_ar', 'backward', 'continuous'),
                                         ('interp_combined_float', 'combined', 'off')]:
                run = root/code/name
                run.mkdir(parents=True, exist_ok=True)
                conf, pos = run/'settings.conf', run/'solution.pos'
                interp = name.startswith('interp_')
                conf.write_text('pos1-posmode =kinematic\npos1-frequency =l1+l2+l5\n'
                    f'pos1-soltype ={direction}\npos1-navsys =73\npos1-elmask =15\n'
                    f'pos2-armode ={ar}\npos2-arthres =3\npos2-arlockcnt =5\n'
                    f'pos2-maxage ={30 if interp else 15}\nant2-postype =rinexhead\n'
                    'out-solformat =llh\nout-timesys =utc\nout-timeform =hms\nout-height =ellipsoidal\n'
                    + ('misc-timeinterp =on\n' if interp else ''), encoding='ascii')
                command = [str(binary), '-k', str(conf.resolve()), '-t', '-u', '-o', str(pos.resolve()),
                           str((src/'rover.obs').resolve()), str(base.resolve()), *[str(p.resolve()) for p in nav]]
                signature = {'command': command, 'inputs': [hashed(p) for p in [conf, base, *nav, src/'rover.obs', binary]]}
                cached = run/'completed.json'
                if not (pos.exists() and cached.exists() and json.loads(cached.read_text()) == signature):
                    with (run/'solver.log').open('w') as log:
                        process = subprocess.Popen(command, stdout=log, stderr=log)
                        event = {'pid': process.pid, 'start_utc': datetime.now(UTC).isoformat(),
                                 'purpose': f'{code}/{name}', 'owner': 'compare_ngt_ppk_bases', 'timeout_s': 120}
                        dump(run/'process.json', event)
                        print(json.dumps(event), flush=True)
                        try:
                            result = process.wait(timeout=120)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
                            raise
                    if result:
                        raise RuntimeError(f'Solver failed {result}')
                    dump(cached, signature)
                rows = sorted(read_positions(pos, origin, sync), key=lambda r: r['utc_s'])
                if len({r['utc_s'] for r in rows}) != len(rows):
                    raise ValueError('Duplicate solver output epochs')
                runs[name] = rows
                with (run/'positions.csv').open('w', newline='', encoding='utf-8') as handle:
                    writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                    writer.writeheader()
                    writer.writerows(rows)
                manifest['runs'][f'{code}/{name}'] = {'station_name': station['observation_name'],
                    'distance_km': station['distance_km'], 'metrics': metrics(rows),
                    'positions': hashed(run/'positions.csv'), 'solution': hashed(pos),
                    'settings': hashed(conf), 'inputs': signature['inputs'], 'command': command,
                    'base_info': info, 'download_manifest': hashed(destination/'download_manifest.json')}
                dump(out/'manifest.json', manifest)
                print(code, name, json.dumps(metrics(rows), ensure_ascii=True), flush=True)
            agreement = direction_agreement(runs['forward_ar'], runs['backward_ar'])
            manifest['runs'][f'{code}/combined_ar']['direction_agreement'] = agreement
            manifest['runs'][f'{code}/interp_combined_ar']['direction_agreement'] = direction_agreement(
                runs['interp_forward_ar'], runs['interp_backward_ar'])
            dump(out/'manifest.json', manifest)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, StopIteration) as exc:
            manifest['failures'].append({'station': code, 'error': repr(exc)})
            dump(out/'manifest.json', manifest)
            print('station failure', code, repr(exc), flush=True)
    print('comparison finished; decision remains separate', flush=True)


if __name__ == '__main__':
    main()
