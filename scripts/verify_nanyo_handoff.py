"""Check transferred processed data without raw/interim files or old PC paths."""
import argparse
import csv
import json
from pathlib import Path

from aquabeacon_analysis.experiments import load_experiment


def local_path(root, value):
    """Resolve archived processed paths on Windows and POSIX without editing provenance."""
    value = str(value).replace('\\', '/')
    marker = 'data/processed/'
    if marker not in value:
        raise ValueError(f'Not a processed path: {value}')
    target = (root / (marker + value.split(marker, 1)[1])).resolve()
    if not target.is_relative_to((root / 'data/processed').resolve()):
        raise ValueError('Processed path escapes repository')
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment', required=True)
    parser.add_argument('--verify-csv', action='store_true', help='Verify all split CSV rows and bounds')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    exp = load_experiment(root, args.experiment)
    manifest = json.loads((exp.stage_path('results') / 'segmentation/manifest.json').read_text(encoding='utf-8'))
    segments = manifest.get('segments', [manifest])
    ids = {args.experiment, *(s['experiment_id'] for s in segments)}
    inventory = json.loads((root / 'docs/handoff/nanyo_processed_inventory.json').read_text())
    expected = [f for f in inventory['files'] if f['path'].split('/')[2] in ids]
    if not expected:
        raise ValueError('No inventory for this experiment')
    failures = []
    for entry in expected:
        path = local_path(root, entry['path'])
        if not path.is_file():
            failures.append(f'Missing: {entry["path"]}')
        elif path.stat().st_size != entry['bytes']:
            failures.append(f'Size mismatch: {entry["path"]}')
    csv_files = rows_checked = 0
    if args.verify_csv and not failures:
        for segment in segments:
            for item in segment['sensor_files']:
                count = 0
                with local_path(root, item['output']).open(encoding='utf-8', newline='') as handle:
                    for row in csv.DictReader(handle):
                        utc = float(row['sync_utc_s'])
                        if not segment['start_utc_s'] <= utc < segment['end_utc_s']:
                            raise ValueError(f'UTC outside trial: {item["output"]}')
                        if abs(float(row['segment_time_s'])-(utc-segment['start_utc_s'])) > 1e-6:
                            raise ValueError(f'Wrong trial time: {item["output"]}')
                        count += 1
                if count != item['rows']:
                    raise ValueError(f'Row count mismatch: {item["output"]}')
                csv_files += 1
                rows_checked += count
    print(json.dumps({'experiment_id': args.experiment, 'status': 'failed' if failures else 'passed',
                      'processed_files': len(expected), 'csv_files_checked': csv_files,
                      'csv_rows_checked': rows_checked, 'failures': failures,
                      'note': 'Size/presence checks are not cryptographic integrity checks.'}, indent=2))
    if failures:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
