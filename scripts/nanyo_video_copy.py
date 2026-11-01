"""Preserve MP4 edit-list timing after lossless concatenation of trimmed pieces."""
import os
import subprocess
from pathlib import Path


def frame_times(ffmpeg, path, start, duration):
    result = subprocess.run([str(ffmpeg), '-v', 'error', '-ss', str(start), '-i', str(path),
                             '-t', str(duration), '-map', '0:v:0', '-an', '-f', 'framemd5', '-'],
                            capture_output=True, text=True, check=True)
    timebase = None
    frames = []
    for line in result.stdout.splitlines():
        if line.startswith('#tb 0:'):
            numerator, denominator = line.split(':')[1].strip().split('/')
            timebase = float(numerator)/float(denominator)
        elif line and not line.startswith('#'):
            fields = line.split(',')
            frames.append((start+int(fields[2])*timebase, fields[-1].strip()))
    return frames


def preserve_clip_timing(item, ffmpeg):
    """Concat exposes initial negative-PTS preroll; restore the first piece origin.

    Following pieces begin at original file zero (automatic splits). Arbitrary
    trimmed intermediate pieces are refused because they require more mappings.
    Keep the uncorrected derived file as provenance, never modify raw sources.
    """
    target = Path(item['output']).resolve()
    root = Path('data/processed').resolve()
    if not target.is_relative_to(root):
        raise ValueError('Video output must be inside data/processed')
    backup = target.with_name(target.stem+'_concat_untrimmed.mp4')
    if backup.exists():
        raise FileExistsError(f'Already adjusted: {target}')
    pieces = item['pieces']
    if len(pieces) == 1:
        target.rename(backup)
        os.link(Path(pieces[0]['output']).resolve(), target)
        item['edit_list_correction'] = {'method': 'hardlink to original trimmed piece',
                                        'uncorrected_concat': str(backup)}
        return
    if any(abs(p['source_start_s']) > .05 for p in pieces[1:]):
        raise ValueError('Intermediate trimmed pieces require independent edit-list handling')
    reference = frame_times(ffmpeg, pieces[0]['output'], 1, .1)
    joined = {digest: time for time, digest in frame_times(ffmpeg, target, 0, 4)}
    shifts = [joined[digest]-time for time, digest in reference if digest in joined]
    if len(shifts) < 3 or max(shifts)-min(shifts) > .001:
        raise ValueError('Cannot determine constant concat preroll from decoded frames')
    shift = sum(shifts)/len(shifts)
    if not 0 <= shift <= 3:
        raise ValueError(f'Unexpected concat preroll: {shift}')
    target.rename(backup)
    duration = pieces[-1]['joined_end_s']
    command = [str(ffmpeg), '-v', 'warning', '-nostdin', '-y', '-ss', f'{shift:.9f}',
               '-i', str(backup), '-t', f'{duration:.9f}', '-map', '0:v:0', '-map', '0:a:0?',
               '-c', 'copy', '-map_metadata', '-1', '-metadata', 'creation_time=2000-01-01T00:00:00Z',
               '-movflags', '+faststart', str(target)]
    with target.with_suffix('.timing_fix.log').open('w') as log:
        subprocess.run(command, stderr=log, stdout=subprocess.DEVNULL, check=True)
    item['edit_list_correction'] = {'method': 'restore hidden initial preroll after concat',
                                    'preroll_s': shift, 'matching_frames': len(shifts),
                                    'uncorrected_concat': str(backup), 'command': command}
