"""Fit each DJI clock independently from body rotation; preserve rejected windows."""
import argparse
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import matplotlib

matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

from aquabeacon_analysis.editable_plots import save_editable_figure
from aquabeacon_analysis.experiments import load_experiment
from aquabeacon_analysis.ngt_binary import sha256
from aquabeacon_analysis.ngt_sync import angular_signal, fit_rotation, numeric_csv, sample_with_gaps


def search(ct, cn, bt, bn, window, offsets):
    grid = np.arange(*window, 1 / 30)
    a = sample_with_gaps(ct, cn, grid, .15)
    scores = []
    for offset in offsets:
        b = np.interp(grid + offset, bt, bn, left=np.nan, right=np.nan)
        ix = np.clip(np.searchsorted(bt, grid + offset), 1, len(bt)-1)
        b[(bt[ix]-bt[ix-1]) > .15] = np.nan
        ok = np.isfinite(a) & np.isfinite(b)
        scores.append(float(np.corrcoef(a[ok], b[ok])[0, 1])
                      if ok.mean() >= .8 and np.std(a[ok]) > .01 else np.nan)
    scores = np.asarray(scores)
    if not np.isfinite(scores).any():
        raise ValueError('Insufficient overlap or motion')
    best = np.nanargmax(scores)
    outside = np.abs(offsets-offsets[best]) > 1
    return {'offset_s': float(offsets[best]), 'correlation': float(scores[best]),
                'boundary': bool(best in (0, len(scores)-1)),
                'competitor': float(np.nanmax(scores[outside])) if outside.any() else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment', required=True)
    args = parser.parse_args()
    exp = load_experiment(Path.cwd(), args.experiment)
    folder = exp.stage_path('interim') / 'synchronization'
    output = exp.stage_path('results') / 'synchronization'
    output.mkdir(parents=True, exist_ok=True)
    config_path = Path(exp.metadata['config_paths'][0])
    cfg = json.loads(config_path.read_text())
    manifest = {'experiment_id': args.experiment, 'config': cfg,
                    'git_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'],
                                                       text=True).strip(),
                    'calibration_paths': [], 'cameras': {},
                    'clock_equation': 'utc_s = utc_at_video_zero_s + time_scale * video_time_s',
                    'limitations': ['Timing residuals are internal consistency, not absolute accuracy.',
                                 'Child UTC is logged gps_pps_candidate, not independent GPS truth.',
                                 'Mount rotations are fitted locally; no optical/body calibration.',
                                 'Do not extrapolate outside validated interval without review.']}
    for telemetry in sorted(folder.glob('*_DJI*.csv')):
        role = telemetry.name.split('_')[0]
        meta = json.loads(telemetry.with_suffix('.media.json').read_text())
        epoch = meta['start_utc_us'] / 1e6
        if role == 'parent':
            sources = [folder / 'parent_100hz.csv']
            body = numeric_csv(sources[0], ['sample_utc_us', 'corrected_gx',
                                           'corrected_gy', 'corrected_gz'])
        else:
            sources = sorted((folder / 'child').glob('*.imu.csv'))
            body = np.concatenate([numeric_csv(p, ['utc_us', 'gx', 'gy', 'gz'])
                                   for p in sources])
        body = body[body[:, 0] > 1e15]
        body = body[np.argsort(body[:, 0])]
        bt, bn = body[:, 0]/1e6-epoch, np.linalg.norm(body[:, 1:], axis=1)
        cam = numeric_csv(telemetry, ['video_time_s', 'qw', 'qx', 'qy', 'qz'])
        ct, cg = angular_signal(cam)
        cn = np.linalg.norm(cg, axis=1)
        rows = []
        camera_cfg = cfg.get('camera_overrides', {}).get(telemetry.stem, {})
        window_s = camera_cfg.get('window_s', cfg['window_s'])
        stride_s = camera_cfg.get('window_stride_s', cfg['window_stride_s'])
        for start in np.arange(10, ct[-1]-window_s, stride_s):
            win = [float(start), float(start+window_s)]
            row = {'window_s': win, 'accepted': False}
            try:
                coarse = search(ct, cn, bt, bn, win,
                                np.arange(-cfg['coarse_search_s'], cfg['coarse_search_s']+.01,
                                          cfg['coarse_step_s']))
                fine = search(ct, cn, bt, bn, win,
                              np.arange(coarse['offset_s']-.2, coarse['offset_s']+.201,
                                        cfg['fine_step_s']))
                row.update(fine, coarse_competitor=coarse['competitor'])
                select = (ct >= win[0]) & (ct < win[1])
                rotation, quality = fit_rotation(
                    sample_with_gaps(bt, body[:, 1:], ct[select]+fine['offset_s']), cg[select])
                row.update(rotation_quality=quality, R_camera_imu=rotation.tolist())
                row['accepted'] = bool(fine['correlation'] >= cfg['minimum_correlation']
                    and quality['correlation'] >= cfg['minimum_vector_correlation']
                    and quality['singular_ratio'] >= cfg['minimum_singular_ratio']
                    and not coarse['boundary'] and not fine['boundary'])
            except ValueError as error:
                row['rejected_reason'] = str(error)
            rows.append(row)
            print(telemetry.stem, win, {k: row.get(k) for k in
                                      ['offset_s', 'correlation', 'accepted']}, flush=True)
        accepted = [r for r in rows if r['accepted']]
        centers = np.array([np.mean(r['window_s']) for r in accepted])
        offsets = np.array([r['offset_s'] for r in accepted])
        if len(accepted) < 3:
            raise ValueError(f'{telemetry}: fewer than three accepted windows: {rows}')
        slope, intercept = np.polyfit(centers, offsets, 1)
        residual = offsets - (intercept+slope*centers)
        # Hold out each window in turn to measure clock prediction, not just fit residual.
        holdout = []
        for i in range(len(centers)):
            keep = np.arange(len(centers)) != i
            held_slope, held_intercept = np.polyfit(centers[keep], offsets[keep], 1)
            holdout.append(float(offsets[i] - (held_intercept+held_slope*centers[i])))
        result = {'utc_at_video_zero_s': epoch+intercept, 'time_scale': 1+slope,
                      'utc_at_video_zero_iso': datetime.fromtimestamp(epoch+intercept, UTC).isoformat(),
                      'offset_from_creation_time_s': intercept, 'drift_ppm': slope*1e6,
                      'residual_max_s': float(np.abs(residual).max()),
                      'residual_rms_s': float(np.sqrt(np.mean(residual**2))),
                      'leave_one_window_out_max_s': float(np.max(np.abs(holdout))),
                      'validated_interval_s': [accepted[0]['window_s'][0],
                                            accepted[-1]['window_s'][1]],
                      'source_video': meta, 'windows': rows,
                      'inputs': [{'path': str(p), 'sha256': sha256(p)} for p in [telemetry, *sources]]}
        manifest['cameras'][telemetry.stem] = result
        np.savetxt(output / f'{telemetry.stem}_windows.csv',
                   np.column_stack([centers, offsets, residual]), delimiter=',',
                   header='video_center_s,offset_s,fit_residual_s', comments='')
        synced = exp.stage_path('processed') / 'synchronization'
        synced.mkdir(parents=True, exist_ok=True)
        within = ((cam[:, 0] >= result['validated_interval_s'][0])
                  & (cam[:, 0] <= result['validated_interval_s'][1]))
        np.savetxt(synced / f'{telemetry.stem}_attitude.csv',
                   np.column_stack([cam[:, 0], epoch+intercept+(1+slope)*cam[:, 0],
                                    cam[:, 1:], within]),
                   delimiter=',', header='video_time_s,sync_utc_s,qw,qx,qy,qz,clock_interpolated',
                   comments='')
        fig, axes = plt.subplots(2, 1, figsize=(10, 6))
        axes[0].plot(ct, cn, lw=.6, label='DJI quaternion angular speed')
        axes[0].plot(ct, sample_with_gaps(bt, bn, intercept+(1+slope)*ct),
                     lw=.6, alpha=.7, label='Body gyro after synchronization')
        axes[0].set(ylabel='Angular speed [rad/s]', xlabel='Video time [s]')
        axes[1].plot(centers, offsets, 'o', label='Independent motion windows')
        axes[1].plot(centers, intercept+slope*centers, label=f'Affine fit: {slope*1e6:.2f} ppm')
        axes[1].set(xlabel='Video time [s]', ylabel='Offset from creation time [s]')
        for ax in axes:
            ax.legend()
            ax.grid(alpha=.3)
        fig.tight_layout()
        save_editable_figure(fig, output / f'{telemetry.stem}.png', dpi=150)
        plt.close(fig)
        (output / 'manifest.json').write_text(json.dumps(manifest, indent=2))
    manifest['code_hashes'] = {str(p): sha256(p) for p in
                              [Path(__file__), Path('scripts/prepare_nanyo_sync.py'), config_path]}
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2))


if __name__ == '__main__':
    main()
