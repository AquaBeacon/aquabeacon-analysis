"""Package explicitly configured submerged AND parent-ranging trials, across restarts."""
import argparse
import csv
import json
import subprocess
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path

import matplotlib

matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
from scipy.ndimage import median_filter

from aquabeacon_analysis.editable_plots import save_editable_figure
from aquabeacon_analysis.experiments import load_experiment
from aquabeacon_analysis.interval_selection import (
    group_events,
    intersect_activity,
    map_anchor,
    valid_anchors,
)
from aquabeacon_analysis.ngt_binary import sha256
from aquabeacon_analysis.time_sync import probe_media, resolve_ffmpeg


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def read(path):
    with path.open(encoding='utf-8-sig', newline='') as handle:
        return list(csv.DictReader(handle))


def utc_label(value):
    return datetime.fromtimestamp(value, timezone(timedelta(hours=9))).strftime('%H:%M:%S.%f')[:-3]


def run_video(arguments, output, ffmpeg):
    command = [str(ffmpeg), '-hide_banner', '-loglevel', 'warning', '-nostdin', '-y',
               *arguments, '-map', '0:v:0', '-map', '0:a:0?', '-c', 'copy',
               '-map_metadata', '-1', '-metadata', 'creation_time=2000-01-01T00:00:00Z',
               '-movflags', '+faststart', str(output)]
    with output.with_suffix('.ffmpeg.log').open('w') as log:
        subprocess.run(command, stderr=log, stdout=subprocess.DEVNULL, check=True)
    return command


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment', required=True)
    parser.add_argument('--skip-video', action='store_true')
    args = parser.parse_args()
    exp = load_experiment(Path.cwd(), args.experiment)
    cfg_path = Path('config') / args.experiment / 'segments.json'
    cfg = json.loads(cfg_path.read_text())
    folder = exp.stage_path('interim') / 'synchronization'
    out = exp.stage_path('results') / 'segmentation'
    out.mkdir(parents=True, exist_ok=True)
    clock_path = exp.stage_path('results') / 'synchronization/manifest.json'
    clocks = json.loads(clock_path.read_text())['cameras']
    anchors, shot_times, sources = {}, {}, []
    parent_shots = []
    child_health = []
    for role, suffixes in cfg['log_suffixes'].items():
        for suffix in suffixes:
            prefix = f'{role[0].upper()}_MAIN_HEALTH_RANGE_{suffix}'
            health = read(folder / role / f'{prefix}.health.csv')
            anchor = valid_anchors(health)
            anchors[role, suffix] = anchor
            if role == 'child':
                child_health.extend((map_anchor(int(r['mono_us']), anchor), r) for r in health)
            for r in read(folder / role / f'{prefix}.shot.csv'):
                # Parent scheduled time belongs to parent; tx_us belongs to child.
                key = 'parent_scheduled_us' if role == 'parent' else 'tx_us'
                utc = map_anchor(int(r[key]), anchor)
                shot_times[role, suffix, r['shot_seq']] = utc
                if role == 'parent' and utc is not None:
                    parent_shots.append(utc)
            sources.extend((role, suffix, p) for p in sorted((folder / role).glob(f'*_{suffix}.*.csv')))
    child_health = sorted((t, r) for t, r in child_health if t is not None)
    t = np.array([v[0] for v in child_health])
    pressure = np.array([float(v[1]['pressure_mbar']) for v in child_health])
    baseline = float(np.median(pressure[t < t[0]+cfg['baseline_first_s']]))
    depth = (pressure-baseline)*100/(cfg['density_kg_m3_assumed']*cfg['gravity_m_s2'])
    filtered = median_filter(depth, size=cfg['median_samples'])
    runs = group_events(parent_shots, cfg['ranging_gap_s'], 0)
    for run in runs:
        run['end_utc_s'] += cfg['sample_tail_s']

    def select(threshold):
        immersions = group_events(t[filtered > threshold], cfg['immersion_merge_gap_s'], 0)
        for immersion in immersions:
            immersion['end_utc_s'] += cfg['sample_tail_s']
        return intersect_activity(immersions, runs, cfg['minimum_duration_s'])

    segments = select(cfg['depth_threshold_m'])
    if not segments:
        raise ValueError('No overlapping immersion and ranging')
    manifest = {'experiment_id': args.experiment, 'config': cfg,
                'selection_rule': 'child submerged AND parent ranging; start=max(immersion, ranging start)',
                'pressure_baseline_mbar': baseline, 'ranging_runs': runs,
                'threshold_sensitivity': {str(th): select(th) for th in [.1, .2, .3]},
                'clock_manifest_sha256': sha256(clock_path), 'config_sha256': sha256(cfg_path),
                'git_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
                'calibration_paths': [], 'segments': segments,
                'limitations': ['Pressure baseline and seawater density are provisional.',
                                'Ranging starts are first logged scheduled shots, not UI button timestamps.',
                                'No retiming; each video piece retains its own source clock.',
                                'Video keyframe preroll may be retained in MP4 edit lists.',
                                'Creation tag is a 2000-01-01 placeholder; use clock sidecars.',
                                'Clock extrapolation is flagged; no independent absolute timing truth.']}
    with (out / 'selection.csv').open('w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['sync_utc_s', 'pressure_mbar', 'depth_m', 'median_depth_m', 'underwater'])
        writer.writerows(zip(t, pressure, depth, filtered, filtered > cfg['depth_threshold_m'], strict=True))
    for i, s in enumerate(segments, 1):
        eid = args.experiment[:-3]+f'{i:03}'
        s.update(experiment_id=eid, sensor_files=[], videos=[])
        s['max_relative_depth_m'] = float(depth[(t >= s['start_utc_s']) & (t < s['end_utc_s'])].max())
        metadata = {**exp.metadata, 'experiment_id': eid,
                    'description': f'Nanyo day 2 submerged parent-ranging trial {i}',
                    'parent_acquisition': args.experiment,
                    'utc_interval_s': [s['start_utc_s'], s['end_utc_s']],
                    'c_main_suffix': cfg['log_suffixes']['child'][0],
                    'p_main_suffix': None, 'config_paths': [cfg_path.as_posix()],
                    'log_suffixes': cfg['log_suffixes']}
        dump(Path('experiments') / eid / 'experiment.json', metadata)
    sources.append(('parent', None, folder / 'parent_100hz.csv'))
    sources.extend(('camera', None, p) for p in sorted(
        (exp.stage_path('processed') / 'synchronization').glob('*_attitude.csv')))
    for role, suffix, source in sources:
        print('Subset', source.name, flush=True)
        counts = [0]*len(segments)
        unmapped = 0
        paths = [Path('data/processed') / s['experiment_id'] / 'synchronized' / role / source.name
                 for s in segments]
        with ExitStack() as stack:
            rows = csv.DictReader(stack.enter_context(source.open(newline='', encoding='utf-8-sig')))
            fields = list(dict.fromkeys([*rows.fieldnames, 'sync_utc_s', 'segment_time_s', 'clock_basis']))
            writers = []
            for path in paths:
                path.parent.mkdir(parents=True, exist_ok=True)
                writer = csv.DictWriter(stack.enter_context(path.open('w', newline='', encoding='utf-8')),
                                        fieldnames=fields)
                writer.writeheader()
                writers.append(writer)
            for row in rows:
                utc, basis = None, 'recorded_or_camera_aligned_utc'
                if source.name.endswith(('.shot.csv', '.range_measurement.csv')):
                    utc = shot_times.get((role, suffix, row['shot_seq']))
                    basis = 'parent_scheduled_anchor' if role == 'parent' else 'child_transmit_anchor'
                else:
                    for field, divisor in [('sync_utc_s', 1), ('sample_utc_us', 1e6),
                                           ('utc_us', 1e6), ('utc_read_us', 1e6),
                                           ('utc_reference_ms', 1000)]:
                        if row.get(field) and float(row[field])/divisor > 1e9:
                            utc = float(row[field])/divisor
                            break
                    if utc is None and suffix is not None and row.get('mono_us'):
                        utc = map_anchor(int(row['mono_us']), anchors[role, suffix])
                        basis = 'valid_health_anchor_at_record'
                if utc is None:
                    unmapped += 1
                    continue
                for i, (s, writer) in enumerate(zip(segments, writers, strict=True)):
                    if s['start_utc_s'] <= utc < s['end_utc_s']:
                        writer.writerow({**row, 'sync_utc_s': utc,
                                          'segment_time_s': utc-s['start_utc_s'], 'clock_basis': basis})
                        counts[i] += 1
        digest = sha256(source)
        for s, path, count in zip(segments, paths, counts, strict=True):
            s['sensor_files'].append({'source': str(source), 'output': str(path), 'rows': count,
                                      'unmapped_source_rows': unmapped, 'source_sha256': digest})
    ffmpeg = resolve_ffmpeg()
    for s in segments:
        dest = Path('data/processed') / s['experiment_id'] / 'videos'
        dest.mkdir(parents=True, exist_ok=True)
        for role in ('parent', 'child'):
            pieces, elapsed = [], 0.
            for name, c in sorted(clocks.items(), key=lambda item: item[1]['utc_at_video_zero_s']):
                if not name.startswith(role+'_'):
                    continue
                origin, scale = c['utc_at_video_zero_s'], c['time_scale']
                a = max(s['start_utc_s'], origin)
                b = min(s['end_utc_s'], origin+scale*c['source_video']['duration_s'])
                if b <= a:
                    continue
                start, end = (a-origin)/scale, (b-origin)/scale
                path = dest / f'{role}_part{len(pieces)+1:02}.mp4'
                piece = {'camera_key': name, 'source': c['source_video']['path'], 'output': str(path),
                         'source_start_s': start, 'source_end_s': end,
                         'sync_utc_start_s': a, 'sync_utc_end_s': b,
                         'joined_start_s': elapsed, 'joined_end_s': elapsed+end-start,
                         'utc_at_source_zero_s': origin, 'time_scale': scale,
                         'validated_source_interval_s': c['validated_interval_s'],
                         'clock_extrapolated_at_start': start < c['validated_interval_s'][0],
                         'clock_extrapolated_at_end': end > c['validated_interval_s'][1]}
                if not args.skip_video:
                    print('Video', path, flush=True)
                    piece['command'] = run_video(['-ss', f'{start:.9f}', '-i', piece['source'],
                                                  '-t', f'{end-start:.9f}'], path, ffmpeg)
                    piece['container_duration_s'] = probe_media(path, ffmpeg).duration_s
                    if abs(piece['container_duration_s']-(end-start)) > 3:
                        raise ValueError(f'Clip duration mismatch: {path}')
                pieces.append(piece)
                elapsed += end-start
            target = dest / f'{role}.mp4'
            if pieces and not args.skip_video:
                concat = dest / f'{role}_concat.txt'
                concat.write_text(''.join(f"file '{Path(p['output']).resolve().as_posix()}'\n"
                                         f"duration {p['joined_end_s']-p['joined_start_s']:.9f}\n"
                                         for p in pieces), encoding='utf-8')
                run_video(['-f', 'concat', '-safe', '0', '-i', str(concat)], target, ffmpeg)
            coverage = sum(p['sync_utc_end_s']-p['sync_utc_start_s'] for p in pieces)
            item = {'role': role, 'output': str(target), 'pieces': pieces,
                    'uncovered_duration_s': max(0., s['end_utc_s']-s['start_utc_s']-coverage)}
            s['videos'].append(item)
            dump(dest / f'{role}.clock.json', item)
        dump(Path('results') / s['experiment_id'] / 'segmentation/manifest.json', s)
        report = Path('results') / s['experiment_id'] / 'segmentation/report.md'
        report.write_text(f"# {s['experiment_id']}\n\n"
                          f"JST {utc_label(s['start_utc_s'])}–{utc_label(s['end_utc_s'])}。"
                          '子機入水 AND 親機測距。前後余白なし。\n\n'
                          f"[親機動画](../../../data/processed/{s['experiment_id']}/videos/parent.mp4) / "
                          f"[子機動画](../../../data/processed/{s['experiment_id']}/videos/child.mp4)\n\n"
                          '同期CSVは同じ実験IDのsynchronized/。動画時計・欠測はvideos/*.clock.json参照。\n',
                          encoding='utf-8')
    fig, ax = plt.subplots(figsize=(12, 4))
    origin = t[0]
    ax.plot((t-origin)/60, depth, lw=.7, label='Pressure depth (surface proxy)')
    for i, s in enumerate(segments, 1):
        ax.axvspan((s['start_utc_s']-origin)/60, (s['end_utc_s']-origin)/60,
                   alpha=.15, label=f'Trial {i}')
    ax.set(xlabel=f'Minutes from {utc_label(origin)} JST', ylabel='Relative depth [m]')
    ax.invert_yaxis()
    ax.legend()
    ax.grid(alpha=.3)
    fig.tight_layout()
    save_editable_figure(fig, out / 'intervals.png', dpi=150)
    plt.close(fig)
    manifest['code_sha256'] = sha256(Path(__file__))
    dump(out / 'manifest.json', manifest)
    lines = ['# Nanyo day2：時刻同期・実験分割', '',
             '開始=max(子機入水, 親機の最初の測距shot)、終了=min(離水, 測距停止)。前後余白なし。',
             '測距成功の有無で除外しない。親機scheduled時刻と子機tx時刻は各機器のアンカーで変換。',
             f'水面圧の代理値は最初の{cfg["baseline_first_s"]}秒の中央値 {baseline:.2f} mbar。',
             '0.2 m超の5点中央値を入水とし、30秒以内の水面付近の揺れを結合。水深は未校正。', '',
             '| 実験 | JST開始 | JST終了 | 秒 | 最大相対水深 m |', '|---|---|---|---:|---:|']
    for s in segments:
        lines.append(f"| [{s['experiment_id']}](../../{s['experiment_id']}/segmentation/report.md) | "
                     f"{utc_label(s['start_utc_s'])} | {utc_label(s['end_utc_s'])} | "
                     f"{s['end_utc_s']-s['start_utc_s']:.2f} | {s['max_relative_depth_m']:.2f} |")
    lines += ['', '[同期結果](../synchronization/report.md)。全動画を個別同期し、再起動前後の時計を共有しない。',
              '自動分割された動画は区間内で無再圧縮連結。各部分の時計と欠測はsidecarに保持。',
              'カメラの採用窓の外は外挿フラグ付き。元データは変更していない。', '',
              '![区間](intervals.png)', '', '[詳細manifest](manifest.json)']
    (out / 'report.md').write_text('\n'.join(lines), encoding='utf-8')
    print(json.dumps([{'id': s['experiment_id'], 'start': utc_label(s['start_utc_s']),
                      'end': utc_label(s['end_utc_s'])} for s in segments], indent=2))


if __name__ == '__main__':
    main()
