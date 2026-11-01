"""Join parent recordings and package UTC-selected underwater/acoustic intervals."""
import argparse
import csv
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import matplotlib

matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
from scipy.ndimage import median_filter

from aquabeacon_analysis.editable_plots import save_editable_figure
from aquabeacon_analysis.experiments import load_experiment
from aquabeacon_analysis.interval_selection import group_events, map_anchor, valid_anchors
from aquabeacon_analysis.ngt_binary import export_binary, sha256
from aquabeacon_analysis.time_sync import probe_media, resolve_ffmpeg


def read(path):
    with path.open(encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))


def write(path, rows, fields=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields or list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding='utf-8')


def media_run(args, output, expected_duration, ffmpeg, journal):
    command = [str(ffmpeg), '-hide_banner', '-loglevel', 'warning', '-nostdin', '-y', *args,
               '-map', '0:v:0', '-map', '0:a:0?', '-c', 'copy',
               '-metadata', 'creation_time=2026-10-31T00:00:00Z',
               '-movflags', '+faststart', str(output)]
    print('Video:', output, flush=True)
    with output.with_suffix('.ffmpeg.log').open('w', encoding='utf-8') as log:
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=log)
        print('ffmpeg PID', process.pid, flush=True)
        code = process.wait()
    if code:
        raise RuntimeError(f'ffmpeg failed; see {output.with_suffix(".ffmpeg.log")}')
    info = probe_media(output, ffmpeg)
    # Stream-copy preroll may contribute to container duration. Keep the exact request separately.
    if abs(info.duration_s-expected_duration) > 3:
        raise ValueError(f'Unexpected duration {info.duration_s} vs {expected_duration}')
    journal.append({'command': command, 'pid': process.pid, 'output': str(output),
                    'requested_duration_s': expected_duration, 'container_duration_s': info.duration_s,
                    'file_bytes': output.stat().st_size,
                    'creation_tag_policy': 'placeholder only; use sidecar clock mapping, not tag'})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment', required=True)
    parser.add_argument('--firmware-root', required=True, type=Path)
    parser.add_argument('--skip-video', action='store_true')
    args = parser.parse_args()
    exp = load_experiment(Path.cwd(), args.experiment)
    cfg_path = Path('config') / args.experiment / 'segments.json'
    cfg = json.loads(cfg_path.read_text())
    sync_path = exp.stage_path('results') / 'synchronization/manifest.json'
    clocks = json.loads(sync_path.read_text())['cameras']
    folder = exp.stage_path('interim') / 'synchronization'
    out = exp.stage_path('results') / 'segmentation'
    out.mkdir(parents=True, exist_ok=True)
    # Decode additional streams without touching raw inputs or previous exports.
    for role, pattern in [('child', f'C_MAIN_BNO_EVENTS_{cfg["child_suffix"]}.bin'),
                          ('child', f'C_MAIN_MULTIPATH_{cfg["child_suffix"]}.bin'),
                          ('parent', f'P_MAIN_HEALTH_RANGE_{cfg["parent_suffix"]}.bin')]:
        for source in (exp.raw_root / role).glob(pattern):
            if not (folder / role / f'{source.name}.decode.json').exists():
                print('Decode:', source.name, flush=True)
                export_binary(source, folder / role, args.firmware_root)
    health = read(folder / 'child' / f'C_MAIN_HEALTH_RANGE_{cfg["child_suffix"]}.health.csv')
    anchors = valid_anchors(health)
    health = [r for r in health if map_anchor(int(r['mono_us']), anchors) is not None]
    t = np.array([map_anchor(int(r['mono_us']), anchors) for r in health])
    p = np.array([float(r['pressure_mbar']) for r in health])
    baseline = float(np.median(p[t < t[0]+cfg['baseline_first_s']]))
    depth = (p-baseline)*100/(cfg['density_kg_m3_assumed']*cfg['gravity_m_s2'])
    filtered = median_filter(depth, size=cfg['median_samples'])
    underwater = filtered > cfg['depth_threshold_m']
    shot_path = folder / 'child' / f'C_MAIN_HEALTH_RANGE_{cfg["child_suffix"]}.shot.csv'
    shots = read(shot_path)
    shot_times = {int(r['shot_seq']): map_anchor(int(r['tx_us']), anchors) for r in shots}
    acoustic = [shot_times[int(r['shot_seq'])] for r in shots
                if int(r['range_state']) in cfg['acoustic_candidate_states']
                and int(r['tof_us']) > 0 and shot_times[int(r['shot_seq'])] is not None]
    segments = group_events([*t[underwater], *acoustic], cfg['merge_gap_s'], cfg['padding_s'])
    if not segments:
        raise ValueError('No selected intervals')
    parent = sorted([(k, c) for k, c in clocks.items() if k.startswith('parent')],
                    key=lambda kv: kv[1]['utc_at_video_zero_s'])
    child = next((k, c) for k, c in clocks.items() if k.startswith('child'))
    elapsed = 0.
    mapping = []
    for name, c in parent:
        duration = c['source_video']['duration_s']
        mapping.append({'source': c['source_video']['path'], 'camera_key': name,
                        'joined_start_s': elapsed, 'joined_end_s': elapsed+duration,
                        'utc_at_source_zero_s': c['utc_at_video_zero_s'], 'time_scale': c['time_scale']})
        elapsed += duration
    gap = (mapping[1]['utc_at_source_zero_s'] - (mapping[0]['utc_at_source_zero_s']
           + mapping[0]['time_scale']*parent[0][1]['source_video']['duration_s']))
    manifest = {'experiment_id': args.experiment, 'config': cfg, 'pressure_baseline_mbar': baseline,
                'pressure_baseline_utc_s': [float(t[0]), float(t[0]+cfg['baseline_first_s'])],
                'pressure_baseline_note': 'Stable initial surface-pressure proxy; not calibrated depth.',
                'clock_manifest_sha256': sha256(sync_path), 'config_sha256': sha256(cfg_path),
                'git_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
                'parent_join_mapping': mapping, 'parent_join_estimated_gap_s': gap,
                'segments': segments, 'processes': [],
                'selection_rule': 'pressure median depth > threshold OR state 3 positive ToF',
                'limitations': ['State 3 is an uncalibrated detection candidate, not validated xyz.',
                                'Clock equations remain piecewise across joined video.',
                                'Video streams are copied without retiming; CSV uses synchronized UTC.',
                                'MP4 edit lists may retain hidden keyframe preroll.']}
    selection = [{'sync_utc_s': float(ti), 'pressure_mbar': float(pi), 'relative_depth_m': float(di),
                  'median_depth_m': float(fi), 'underwater': int(ui)}
                 for ti, pi, di, fi, ui in zip(t, p, depth, filtered, underwater, strict=True)]
    write(out / 'selection.csv', selection)
    write(out / 'acoustic_candidates.csv', [{'sync_utc_s': ti} for ti in acoustic])
    manifest['threshold_sensitivity'] = {str(th): group_events([*t[filtered > th], *acoustic],
                                        cfg['merge_gap_s'], cfg['padding_s']) for th in [.1, .2, .3]}
    sources = []
    for role, suffix in [('child', cfg['child_suffix']), ('parent', cfg['parent_suffix'])]:
        for path in (folder / role).glob(f'*{suffix}*.csv'):
            sources.append((role, path))
    sources += [('parent', folder / 'parent_100hz.csv')]
    sources += [('camera', path) for path in
                (exp.stage_path('processed') / 'synchronization').glob('*_attitude.csv')]
    # Register both intervals before creating any interval-specific analysis products.
    for i, segment in enumerate(segments, 1):
        eid = args.experiment[:-3] + f'{i:03}'
        segment.update(experiment_id=eid, sensor_files=[], videos=[])
        metadata = {**exp.metadata, 'experiment_id': eid,
                    'description': f'Nanyo underwater/acoustic interval {i}',
                    'c_main_suffix': cfg['child_suffix'], 'p_main_suffix': cfg['parent_suffix'],
                    'config_paths': [str(cfg_path).replace('\\', '/')],
                    'utc_interval_s': [segment['start_utc_s'], segment['end_utc_s']],
                    'parent_acquisition': args.experiment}
        dump(Path('experiments') / eid / 'experiment.json', metadata)
    for role, source in sources:
        rows = read(source)
        if not rows:
            continue
        role_anchors = anchors
        if role == 'parent':
            role_anchors = valid_anchors(read(folder / 'parent' /
                f'P_MAIN_HEALTH_RANGE_{cfg["parent_suffix"]}.health.csv'))
        selected = [[] for _ in segments]
        unmapped = 0
        for row in rows:
            utc = None
            for field, divisor in [('sync_utc_s', 1), ('sample_utc_us', 1e6),
                                   ('utc_us', 1e6), ('utc_read_us', 1e6),
                                   ('utc_reference_ms', 1000)]:
                if row.get(field) and float(row[field])/divisor > 1e9:
                    utc = float(row[field])/divisor
                    break
            basis = 'recorded_or_camera_aligned_utc'
            if '.shot.csv' in source.name or '.range_measurement.csv' in source.name:
                utc = shot_times.get(int(row['shot_seq']))
                basis = 'child_transmit_edge_anchor'
            elif utc is None and row.get('mono_us'):
                utc = map_anchor(int(row['mono_us']), role_anchors)
                basis = 'valid_health_anchor_at_record'
            if utc is None:
                unmapped += 1
                continue
            for segment, target in zip(segments, selected, strict=True):
                if segment['start_utc_s'] <= utc < segment['end_utc_s']:
                    target.append({**row, 'sync_utc_s': utc,
                                   'segment_time_s': utc-segment['start_utc_s'],
                                   'clock_basis': basis})
        for segment, target in zip(segments, selected, strict=True):
            dest = Path('data/processed') / segment['experiment_id'] / 'synchronized' / role / source.name
            fields = list(dict.fromkeys([*rows[0], 'sync_utc_s', 'segment_time_s', 'clock_basis']))
            write(dest, target, fields)
            segment['sensor_files'].append({'source': str(source), 'output': str(dest),
                                           'rows': len(target), 'unmapped_source_rows': unmapped,
                                           'source_sha256': sha256(source)})
        print('Subset:', source.name, [len(x) for x in selected], flush=True)
    joined = exp.stage_path('processed') / 'videos'
    joined.mkdir(parents=True, exist_ok=True)
    concat = joined / 'parent_concat.txt'
    concat.write_text(''.join(f"file '{Path(m['source']).as_posix()}'\n"
                             f"duration {m['joined_end_s']-m['joined_start_s']:.9f}\n"
                             for m in mapping), encoding='utf-8')
    dump(joined / 'parent_joined.clock.json', mapping)
    ffmpeg = resolve_ffmpeg()
    full = joined / 'parent_joined.mp4'
    if not args.skip_video:
        media_run(['-f', 'concat', '-safe', '0', '-i', str(concat)], full,
                  elapsed, ffmpeg, manifest['processes'])
    for segment in segments:
        eid = segment['experiment_id']
        dest = Path('data/processed') / eid / 'videos'
        dest.mkdir(parents=True, exist_ok=True)
        for role in ('parent', 'child'):
            def source_time(utc, role=role):
                if role == 'child':
                    return (utc-child[1]['utc_at_video_zero_s'])/child[1]['time_scale']
                m = next((m for m in reversed(mapping) if utc >= m['utc_at_source_zero_s']), mapping[0])
                return m['joined_start_s']+(utc-m['utc_at_source_zero_s'])/m['time_scale']
            start, end = (source_time(segment[k]) for k in ['start_utc_s', 'end_utc_s'])
            video = full if role == 'parent' else Path(child[1]['source_video']['path'])
            target = dest / f'{role}.mp4'
            info = {'role': role, 'source': str(video), 'output': str(target),
                    'source_start_s': start, 'source_end_s': end,
                    'sync_utc_start_s': segment['start_utc_s'], 'sync_utc_end_s': segment['end_utc_s']}
            segment['videos'].append(info)
            if not args.skip_video:
                media_run(['-ss', f'{start:.9f}', '-i', str(video), '-t', f'{end-start:.9f}'],
                          target, end-start, ffmpeg, manifest['processes'])
        keep = (t >= segment['start_utc_s']) & (t < segment['end_utc_s'])
        segment['max_relative_depth_m'] = float(depth[keep].max())
        segment['acoustic_candidate_shots'] = sum(segment['start_utc_s'] <= x < segment['end_utc_s']
                                                   for x in acoustic)
        dump(Path('results') / eid / 'segmentation/manifest.json', segment)
    fig, ax = plt.subplots(figsize=(12, 4))
    origin = parent[0][1]['utc_at_video_zero_s']
    ax.plot((t-origin)/60, depth, lw=.8, label='Pressure depth (surface proxy)')
    ax.scatter((np.array(acoustic)-origin)/60, np.zeros(len(acoustic)), s=3,
               label='Acoustic state 3 + positive ToF')
    for i, s in enumerate(segments, 1):
        ax.axvspan((s['start_utc_s']-origin)/60, (s['end_utc_s']-origin)/60,
                   alpha=.15, color='orange', label=f'Interval {i}')
    ax.set(xlabel='Minutes from parent first video start', ylabel='Relative depth [m]')
    ax.invert_yaxis()
    ax.legend()
    ax.grid(alpha=.3)
    fig.tight_layout()
    save_editable_figure(fig, out / 'intervals.png', dpi=150)
    plt.close(fig)
    manifest['code_sha256'] = sha256(Path(__file__))
    dump(out / 'manifest.json', manifest)
    jst = timezone(timedelta(hours=9))
    lines = ['# Nanyo：連結動画と解析区間', '',
             (f'水面基準圧 {baseline:.1f} mbar（最初の180秒の中央値）。'
             '仮定密度1025 kg/m³、5点中央値の相対水深0.2 m超、'
             'または子機shotのstate=3かつToF正値を選択。30秒以内の空白を結合し前後10秒を付加。'), '',
             (f'親機2本を無再圧縮連結。継ぎ目の推定UTC差は {gap*1000:.1f} ms。'
             'この微小差の映像補完や再生速度補正は行わず、piecewise時計をsidecarに保持。'), '',
             f'[連結親機動画](../../../data/processed/{args.experiment}/videos/parent_joined.mp4)', '',
             '| 区間 | 切出し時刻 JST | 長さ | 相対最大深度 | 音響候補shot |',
             '|---|---|---:|---:|---:|']
    for s in segments:
        a, b = [datetime.fromtimestamp(s[k], jst).strftime('%H:%M:%S')
                for k in ['start_utc_s', 'end_utc_s']]
        lines.append(f"| {s['experiment_id']} | {a}–{b} | "
                     f"{s['end_utc_s']-s['start_utc_s']:.1f} s | "
                     f"{s['max_relative_depth_m']:.2f} m | {s['acoustic_candidate_shots']} |")
        report = Path('results') / s['experiment_id'] / 'segmentation/report.md'
        report.write_text(f"# {s['experiment_id']}\n\nJST {a}–{b}（前後10秒付き）。\n\n"
                          f"[親機動画](../../../data/processed/{s['experiment_id']}/videos/parent.mp4) / "
                          f"[子機動画](../../../data/processed/{s['experiment_id']}/videos/child.mp4)\n\n"
                          f"ログは `data/processed/{s['experiment_id']}/synchronized/`。"
                          'sync_utc_s と segment_time_s で照合。元列・不成立行も保持。\n', encoding='utf-8')
    lines += ['', '![抽出区間](intervals.png)', '',
              ('水面圧・海水密度は区間選別用の暫定値。state=3は検出候補で、'
              '検証済み測位や直接波を保証しない。0.1/0.2/0.3 mの感度比較をmanifestへ保存。'), '',
              ('各区間の動画は元解像度・fps・音声を無再圧縮で維持。'
              'DJIテレメトリは同期済みCSVに保存。コンテナ作成時刻を計測時刻に使わない。'
              '区間は半開区間。未対応時刻の行数はmanifestへ記録。'), '',
              ('ログには子機IMU/BNO/health/shot/multipath、親機health/range/time_position、'
              '親機100 Hz IMU、DJI同期姿勢を含む。GNSS生観測・960 Hz親機原本は共有raw参照に保持。')]
    (out / 'report.md').write_text('\n'.join(lines), encoding='utf-8')
    print(json.dumps([{k: v for k, v in s.items() if k not in ('sensor_files', 'videos')}
                      for s in segments], indent=2), flush=True)


if __name__ == '__main__':
    main()
