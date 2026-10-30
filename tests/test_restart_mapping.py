import numpy as np

from aquabeacon_analysis.restart_mapping import (
    position_registration,
    prior_anchor_indices,
    temporal_windows,
)


def test_upsampling_does_not_multiply_prior_weight_or_restore_missing_fixes():
    t = np.arange(0, 1, 1/120)
    available = (t < .3) | (t > .7)
    selected = prior_anchor_indices(t, available)
    assert available[selected].all()
    assert len(np.unique(np.floor(t[selected] / .2 + .5))) == len(selected)
    assert np.allclose(t[selected[:2]], [0, .2])
    assert not np.any((t[selected] >= .3) & (t[selected] <= .7))


def test_restart_windows_cover_tail_without_needing_earlier_success():
    t = 1260 + np.arange(33564) * 1001 / 120000
    windows = temporal_windows(t)
    assert np.array_equal(np.unique(np.concatenate(windows)), np.arange(len(t)))
    assert windows[-1][-1] == len(t) - 1
    assert windows[1][0] < windows[0][-1]


def test_registration_recovers_metric_transform_but_rejects_straight_line():
    rng = np.random.default_rng(7)
    source = rng.normal(size=(20, 3))
    rotation = np.array([[0., -1., 0.], [1., 0., 0.], [0., 0., 1.]])
    target = 2.5 * source @ rotation.T + [4., 7., 1.]
    fit = position_registration(source, target)
    assert fit['accepted']
    assert fit['fit_rms_m'] < 1e-12
    assert abs(fit['scale'] - 2.5) < 1e-12
    line = np.column_stack([np.arange(20), np.zeros((20, 2))])
    assert not position_registration(line, 2 * line + 4)['accepted']


def test_position_jump_is_downweighted_and_remains_in_residual_accounting():
    rng = np.random.default_rng(19)
    source = rng.normal(size=(30, 3))
    target = 2 * source + [3., 1., -2.]
    target[-1] += [30., -40., 20.]
    fit = position_registration(source, target, huber_delta_m=.05)
    assert fit['accepted']
    assert fit['weights'][-1] < .01
    assert fit['fit_median_m'] < .02
    assert fit['fit_max_m'] > 50
    assert fit['correspondences'] == 30
