from pathlib import Path

import numpy as np
import pytest

from aquabeacon_analysis.camera_calibration import load_camera_intrinsics

ROOT = Path(__file__).resolve().parents[1]


def test_parent_child_share_user_selected_underwater_calibration():
    parent = load_camera_intrinsics(ROOT, 'P_MAIN_CAM', (1280, 720))
    child = load_camera_intrinsics(ROOT, 'C_MAIN_CAM', (1280, 720))
    np.testing.assert_array_equal(parent[0], child[0])
    np.testing.assert_array_equal(parent[1], child[1])
    assert parent[2]['sha256'] == child[2]['sha256']
    assert parent[2]['status'].startswith('provisional')


def test_native_matrix_is_scaled_but_distortion_unchanged():
    native = load_camera_intrinsics(ROOT, 'C_MAIN_CAM', (1920, 1080))
    resized = load_camera_intrinsics(ROOT, 'C_MAIN_CAM', (1280, 720))
    np.testing.assert_allclose(native[0][:2]*2/3, resized[0][:2])
    np.testing.assert_array_equal(native[1], resized[1])


def test_crop_geometry_is_not_silently_accepted():
    with pytest.raises(ValueError, match='aspect-preserving'):
        load_camera_intrinsics(ROOT, 'C_MAIN_CAM', (1280, 800))
