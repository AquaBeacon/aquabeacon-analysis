"""Extract additional configured trial streams with an explicit firmware decoder."""
import argparse
import json
from pathlib import Path

from aquabeacon_analysis.experiments import load_experiment
from aquabeacon_analysis.ngt_binary import export_binary, load_reader, sha256


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment', required=True)
    parser.add_argument('--firmware-root', required=True, type=Path)
    args = parser.parse_args()
    exp = load_experiment(Path.cwd(), args.experiment)
    cfg = json.loads((Path('config') / args.experiment / 'segments.json').read_text())
    reader = load_reader(args.firmware_root, 'binary_log')
    if 16 not in reader.NAMES:
        raise ValueError('A decoder supporting BNO absolute-attitude kind 16 is required')
    folder = exp.stage_path('interim') / 'synchronization'
    for role, stems in [('parent', ['P_MAIN_HEALTH_RANGE']),
                        ('child', ['C_MAIN_BNO_EVENTS', 'C_MAIN_MULTIPATH'])]:
        for suffix in cfg['log_suffixes'][role]:
            for stem in stems:
                source = exp.raw_root / role / f'{stem}_{suffix}.bin'
                marker = folder / role / f'{source.name}.decode.json'
                if not marker.exists():
                    export_binary(source, folder / role, args.firmware_root)
                report = json.loads(marker.read_text())
                assert report['complete'], marker
                assert sha256(source) == report['source_sha256'], source
                for stream in report['streams'].values():
                    assert sha256(Path(stream['csv'])) == stream['csv_sha256']
                print('Verified', source.name, flush=True)


if __name__ == '__main__':
    main()
