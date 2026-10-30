"""Measured stereo correspondences and gap-preserving local image-grid surfaces."""
from __future__ import annotations

import cv2
import numpy as np
from scipy.spatial.transform import Rotation


def relative_camera_pose(left, right):
    """Return X_right=R*X_left+t from two T_world_camera poses."""
    r1 = Rotation.from_quat(left['quaternion_xyzw']).as_matrix()
    r2 = Rotation.from_quat(right['quaternion_xyzw']).as_matrix()
    return r2.T @ r1, r2.T @ (np.asarray(left['position']) - right['position'])


def consistent_disparity(left, right, valid_left, valid_right, axis=0,
                         minimum=1.5, tolerance=1.0):
    """Positive left disparity and actual corresponding right-disparity agreement."""
    y, x = np.indices(left.shape)
    xr = np.rint(x - left).astype(int) if axis == 0 else x
    yr = np.rint(y - left).astype(int) if axis == 1 else y
    inside = (xr >= 0) & (xr < left.shape[1]) & (yr >= 0) & (yr < left.shape[0])
    xx, yy = np.clip(xr, 0, left.shape[1]-1), np.clip(yr, 0, left.shape[0]-1)
    error = np.abs(left + right[yy, xx])
    keep = (inside & valid_left & valid_right[yy, xx] & np.isfinite(left)
            & (left >= minimum) & (right[yy, xx] < 0) & (error <= tolerance))
    return keep, error


def triangulate_rectified(xy, disparity, p1, p2, axis=0):
    """Triangulate only actual finite nonzero-disparity rectified matches."""
    xy = np.asarray(xy, float)
    d = np.asarray(disparity, float)
    if not np.isfinite(d).all() or np.any(d <= 0):
        raise ValueError('finite positive measured disparity required')
    other = xy.copy()
    other[:, axis] -= d
    homogeneous = cv2.triangulatePoints(p1, p2, xy.T, other.T).T
    with np.errstate(divide='ignore', invalid='ignore'):
        points = homogeneous[:, :3] / homogeneous[:, 3:4]
    return points


def grid_surface(valid, depths, maximum_depth_ratio=1.05):
    """Connect only adjacent measured samples; never bridge an invalid grid sample."""
    ids = np.full(valid.shape, -1, dtype=int)
    ids[valid] = np.arange(valid.sum())
    a, b, c, d = ids[:-1, :-1], ids[:-1, 1:], ids[1:, :-1], ids[1:, 1:]
    proposed = np.concatenate([np.stack([a,b,c], axis=-1).reshape(-1,3),
                                np.stack([b,d,c], axis=-1).reshape(-1,3)])
    proposed = proposed[(proposed >= 0).all(axis=1)]
    if not len(proposed):
        return np.empty((0,3), dtype=int)
    z = np.asarray(depths)[valid][proposed]
    return proposed[(z.min(axis=1)>0) & (z.max(axis=1)/z.min(axis=1)<=maximum_depth_ratio)]


def patches(gray, xy, radius=2):
    offsets = np.arange(-radius, radius+1)
    dx, dy = np.meshgrid(offsets, offsets)
    x = (xy[:,0,None] + dx.ravel()).astype(np.float32)
    y = (xy[:,1,None] + dy.ravel()).astype(np.float32)
    return cv2.remap(gray, x, y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT).astype(float)


def independent_photo_support(left_gray, third_gray, left_xy, third_xy,
                              correlation_minimum=.7, minimum_std=3, max_mean_difference=20):
    """Check real third-image patches at calibrated 3D reprojection, without local search."""
    a, b = patches(left_gray, left_xy), patches(third_gray, third_xy)
    am, bm = a.mean(axis=1), b.mean(axis=1)
    ac, bc = a-am[:,None], b-bm[:,None]
    denom = np.linalg.norm(ac,axis=1)*np.linalg.norm(bc,axis=1)
    correlation = np.sum(ac*bc,axis=1)/np.maximum(denom,1e-9)
    inside = (third_xy[:,0]>=3)&(third_xy[:,1]>=3)&(third_xy[:,0]<third_gray.shape[1]-3)&(third_xy[:,1]<third_gray.shape[0]-3)
    keep = (inside & (a.std(axis=1)>=minimum_std) & (b.std(axis=1)>=minimum_std)
            & (np.abs(am-bm)<=max_mean_difference) & (correlation>=correlation_minimum))
    return keep, correlation
