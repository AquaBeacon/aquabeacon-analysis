"""Bounded actual-CAD presentation renderer and preview encoder (NGT004)."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import imageio_ffmpeg
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT/'results/20261028_ngt_004/presentation_positions'


def hashed(path):
    return {'path': str(path.relative_to(ROOT)), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def wait_owned(process, timeout, record, record_path):
    """Wait on the exact Popen child handle; stop only this owned child on failure."""
    try:
        code = process.wait(timeout=timeout)
        if code:
            raise RuntimeError(f'Owned process {process.pid} failed ({code})')
        return code
    except BaseException as error:
        record['failure'] = str(error)
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                # Popen retains the created process handle, protecting against PID reuse.
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=15)
        raise
    finally:
        record.update(ended_utc=datetime.now(UTC).isoformat(),
                      exited=process.poll() is not None, returncode=process.poll())
        record_path.write_text(json.dumps(record, indent=2), encoding='utf-8')


def render_blender():
    OUT.mkdir(parents=True, exist_ok=True)
    blender = Path('C:/Program Files/Blender Foundation/Blender 5.2/blender.exe')
    args = [str(blender), '-b', '-t', '2', '--python-exit-code', '1', '--python',
            str(ROOT/'blender/create_ngt_cad_positions.py')]
    with (OUT/'render.log').open('w', encoding='utf-8') as log:
        process = subprocess.Popen(args, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
        record = {'pid': process.pid, 'started_utc': datetime.now(UTC).isoformat(),
                  'purpose': 'task-owned Blender frozen CAD metric position film', 'owner_pid': os.getpid()}
        print(json.dumps(record), flush=True)
        (OUT/'process.json').write_text(json.dumps(record, indent=2), encoding='utf-8')
        wait_owned(process, 1500, record, OUT/'process.json')
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment', required=True, choices=['20261028_ngt_004'])
    parser.add_argument('--postprocess-only', action='store_true',
                        help='Reuse an already verified saved blend and frame sequence')
    args = parser.parse_args()
    if args.postprocess_only:
        record = json.loads((OUT/'process.json').read_text(encoding='utf-8'))
        if not record.get('exited') or not (OUT/'blender_verification.json').exists():
            raise ValueError('Complete the bounded Blender run before reusing frames')
    else:
        record = render_blender()
    font = ImageFont.truetype('C:/Windows/Fonts/YuGothM.ttc', 21)
    with (OUT/'preview_samples.csv').open(encoding='utf-8', newline='') as stream:
        rows = list(csv.DictReader(stream))
    for row in rows:
        path = OUT/'frames'/f'{int(row["preview_frame"]):04d}.png'
        with Image.open(path) as image:
            image = image.convert('RGB')
            draw = ImageDraw.Draw(image)
            draw.rectangle((0, 0, image.width, 92), fill='white')
            draw.text((18, 7), f'親子の推定位置 × 実機CAD　t={float(row["parent_video_s"]):.1f} s　（全区間を約8秒に短縮）', font=font, fill='#173c50')
            draw.text((18, 35), '機体の向きは未計測の中立表示。設計原点を推定位置へ配置（測定中心は未校正）。', font=font, fill='#475968')
            draw.text((18, 63), 'CAD上の子カメラ・ライト配置は実機と逆。既知の欠測では機体全体を非表示。', font=font, fill='#7b4d27')
            image.save(path)
    source_path = ROOT/'results/20261028_ngt_004/position_visualization/scene_data.json'
    source = json.loads(source_path.read_text(encoding='utf-8'))
    full_rows = []
    for index, time in enumerate(source['times']):
        entry = {'source_index': index, 'blend_frame': index+1, 'parent_video_s': time}
        for body in ['parent', 'child']:
            point = source[body][index]
            valid = point is not None and all(value is not None for value in point)
            entry[body+'_valid'] = valid
            for axis, name in enumerate(['east_m', 'north_m', 'surface_up_m']):
                entry[body+'_'+name] = point[axis] if valid else ''
        full_rows.append(entry)
    with (OUT/'full_samples.csv').open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(full_rows[0]))
        writer.writeheader()
        writer.writerows(full_rows)
    for name in ['cad_positions_1080.png', 'cad_positions_overview_1080.png']:
        with Image.open(OUT/name) as image:
            image = image.convert('RGB')
            draw = ImageDraw.Draw(image)
            draw.rectangle((0, 0, image.width, 92), fill='white')
            draw.text((18, 7), '親子の推定位置 × 固定cad_20261028実寸形状', font=font, fill='#173c50')
            draw.text((18, 35), '向きは未計測の中立表示。CAD設計原点と測定中心の対応は未校正。', font=font, fill='#475968')
            draw.text((18, 63), '子カメラ・ライトは実機と逆の名目配置。赤=東・緑=北・青=上の軸線は各1 m。', font=font, fill='#7b4d27')
            image.save(OUT/name)
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    encoder = subprocess.Popen([ffmpeg, '-y', '-threads', '2', '-framerate', '12', '-i',
                    str(OUT/'frames/%04d.png'), '-c:v', 'libx264', '-threads', '2',
                    '-pix_fmt', 'yuv420p', '-crf', '20', '-movflags', '+faststart',
                    str(OUT/'cad_positions_720p.mp4')],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    encoder_record = {'pid': encoder.pid, 'started_utc': datetime.now(UTC).isoformat(),
                      'purpose': 'task-owned bounded MP4 encoder', 'owner_pid': os.getpid()}
    wait_owned(encoder, 120, encoder_record, OUT/'encoder_process.json')
    inputs = [ROOT/'results/20261028_ngt_004/position_visualization/scene_data.json',
              ROOT/'calibration/child_mount_20261028_actual.json', Path(__file__),
              ROOT/'blender/create_ngt_cad_positions.py', ROOT/'blender/cad_assets.py',
              ROOT/'blender/create_ngt_visualizations.py']
    inputs.extend((ROOT/'data/interim/cad/cad_20261028').glob('*'))
    manifest = {'experiment_id': '20261028_ngt_004', 'created_utc': datetime.now(UTC).isoformat(),
                'blend_reused_from_verified_render': args.postprocess_only,
                'provenance_note': 'Wrapper CLI and failure cleanup updated after original Blender render; scene geometry script and saved .blend not regenerated. Final wrapper hash records postprocessing code.',
                'git_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                'inputs': [hashed(p) for p in inputs if p.is_file()],
                'units': 'metres; x East y North z surface-height display from immutable source',
                'positions': 'Original 5Hz source, exact constant sample hold; no alignment/rescale/filter',
                'orientation': 'Neutral identity rotation, explicitly unmeasured',
                'cad': 'Frozen cad_20261028 GLB/rig hash checked. Live mechanical source drift not regenerated.',
                'center_warning': 'Design origins anchored to estimated antenna/optical positions; measured centres/extrinsics uncalibrated',
                'mount_warning': 'Actual child camera/light swapped; transforms null, nominal CAD retained',
                'preview': {'frames': 96, 'fps': 12, 'duration_s': 8, 'resolution': [1280, 720]},
                'process': record, 'encoder_process': encoder_record}
    (OUT/'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    (OUT/'report.md').write_text('# 実機CADと親子の推定位置\n\n'
        '[編集可能なBlender](cad_positions.blend)・[8秒動画](cad_positions_720p.mp4)・[1080画像](cad_positions_1080.png)・[全体像](cad_positions_overview_1080.png)\n\n'
        '元scene_data.jsonの5Hz位置をそのまま保持。東・北・水面基準高さをmで配置し、欠測をまたぐ線を描かない。'
        '親子は固定CADの実寸形状。向きは未計測の中立姿勢であり軌跡から推定しない。CAD設計原点を推定アンテナ・光学位置へ置く表示で、測定中心や取付けは未校正。'
        '実機の子カメラ・ライトはCADと位置が逆だが変換未確定のため名目形状を保持。現行機械設計に変更があっても固定スナップショットを再生成しない。\n\n'
        'blendは全1400サンプル・5fpsの原タイムライン、動画は全区間から96点を選んだ12fpsの8秒プレビュー。'
        'プレビュー時刻・元サンプル番号・座標・有効性は[プレビューCSV](preview_samples.csv)、[全サンプルCSV](full_samples.csv)に保持。欠測時は各子部品のvisibility driverで機体全体を隠す。\n\n'
        '[検証](blender_verification.json)・[入力ハッシュと条件](manifest.json)。正式な評価計算はPython、Blenderは表示専用。\n', encoding='utf-8')
    print('Completed CAD positions blend, stills and 8 second MP4; Blender exited.', flush=True)


if __name__ == '__main__':
    main()
