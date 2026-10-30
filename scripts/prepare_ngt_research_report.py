"""Build frame-traceable figures, interval tables and paired review clips for the pool report."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

for key in ['OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'OPENCV_FFMPEG_THREADS']:
    os.environ.setdefault(key, '2')
import cv2
import matplotlib
import numpy as np

matplotlib.use('Agg')
import matplotlib.pyplot as plt
from PIL import Image, ImageDraw, ImageFont

from aquabeacon_analysis.camera_calibration import load_camera_intrinsics
from aquabeacon_analysis.editable_plots import save_editable_figure
from aquabeacon_analysis.ngt_position import calibrated_rays
from aquabeacon_analysis.ngt_research_review import (
    clockwise_display_to_sensor,
    position_state,
    sample_cell_vertices,
    sensor_rays_to_clockwise_display,
    state_intervals,
)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'results/20261028_ngt_000/research_report'
CONFIG_PATH = ROOT / 'config/20261028_ngt/research_report.json'
MAP = ROOT / 'results/20261028_ngt_004/wall_mapping/native120_restart'
SOURCE_IMAGES = ROOT / 'data/interim/20261028_ngt_004/child_only_vslam/okinawa_native120/mav0/cam0/data'
SYNC_PATH = ROOT / 'results/20261028_ngt_000/synchronization/manifest.json'
BLUE, GREEN, ORANGE, GRAY = '#00739c', '#176b46', '#c76e27', '#d8dfe2'


def read_csv(path):
    with path.open(encoding='utf-8-sig', newline='') as handle:
        return list(csv.DictReader(handle))


def write_csv(path, rows):
    if not rows:
        return
    with path.open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.DictWriter(handle, list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')


def digest(path):
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def selected(rows, start, end, key='parent_video_s'):
    return [r for r in rows if start-1e-7 <= float(r[key]) < end-1e-7]


def fraction(rows, key):
    return sum(r[key] == 'True' for r in rows)/len(rows) if rows else None


def save(fig, name):
    save_editable_figure(fig, OUT / f'{name}.png', dpi=170)
    plt.close(fig)


def paint_cells(ax, times, values, row, color, end_s):
    x, valid = sample_cell_vertices(times, values, end_s)
    ax.fill_between(x, row-.3, row+.3, color=GRAY)
    ax.fill_between(x, row-.3, row+.3, where=valid, color=color)


def map_example():
    """Show saved local geometry without promoting its legacy metric scale to a measurement."""
    sys.path.insert(0, str(ROOT / 'data/interim/20261028_ngt_004/wall_mapping/runtime'))
    import pycolmap
    models = json.loads((MAP / 'models.json').read_text())
    item = next(m for m in models if m['window'] == 14 and m['model'] == 1)
    folder = ROOT / item['folder']
    model = pycolmap.Reconstruction(str(folder))
    ids = sorted(model.points3D)
    xyz = np.array([model.points3D[i].xyz for i in ids])
    rgb = np.array([model.points3D[i].color for i in ids])
    origin = xyz.mean(axis=0)
    _, _, basis = np.linalg.svd(xyz-origin, full_matrices=False)
    coords = (xyz-origin) @ basis.T
    write_csv(OUT / 'map_example_points.csv', [{'point_id': int(i),
        'local_x':float(p[0]), 'local_y':float(p[1]), 'local_z':float(p[2]),
        'pc1':float(q[0]), 'pc2':float(q[1]), 'pc3':float(q[2]),
        'r':int(c[0]), 'g':int(c[1]), 'b':int(c[2])}
        for i, p, q, c in zip(ids, xyz, coords, rgb, strict=True)])
    review = MAP / 'reviews/window_14_model_1.png'
    fig, axes = plt.subplots(1, 3, figsize=(12, 4.2), layout='constrained')
    axes[0].imshow(cv2.cvtColor(cv2.imread(str(review)), cv2.COLOR_BGR2RGB))
    axes[0].set_title('登録画像と対応する3D点'); axes[0].axis('off')
    for ax, j in zip(axes[1:], [1, 2], strict=True):
        ax.scatter(coords[:, 0], coords[:, j], s=1.1, c=rgb/255, linewidths=0)
        ax.set(xlabel='PC1 [局所モデル単位]', ylabel=f'PC{j+1} [局所モデル単位]')
        ax.set_aspect('equal', adjustable='box'); ax.grid(alpha=.2)
    save(fig, 'map_example_local')
    inputs = [folder / f for f in ['points3D.bin', 'images.bin', 'cameras.bin']] + [review]
    write_json(OUT / 'map_example_metadata.json', {'model':'14_1', 'points':len(ids),
        'registered_images':item['registered_images'], 'parent_interval_s':[item['first_parent_s'], item['last_parent_s']],
        'local_origin':origin.tolist(), 'pca_basis_rows':basis.tolist(), 'all_points_displayed':True,
        'scale':'local model units, not metres; legacy position priors were used during optimization',
        'inputs_sha256':{str(p.relative_to(ROOT)):digest(p) for p in inputs}})


class CameraFrames:
    """Preserve source aspect and orientation; never silently resize a portrait frame."""
    def __init__(self, mapping):
        self.sync = json.loads(SYNC_PATH.read_text())['cameras']
        self.paths = {role: Path(v['video']).with_suffix('.LRF') for role, v in self.sync.items()}
        self.caps = {role: cv2.VideoCapture(str(path), cv2.CAP_FFMPEG,
            [cv2.CAP_PROP_N_THREADS, 2]) for role, path in self.paths.items()}
        assert all(cap.isOpened() for cap in self.caps.values())
        self.mapping = mapping
        self.mt = np.array([float(r['parent_video_s']) for r in mapping])
        self.stats = {str(p): {'size': p.stat().st_size, 'mtime_ns': p.stat().st_mtime_ns} for p in self.paths.values()}

    def child_time(self, parent_s):
        p, c = self.sync['parent'], self.sync['child']
        return (p['utc_at_video_zero_s'] + p['time_scale']*parent_s-c['utc_at_video_zero_s'])/c['time_scale']

    def get(self, role, parent_s, use_native=True):
        source_s = parent_s if role == 'parent' else self.child_time(parent_s)
        if role == 'child' and use_native and self.mt[0] <= parent_s <= self.mt[-1]:
            ix = int(np.argmin(abs(self.mt-parent_s)))
            row = self.mapping[ix]
            path = SOURCE_IMAGES / row['source_image_name']
            frame = cv2.imread(str(path))
            assert frame is not None
            return frame, {'source': str(path.relative_to(ROOT)), 'source_s': float(row['child_video_s']),
                'source_frame': int(row['source_frame']), 'actual_parent_s': float(row['parent_video_s']),
                'native_mapping_index': ix, 'orientation': 'unchanged native SLAM JPEG, 1280x720'}
        cap = self.caps[role]
        target = round(source_s*cap.get(cv2.CAP_PROP_FPS))
        current = int(cap.get(cv2.CAP_PROP_POS_FRAMES))
        if target < current or target-current > 90:
            assert cap.set(cv2.CAP_PROP_POS_FRAMES, target)
        while int(cap.get(cv2.CAP_PROP_POS_FRAMES)) <= target:
            ok, frame = cap.read()
            if not ok:
                raise RuntimeError(f'Cannot decode {role} frame {target}')
        pts = cap.get(cv2.CAP_PROP_POS_MSEC)/1000
        actual_parent = pts if role == 'parent' else (self.sync['child']['utc_at_video_zero_s']
            + self.sync['child']['time_scale']*pts-self.sync['parent']['utc_at_video_zero_s'])/self.sync['parent']['time_scale']
        return frame, {'source': str(self.paths[role].relative_to(ROOT)), 'source_s': pts,
            'source_frame': int(cap.get(cv2.CAP_PROP_POS_FRAMES))-1, 'actual_parent_s': actual_parent,
            'native_mapping_index': None, 'orientation': f"OpenCV automatic display rotation {cap.get(cv2.CAP_PROP_ORIENTATION_META):g} degrees"}

    def close(self):
        for cap in self.caps.values():
            cap.release()
        for path, stat in self.stats.items():
            p = Path(path)
            assert (p.stat().st_size, p.stat().st_mtime_ns) == (stat['size'], stat['mtime_ns'])


def nearest(rows, t):
    return min(rows, key=lambda r: abs(float(r['parent_video_s'])-t))


def media(cfg, tables, mapping):
    source = CameraFrames(mapping)
    records = []
    try:
        for case in cfg['cases']:
            selected_frames = []
            for i, t in enumerate(case['samples_s']):
                for role in ['parent', 'child']:
                    frame, meta = source.get(role, t)
                    path = OUT / 'frames' / f"{case['id']}_{i}_{role}.png"
                    cv2.imwrite(str(path), frame)
                    p = nearest(tables[case['experiment']], t)
                    rec = {'case': case['id'], 'role': role, 'requested_parent_s': t,
                        **meta, 'width': frame.shape[1], 'height': frame.shape[0],
                        'file': str(path.relative_to(OUT)), 'sha256': digest(path),
                        'position_state': position_state(p)}
                    records.append(rec)
                    if role == case['focus']:
                        selected_frames.append((frame, rec))
            portrait = case['focus'] == 'parent'
            fig, axes = plt.subplots(1, 3, figsize=(10.4, 7.4) if portrait else (12, 3.3), layout='constrained')
            for ax, (frame, rec) in zip(axes, selected_frames, strict=True):
                ax.imshow(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                ax.set_title(f"親動画 {rec['actual_parent_s']:.2f} s" +
                    (f"\n子動画 {rec['source_s']:.2f} s" if not portrait else ''), fontsize=11)
                ax.axis('off')
            save(fig, f"case_{case['id']}")
        write_csv(OUT / 'frame_evidence.csv', records)
        write_json(OUT / 'source_video_stat.json', source.stats)
    finally:
        source.close()


def audit_orientation(tables):
    path = ROOT / 'data/raw/ngtHighSchoolExperiment1/parent/DJI/DJI_20261028140300_0024_D.LRF'
    pairs = []
    for index in [32068, 42468, 45375]:
        frames = []
        for auto in [0, 1]:
            cap = cv2.VideoCapture(str(path), cv2.CAP_FFMPEG, [cv2.CAP_PROP_N_THREADS, 2])
            try:
                cap.set(cv2.CAP_PROP_ORIENTATION_AUTO, auto)
                cap.set(cv2.CAP_PROP_POS_FRAMES, index)
                ok, frame = cap.read()
                assert ok
                frames.append(frame)
            finally:
                cap.release()
        assert np.array_equal(cv2.rotate(frames[0], cv2.ROTATE_90_CLOCKWISE), frames[1])
        pairs.append({'frame': index, 'sensor_size_wh': list(frames[0].shape[1::-1]),
            'display_size_wh': list(frames[1].shape[1::-1]), 'clockwise_pixel_equality': True})
        if index == 32068:
            fig, axes = plt.subplots(1, 2, figsize=(9, 6), layout='constrained', width_ratios=[1.5, 1])
            for ax, frame, title in zip(axes, frames,
                ['センサー画像 1280×720', '自動回転後 720×1280'], strict=True):
                ax.imshow(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)); ax.set_title(title); ax.axis('off')
            save(fig, 'orientation_audit')
    k, d, provenance = load_camera_intrinsics(ROOT, 'P_MAIN_CAM', (1280, 720), 'proxy')
    sensitivity, stats = [], []
    for exp, rows in tables.items():
        good = [r for r in rows if r['optical_available'] == 'True']
        pixel = np.array([[float(r['light_x_px']), float(r['light_y_px'])] for r in good])
        old = calibrated_rays(pixel, k, d)
        sensor = calibrated_rays(clockwise_display_to_sensor(pixel, 720), k, d)
        corrected = sensor_rays_to_clockwise_display(sensor)
        delta = np.degrees(np.arccos(np.clip(np.sum(old*corrected, axis=1), -1, 1)))
        for r, a, ray in zip(good, delta, corrected, strict=True):
            sensitivity.append({'experiment': exp, 'parent_video_s': r['parent_video_s'],
                'angular_difference_deg': float(a), 'old_off_axis_deg': float(r['off_axis_deg']),
                'orientation_corrected_off_axis_deg': float(np.degrees(np.arccos(ray[2])))})
        stats.append({'experiment': exp, 'optical_samples': len(good),
            'ray_angle_change_median_deg': float(np.median(delta)), 'ray_angle_change_p95_deg': float(np.percentile(delta, 95))})
    write_csv(OUT / 'orientation_sensitivity.csv', sensitivity)
    write_json(OUT / 'orientation_audit.json', {'pixel_checks': pairs, 'intrinsics': provenance,
        'sensitivity': stats, 'status': 'legacy_coordinate_mismatch_confirmed',
        'scope': 'Display-basis optical-ray sensitivity only. No replacement body extrinsic or world position certified.',
        'impact': 'Legacy availability flags remain log facts. Legacy position scatter, angles, tracks and position-conditioned map geometry require re-evaluation.'})


def tables_and_figures(cfg, tables, mapping):
    intervals, bins, counts, summaries = [], [], [], []
    labels = {'002': '静止', '003': '測位限界', '004': '同時移動'}
    keys = ['optical_available', 'range_formula_available', 'magnetic_attitude_available', 'magnetic_position_available']
    for exp, rows in tables.items():
        metrics = json.loads((ROOT / f'results/20261028_ngt_{exp}/position_evaluation/metrics.json').read_text())
        t = np.array([float(r['parent_video_s']) for r in rows])
        states = [position_state(r) for r in rows]
        for run in state_intervals(t, states, metrics['interval_s'][1]):
            intervals.append({'experiment': exp, **run})
        for state, n in Counter(states).items():
            counts.append({'experiment': exp, 'state': state, 'samples': n, 'duration_s': n*.2, 'fraction': n/len(rows)})
        summaries.append({'experiment': exp, 'label': labels[exp], 'start_s': metrics['interval_s'][0],
            'end_s': metrics['interval_s'][1], 'duration_s': metrics['duration_s'], 'samples': len(rows),
            'position_samples': sum(s == 'available' for s in states), 'longest_gap_s': metrics['longest_gap_s'],
            **{k: fraction(rows, k) for k in keys}})
    write_csv(OUT / 'position_intervals.csv', intervals)
    write_csv(OUT / 'position_failure_patterns.csv', counts)
    for start in range(1260, 1540, 10):
        p, m = selected(tables['004'], start, start+10), selected(mapping, start, start+10)
        bins.append({'start_s': start, 'end_s': start+10, 'position_cells': len(p),
            **{k: sum(r[k] == 'True' for r in p) for k in keys}, 'mapping_frames': len(m),
            **{k: sum(r[k] == 'True' for r in m) for k in ['prior_available', 'registered_structural', 'position_registered']},
            'median_sift_features': float(np.median([int(r['features']) for r in m]))})
    write_csv(OUT / 'movement_10s_bins.csv', bins)
    boundary_samples = [{'source_frame': r['source_frame'], 'parent_video_s': r['parent_video_s'],
        'source_image_name': r['source_image_name'], 'reason': 'Native baseline frame outside nominal [1260,1540) after clock conversion'}
        for r in mapping if not 1260 <= float(r['parent_video_s']) < 1540]
    write_csv(OUT / 'movement_boundary_frames.csv', boundary_samples)
    mt = np.array([float(r['parent_video_s']) for r in mapping])
    states = ['positioned' if r['position_registered'] == 'True' else 'local' if r['registered_structural'] == 'True' else 'unregistered' for r in mapping]
    mapping_end = mt[-1] + np.median(np.diff(mt))
    write_csv(OUT / 'mapping_intervals.csv', state_intervals(mt, states, mapping_end))
    # All states remain in each denominator; failures are never visually interpolated.
    fig, axes = plt.subplots(3, 1, figsize=(11, 6.5), layout='constrained')
    for ax, (exp, rows) in zip(axes, tables.items(), strict=True):
        t = np.array([float(r['parent_video_s']) for r in rows])
        for j, (key, label) in enumerate(zip(keys, ['光点候補', '測距候補', '親機姿勢', '位置算出'], strict=True)):
            ok = np.array([r[key] == 'True' for r in rows])
            paint_cells(ax, t, ok, j, GREEN if j == 3 else BLUE, t[-1]+.2)
        ax.set(yticks=range(4), yticklabels=['光点候補', '測距候補', '親機姿勢', '位置算出'],
            title=f"{exp} {labels[exp]}", xlim=(t[0], t[-1]+.2))
        ax.invert_yaxis(); ax.grid(axis='x', alpha=.2)
    axes[-1].set_xlabel('親機元動画の時刻 [s]   色：成立 / 灰：不成立')
    save(fig, 'position_timeline')
    fig, axes = plt.subplots(3, 1, figsize=(12, 7), sharex=True, layout='constrained', height_ratios=[2, 1.4, 1])
    p = tables['004']; pt = np.array([float(r['parent_video_s']) for r in p])
    series = [(pt, [r['magnetic_position_available'] == 'True' for r in p], '位置算出（5 Hz）', BLUE),
        (mt, [r['prior_available'] == 'True' for r in mapping], '位置拘束あり（補間後）', '#57a5bf'),
        (mt, [r['registered_structural'] == 'True' for r in mapping], '形状あり（50点以上）', GREEN),
        (mt, [r['position_registered'] == 'True' for r in mapping], '共通座標へ配置（旧条件）', ORANGE)]
    for j, (t, v, label, color) in enumerate(series):
        paint_cells(axes[0], t, v, j, color, 1540 if len(t) == len(p) else mapping_end)
    axes[0].set(yticks=range(4), yticklabels=[s[2] for s in series]); axes[0].invert_yaxis()
    axes[1].plot(mt, [int(r['features']) for r in mapping], lw=.6, color=BLUE)
    axes[1].set_ylabel('壁ROIのSIFT特徴数')
    centers = [b['start_s']+5 for b in bins]
    for key, label, color in [('magnetic_position_available', '位置算出', BLUE), ('registered_structural', '形状登録', GREEN), ('position_registered', '共通座標（旧条件）', ORANGE)]:
        y = [100*b[key]/b['position_cells' if key == 'magnetic_position_available' else 'mapping_frames'] for b in bins]
        axes[2].plot(centers, y, '.-', color=color, label=label)
    axes[2].set(ylabel='10秒ごとの割合 [%]', ylim=(-3, 103), xlabel='親機元動画の時刻 [s]')
    axes[2].legend(fontsize=9, ncol=3)
    for ax in axes:
        ax.axvline(1415, color='.4', ls='--', lw=1); ax.set_xlim(1260, 1540); ax.grid(alpha=.2)
    save(fig, 'movement_mapping_timeline')
    # Cases are explicit counterexamples, not a random selection.
    case_stats = []
    for case in cfg['cases']:
        p = selected(tables[case['experiment']], *case['clip_s'])
        m = selected(mapping, *case['clip_s']) if case['experiment'] == '004' else []
        case_stats.append({'id': case['id'], 'start_s': case['clip_s'][0], 'end_s': case['clip_s'][1],
            'position_cells': len(p), **{k: sum(r[k] == 'True' for r in p) for k in keys},
            'failure_patterns': dict(Counter(position_state(r) for r in p)), 'mapping_frames': len(m),
            **{k: sum(r[k] == 'True' for r in m) for k in ['prior_available', 'registered_structural', 'position_registered']}})
    write_json(OUT / 'statistics.json', {'positions': summaries, 'cases': case_stats,
        'mapping': json.loads((MAP / 'summary.json').read_text()),
        'mapping_sampling_bounds': {'nominal_interval_s':[1260,1540],
            'actual_first_last_s':[float(mt[0]),float(mt[-1])], 'outside_nominal_frames':len(boundary_samples),
            'policy':'Preserve exact native baseline frame identities; list nominal-boundary spill separately from 10-second bins'},
        'position_interpretation': 'Availability of legacy candidate pipeline; coordinates require orientation correction',
        'mapping_interpretation': 'Saved reconstruction under legacy position priors, not corrected geometry'})


def clips(cfg, tables, mapping):
    import imageio_ffmpeg
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    source = CameraFrames(mapping)
    font = ImageFont.truetype('C:/Windows/Fonts/meiryo.ttc', 23)
    small = ImageFont.truetype('C:/Windows/Fonts/meiryo.ttc', 19)
    records, processes = [], []
    try:
        for case in cfg['cases']:
            start, end = case['clip_s']
            path = OUT / 'clips' / f"{case['id']}.mp4"
            count = round((end-start)*cfg['clip_fps'])
            command = [ffmpeg, '-hide_banner', '-loglevel', 'error', '-y', '-f', 'rawvideo',
                '-pixel_format', 'rgb24', '-video_size', '1344x768', '-framerate', str(cfg['clip_fps']),
                '-i', 'pipe:0', '-an', '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '18',
                '-threads', '2', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', str(path)]
            with (OUT / 'clips' / f"{case['id']}.log").open('wb') as log:
                process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=log, stderr=log,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                meta = {'pid': process.pid, 'started_utc': datetime.now(UTC).isoformat(),
                    'owner': 'ngt_research_report', 'purpose': f"Encode paired camera review {case['id']}", 'command': command}
                processes.append(meta)
                write_json(OUT / 'clip_processes.json', processes)
                begun = time.monotonic()
                try:
                    for i in range(count):
                        if time.monotonic()-begun > 240:
                            raise TimeoutError(f"Clip {case['id']} exceeded 240 seconds")
                        t = start + i/cfg['clip_fps']
                        parent, pm = source.get('parent', t)
                        child, cm = source.get('child', t)
                        p = nearest(tables[case['experiment']], t)
                        canvas = Image.new('RGB', tuple(cfg['clip_size']), 'white')
                        draw = ImageDraw.Draw(canvas)
                        draw.text((18, 10), f"{case['id']}  {case['title']}", font=font, fill='#1a2023')
                        draw.text((18, 50), f"親動画 {pm['source_s']:.3f} s    子動画 {cm['source_s']:.3f} s    時刻同期済み・12 fps表示", font=small, fill='#40484b')
                        status = ' / '.join(f"{name}：{'あり' if p[key]=='True' else 'なし'}" for name, key in
                            [('光点候補','optical_available'),('測距候補','range_formula_available'),('親機姿勢','magnetic_attitude_available'),('位置算出','magnetic_position_available')])
                        draw.text((18, 83), status, font=small, fill='#1a2023')
                        draw.text((18, 122), '親機カメラ', font=small, fill=BLUE)
                        draw.text((370, 122), '子機カメラ', font=small, fill=GREEN)
                        for frame, box in [(parent, (18, 154, 324, 576)), (child, (370, 154, 960, 576))]:
                            im = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                            im.thumbnail((box[2], box[3]), Image.Resampling.LANCZOS)
                            canvas.paste(im, (box[0]+(box[2]-im.width)//2, box[1]+(box[3]-im.height)//2))
                        draw.text((18, 739), '映像の縦横比を保持。ラベルは既存解析の候補フラグ。位置座標の精度保証ではない。', font=small, fill='#50585b')
                        process.stdin.write(canvas.tobytes())
                        records.append({'case':case['id'], 'clip_frame':i, 'clip_time_s':i/cfg['clip_fps'],
                            'requested_parent_s':t, 'parent_pts_s':pm['source_s'], 'parent_frame':pm['source_frame'],
                            'child_pts_s':cm['source_s'], 'child_frame':cm['source_frame'],
                            'child_actual_parent_s':cm['actual_parent_s'], 'child_source':cm['source'],
                            'position_state':position_state(p)})
                    process.stdin.close()
                    if process.wait(timeout=60) != 0:
                        raise RuntimeError(f'ffmpeg failed for {case["id"]}')
                    cap = cv2.VideoCapture(str(path))
                    try:
                        assert int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) == count
                        assert cap.get(cv2.CAP_PROP_FPS) == cfg['clip_fps']
                        assert int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) == cfg['clip_size'][0]
                        for ix in [0, count//2, count-1]:
                            cap.set(cv2.CAP_PROP_POS_FRAMES, ix)
                            ok, frame = cap.read(); assert ok
                            if ix == count//2:
                                cv2.imwrite(str(OUT / 'clips' / f"{case['id']}_preview.jpg"), frame)
                    finally:
                        cap.release()
                    meta.update(status='completed_and_decode_verified', frames=count)
                    print(f"Clip {case['id']}: {count} frames verified", flush=True)
                finally:
                    if process.stdin and not process.stdin.closed:
                        process.stdin.close()
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill(); process.wait(timeout=5)
                    meta.update(ended_utc=datetime.now(UTC).isoformat(), exit_code=process.returncode)
                    write_json(OUT / 'clip_processes.json', processes)
        write_csv(OUT / 'clip_frames.csv', records)
    finally:
        source.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment', required=True, choices=['20261028_ngt_000'])
    parser.add_argument('--phase', choices=['prepare', 'clips'], required=True)
    args = parser.parse_args()
    cfg = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
    cv2.setNumThreads(2)
    plt.rcParams.update({'font.family':'Yu Gothic', 'font.size':11, 'axes.unicode_minus':False,
        'axes.spines.top':False, 'axes.spines.right':False, 'figure.facecolor':'white'})
    for directory in [OUT, OUT / 'frames', OUT / 'clips']:
        directory.mkdir(parents=True, exist_ok=True)
    record = {'pid':os.getpid(), 'started_utc':datetime.now(UTC).isoformat(), 'owner':'ngt_research_report',
        'purpose':f'Prepare report {args.phase}', 'max_threads':2, 'status':'running'}
    write_json(OUT / f'{args.phase}.process.json', record)
    input_paths = [ROOT / f'results/{e}/position_evaluation/positions_5hz.csv' for e in cfg['position_experiments']]
    input_paths += [ROOT / f'results/{e}/position_evaluation/metrics.json' for e in cfg['position_experiments']]
    input_paths += [MAP / 'frame_evaluation.csv', MAP / 'summary.json', MAP / 'models.json', MAP / 'manifest.json',
        CONFIG_PATH, SYNC_PATH, ROOT / 'calibration/camera_defaults.json',
        ROOT / 'calibration/parent_camera_underwater_20260924_provisional.json', Path(__file__),
        ROOT / 'src/aquabeacon_analysis/ngt_research_review.py', ROOT / 'results/20261028_ngt_000/experiment_plan.txt']
    try:
        tables = {e[-3:]: read_csv(ROOT / f'results/{e}/position_evaluation/positions_5hz.csv') for e in cfg['position_experiments']}
        mapping = read_csv(MAP / 'frame_evaluation.csv')
        if args.phase == 'prepare':
            tables_and_figures(cfg, tables, mapping)
            audit_orientation(tables)
            map_example()
            media(cfg, tables, mapping)
        else:
            clips(cfg, tables, mapping)
        write_json(OUT / 'manifest.json', {'experiment_id':args.experiment, 'created_utc':datetime.now(UTC).isoformat(),
            'git_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
            'inputs_sha256':{str(p.relative_to(ROOT)):digest(p) for p in input_paths},
            'source_policy':'No raw modifications; original frame aspect, source times and validity preserved',
            'calibration_audit':'Parent display/sensor mismatch confirmed; legacy coordinate-dependent conclusions withheld',
            'selection_policy':cfg['selection_policy'], 'config':cfg})
        shutil.copy2(__file__, OUT / 'generate.py')
        record['status'] = 'completed'
    except BaseException as error:
        record.update(status='failed', error=str(error))
        raise
    finally:
        record['ended_utc'] = datetime.now(UTC).isoformat()
        write_json(OUT / f'{args.phase}.process.json', record)


if __name__ == '__main__':
    main()
