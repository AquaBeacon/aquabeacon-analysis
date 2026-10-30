import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from aquabeacon_analysis.child_axis_calibration import body_quaternions, fit_frd


def poses():
    matrix = Rotation.from_euler("xyz", [20, -10, 30], degrees=True).as_matrix()
    expected = np.array([[0, 0, -1], [1, 0, 0], [0, 1, 0], [-1, 0, 0]]) * 9.80665
    samples = [(np.tile(v @ matrix, (100, 1)), np.zeros((100, 3)), np.arange(100)*.01)
               for v in expected]
    return matrix, samples


def test_axes_and_quaternion_direction():
    expected, samples = poses()
    result, _ = fit_frd(samples)
    np.testing.assert_allclose(result, expected, atol=1e-12)
    # Sensor-to-world equals sensor-to-body when body is world-aligned.
    q = Rotation.from_matrix(expected).as_quat()[[3, 0, 1, 2]]
    np.testing.assert_allclose(np.abs(body_quaternions([q], result)), [[1, 0, 0, 0]],
                               atol=1e-12)


def test_bad_fourth_pose_and_gaps_rejected():
    _, samples = poses()
    samples[3] = samples[1]
    with pytest.raises(ValueError, match="direction gate"):
        fit_frd(samples)
    _, samples = poses()
    samples[0][2][50:] += 1
    with pytest.raises(ValueError, match="discontinuous"):
        fit_frd(samples)


def test_missing_quaternion_and_reflection():
    assert np.isnan(body_quaternions([[0, 0, 0, 0]], np.eye(3))).all()
    with pytest.raises(ValueError, match="proper"):
        body_quaternions([[1, 0, 0, 0]], np.diag([-1, 1, 1]))
