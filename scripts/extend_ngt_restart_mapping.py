"""Retry explicitly selected capped windows with a larger restart count, preserving first trials."""
from __future__ import annotations

import argparse
import gc
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from run_ngt_restart_mapping import (
    CONFIG,
    DATABASE,
    EXPERIMENT,
    IMAGES,
    OUT,
    ROOT,
    WORK,
    colmap,
    export_model,
    read_csv,
    write_json,
)

from aquabeacon_analysis.ngt_binary import sha256
from aquabeacon_analysis.restart_mapping import temporal_windows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment', required=True, choices=[EXPERIMENT])
    parser.add_argument('--window', required=True, action='append', type=int)
    args = parser.parse_args()
    out = OUT / 'extended_restarts'
    out.mkdir(exist_ok=True)
    config = {**CONFIG, 'mapper_max_models_per_window': 200}
    write_json(out / 'config.json', config)
    process = {'pid': os.getpid(), 'started_utc': datetime.now(UTC).isoformat(),
        'owner': 'native120_restart_mapping', 'purpose': __doc__, 'threads': CONFIG['num_threads'],
        'windows': sorted(set(args.window)), 'generator_sha256': sha256(Path(__file__))}
    write_json(out / 'map.process.json', process)
    source_paths = [Path(__file__), ROOT / 'scripts/run_ngt_restart_mapping.py',
        ROOT / 'src/aquabeacon_analysis/restart_mapping.py', OUT / 'manifest.json',
        WORK / 'optimizer_frame_priors.csv', out / 'config.json']
    write_json(out / 'manifest.json', {'inputs_sha256': {str(p.relative_to(ROOT)): sha256(p) for p in source_paths},
        'reason': 'First trial reached ten-model cap before exhausting its time allowance; increase restart count only',
        'selection_policy': 'Use this newer trial for selected windows regardless of numerical improvement; retain originals',
        'config': config})
    rows = read_csv(WORK / 'optimizer_frame_priors.csv')
    lookup = {r['image_name']: r for r in rows}
    times = np.array([float(r['parent_video_s']) for r in rows])
    windows = temporal_windows(times, CONFIG['window_width_s'], CONFIG['window_stride_s'])
    try:
        for number in sorted(set(args.window)):
            if number < 0 or number >= len(windows):
                raise ValueError(f'Window out of range: {number}')
            original = json.loads((OUT / f'window_{number:02d}/summary.json').read_text())
            if len(original['models']) < CONFIG['mapper_max_models_per_window']:
                raise ValueError(f'Window {number} did not reach original restart-count cap')
            destination = out / f'window_{number:02d}'
            destination.mkdir(exist_ok=True)
            if (destination / 'summary.json').exists():
                continue
            selected = [rows[i] for i in windows[number]]
            options = colmap.IncrementalPipelineOptions(num_threads=CONFIG['num_threads'], random_seed=7,
                image_names=[r['image_name'] for r in selected], max_num_models=200,
                min_model_size=CONFIG['mapper_min_model_size'], init_num_trials=100,
                ba_refine_focal_length=False, ba_refine_principal_point=False, ba_refine_extra_params=False,
                ba_local_max_num_iterations=15, ba_global_max_num_iterations=25,
                ba_global_max_refinements=2, ba_min_num_residuals_for_cpu_multi_threading=1,
                max_runtime_seconds=CONFIG['mapper_max_seconds_per_window'],
                use_prior_position=True, use_robust_loss_on_prior_position=True)
            options.mapper.num_threads = CONFIG['num_threads']
            options.mapper.init_min_num_inliers = CONFIG['mapper_min_inliers']
            options.mapper.init_min_tri_angle = CONFIG['mapper_init_min_triangle_deg']
            options.mapper.abs_pose_min_num_inliers = 20
            options.mapper.abs_pose_refine_focal_length = False
            options.mapper.abs_pose_refine_extra_params = False
            options.triangulation.min_angle = 1.
            options.mapper.filter_min_tri_angle = 1.
            model_root = WORK / 'extended_restarts' / f'window_{number:02d}'
            model_root.mkdir(parents=True, exist_ok=True)
            start = time.monotonic()
            print(json.dumps({'restarting_window': number, 'model_limit': 200}), flush=True)
            models = colmap.incremental_mapping(DATABASE, IMAGES, model_root, options=options)
            result = {k: v for k, v in original.items() if k not in ['models', 'status', 'elapsed_s', 'time_limit_may_have_truncated']}
            result.update(models=[], status='models_returned' if models else 'no_model',
                variant='extended_restarts', model_limit=200,
                baseline_summary=str((OUT / f'window_{number:02d}/summary.json').relative_to(ROOT)))
            for index, model in models.items():
                result['models'].append({'model': int(index), **export_model(model, model_root / str(index), lookup)})
            result['elapsed_s'] = time.monotonic()-start
            result['time_limit_may_have_truncated'] = result['elapsed_s'] >= CONFIG['mapper_max_seconds_per_window']
            write_json(destination / 'summary.json', result)
            print(json.dumps({'window': number, 'models': len(result['models']),
                'positioned': sum(m['position_registered'] for m in result['models']), 'seconds': result['elapsed_s']}), flush=True)
            del models
            gc.collect()
        process['status'] = 'completed'
    except BaseException as error:
        process.update(status='failed', error=str(error))
        raise
    finally:
        process['ended_utc'] = datetime.now(UTC).isoformat()
        write_json(out / 'map.process.json', process)


if __name__ == '__main__':
    main()
