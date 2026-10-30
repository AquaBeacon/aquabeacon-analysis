"""Account for every native frame and display separately restarted wall reconstructions."""
from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

os.environ.setdefault('OPENBLAS_NUM_THREADS', '2')
os.environ.setdefault('OMP_NUM_THREADS', '2')
import cv2
import matplotlib
import numpy as np

matplotlib.use('Agg')
import matplotlib.pyplot as plt
from run_ngt_restart_mapping import (
    CONFIG,
    EXPERIMENT,
    IMAGES,
    OUT,
    ROOT,
    SOURCE,
    WORK,
    colmap,
    read_csv,
    write_csv,
    write_json,
)
from visualize_ngt_positions import clean, viewer

from aquabeacon_analysis.editable_plots import save_editable_figure
from aquabeacon_analysis.ngt_binary import sha256
from aquabeacon_analysis.ngt_visualization import contiguous_segments


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment', required=True, choices=[EXPERIMENT])
    parser.parse_args()
    cv2.setNumThreads(2)
    (OUT / 'reviews').mkdir(exist_ok=True)
    rows = read_csv(WORK / 'optimizer_frame_priors.csv')
    baseline_path = ROOT / f'results/{EXPERIMENT}/child_only_vslam/okinawa_native120/TrackedFrameSummary.csv'
    baseline_rows = read_csv(baseline_path)
    assert len(rows) == len(baseline_rows)
    for i, (row, baseline) in enumerate(zip(rows, baseline_rows, strict=True)):
        assert int(baseline['frame_index']) == i
        assert baseline['utc_ns'] + '.jpg' == row['source_image_name']
        row['monocular_tracking_state'] = int(baseline['tracking_state'])
        row['monocular_tracking_ok'] = int(baseline['tracking_state']) == 2
    times = np.array([float(row['parent_video_s']) for row in rows])
    lookup = {r['image_name']: r for r in rows}
    window_paths = sorted(OUT.glob('window_*/summary.json'))
    baseline_windows = [json.loads(path.read_text(encoding='utf-8')) for path in window_paths]
    extended_paths = sorted((OUT / 'extended_restarts').glob('window_*/summary.json'))
    extensions = {v['window']: v for v in [json.loads(p.read_text(encoding='utf-8')) for p in extended_paths]}
    windows = [extensions.get(v['window'], v) for v in baseline_windows]
    all_registered, structural_registered, positioned_registered = set(), set(), set()
    maps, models, inputs = [], [], [WORK / 'optimizer_frame_priors.csv', baseline_path, OUT / 'manifest.json', *window_paths, *extended_paths]
    if extended_paths:
        inputs += [OUT / 'extended_restarts/manifest.json', OUT / 'extended_restarts/config.json']
    palette = plt.get_cmap('tab20')
    from matplotlib.colors import to_hex
    for window in windows:
        for item in window['models']:
            folder = ROOT / item['folder']
            cams = read_csv(folder / 'camera_centers.csv')
            names = {r['image_name'] for r in cams}
            all_registered.update(names)
            if item['structural']:
                structural_registered.update(names)
            if item['position_registered']:
                positioned_registered.update(names)
            models.append({'window': window['window'], **item})
            source = folder / 'position_registered' if item['position_registered'] else folder
            model = colmap.Reconstruction(source)
            points = list(model.points3D.values())
            full_xyz = np.array([p.xyz for p in points]).reshape(-1, 3)
            if len(full_xyz) >= 3 and np.isfinite(full_xyz).all():
                centered = full_xyz - full_xyz.mean(axis=0)
                _, _, directions = np.linalg.svd(centered, full_matrices=False)
                plane_rms = float(np.sqrt(np.mean((centered @ directions[-1])**2)))
                span = float(np.ptp(np.percentile(centered @ directions[0], [5, 95])))
                models[-1]['shape_diagnostic'] = {'plane_rms_model_units': plane_rms,
                    'primary_span_p90_model_units': span,
                    'plane_rms_over_span': plane_rms / span if span > 0 else None,
                    'units': 'conditional m' if item['position_registered'] else 'local arbitrary units',
                    'meaning': 'Internal shape consistency, not surveyed wall error'}
            # Deterministic display subset only. Complete unfiltered models and PLY stay on disk.
            stride = max(1, int(np.ceil(len(points) / 5000)))
            displayed = points[::stride]
            xyz = np.array([p.xyz for p in displayed]).reshape(-1, 3)
            colors = [to_hex(np.asarray(p.color) / 255) for p in displayed]
            cameras = np.array([[float(r[k]) for k in ['x', 'y', 'z']] for r in cams])
            camera_times = np.array([float(r['parent_video_s']) for r in cams])
            wall_period = bool(np.median(camera_times) >= 1415)
            models[-1]['wall_period'] = wall_period
            priors = np.array([[float(lookup[r['image_name']][k]) for k in ['east_m', 'north_m', 'up_m']]
                for r in cams if lookup[r['image_name']]['prior_available'] == 'True']).reshape(-1, 3)
            color = to_hex(palette(window['window'] % 20))
            label = f"区間 {window['window']:02d} / 断片 {item['model']}"
            label += ' / 位置あり' if item['position_registered'] else ' / 局所座標'
            if not item['structural']:
                label += ' / 退化診断'
            if not wall_period:
                label += ' / 壁確認前'
            representative = cams[len(cams)//2]['image_name']
            image_path = IMAGES / representative
            image = cv2.imread(str(image_path))
            representative_camera = next(im for im in model.images.values() if im.name == representative)
            for point in representative_camera.points2D:
                if point.point3D_id in model.points3D and np.isfinite(point.xy).all():
                    cv2.circle(image, tuple(np.round(point.xy).astype(int)), 2, (0, 0, 255), 1)
            review_path = OUT / 'reviews' / f"window_{window['window']:02d}_model_{item['model']}.png"
            cv2.imwrite(str(review_path), image)
            embedded_review = ''
            if item['structural']:
                ok, encoded = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, 75])
                assert ok
                embedded_review = 'data:image/jpeg;base64,' + base64.b64encode(encoded).decode()
            inputs += [folder / 'position_registration.json', folder / 'camera_centers.csv',
                       source / 'points3D.bin', source / 'images.bin', source / 'cameras.bin', image_path]
            caption = (f"{label}\n登録 {len(cams):,} 枚 / 点 {len(points):,} 個\n"
                f"親動画 {camera_times.min():.2f}〜{camera_times.max():.2f} 秒\n"
                f"再投影平均 {item['reprojection_px']:.3f} px\n")
            if item['position_registered']:
                caption += f"入力位置への適合 RMS {item['position_fit']['fit_rms_m']:.3f} m（独立精度ではない）"
            else:
                caption += '共通座標への配置は未成立。独立した任意尺度として表示。'
            maps.append({'label': label, 'mode': 'aquabeacon_prior', 'model': f"{window['window']:02d}_{item['model']}",
                'metric': item['position_registered'], 'valid_structure': item['structural'],
                'window': window['window'], 'wall_period': wall_period, 'points': xyz, 'colors': colors, 'window_color': color,
                'cameras': cameras, 'camera_segments': contiguous_segments(cameras, camera_times, .25),
                'priors': priors if item['position_registered'] else [], 'caption': caption,
                'image': embedded_review,
                'image_caption': '赤点が3D点に対応する特徴。壁確認前の点群には壁以外や動く対象が含まれ得ます。',
                'source_pointcloud': str((source / ('points.ply' if item['position_registered'] else 'points_local.ply')).relative_to(ROOT)),
                'complete_point_count': len(points), 'display_stride': stride})
    combined = {'label': '壁が見える時間帯の地図を共通位置座標に配置', 'mode': 'aquabeacon_prior', 'model': 'combined', 'aggregate': True,
        'metric': True, 'valid_structure': True, 'points': [], 'colors': [], 'cameras': [],
        'camera_segments': [], 'priors': [], 'image': '',
        'caption': '各時間区間で独立に再構成し、対応する推定位置へ位置合わせ。色は時間区間。\n重複領域の点統合・壁面補完はしていません。位置・尺度・向きは入力位置の誤差にも依存します。'}
    for item in maps:
        if not item['metric'] or not item['valid_structure'] or not item['wall_period']:
            continue
        offset = len(combined['cameras'])
        combined['points'].extend(item['points'])
        combined['colors'].extend([item['window_color']] * len(item['points']))
        combined['cameras'].extend(item['cameras'])
        combined['camera_segments'].extend([[i+offset for i in seg] for seg in item['camera_segments']])
        combined['priors'].extend(item['priors'])
    if not combined['points']:
        combined['valid_structure'] = False
    data = {'kind': 'maps', 'experiment': EXPERIMENT, 'maps': [combined, *maps],
        'description': '単眼SLAMと同じ全33,564フレームを入力。主表示は壁が見える時間帯の点群。壁確認前の点群候補も個別に保持。',
        'notes': '親機GNSS＋光学音響の水平位置と圧力深度を使用。位置が欠ける区間を外挿せず、局所地図は独立尺度で保持。共通座標表示は位置対応に基づく条件付き配置です。地図点数には重複する時間窓の点を含みます。'}
    position_path = ROOT / f'results/{EXPERIMENT}/position_evaluation/positions_5hz.csv'
    position_rows = read_csv(position_path)
    position_times = np.array([float(r['parent_video_s']) for r in position_rows])
    parent_route = np.array([[float(r['parent_east_m']), float(r['parent_north_m']), 0.]
        for r in position_rows])
    child_route = np.array([[float(r['child_east_declination0_m']), float(r['child_north_declination0_m']),
        -float(r['pressure_depth_m'])] if r['magnetic_position_available'] == 'True' else [np.nan]*3
        for r in position_rows])
    data['estimated_routes'] = {'parent': parent_route, 'child': child_route,
        'parent_segments': contiguous_segments(parent_route, position_times, .25),
        'child_segments': contiguous_segments(child_route, position_times, .25),
        'coordinate_policy': 'Same horizontal origin as mapping. Parent z=0 display; child z=-pressure depth, matching map priors. Raw GNSS heights unchanged. No interpolation across missing observations.'}
    inputs.append(position_path)
    write_json(OUT / 'map_data.json', clean(data))
    viewer(OUT / 'wall_viewer.html', '長田高校 全フレームによる壁の再構成', data)
    counts = np.zeros(len(rows), int)
    for w in windows:
        counts[w['first_index']:w['last_index']+1] += 1
    assert counts.min() >= 1, 'Every native frame must belong to a processed window'
    for row, count in zip(rows, counts, strict=True):
        row.update(attempted_windows=int(count), registered_any=row['image_name'] in all_registered,
            registered_structural=row['image_name'] in structural_registered,
            position_registered=row['image_name'] in positioned_registered)
    write_csv(OUT / 'frame_evaluation.csv', rows)
    summary = {'input_frames': len(rows), 'processed_feature_frames': sum(int(r['features']) >= 0 for r in rows),
        'prior_frames': sum(r['prior_available'] == 'True' for r in rows),
        'optimizer_prior_anchors': sum(r['optimizer_prior'] == 'True' for r in rows),
        'matching_eligible_frames': sum(int(r['features']) >= CONFIG['pair_min_features'] for r in rows),
        'windows_attempted': len(windows), 'window_trials_including_retries': len(windows)+len(extensions),
        'extended_restart_windows': sorted(extensions),
        'windows_with_structural_models': sum(any(m['structural'] for m in w['models']) for w in windows),
        'windows_with_positioned_models': sum(any(m['position_registered'] for m in w['models']) for w in windows),
        'models': len(models), 'structural_models': sum(m['structural'] for m in models),
        'positioned_models': sum(m['position_registered'] for m in models),
        'wall_period_positioned_models': sum(m['position_registered'] and m['wall_period'] for m in models),
        'wall_period_positioned_points_sum': sum(m['points'] for m in models if m['position_registered'] and m['wall_period']),
        'registered_any_unique': len(all_registered), 'registered_structural_unique': len(structural_registered),
        'position_registered_unique': len(positioned_registered),
        'structural_points_sum_including_window_overlap': sum(m['points'] for m in models if m['structural']),
        'positioned_points_sum_including_window_overlap': sum(m['points'] for m in models if m['position_registered']),
        'display_points': len(combined['points']),
        'window_mapper_seconds': sum(w['elapsed_s'] for w in windows),
        'windows_reaching_time_limit': [w['window'] for w in windows if w['time_limit_may_have_truncated']],
        'windows_reaching_model_count_limit': [w['window'] for w in windows if len(w['models']) >= w.get('model_limit', CONFIG['mapper_max_models_per_window'])],
        'baseline_frames': len(read_csv(SOURCE / 'frames.csv')), 'wall_interval_parent_s': [1415, 1540]}
    wall = [r for r in rows if 1415 <= float(r['parent_video_s']) < 1540]
    summary['wall_interval'] = {'input_frames': len(wall),
        'structural_registered_unique': sum(r['registered_structural'] for r in wall),
        'position_registered_unique': sum(r['position_registered'] for r in wall)}
    summary['registered_native_frame_duration_s'] = len(structural_registered) * 1001 / 120000
    summary['positioned_native_frame_duration_s'] = len(positioned_registered) * 1001 / 120000
    summary['positioned_while_monocular_not_tracking'] = sum(r['position_registered'] and not r['monocular_tracking_ok'] for r in rows)
    summary['monocular_tracking_frames'] = sum(r['monocular_tracking_ok'] for r in rows)
    summary['wall_interval']['structural_fraction'] = summary['wall_interval']['structural_registered_unique'] / len(wall)
    summary['wall_interval']['positioned_fraction'] = summary['wall_interval']['position_registered_unique'] / len(wall)
    summary['wall_interval']['positioned_while_monocular_not_tracking'] = sum(r['position_registered'] and not r['monocular_tracking_ok'] for r in wall)
    summary['optimization_log_diagnostics'] = []
    for log in [OUT / 'map.log', OUT / 'extended_restarts.log']:
        if log.exists():
            contents = log.read_text(encoding='utf-8', errors='replace')
            summary['optimization_log_diagnostics'].append({'file': log.name,
                'linear_solver_failure_lines': contents.count('Linear solver failure'),
                'not_positive_definite_lines': contents.count('not positive definite')})
            inputs.append(log)
    write_json(OUT / 'summary.json', summary)
    write_json(OUT / 'models.json', models)
    # Each full-frame accounting row remains present; the figures do not interpolate outages.
    fig, axes = plt.subplots(5, 1, figsize=(12, 8), sharex=True, layout='constrained')
    for ax, key, label in zip(axes, ['prior_available', 'features', 'monocular_tracking_ok', 'registered_structural', 'position_registered'],
        ['Position\nsupplied', 'SIFT features\nin wall ROI', 'Monocular\ntracking OK', 'Structural\nreconstruction', 'Placed in\nposition frame'], strict=True):
        if key == 'features':
            ax.plot(times, [int(r[key]) for r in rows], lw=.5)
        else:
            good = np.array([r[key] == 'True' if isinstance(r[key], str) else r[key] for r in rows], bool)
            ax.fill_between(times, 0, good.astype(int), step='post', alpha=.8)
            ax.set_ylim(0, 1.1)
        ax.set_ylabel(label, fontsize=10)
        ax.axvline(1415, color='gray', ls='--')
        ax.grid(alpha=.2)
    axes[-1].set_xlabel('Parent video time [s]')
    fig.suptitle('Every native frame accounted for | dashed line: prior wall evaluation start')
    save_editable_figure(fig, OUT / 'frame_coverage.png', dpi=150)
    plt.close(fig)
    if combined['points']:
        fig = plt.figure(figsize=(11, 7), layout='constrained')
        ax = fig.add_subplot(projection='3d')
        xyz = np.array(combined['points'])
        displayed_maps = [m for m in maps if m['metric'] and m['valid_structure'] and m['wall_period']]
        for window in sorted({m['window'] for m in displayed_maps}):
            group = [m for m in displayed_maps if m['window'] == window]
            points = np.vstack([m['points'] for m in group])
            ax.scatter(*points.T, color=group[0]['window_color'], s=.8, alpha=.6, label=f'Window {window:02d}')
        ax.set(xlabel='East scenario [m]', ylabel='North scenario [m]', zlabel='Up [m]',
            title='Independently restarted wall maps | colored by time window')
        ax.set_box_aspect(np.maximum(np.ptp(xyz, axis=0), .1))
        from matplotlib.ticker import MaxNLocator
        ax.yaxis.set_major_locator(MaxNLocator(4))
        ax.zaxis.set_major_locator(MaxNLocator(3))
        ax.legend(fontsize=8, ncol=2)
        save_editable_figure(fig, OUT / 'wall_fragments_3d.png', dpi=160)
        plt.close(fig)
        fig, ax = plt.subplots(figsize=(10, 6), layout='constrained')
        positions = np.array([[float(r[k]) for k in ['east_m', 'north_m', 'up_m']] for r in rows])
        positions[[r['prior_available'] != 'True' for r in rows]] = np.nan
        ax.plot(parent_route[:, 0], parent_route[:, 1], color='#0966b3', lw=1, label='Parent GNSS route')
        ax.plot(positions[:, 0], positions[:, 1], color='#e67518', lw=1, label='Child position supplied')
        for window in sorted({m['window'] for m in displayed_maps}):
            group = [m for m in displayed_maps if m['window'] == window]
            xyz = np.vstack([m['points'] for m in group])
            ax.scatter(xyz[:, 0], xyz[:, 1], s=.8, color=group[0]['window_color'], alpha=.5, label=f'Wall window {window:02d}')
        cams = np.vstack([m['cameras'] for m in displayed_maps])
        ax.scatter(cams[:, 0], cams[:, 1], s=1.5, color='#46515a', label='Reconstructed camera')
        ax.set(xlabel='East scenario [m]', ylabel='North scenario [m]',
               title='Reconstructed points and camera coverage along the estimated route')
        ax.axis('equal')
        ax.grid(alpha=.2)
        ax.legend(fontsize=8, ncol=2)
        save_editable_figure(fig, OUT / 'camera_coverage.png', dpi=150)
        plt.close(fig)
    text = f'''# 長田高校 全フレームを使った位置拘束付き壁マッピング

子機の単眼SLAMと同じ **{len(rows):,}フレーム**（約280秒、119.88 fps）を入力した。
20秒の窓を15秒刻みで独立に開始し、前の地図が失敗しても後の区間を再構成する。
**{summary['windows_with_structural_models']} / {len(windows)}区間で形状を持つ地図、{summary['windows_with_positioned_models']}区間で推定位置に配置できる地図が得られた。**

この全期間集計には壁確認前の点群候補も含む。壁が見える時間帯を主表示とし、登録画像の半数以上が親動画1415秒以降のモデルを選ぶ。
主表示は **{summary['wall_period_positioned_models']}断片、延べ{summary['wall_period_positioned_points_sum']:,}点**。壁確認前の点群を壁の復元成功とは扱わない。

[回転できる地図](wall_viewer.html) / [全フレームの結果](frame_evaluation.csv) / [数値集計](summary.json)

[Blenderの地図と移動経路](wall_map.blend)には、壁全体、親子の推定軌跡を重ねた全体表示、主要断片の拡大シーンを保存する。
経路表示の青は親機、橙は子機。水平原点を地図と統一し、親機高さ0 m、子機はマッピングと同じ圧力深度で表示する。

## フレーム数と再構成範囲

| 項目 | 結果 |
|---|---:|
| 入力と特徴抽出を完了したフレーム | {summary['processed_feature_frames']:,} / {len(rows):,} |
| 有限の入力位置があるフレーム | {summary['prior_frames']:,} |
| 最適化に与えた位置拘束 元の5 Hz間隔で選択 | {summary['optimizer_prior_anchors']:,} |
| 25特徴以上で対応探索に使えるフレーム | {summary['matching_eligible_frames']:,} |
| 形状のある地図に登録した画像 重複除去 | {len(structural_registered):,} |
| 共通位置座標へ配置した画像 重複除去 | {len(positioned_registered):,} |
| 共通位置座標の断片 | {summary['positioned_models']} |
| 点数 重複する窓の点を含む延べ数 | {summary['positioned_points_sum_including_window_overlap']:,} |
| 形状のある地図に登録したフレームの時間合計 | {summary['registered_native_frame_duration_s']:.2f}秒 |
| 単眼SLAMの追跡成功フレーム | {summary['monocular_tracking_frames']:,} |
| 単眼SLAMが追跡状態でなかったが今回共通座標に登録できたフレーム | {summary['positioned_while_monocular_not_tracking']:,} |

![全フレームの再構成成立範囲](frame_coverage.png)

前回の5 Hzは壁が見える125秒だけの625枚だった。今回は全移動280秒を処理した。
同じ壁125秒に限ると、入力{len(wall):,}枚中、形状を持つ地図に登録した画像は{summary['wall_interval']['structural_registered_unique']:,}枚。
同範囲の画像登録率は{100*summary['wall_interval']['structural_fraction']:.2f}%（前回5 Hzは19.84%）。
そのうち共通位置座標へ配置できたのは{summary['wall_interval']['position_registered_unique']:,}枚（{100*summary['wall_interval']['positioned_fraction']:.2f}%）。
このうち{summary['wall_interval']['positioned_while_monocular_not_tracking']:,}枚は、同じ元画像を処理した単眼SLAMでは追跡成功状態ではなかった。
先頭の低コントラスト部分も分母から除いていない。
時間窓を独立に解く設計のため、今回の断片数をそのまま前回の地図分断数と比較しない。
単眼SLAMとの比較は同じ元フレームで照合した。SLAMのオンライン追跡状態と、後処理SfMの画像登録は異なる判定であり、同じ意味の精度指標ではない。

## 位置が分かることを使った再開

各窓の画像特徴からカメラの向きと地図を推定し、利用できるAquaBeacon水平位置と圧力深度を弱い位置拘束に使う。
さらに各地図のカメラ位置を入力位置へ対応させて共通座標に配置する。画像間の連続追跡が切れても、別の窓で初期化を開始する。
元の5 Hz間隔で選んだ位置対応が6枚未満、またはほぼ一直線で回転・尺度が決まらない断片は、局所座標のまま別表示にする。
位置登録はHuber損失（切替1 m）の反復重み付き適合とし、位置飛びの影響を抑える。各対応の重みと全対応の残差を保存し、外れた位置を記録から削除しない。

入力位置はすでに得た推定値であり、誤差のない既知カメラ姿勢ではない。
位置拘束の有効画像数は5 Hzの値を隣接する有効行間だけで補間した枚数で、独立した測位回数ではない。
最適化には元の5 Hz時間セル当たり1枚の位置拘束を選び、画像の高頻度化だけで位置の重みが増えないようにした。
姿勢・対応特徴・視差が不足すれば、位置があってもその区間の壁形状は得られない。
窓の重複部にある点は重複を残し、未再構成部分を面で補っていない。
共通座標への配置と、同じ特徴点を一つに融合した連続メッシュの作成は別段階である。

## 時間区間ごとの結果

| 窓 | 親動画時刻 秒 | 入力枚数 | 位置あり | 地図断片 | 共通座標断片 | 状態 |
|---|---|---:|---:|---:|---:|---|
'''
    for w in windows:
        limitations = []
        if w['time_limit_may_have_truncated']:
            limitations.append('時間上限の可能性')
        if len(w['models']) >= w.get('model_limit', CONFIG['mapper_max_models_per_window']):
            limitations.append('モデル数上限')
        text += (f"| {w['window']:02d} | {w['first_parent_s']:.1f}〜{w['last_parent_s']:.1f} | "
            f"{w['input_frames']} | {w['prior_frames']} | {len(w['models'])} | "
            f"{sum(m['position_registered'] for m in w['models'])} | {w['status']}"
            + (' ' + '・'.join(limitations) if limitations else '') + ' |\n')
    if extensions:
        text += '\n壁の時間帯で10モデル上限に達した窓は、画像・対応・位置を変えず、再開数上限だけ200へ増やして追加試行した。最終集計は追加試行を採用し、元試行も保持する。時間上限は同じ120秒。\n\n| 窓 | 初回モデル数 | 追加試行モデル数 | 初回の位置登録モデル数 | 追加試行の位置登録モデル数 |\n|---|---:|---:|---:|---:|\n'
        for original in baseline_windows:
            if original['window'] in extensions:
                new = extensions[original['window']]
                text += f"| {original['window']} | {len(original['models'])} | {len(new['models'])} | {sum(m['position_registered'] for m in original['models'])} | {sum(m['position_registered'] for m in new['models'])} |\n"
    if combined['points']:
        text += '\n![共通位置座標に配置した壁断片](wall_fragments_3d.png)\n\n![推定した移動経路と再構成範囲](camera_coverage.png)\n'
    text += f"\n最適化の全試行ログにはLinear solver failure警告が計{sum(d['linear_solver_failure_lines'] for d in summary['optimization_log_diagnostics']):,}行ある。反復や再試行を含む行数で、失敗モデル数ではない。保存できた点群についても全ての局所最適化の収束・壁形状の正しさを保証しない。原ログを保持する。\n"
    text += '''
## 入力と比較条件

既存の単眼SLAM用MP4フレームを再利用し、全原画像のSHA256を照合した。元画像・元SLAM結果・前回の壁地図は変更しない。
右側の壁領域だけにSIFT特徴を限定。画像の切出しに合わせて内部行列の主点を平行移動し、焦点距離と歪み係数を保持した。
前回のLRF＋CLAHEとは入力系列と画像処理条件が異なるため、差をフレーム数だけの効果とはしない。
位置拘束の標準偏差は水平1 m、鉛直0.3 m。窓ごとの処理上限と全失敗を残した。
入力・特徴抽出は全フレームを完了しているが、時間・モデル数上限に達した窓の未登録画像を再構成不能と断定しない。

[設定](config.json) / [入力と校正の来歴](manifest.json) / [各地図と位置適合](models.json)

地図の入力位置への適合残差は内部整合性であり、独立した位置精度や壁寸法精度ではない。
50点以上の点群を集計対象にしたが、点数だけでは形状の正しさを保証しない。
壁領域にはロープや浮遊物の特徴も入り得るため、全点が壁面であるとは限らない。
静的図はPNGに加えSVGと共通のブラウザ編集用マスターを保存した。
HTML・図・Blenderの点群は1モデル当たり最大5,000点程度へ表示用に間引く。集計は全点を使い、全点のCOLMAPモデルとPLYを保持する。
HTMLに埋め込む確認画像だけJPEG品質75で圧縮し、元の確認用PNGはreviews/に保持する。解析画像は再圧縮しない。
HTMLの操作ロジックとブラウザ実画面の確認範囲は検証記録へ記す。

[検証・目視確認・プロセス終了の記録](validation.md)

静的グラフは、プロジェクト直下で次のコマンドを実行すると共通のブラウザ編集画面で編集・保存できる。
軸・文字・系列・寸法・表示範囲を調整し、設定を再読込でき、PNG/SVG/PDFへ出力できる。
他の図を編集する場合は同じフォルダの対応する `.figure.pickle` を指定する。

```powershell
python -m aquabeacon_analysis.editable_plots results/20261028_ngt_004/wall_mapping/native120_restart/camera_coverage.figure.pickle
```

方式の参照：[COLMAPの位置priorと既知姿勢からの再構成](https://colmap.github.io/faq.html#reconstruction-with-pose-priors-gps)。
'''
    (OUT / 'report.md').write_text(text, encoding='utf-8')
    inputs += [Path(__file__), ROOT / 'scripts/templates/ngt_scene_viewer.html']
    write_json(OUT / 'report_manifest.json', {'generator': str(Path(__file__).relative_to(ROOT)),
        'inputs_sha256': {str(p.relative_to(ROOT)): sha256(p) for p in inputs},
        'display_sampling': 'Every ceil(points/5000)-th point per model; full PLY retained',
        'metric_policy': 'Position-registered reconstructions are conditional, not independent ground truth'})
    shutil.copy2(__file__, OUT / 'generate_report.py')
    print(json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    record = {'pid': os.getpid(), 'started_utc': datetime.now(UTC).isoformat(),
        'owner': 'native120_restart_mapping', 'purpose': __doc__, 'threads': 2}
    try:
        main()
        record['status'] = 'completed'
    except BaseException as error:
        record.update(status='failed', error=str(error))
        raise
    finally:
        record['ended_utc'] = datetime.now(UTC).isoformat()
        write_json(OUT / 'report.process.json', record)
