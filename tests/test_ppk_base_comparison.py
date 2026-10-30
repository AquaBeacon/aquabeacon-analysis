"""Validate fixed corroboration and static-scatter definitions without real GNSS."""
import importlib.util
import sys
from pathlib import Path

import pytest

scripts = Path(__file__).parents[1]/'scripts'
sys.path.insert(0, str(scripts))
spec = importlib.util.spec_from_file_location('compare_ngt_ppk_bases', scripts/'compare_ngt_ppk_bases.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
sys.path.pop(0)


def row(t, east, quality=1):
    return {'utc_s': t+10000, 'parent_video_s': t, 'east_m': east,
            'north_m': 0., 'up_m': 0., 'quality': quality}


def test_scatter_uses_mean_and_time_windows_not_absolute_offset():
    rows = [row(240, 100), row(241, 102), row(400, 101), row(480, 999)]
    result = module.metrics(rows)
    assert result['static']['count'] == 3
    assert result['static']['horizontal_rms_m'] == pytest.approx((2/3)**.5)
    assert result['static_first_half']['count'] == 2
    assert result['static_second_half']['count'] == 1
    assert result['step_over_2m_count'] == 0  # Never treat a long gap as a 1s step.
    assert module.metrics(list(reversed(rows))) == result  # Backward solver outputs newest first.


def test_fixed_requires_both_directions_and_includes_disagreement():
    result = module.direction_agreement([row(240, 0), row(241, 0), row(242, 0)],
                                        [row(240, .1), row(241, 2), row(242, 0, 2)])
    assert result['all']['count'] == 3
    assert result['both_fixed']['count'] == 2
    assert result['both_fixed']['within_h02_v04_fraction'] == .5
    assert module.direction_agreement([row(240, 0)], [row(241, 0)])['both_fixed']['count'] == 0


def test_more_fixed_cannot_override_worse_static_or_direction_disagreement():
    sys.path.insert(0, str(scripts))
    try:
        spec = importlib.util.spec_from_file_location('report_ngt_ppk_comparison',
                                                     scripts/'report_ngt_ppk_comparison.py')
        report = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(report)
    finally:
        sys.path.pop(0)
    reference = {'static': {'count': 240, 'horizontal_rms_m': .22, 'three_d_rms_m': .92}}
    worse = {'static': {'count': 240, 'horizontal_rms_m': .6, 'three_d_rms_m': 2.}}
    good_agreement = {'both_fixed': {'count': 50, 'within_h02_v04_fraction': 1.}}
    assert len(report.assess(worse, reference, good_agreement)) == 2
    assert report.assess(reference, reference, {'both_fixed': {'count': 0}})
    assert report.assess(reference, reference, good_agreement) == []
