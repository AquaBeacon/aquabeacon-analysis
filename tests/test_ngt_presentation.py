"""Meaningful replay tests independent of Blender's Python runtime."""
import importlib.util
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[1]/'blender/create_ngt_presentation_scene.py'
spec = importlib.util.spec_from_file_location('presentation', PATH)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_replay_never_interpolates_gap():
    first, last = {'source_video_s': 1}, {'source_video_s': 3}
    samples = module.replay_samples([last, first], frame_count=5, max_hold_s=.5)
    assert [item[1] for item in samples] == [first, first, None, None, last]
    assert [item[0] for item in samples] == [1, 1.5, 2, 2.5, 3]


def test_no_keyframes_fails():
    with pytest.raises(ValueError, match='No recorded'):
        module.replay_samples([])


def test_first_and_last_observations_preserved():
    frames = [{'source_video_s': .1}, {'source_video_s': .2}]
    samples = module.replay_samples(frames, frame_count=2)
    assert samples[0][1] is frames[0]
    assert samples[-1][1] is frames[-1]
