"""Trial magnetic/local inertial dead reckoning with pressure and visual updates."""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import matplotlib
import numpy as np
from scipy.spatial.transform import Rotation

matplotlib.use('Agg')
import matplotlib.pyplot as plt
from prepare_ngt_child_fusion import OUTPUT
from run_ngt_child_vslam import ROOT, sha, write_json

from aquabeacon_analysis.child_sensor_fusion import inertial_pressure_filter, pressure_scale
from aquabeacon_analysis.child_vslam import sample_rotations
from aquabeacon_analysis.editable_plots import save_editable_figure
from aquabeacon_analysis.ngt_fusion import pressure_depth
from aquabeacon_analysis.ngt_sync import numeric_csv, sample_with_gaps


def visual_updates(experiment, times, sensors, manifest, health):
    path = ROOT/f'results/{experiment}/child_only_vslam/frame_evaluation.csv'
    visual = numeric_csv(path, ['source_video_s', 'tracking_state', 'x_slam', 'y_slam', 'z_slam',
                                'qx', 'qy', 'qz', 'qw'])
    valid = (visual[:, 1] == 2) & np.isfinite(visual[:, 2:]).all(1)
    q = sample_rotations(sensors['video_s'], sensors['magnetic_xyzw'], visual[:, 0])
    valid &= np.isfinite(q).all(1)
    empty = np.full((len(times), 3), np.nan)
    if valid.sum() < 30:
        return empty, {'accepted': False, 'reason': 'no sufficiently tracked overlapping map'}, None
    body_optical = (sensors['body_from_sensor']
                    @ np.array(manifest['rotations']['bno']['R_optical_from_sensor']).T)
    alignment = (Rotation.from_quat(q[valid])*Rotation.from_matrix(body_optical)
                 * Rotation.from_quat(visual[valid, 5:]).inv())
    mean = alignment.mean()
    dispersion = np.degrees((mean.inv()*alignment).magnitude())
    position = np.full((len(visual), 3), np.nan)
    position[valid] = mean.apply(visual[valid, 2:5])
    at_depth = sample_with_gaps(visual[:, 0], position[:, 2], health[:, 0], .1)
    candidate = pressure_scale(at_depth, health[:, 1], health[:, 0])
    candidate['orientation_alignment_median_residual_deg'] = float(np.median(dispersion))
    candidate['R_magnetic_ned_from_slam'] = mean.as_matrix().tolist()
    if not candidate['accepted']:
        return empty, candidate, None
    scaled = position*candidate['scale_m_per_slam_unit']
    # A 0.5 s visual velocity only when every source frame in that window is tracked.
    velocity = empty.copy()
    lag = 60
    invalid_prefix = np.r_[0, np.cumsum(~valid)]
    for i in range(1, len(times), 10):
        stop = np.searchsorted(visual[:, 0], times[i])
        start = stop-lag
        if start < 0 or stop >= len(visual) or invalid_prefix[stop+1] != invalid_prefix[start]:
            continue
        velocity[i] = (scaled[stop]-scaled[start])/(visual[stop, 0]-visual[start, 0])
    candidate['velocity_updates'] = int(np.isfinite(velocity).all(1).sum())
    return velocity, candidate, np.column_stack([visual[:, 0], scaled])


