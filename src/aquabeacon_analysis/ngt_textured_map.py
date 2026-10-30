"""Conservative photo projection onto existing, independently framed SLAM points."""
from __future__ import annotations

from itertools import pairwise

import numpy as np
from scipy.spatial import Delaunay, QhullError
from scipy.spatial.transform import Rotation


def scaled_intrinsics(matrix, original_size, image_size):
    """Scale a full-frame pinhole matrix; reject unexplained aspect-ratio changes."""
    sx, sy = np.asarray(image_size, float) / np.asarray(original_size, float)
    if not np.isclose(sx, sy, atol=1e-8):
        raise ValueError('aspect ratio changed: an explicit crop calibration is required')
    return np.diag([sx, sy, 1.0]) @ np.asarray(matrix, float)


def camera_projection(points, position, quaternion_xyzw, matrix):
    """Project T_world_camera, OpenCV right/down/forward axes, without distortion."""
    camera = (np.asarray(points) - position) @ Rotation.from_quat(quaternion_xyzw).as_matrix()
    homogeneous = camera @ np.asarray(matrix).T
    with np.errstate(divide='ignore', invalid='ignore'):
        pixels = homogeneous[:, :2] / homogeneous[:, 2:3]
    return pixels, camera[:, 2]


def projected_surface(points, position, quaternion_xyzw, matrix, image_size,
                      max_edge_px=80.0, max_depth_ratio=1.15,
                      max_world_edge_fraction=0.08, visibility_cell_px=8):
    """Return original vertex indices, UV and triangle indices; never invent geometry.

    An 8px approximate foreground test removes deeper points within each pixel cell.
    This is only a sparse occlusion approximation, not a dense visibility proof.
    """
    points = np.asarray(points, float)
    pixels, depth = camera_projection(points, position, quaternion_xyzw, matrix)
    width, height = image_size
    valid = (np.isfinite(pixels).all(axis=1) & (depth > 1e-6)
             & (pixels[:, 0] >= 0) & (pixels[:, 0] < width)
             & (pixels[:, 1] >= 0) & (pixels[:, 1] < height))
    ids = np.flatnonzero(valid)
    if len(ids):
        cells = np.floor(pixels[ids] / visibility_cell_px).astype(int)
        cell_ids = cells[:, 0] + cells[:, 1] * (int(width / visibility_cell_px) + 1)
        nearest = {}
        for i, cell in zip(ids, cell_ids, strict=True):
            nearest[cell] = min(nearest.get(cell, np.inf), depth[i])
        ids = np.array([i for i, cell in zip(ids, cell_ids, strict=True)
                        if depth[i] <= nearest[cell] * 1.03], dtype=int)
        _, unique = np.unique(np.round(pixels[ids], 2), axis=0, return_index=True)
        ids = ids[unique]
    uv = np.column_stack((pixels[ids, 0] / width, 1 - pixels[ids, 1] / height))
    empty = np.empty((0, 3), dtype=int)
    if len(ids) < 4:
        return ids, uv, empty
    try:
        faces = Delaunay(pixels[ids]).simplices
    except QhullError:
        return ids, uv, empty
    corners = pixels[ids][faces]
    edge_px = np.linalg.norm(corners - np.roll(corners, 1, axis=1), axis=2).max(axis=1)
    z = depth[ids][faces]
    world = points[ids][faces]
    edge_world = np.linalg.norm(world - np.roll(world, 1, axis=1), axis=2).max(axis=1)
    radius = np.percentile(np.linalg.norm(points - np.median(points, axis=0), axis=1), 90)
    e1, e2 = corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0]
    area = np.abs(e1[:, 0] * e2[:, 1] - e1[:, 1] * e2[:, 0]) / 2
    keep = ((edge_px <= max_edge_px) & (z.max(axis=1) / z.min(axis=1) <= max_depth_ratio)
            & (edge_world <= radius * max_world_edge_fraction) & (area >= 2))
    return ids, uv, faces[keep]


def source_video_seconds(synthetic_ns):
    """The ORB dataset synthetic time is explicitly not UTC."""
    return (int(synthetic_ns) - 10**18) / 1e9


def path_segments(keyframes, maximum_gap_s=1.0):
    """Only connect chronological neighboring poses across short recorded intervals."""
    return [[a['position'], b['position']] for a, b in pairwise(keyframes)
            if 0 <= b['source_video_s'] - a['source_video_s'] <= maximum_gap_s]
