"""Verify child-only VI/fusion provenance, all-frame accounting and editable plots."""
from __future__ import annotations

import argparse
import csv
import json
import pickle
import shutil
import subprocess
from itertools import pairwise
from pathlib import Path

import matplotlib
import numpy as np

matplotlib.use('Agg')
import matplotlib.pyplot as plt
from prepare_ngt_child_fusion import HEALTH, IMU, OUTPUT
from run_ngt_child_vslam import ROOT, owned, sha, write_json

from aquabeacon_analysis.editable_plots import PlotDocument


def rows(path):
    with path.open(encoding='utf-8') as handle:
        return list(csv.DictReader(handle))


def verify(experiments):
    checks = {'experiments': {}, 'raw_and_decoded': {}, 'static_plot_count': 0}
    for stem, stream, decoded in [('C_MAIN_IMU_100Hz_83304', 'imu', IMU),
                                  ('C_MAIN_HEALTH_RANGE_83304', 'health', HEALTH)]:
        manifest = json.loads((decoded.parent/(stem+'.bin.decode.json')).read_text())
        assert sha(Path(manifest['source'])) == manifest['source_sha256']
        assert sha(decoded) == manifest['streams'][stream]['csv_sha256']
        checks['raw_and_decoded'][stem] = 'hashes match original decode manifest'
    sensor_manifest = json.loads((OUTPUT/'sensor_manifest.json').read_text(encoding='utf-8'))
    for path, digest in sensor_manifest['inputs_sha256'].items():
        if Path(path).suffix in {'.csv', '.json'}:
            assert sha(ROOT/path) == digest, path
    for experiment in experiments:
        result = ROOT/f'results/{experiment}/child_sensor_fusion'
        original = rows(ROOT/f'data/interim/{experiment}/child_only_vslam/okinawa_native120/frames.csv')
        per = {}
        for variant in ['vi30', 'vi30_bracketed', 'camera_vi30_bracketed']:
            run = result/variant
            if not (run/'summary.json').exists():
                continue
            name = 'camera_vi30' if variant.startswith('camera') else 'vi30'
            dataset = ROOT/f'data/interim/{experiment}/child_sensor_fusion/{name}'
            frames = rows(dataset/'frames.csv')
            assert frames == original[::4]
            stamps = (dataset/'timestamps.txt').read_bytes()
            assert b'\r' not in stamps
            assert stamps.decode().splitlines() == [row['synthetic_ns'] for row in frames]
            imu = rows(dataset/'mav0/imu0/data.csv')
            times = [int(row['#timestamp [ns]']) for row in imu]
            assert all(b > a for a, b in pairwise(times))
            assert np.isfinite([[float(row[k]) for k in ['wx', 'wy', 'wz', 'ax', 'ay', 'az']]
                                for row in imu]).all()
            summary = json.loads((run/'summary.json').read_text())
            assert len(rows(run/'frame_evaluation.csv')) == len(frames)
            assert summary['processed_frames']+summary['unprocessed_frames'] == len(frames)
            export = json.loads((dataset/'export_manifest.json').read_text())
            for path, digest in export['inputs_sha256'].items():
                assert sha(ROOT/path) == digest, path
            assert not export['parent_observations_used']
            process = json.loads((run/'run.process.json').read_text())
            assert 'ended_utc' in process
            per[variant] = {key: summary[key] for key in ['exit_code', 'input_frames',
                            'unprocessed_frames', 'imu_initialized_frames', 'surviving_nonempty_maps']}
        for master in result.rglob('*.figure.pickle'):
            base = str(master).removesuffix('.figure.pickle')
            assert all(Path(base+suffix).exists() for suffix in ['.png', '.svg', '.plot.json'])
            # Only our locally generated Matplotlib master files are loaded.
            with master.open('rb') as handle:
                figure = pickle.load(handle)
            assert figure.axes
            plt.close(figure)
            checks['static_plot_count'] += 1
        checks['experiments'][experiment] = per
        write_json(result/'artifacts_manifest.json', {
            str(path.relative_to(result)).replace('\\', '/'): sha(path)
            for path in result.rglob('*') if path.is_file() and path.name != 'artifacts_manifest.json'})
    master = ROOT/'results/20261028_ngt_004/child_sensor_fusion/fusion_comparison.figure.pickle'
    if master.exists():
        target = OUTPUT/'editor_validation'
        target.mkdir(exist_ok=True)
        for suffix in ['.figure.pickle', '.plot.json']:
            source = Path(str(master).removesuffix('.figure.pickle')+suffix)
            shutil.copy2(source, target/source.name)
        original_hash = sha(master)
        document = PlotDocument(target/master.name)
        document.style = {}  # This is a task-owned verification copy, not a user-edited figure.
        original_lines = [(line.get_xdata().copy(), line.get_ydata().copy())
                          for axis in document.figure.axes for line in axis.lines]
        document.update({'figure': {'width': 10., 'height': 8.},
                         'axes': {'0': {'xmin': 0., 'xmax': 250.}}})
        document.save()
        reloaded = PlotDocument(target/master.name)
        assert np.allclose(reloaded.figure.get_size_inches(), [10, 8])
        assert np.allclose(reloaded.figure.axes[0].get_xlim(), [0, 250])
        new_lines = [(line.get_xdata(), line.get_ydata())
                     for axis in reloaded.figure.axes for line in axis.lines]
        for before, after in zip(original_lines, new_lines, strict=True):
            assert all(np.array_equal(a, b, equal_nan=True) for a, b in zip(before, after, strict=True))
        assert sha(master) == original_hash
        checks['editable_plot_roundtrip'] = {'dimensions': True, 'xlim': True,
                                             'png_svg_pdf': True, 'data_unchanged': True}
        plt.close(document.figure)
        plt.close(reloaded.figure)
    javascript = subprocess.run(['node', str(ROOT/'tests/check_child_map_viewer.cjs'),
                                 str(OUTPUT/'maps.html')], capture_output=True, text=True,
                                timeout=30, check=True)
    checks['viewer_javascript_logic'] = json.loads(javascript.stdout)
    checks['actual_browser_render'] = 'not verified: file URL blocked by app browser policy'
    checks['status'] = 'numerical/provenance/editor/JavaScript checks passed; browser render unverified'
    write_json(OUTPUT/'verification.json', checks)
    print(json.dumps(checks), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment', action='append', required=True,
                        choices=[f'20261028_ngt_{i:03}' for i in range(1, 5)])
    parser.add_argument('--tests', action='store_true')
    args = parser.parse_args()
    if args.tests:
        import sys
        for name, command in [('pytest', [sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider']),
                               ('ruff', [sys.executable, '-m', 'ruff', 'check', '.'])]:
            result = owned(command, OUTPUT/(name+'.log'), 300, 'child fusion '+name)
            if result['returncode']:
                raise RuntimeError(name+' failed; see recorded log')
    verify(args.experiment)


if __name__ == '__main__':
    main()
