"""Verify interval bounds, copied video frames and source-clock continuity."""
import argparse
import csv
import json
import subprocess
from pathlib import Path

from aquabeacon_analysis.ngt_binary import sha256
from aquabeacon_analysis.time_sync import resolve_ffmpeg


def hashes(ffmpeg, path, start, duration=.08):
    result = subprocess.run([str(ffmpeg), '-v', 'error', '-ss', f'{max(0, start):.9f}',
                             '-i', str(path), '-t', str(duration), '-map', '0:v:0',
                             '-an', '-f', 'framemd5', '-'], capture_output=True, check=True,
                            text=True, encoding='utf-8', errors='replace')
    return [line.split(',')[-1].strip() for line in result.stdout.splitlines()
            if line and not line.startswith('#')]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment', required=True)
    args = parser.parse_args()
    folder = Path('results') / args.experiment / 'segmentation'
    manifest = json.loads((folder / 'manifest.json').read_text())
    assert len(manifest['segments']) == 2
    assert all(len(v) == 2 for v in manifest['threshold_sensitivity'].values())
    ffmpeg = resolve_ffmpeg()
    checks = []
    full = Path('data/processed') / args.experiment / 'videos/parent_joined.mp4'
    for i, mapping in enumerate(manifest['parent_join_mapping']):
        for source_s in ([1., 100.] if i == 1 else [1., 100., 2298.]):
            expected = hashes(ffmpeg, mapping['source'], source_s, .12)
            actual = hashes(ffmpeg, full, mapping['joined_start_s']+source_s, .12)
            assert set(expected) & set(actual), ('Joined frame mismatch', i, source_s)
            checks.append({'kind': 'joined_frame_match', 'source_index': i,
                           'source_time_s': source_s, 'matching_decoded_frames':
                           len(set(expected) & set(actual))})
    for segment in manifest['segments']:
        for item in segment['sensor_files']:
            count = 0
            with Path(item['output']).open(encoding='utf-8', newline='') as f:
                for row in csv.DictReader(f):
                    utc = float(row['sync_utc_s'])
                    assert segment['start_utc_s'] <= utc < segment['end_utc_s']
                    assert abs(float(row['segment_time_s']) -
                               (utc-segment['start_utc_s'])) < 1e-6
                    count += 1
            assert count == item['rows']
        for item in segment['videos']:
            for clip_s in [1., (item['source_end_s']-item['source_start_s'])/2]:
                expected = hashes(ffmpeg, item['source'], item['source_start_s']+clip_s, .15)
                actual = hashes(ffmpeg, item['output'], clip_s, .15)
                assert set(expected) & set(actual), ('Clip frame mismatch', item, clip_s)
                checks.append({'kind': 'clip_frame_match', 'path': item['output'],
                               'clip_time_s': clip_s, 'matching_decoded_frames':
                               len(set(expected) & set(actual))})
            screenshot = folder / f"{segment['experiment_id']}_{item['role']}_review.png"
            subprocess.run([str(ffmpeg), '-v', 'error', '-y', '-ss', '30', '-i', item['output'],
                            '-frames:v', '1', '-vf', 'scale=640:-1', str(screenshot)], check=True)
    result = {'status': 'passed', 'intervals': 2, 'decoded_frame_checks': checks,
              'all_csv_bounds_and_row_counts': 'passed',
              'manifest_sha256': sha256(folder / 'manifest.json'),
              'verification_script_sha256': sha256(Path(__file__))}
    (folder / 'verification.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    report = folder / 'report.md'
    note = ('\n\n## 自動検証\n\n'
            f'区間数2、全切出しCSVのUTC境界・区間秒・行数を確認。'
            f'連結前後・切出し先頭付近・中間の計{len(checks)}箇所で、'
            '復号フレームのMD5が元動画と一致。'
            '詳細は [verification.json](verification.json)。\n')
    report.write_text(report.read_text(encoding='utf-8').split('\n\n## 自動検証')[0]+note,
                      encoding='utf-8')
    print(json.dumps({'status': 'passed', 'decoded_frame_checks': len(checks)}))


if __name__ == '__main__':
    main()
