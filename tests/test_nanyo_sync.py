"""Regression checks for lag sign and missing-data rejection."""
import importlib.util
from pathlib import Path

import numpy as np
import pytest


def load_search():
    path = Path(__file__).resolve().parents[1] / 'scripts/synchronize_nanyo.py'
    spec = importlib.util.spec_from_file_location('nanyo_sync', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.search


def test_positive_offset_and_competing_peak():
    search = load_search()
    t = np.arange(0, 40, .01)
    y = np.sin(t*.8) + .4*np.sin(t*2.37) + .2*np.cos(t*t*.1)
    result = search(t, y, t+1.7, y, [5, 30], np.arange(-3, 3.001, .01))
    assert result['offset_s'] == pytest.approx(1.7)
    assert result['correlation'] > .999
    assert result['competitor'] < .95
    assert not result['boundary']


def test_no_interpolation_across_large_outage():
    search = load_search()
    t = np.arange(0, 40, .01)
    y = np.sin(t)
    keep = (t < 8) | (t > 28)
    with pytest.raises(ValueError, match='Insufficient overlap'):
        search(t, y, t[keep], y[keep], [5, 30], np.arange(-1, 1, .1))
