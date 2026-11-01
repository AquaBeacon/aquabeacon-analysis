"""Read-only extraction for an explicitly registered ocean acquisition."""
import argparse
import json
from dataclasses import asdict
from pathlib import Path

from aquabeacon_analysis.experiments import load_experiment
from aquabeacon_analysis.ngt_binary import export_binary, export_spresense_bin_means
from aquabeacon_analysis.ngt_sync import extract_telemetry
from aquabeacon_analysis.time_sync import probe_media, resolve_ffmpeg


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment', required=True)
    parser.add_argument('--firmware-root', type=Path, required=True)
    args = parser.parse_args()
    exp = load_experiment(Path.cwd(), args.experiment)
    out = exp.stage_path('interim') / 'synchronization'
    out.mkdir(parents=True, exist_ok=True)
    for role, pattern in [('child', 'C_MAIN_IMU*.bin'),
                          ('child', 'C_MAIN_HEALTH*.bin'),
                          ('parent', 'P_MAIN_TIME_POSITION*.bin')]:
        for source in sorted((exp.raw_root / role).glob(pattern)):
            dest = out / role
            if not (dest / f'{source.name}.decode.json').exists():
                print(source.name, flush=True)
                export_binary(source, dest, args.firmware_root)
    target = out / 'parent_100hz.csv'
    if not target.exists():
        print('Spresense PPS UTC bins', flush=True)
        export_spresense_bin_means(sorted((exp.raw_root / 'spresence').glob('I*.BIN')),
                                   target, args.firmware_root)
    for role in ('parent', 'child'):
        for video in sorted((exp.raw_root / role / 'DJI').glob('*.MP4')):
            target = out / f'{role}_{video.stem}.csv'
            if not target.exists():
                print(video.name, flush=True)
                extract_telemetry(video.with_suffix('.LRF'), target)
            metadata = asdict(probe_media(video, resolve_ffmpeg()))
            metadata['path'] = str(metadata['path'])
            target.with_suffix('.media.json').write_text(json.dumps(metadata, indent=2))


if __name__ == '__main__':
    main()
