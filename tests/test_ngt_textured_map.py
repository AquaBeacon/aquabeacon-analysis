import numpy as np
import pytest

from aquabeacon_analysis.ngt_textured_map import (
    camera_projection,
    path_segments,
    projected_surface,
    scaled_intrinsics,
    source_video_seconds,
)


def test_main_video_intrinsics_full_frame_scale_and_crop_rejection():
    k = np.array([[1000, 0, 960], [0, 1000, 540], [0, 0, 1]])
    result = scaled_intrinsics(k, [1920, 1080], [1280, 720])
    assert result[0, 0] == pytest.approx(2000 / 3)
    assert result[0, 2] == 640
    with pytest.raises(ValueError, match='crop'):
        scaled_intrinsics(k, [1920, 1080], [1000, 720])


def test_projection_pose_direction_and_behind_camera():
    k = np.array([[100, 0, 50], [0, 100, 50], [0, 0, 1]])
    pixels, z = camera_projection(np.array([[1, 0, 2], [0, 0, -1]]),
                                  [0, 0, 0], [0, 0, 0, 1], k)
    np.testing.assert_allclose(pixels[0], [100, 50])
    assert z[1] < 0
    pixels, z = camera_projection(np.array([[2, 0, 2]]), [1, 0, 0],
                                  [0, 0, np.sqrt(0.5), np.sqrt(0.5)], k)
    np.testing.assert_allclose(pixels[0], [50, 0], atol=1e-12)
    assert z[0] == pytest.approx(2)


def test_triangles_only_use_original_points_and_reject_depth_bridges():
    x, y = np.meshgrid(np.linspace(-0.4, 0.4, 12), np.linspace(-0.4, 0.4, 12))
    points = np.column_stack([x.ravel(), y.ravel(), np.ones(x.size)])
    k = np.array([[100, 0, 50], [0, 100, 50], [0, 0, 1]])
    ids, uv, faces = projected_surface(points, [0, 0, 0], [0, 0, 0, 1], k, [100, 100],
                                        max_world_edge_fraction=0.5)
    assert len(faces) > 50
    assert faces.max() < len(ids)
    assert (uv >= 0).all() and (uv <= 1).all()
    assert set(ids[faces].ravel()) <= set(range(len(points)))
    points[:, 2] = np.where(np.arange(len(points)) % 2, 1, 10)
    ids, _, faces = projected_surface(points, [0, 0, 0], [0, 0, 0, 1], k, [100, 100],
                                       max_world_edge_fraction=1)
    if len(faces):
        depth = points[ids[faces], 2]
        assert (depth.max(axis=1) / depth.min(axis=1) <= 1.15).all()


def test_collinear_or_missing_points_are_not_filled():
    points = np.array([[x, 0, 1] for x in np.linspace(-0.2, 0.2, 10)])
    _, _, faces = projected_surface(points, [0, 0, 0], [0, 0, 0, 1], np.eye(3), [100, 100])
    assert faces.shape == (0, 3)


def test_synthetic_clock_and_no_path_bridge_over_failure():
    assert source_video_seconds(1000001171445275008) == pytest.approx(1171.445275008)
    poses = [{'source_video_s': t, 'position': [t, 0, 0]} for t in [1, 1.2, 10]]
    assert path_segments(poses) == [[[1, 0, 0], [1.2, 0, 0]]]
