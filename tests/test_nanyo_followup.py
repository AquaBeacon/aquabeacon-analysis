"""Protect the gap policy and prevent unsupported metric placement."""
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"scripts"))
import analyze_nanyo_optical_followup as prepare_script
import map_nanyo_optical_followup as mapping_script

from aquabeacon_analysis.nanyo_followup import bracket_values, longest_runs, positive_position_fit


def test_brackets_do_not_fill_missing_neighbors_or_extrapolate():
    values = np.array([[1., 2., 3.], [2., 3., 4.], [np.nan]*3, [4., 5., 6.]])
    result = bracket_values([0., .1, .2, .3], values, [-.01, 0, .05, .15, .2, .3, .31])
    np.testing.assert_allclose(result[2], [1.5, 2.5, 3.5])
    np.testing.assert_array_equal(result[1], values[0])
    np.testing.assert_array_equal(result[5], values[-1])
    assert np.isnan(result[[0, 3, 4, 6]]).all()


def test_long_gap_is_never_bridged():
    assert np.isnan(bracket_values([0, 1], np.ones((2, 3)), [.5], .15)).all()


def test_longest_missing_episodes_include_both_ends():
    assert longest_runs([True, True, False, True]) == [(0, 2), (3, 4)]
    assert longest_runs([False, False]) == []


def cloud():
    t = np.linspace(-2, 2, 12)
    return np.c_[t, t**2, t**3]


def test_positive_scale_is_supported_without_claiming_ground_truth():
    x = cloud()
    fit = positive_position_fit(x, 2*x+[1, 2, 3])
    assert fit["accepted"] and not fit["accuracy_validated"]
    np.testing.assert_allclose(fit["scale_m_per_unit"], 2, atol=1e-7)
    np.testing.assert_allclose(fit["translation_enu_m"], [1, 2, 3], atol=1e-7)


def test_negative_scale_is_not_forced_into_a_metric_map():
    x = cloud()
    fit = positive_position_fit(x, -2*x+[1, 2, 3])
    assert not fit["accepted"]
    assert fit["scale_m_per_unit"] < 0


def test_insufficient_motion_or_anchor_count_rejects_metric_placement():
    x = cloud()
    assert not positive_position_fit(x[:3], x[:3])["accepted"]
    assert not positive_position_fit(x, np.ones_like(x))["accepted"]


def test_preparation_preserves_existing_results(tmp_path, monkeypatch):
    monkeypatch.setattr(prepare_script, "ROOT", tmp_path)
    out = tmp_path/"results/trial/optical_followup/run"
    out.mkdir(parents=True)
    saved = out/"old_positions.csv"
    saved.write_text("saved measurement\n")
    with pytest.raises(FileExistsError):
        prepare_script.prepare("trial", {"run": "run"})
    assert saved.read_text() == "saved measurement\n"


@pytest.mark.parametrize("existing", ["mapping.json", "mapping_process.json", "mapping.log", "work"])
def test_mapping_checks_all_trials_before_writing_or_starting_workers(tmp_path, monkeypatch, existing):
    monkeypatch.setattr(mapping_script, "ROOT", tmp_path)
    first = tmp_path/"results/first/optical_followup/run"
    second = tmp_path/"results/second/optical_followup/run"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    if existing == "work":
        (tmp_path/"data/interim/second/optical_followup/run").mkdir(parents=True)
    else:
        (second/existing).write_text("preserve original\n")

    def unexpected_worker(*args, **kwargs):
        pytest.fail("Preflight must reject all targets before starting a worker")

    monkeypatch.setattr(mapping_script.subprocess, "Popen", unexpected_worker)
    with pytest.raises(FileExistsError):
        mapping_script.supervise(["first", "second"], {"run": "run"})
    assert not list(first.iterdir())
    if existing != "work":
        assert (second/existing).read_text() == "preserve original\n"
