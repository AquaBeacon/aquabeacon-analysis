"""Verify that the calibrated image ray removes lens distortion."""

import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from estimate_calibrated_positions import undistorted_camera_ray


def test_undistorted_camera_ray_recovers_projected_direction():
    camera = np.array([[671.49, 0, 646.17], [0, 672.27, 360.68], [0, 0, 1]])
    distortion = np.array([0.363, 0.3505, -0.0059, 0.0019, 0.0])
    direction = np.array([0.32, -0.21, 1.0])
    image, _ = cv2.projectPoints(direction[None, :], np.zeros(3), np.zeros(3),
                                 camera, distortion)
    recovered = undistorted_camera_ray(*image[0, 0], camera, distortion)
    np.testing.assert_allclose(recovered, direction / np.linalg.norm(direction), atol=1e-5)
