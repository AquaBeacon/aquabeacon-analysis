"""Archive paths must resolve inside the new PC's processed directory."""
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    'verify_nanyo_handoff', Path(__file__).resolve().parents[1] / 'scripts/verify_nanyo_handoff.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_old_windows_path_on_new_root(tmp_path):
    result = module.local_path(tmp_path, r'C:\old\repo\data\processed\trial\videos\parent.mp4')
    assert result == tmp_path / 'data/processed/trial/videos/parent.mp4'


def test_reject_escape_and_raw(tmp_path):
    with pytest.raises(ValueError):
        module.local_path(tmp_path, 'data/processed/../../private.txt')
    with pytest.raises(ValueError):
        module.local_path(tmp_path, 'data/raw/private.bin')
