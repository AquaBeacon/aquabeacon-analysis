"""Restore source timing in already generated lossless day2 clips."""
import argparse
import json
from pathlib import Path

from nanyo_video_copy import preserve_clip_timing

from aquabeacon_analysis.ngt_binary import sha256
from aquabeacon_analysis.time_sync import resolve_ffmpeg


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment', required=True)
    args = parser.parse_args()
    path = Path('results') / args.experiment / 'segmentation/manifest.json'
    manifest = json.loads(path.read_text(encoding='utf-8'))
    ffmpeg = resolve_ffmpeg()
    for segment in manifest['segments']:
        for item in segment['videos']:
            sidecar = Path(item['output']).with_suffix('.clock.json')
            saved = json.loads(sidecar.read_text(encoding='utf-8'))
            if 'edit_list_correction' in saved:
                item.update(saved)
                continue
            print('Restore timing', item['output'], flush=True)
            preserve_clip_timing(item, ffmpeg)
            sidecar.write_text(json.dumps(item, indent=2), encoding='utf-8')
        target = Path('results') / segment['experiment_id'] / 'segmentation/manifest.json'
        target.write_text(json.dumps(segment, indent=2), encoding='utf-8')
    manifest['video_timing_repair_code_sha256'] = sha256(Path(__file__))
    manifest['video_copy_helper_sha256'] = sha256(Path(__file__).with_name('nanyo_video_copy.py'))
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding='utf-8')


if __name__ == '__main__':
    main()
