import numpy as np
import pytest

from aquabeacon_analysis.stationary_spectra import (
    common_grid_series,
    paired_spectra,
    regularize,
    spectrum,
    trend_periodic_decomposition,
    variance_identity,
)


def test_missing_observations_do_not_join_or_zero_fill():
    t = np.array([0.0, 1.0, 2.0, 7.0, 8.0, 9.0])
    out = regularize(t, t, np.arange(10.0), 1.5)
    np.testing.assert_allclose(out[[0, 1, 2, 7, 8, 9]], t)
    assert np.isnan(out[3:7]).all()


def test_fft_lomb_recover_frequency_with_drift_gaps_and_parseval():
    t = np.arange(0, 60, 0.1)
    y = 2 * np.sin(2 * np.pi * 0.2 * t) + 0.03 * t
    y[(t > 40) & (t < 44)] = np.nan
    info, arrays = spectrum(t, y)
    assert info["lomb_peak_hz"] == pytest.approx(0.2, abs=0.004)
    assert info["fft_peak_hz"] == pytest.approx(0.2, abs=0.026)
    assert info["fft_duration_s"] < 42
    assert info["fft_run_parseval_variance"] == pytest.approx(info["fft_run_detrended_variance"])
    assert np.isfinite(arrays["lomb_normalized_power"]).all()


def test_coherence_requires_multiple_windows_and_preserves_gaps():
    t = np.arange(60.0)
    x = np.sin(2 * np.pi * 0.1875 * t)
    y = 2 * x
    info, arrays = paired_spectra(x, y)
    assert info["welch_windows"] == 6
    assert arrays["coherence"][3] == pytest.approx(1)
    y[10:50] = np.nan
    info, arrays = paired_spectra(x, y)
    assert info["status"] == "insufficient_complete_windows"
    assert not arrays


def test_downsampling_rejects_alias_and_long_gap():
    t = np.arange(0.0, 90.0, 0.1)
    y = np.sin(2 * np.pi * 0.2 * t) + 5 * np.sin(2 * np.pi * 1.2 * t)
    y[(t > 45) & (t < 55)] = np.nan
    grid = np.arange(0.0, 90.0)
    out = common_grid_series(t, y, grid)
    np.testing.assert_allclose(out[10:35], np.sin(2 * np.pi * 0.2 * grid[10:35]), atol=0.04)
    assert np.isnan(out[46:55]).all()


def test_correction_covariance_is_signed_and_exact():
    t = np.arange(20.0)
    fixed = np.column_stack([np.sin(t), np.cos(t), t / 10])
    corrected = 0.2 * fixed + 4
    m = variance_identity(fixed, corrected)
    assert m["twice_covariance_m2"] < 0
    assert m["corrected_variance_m2"] == pytest.approx(
        m["fixed_variance_m2"] + m["correction_variance_m2"] + m["twice_covariance_m2"]
    )


def test_periodic_partition_closes_with_irregular_observations():
    t = np.delete(np.arange(60.0), [13, 14, 15, 46, 47, 48])
    xyz = np.column_stack(
        [np.sin(2 * np.pi * 0.2 * t) + 0.01 * t, np.cos(2 * np.pi * 0.2 * t), t * 0.02]
    )
    m = trend_periodic_decomposition(t, xyz, 0.2)
    assert abs(m["closure_error_m2"]) < 1e-12
    assert m["rms_m"]["remainder"] < 1e-12