def run(experiment):
    output = ROOT/f'results/{experiment}/child_sensor_fusion'
    output.mkdir(parents=True, exist_ok=True)
    source = ROOT/f'data/interim/{experiment}/child_only_vslam/okinawa_native120/export_manifest.json'
    interval = json.loads(source.read_text())['source_video_interval_s']
    sensors = np.load(OUTPUT/'sensors.npz')
    manifest = json.loads((OUTPUT/'sensor_manifest.json').read_text(encoding='utf-8'))
    if experiment.endswith('001'):
        result = {'experiment_id': experiment, 'available': False,
                  'reason': 'No overlapping body IMU or pressure; camera IMU VI trial is separate',
                  'parent_observations_used': False}
        write_json(output/'fusion_summary.json', result)
        return result
    times = np.arange(interval[0], interval[1], .05)
    health = sensors['health'].copy()
    health = health[(health[:, 0] >= interval[0]) & (health[:, 0] < interval[1])]
    baseline = float(health[0, 1])
    health[:, 1] = pressure_depth(health[:, 1], baseline)
    observations = np.full(len(times), np.nan)
    indices = np.searchsorted(times, health[:, 0])
    inside = indices < len(times)
    observations[indices[inside]] = health[inside, 1]
    body_acc = ((sensors['acceleration']-sensors['accelerometer_bias'])
                @ sensors['body_from_sensor'].T)
    sampled = sample_with_gaps(sensors['video_s'], body_acc, times)
    outputs = {}
    acceleration = {}
    for mode in ['magnetic', 'local']:
        q = sample_rotations(sensors['video_s'], sensors[mode+'_xyzw'], times)
        valid = np.isfinite(q).all(1) & np.isfinite(sampled).all(1)
        acceleration[mode] = np.full((len(times), 3), np.nan)
        acceleration[mode][valid] = (Rotation.from_quat(q[valid]).apply(sampled[valid])
                                     + [0, 0, 9.80665])
        outputs[mode] = inertial_pressure_filter(times, acceleration[mode], observations)
    visual_velocity, scale, scaled_visual = visual_updates(experiment, times, sensors,
                                                           manifest, health)
    fused = inertial_pressure_filter(times, acceleration['magnetic'], observations,
                                     visual_velocity=visual_velocity)
    fields = ['source_video_s', 'trial_time_s', 'relative_pressure_down_m',
              'magnetic_segment', 'local_segment', 'visual_velocity_update']
    fields += [f'{mode}_{axis}_m' for mode in ['dr_magnetic', 'pressure_magnetic', 'dr_local',
                                             'pressure_local', 'visual_pressure_magnetic']
               for axis in ['north_or_local_x', 'east_or_local_y', 'down']]
    columns = np.column_stack([times, times-times[0], observations, outputs['magnetic'][3],
                               outputs['local'][3], fused[4], outputs['magnetic'][0],
                               outputs['magnetic'][1], outputs['local'][0], outputs['local'][1],
                               fused[1]])
    with (output/'dead_reckoning.csv').open('w', newline='', encoding='utf-8') as handle:
        writer = csv.writer(handle)
        writer.writerow(fields)
        writer.writerows([['' if not np.isfinite(v) else v for v in row] for row in columns])
    np.savetxt(output/'pressure.csv', health, delimiter=',',
               header='source_video_s,relative_depth_m,temperature_c', comments='')
    if scaled_visual is not None:
        np.savetxt(output/'depth_scaled_visual.csv', scaled_visual, delimiter=',',
                   header='source_video_s,north_m,east_m,down_m', comments='')
    statistics = {}
    for mode in ['magnetic', 'local']:
        raw, position, _, segments, _ = outputs[mode]
        valid = segments >= 0
        ids, counts = np.unique(segments[valid], return_counts=True)
        longest = ids[np.argmax(counts)]
        last = np.flatnonzero(segments == longest)[-1]
        pressure_rows = np.isfinite(observations) & valid
        statistics[mode] = {
            'segments': len(ids), 'available_fraction': float(valid.mean()),
            'longest_segment_s': float(counts.max()*.05),
            'dr_longest_segment_endpoint_norm_m': float(np.linalg.norm(raw[last])),
            'pressure_fused_longest_segment_endpoint_xy_norm_m': float(np.linalg.norm(position[last, :2])),
            'dr_pressure_disagreement_rmse_m': float(np.sqrt(np.mean(
                (raw[pressure_rows, 2]-observations[pressure_rows])**2))),
            'fused_pressure_residual_rmse_m': float(np.sqrt(np.mean(
                (position[pressure_rows, 2]-observations[pressure_rows])**2)))}
    result = {'experiment_id': experiment, 'available': True, 'parent_observations_used': False,
              'pressure_samples': len(health), 'pressure_baseline_mbar': baseline,
              'pressure_span_m': float(np.ptp(health[:, 1])), 'grid_hz': 20,
              'attitude_comparison': statistics, 'visual_depth_scale': scale,
              'visual_velocity_updates_applied': int(fused[4].sum()),
              'initial_velocity_m_s': [0, 0, 0], 'zero_velocity_updates': False,
              'filter_model': {'depth_sigma_m': .03, 'acceleration_sigma_m_s2': .15,
                               'bias_walk_m_s2_sqrt_s': .002, 'visual_velocity_sigma_m_s': .15},
              'caveats': ['relative pressure, not surveyed absolute depth',
                          'pressure residual is in-sample consistency, not accuracy',
                          'horizontal velocity/position is unobserved unless provisional visual scale accepted',
                          'magnetic heading unvalidated; no parent observations or position ground truth',
                          'reset at missing inertial data; separate segments have separate zero origins'],
              'inputs_sha256': {str(p.relative_to(ROOT)): sha(p) for p in
                               [source, OUTPUT/'sensor_manifest.json', OUTPUT/'sensors.npz',
                                Path(__file__), ROOT/'src/aquabeacon_analysis/child_sensor_fusion.py']}}
    write_json(output/'fusion_summary.json', result)
    fig, axes = plt.subplots(3, 1, figsize=(11, 9), sharex=True)
    for mode, color in [('magnetic', '#c34f43'), ('local', '#3b7aab')]:
        raw, position, _, segment, _ = outputs[mode]
        # Break plotted lines at every separately initialized segment.
        raw = raw.copy()
        position = position.copy()
        changes = np.r_[False, np.diff(segment) != 0]
        raw[changes], position[changes] = np.nan, np.nan
        axes[0].plot(times-times[0], raw[:, 2], color=color, lw=.8, label=mode+' IMU only')
        axes[1].plot(times-times[0], position[:, 2], color=color, lw=.8,
                     label=mode+' IMU + pressure')
        axes[2].plot(times-times[0], np.linalg.norm(position[:, :2], axis=1), color=color,
                     lw=.8, label=mode+' horizontal drift (not ground truth)')
    for ax in axes[:2]:
        ax.plot(health[:, 0]-times[0], health[:, 1], '.-', color='black', markersize=2,
                lw=.6, label='Relative pressure depth')
        ax.set_ylabel('Down displacement (m)')
    if scale['accepted']:
        axes[2].plot(times-times[0], np.linalg.norm(fused[1][:, :2], axis=1), color='#2a9565',
                     lw=.8, label='Provisional visual + pressure + IMU')
    axes[2].set(xlabel='Trial time (s)', ylabel='Horizontal displacement (m)')
    for ax in axes:
        ax.grid(alpha=.25)
        ax.legend(fontsize=8)
    axes[0].set_title(experiment+' / child-only DR and pressure fusion; no position GT')
    fig.tight_layout()
    save_editable_figure(fig, output/'fusion_comparison.png', dpi=150)
    plt.close(fig)
    print(json.dumps(result), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment', action='append', required=True,
                        choices=[f'20261028_ngt_{i:03}' for i in range(1, 5)])
    args = parser.parse_args()
    results = [run(experiment) for experiment in args.experiment]
    write_json(OUTPUT/'fusion_comparison.json', results)


if __name__ == '__main__':
    main()
