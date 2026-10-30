import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from aquabeacon_analysis.magnetic_heading import fit_constant_bias, magnetic_candidates


def test_compass_direction_and_vertical_forward():
    q = Rotation.from_euler("z", [[0], [-90], [90]], degrees=True).as_quat()[:, [3, 0, 1, 2]]
    magnetic = Rotation.from_quat(q[:, [1, 2, 3, 0]]).inv().apply([[20, 0, -40]]*3)
    headings, _, _ = magnetic_candidates(q, magnetic, np.eye(3))
    np.testing.assert_allclose(headings, [0, 90, 270], atol=1e-8)
    q = Rotation.from_euler("y", [[90]], degrees=True).as_quat()[:, [3, 0, 1, 2]]
    headings, _, valid = magnetic_candidates(q, [[20, 0, -40]], np.eye(3))
    assert not valid[0] and np.isnan(headings[0])


def test_bias_recovery_and_unobservable_rejection():
    r = Rotation.from_euler("xyz", [[0, 0, 0], [0, 90, 0], [90, 0, 0], [0, -90, 0]],
                            degrees=True)
    q = r.as_quat()[:, [3, 0, 1, 2]]
    magnetic = r.inv().apply([[20, 3, -40]]*4) + [2, -3, 5]
    bias, field, _ = fit_constant_bias(q, magnetic)
    np.testing.assert_allclose(bias, [2, -3, 5], atol=1e-8)
    np.testing.assert_allclose(field, [20, 3, -40], atol=1e-8)
    with pytest.raises(ValueError, match="Insufficient"):
        fit_constant_bias([[1, 0, 0, 0]]*4, magnetic)
