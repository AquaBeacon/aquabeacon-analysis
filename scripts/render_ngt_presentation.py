"""Run the bounded Blender presentation render and encode its PNG timeline."""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run_owned(command, *, purpose, log, timeout_s):
    """Track and reap the exact subprocess created here, including on timeout."""
    with log.open('w', encoding='utf8') as stream:
        started = datetime.now(UTC).isoformat()
        process = subprocess.Popen(command, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT)
        ownership = {'pid': process.pid, 'started_utc': started, 'purpose': purpose,
                     'ownership': 'render_ngt_presentation.py', 'command': command}
        try:
            code = process.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=15)
            raise
        finally:
            ownership['exited'] = process.poll() is not None
            ownership['returncode'] = process.poll()
            log.with_suffix('.process.json').write_text(json.dumps(ownership, indent=2),
                                                       encoding='utf8')
        if code:
            raise RuntimeError(f'{purpose} failed: see {log}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment', required=True)
    parser.add_argument('--frames', type=int, default=100)
    args = parser.parse_args()
    output = ROOT/f'results/{args.experiment}/presentation_animation'
    output.mkdir(parents=True, exist_ok=True)
    blender = 'C:/Program Files/Blender Foundation/Blender 5.2/blender.exe'
    run_owned([blender, '-b', '-t', '2', '--python-exit-code', '1', '--python',
               str(ROOT/'blender/create_ngt_presentation_scene.py'), '--', '--experiment',
               args.experiment, '--frames', str(args.frames), '--render'],
              purpose='Editable map/CAD presentation and bounded PNG frames',
              log=output/'blender.log', timeout_s=900)
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        raise RuntimeError('ffmpeg is required to encode presentation.mp4')
    run_owned([ffmpeg, '-y', '-threads', '2', '-framerate', '10', '-i',
               str(output/'frames/%04d.png'), '-frames:v', str(args.frames),
               '-c:v', 'libx264', '-threads', '2', '-pix_fmt', 'yuv420p', '-crf', '20',
               '-movflags', '+faststart', str(output/'presentation.mp4')],
              purpose='Encode 10fps presentation MP4', log=output/'ffmpeg.log', timeout_s=120)


if __name__ == '__main__':
    main()
