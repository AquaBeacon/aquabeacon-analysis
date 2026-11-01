"""Verify all trial CSV bounds and decoded frames across each source video piece."""
import argparse
import csv
import json
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw
from verify_nanyo_segments import hashes

from aquabeacon_analysis.ngt_binary import sha256
from aquabeacon_analysis.time_sync import probe_media, resolve_ffmpeg


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment', required=True)
    args = parser.parse_args()
    folder = Path('results') / args.experiment / 'segmentation'
    path = folder / 'manifest.json'
    manifest = json.loads(path.read_text(encoding='utf-8'))
    checks, csv_count, digest_cache = [], 0, {}
    ffmpeg = resolve_ffmpeg()
    thumbnails = []
    for segment in manifest['segments']:
        assert segment['start_utc_s'] == max(segment['immersion_start_utc_s'],
                                            segment['parent_ranging_start_utc_s'])
        for item in segment['sensor_files']:
            source = item['source']
            if source not in digest_cache:
                digest_cache[source] = sha256(Path(source))
            assert digest_cache[source] == item['source_sha256']
            count = 0
            with Path(item['output']).open(encoding='utf-8', newline='') as f:
                for row in csv.DictReader(f):
                    utc = float(row['sync_utc_s'])
                    assert segment['start_utc_s'] <= utc < segment['end_utc_s']
                    assert abs(float(row['segment_time_s'])-(utc-segment['start_utc_s'])) < 1e-6
                    count += 1
            assert count == item['rows']
            csv_count += 1
        for item in segment['videos']:
            assert item['pieces'], item
            expected_duration = item['pieces'][-1]['joined_end_s']
            actual_duration = probe_media(Path(item['output']), ffmpeg).duration_s
            assert abs(actual_duration-expected_duration) < 3
            for piece in item['pieces']:
                duration = piece['source_end_s']-piece['source_start_s']
                for offset in (min(1., duration/4), duration/2):
                    source_hashes = hashes(ffmpeg, piece['source'], piece['source_start_s']+offset, .2)
                    result_hashes = hashes(ffmpeg, item['output'], piece['joined_start_s']+offset, .2)
                    matches = len(set(source_hashes) & set(result_hashes))
                    assert matches > 0, ('frame mismatch', piece, offset)
                    checks.append({'video': item['output'], 'camera_key': piece['camera_key'],
                                   'joined_time_s': piece['joined_start_s']+offset,
                                   'matching_frames': matches})
            screenshot = folder / f"{segment['experiment_id']}_{item['role']}_review.png"
            subprocess.run([str(ffmpeg), '-v', 'error', '-y', '-ss', str(expected_duration/2),
                            '-i', item['output'], '-frames:v', '1', '-vf', 'scale=480:-1',
                            str(screenshot)], check=True)
            thumbnails.append((screenshot, f"{segment['experiment_id']} {item['role']}"))
    sheet = Image.new('RGB', (960, 310*((len(thumbnails)+1)//2)), 'white')
    draw = ImageDraw.Draw(sheet)
    for i, (path, label) in enumerate(thumbnails):
        im = Image.open(path).convert('RGB')
        im.thumbnail((480, 280))
        x, y = (i % 2)*480, (i//2)*310
        sheet.paste(im, (x, y+25))
        draw.text((x+5, y+5), label, fill='black')
    sheet.save(folder / 'contact_sheet.png')
    result = {'status': 'passed', 'segments': len(manifest['segments']), 'csv_files': csv_count,
              'decoded_frame_checks': checks, 'source_hashes': 'passed',
              'csv_bounds_and_counts': 'passed', 'manifest_sha256': sha256(folder / 'manifest.json'),
              'verification_script_sha256': sha256(Path(__file__))}
    (folder / 'verification.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps({'status': 'passed', 'segments': result['segments'],
                      'csv_files': csv_count, 'frame_checks': len(checks)}))


if __name__ == '__main__':
    main()
