"""Verify completed child-only VSLAM runs, preserved sources, and editable artifacts."""
from __future__ import annotations

import argparse
import csv
import json
import pickle
from datetime import UTC, datetime

import matplotlib
import numpy as np
from run_ngt_child_vslam import BASE, ROOT, RUN, sha, write_json

matplotlib.use('Agg')

from aquabeacon_analysis.editable_plots import describe_figure


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment', action='append', required=True)
    args = parser.parse_args()
    provenance = json.loads((BASE/'source_provenance.json').read_text(encoding='utf-8'))
    unchanged = {p: sha(ROOT/p) == h for p, h in provenance['inputs_sha256'].items()}
    if not all(unchanged.values()):
        raise ValueError('Source changed: '+str(unchanged))
    checks = {}
    for experiment in args.experiment:
        output = ROOT/f'results/{experiment}/child_only_vslam'
        summary = json.loads((output/'summary.json').read_text(encoding='utf-8'))
        run = output/RUN
        manifest = json.loads((run/'run_manifest.json').read_text(encoding='utf-8'))
        dataset = ROOT/manifest['dataset']
        with (dataset/'frames.csv').open() as handle:
            rows = list(csv.DictReader(handle))
        frames = np.array([int(r['source_frame']) for r in rows])
        stamps = [int(r['synthetic_ns']) for r in rows]
        actual = (dataset/'timestamps.txt').read_bytes()
        if b'\r' in actual or list(map(int, actual.splitlines())) != stamps:
            raise ValueError('Timestamps are not identical LF-delimited input plan')
        if not np.all(np.diff(frames) == 1):
            raise ValueError('Native frames were skipped')
        image_count = len(list((dataset/'mav0/cam0/data').glob('*.jpg')))
        with (output/'frame_evaluation.csv').open() as handle:
            evaluation_rows = sum(1 for _ in csv.DictReader(handle))
        if not image_count == evaluation_rows == len(rows) == summary['input_frames']:
            raise ValueError('Input/evaluation row count mismatch')
        if summary['unprocessed_frames'] or summary['exit_code'] != 0:
            raise ValueError('Incomplete execution diagnostics')
        figures = {}
        for path in output.glob('*.figure.pickle'):
            stem = path.name.removesuffix('.figure.pickle')
            if not all((output/(stem+suffix)).exists() for suffix in ('.png', '.svg', '.plot.json')):
                raise ValueError('Incomplete editable figure package')
            figure = pickle.loads(path.read_bytes())  # local generation only
            description = describe_figure(figure)
            figures[stem] = {'axes': len(description['axes']),
                             'width_in': description['figure']['width'],
                             'height_in': description['figure']['height']}
            del figure
        hashes = {p.relative_to(output).as_posix(): sha(p) for p in output.rglob('*')
                  if p.is_file() and p.name != 'artifacts_manifest.json'}
        write_json(output/'artifacts_manifest.json', {'files_sha256': hashes,
                   'checked_utc': datetime.now(UTC).isoformat()})
        checks[experiment] = {'frames': len(rows), 'all_native_frames': True,
                              'complete_tracking_log': True, 'figures': figures,
                              'raw_untouched': True, 'parent_pool_observations_used': False}
    result = {'checked_utc': datetime.now(UTC).isoformat(), 'sources_unchanged': unchanged,
              'experiments': checks, 'status': 'passed'}
    write_json(BASE/'verification.json', result)
    print(json.dumps(checks, indent=2))


if __name__ == '__main__':
    main()
