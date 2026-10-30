import numpy as np

from aquabeacon_analysis.ngt_sync import correlation_offset, fit_rotation, sample_with_gaps


def test_clock_anchor_does_not_extrapolate_or_use_future_anchor():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "scripts/split_ngt_experiments.py"
    spec = importlib.util.spec_from_file_location("ngt_split_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    anchors = ([100, 300], [(100, 1000, 200), (300, 2000, 400)])
    assert module.anchored_utc(99, anchors) is None
    assert module.anchored_utc(100, anchors) == 1000
    assert module.anchored_utc(199, anchors) == 1099
    assert module.anchored_utc(200, anchors) is None
    assert module.anchored_utc(299, anchors) is None
    assert module.anchored_utc(300, anchors) == 2000
    assert module.anchored_utc(400, anchors) is None


def test_sync_recovers_sign_and_known_delay():
    t = np.arange(0, 30, .01)
    motion = np.sin(t * 1.3) + .4 * np.cos(t * t * .23)
    answer = correlation_offset(t, motion, t + 2.35, motion, (2, 25),
                                np.arange(1.5, 3.01, .01))
    assert abs(answer["offset_s"] - 2.35) < .011
    assert answer["correlation"] > .999


def test_interpolation_preserves_outage_and_no_extrapolation():
    result = sample_with_gaps(np.array([0., .1, 2., 2.1]), np.array([1., 2., 3., 4.]),
                              np.array([-.01, .05, 1., 2.05, 3.]), .2)
    np.testing.assert_allclose(result[[1, 3]], [1.5, 3.5])
    assert np.isnan(result[[0, 2, 4]]).all()


def test_mount_rotation_is_proper_and_recovers_axes():
    rng = np.random.default_rng(52)
    x = rng.normal(size=(1000, 3))
    expected = np.array([[0, -1, 0], [1, 0, 0], [0, 0, 1.]])
    actual, quality = fit_rotation(x, x @ expected.T)
    np.testing.assert_allclose(actual, expected, atol=1e-12)
    assert np.linalg.det(actual) > .999
    assert quality["correlation"] > .999
