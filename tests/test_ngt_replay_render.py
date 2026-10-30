"""Replay selection must retain missing samples and never anticipate an observation."""
import importlib.util
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1]/"blender/create_ngt_replay_v2.py"
SPEC = importlib.util.spec_from_file_location("ngt_replay_render", MODULE)
replay = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(replay)


def test_sample_hold_uses_earlier_source_indices_without_interpolation():
    assert replay.sample_indices([1, 1.2, 1.4, 1.6, 1.8], 1.1, 1.7, 4) == [0, 1, 2, 3]


def test_reveal_does_not_anticipate_source_observation():
    assert replay.reveal_frame(1.21, [1, 1.2, 1.4]) == 3
    assert replay.reveal_frame(1.2, [1, 1.2, 1.4]) == 2
    assert replay.reveal_frame(2, [1, 1.2, 1.4]) == 4


def test_bad_interval_rejected():
    with pytest.raises(ValueError):
        replay.sample_indices([1, 2, 3], .9, 3, 2)
