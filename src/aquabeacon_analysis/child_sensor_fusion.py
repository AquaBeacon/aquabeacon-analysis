"""Child-only inertial/depth experiments; no ground-truth accuracy claims."""
from __future__ import annotations

import numpy as np
from scipy.spatial.transform import Rotation


def fit_vector_rotation(source, target, minimum_speed=.02):
    """Fit a proper sensor-to-optical rotation and expose excitation/residuals."""
    source, target = np.asarray(source), np.asarray(target)
    valid = (np.isfinite(source).all(1) & np.isfinite(target).all(1)
             & (np.linalg.norm(source, axis=1) > minimum_speed)
             & (np.linalg.norm(target, axis=1) > minimum_speed))
    a, b = source[valid], target[valid]
    if len(a) < 30:
        raise ValueError('Insufficient angular excitation')
    rotation, _ = Rotation.align_vectors(b, a)
    singular = np.linalg.svd(a.T @ b)[1]
    prediction = rotation.apply(a)
    return rotation.as_matrix(), {
        'samples': len(a), 'singular_ratio': float(singular[-1]/singular[0]),
        'rms_rad_s': float(np.sqrt(np.mean((prediction-b)**2))),
        'correlation': float(np.corrcoef(prediction.ravel(), b.ravel())[0, 1])}


def inertial_pressure_filter(times, acceleration, pressure_observation, *,
                             depth_sigma_m=.03, acceleration_sigma=.15,
                             visual_velocity=None, visual_sigma_m_s=.15):
    """Independent p,v,b Kalman axes; pressure updates only down, once per sample.

    Initial velocity is zero by assumption. No quiet-motion ZUPT. Missing inertial
    input or gaps >0.1 s reset all axes and create a separately numbered segment.
    Covariances describe the chosen model, not validated measurement accuracy.
    """
    n = len(times)
    output = np.full((n, 3), np.nan)
    velocity = output.copy()
    raw = output.copy()
    segment = np.full(n, -1, int)
    used_visual = np.zeros(n, bool)
    x, covariance, raw_p, raw_v = None, None, None, None
    number = -1
    for i, time in enumerate(times):
        dt = time-times[i-1] if i else 0
        if not np.isfinite(acceleration[i]).all():
            x = None
            continue
        if x is None or not 0 < dt <= .100001:
            number += 1
            x = np.zeros((3, 3))
            covariance = np.repeat(np.diag([.01, 1., .04])[None], 3, axis=0)
            raw_p, raw_v = np.zeros(3), np.zeros(3)
        else:
            mean_acc = (acceleration[i-1]+acceleration[i])/2
            raw_p += raw_v*dt + .5*mean_acc*dt**2
            raw_v += mean_acc*dt
            transition = np.array([[1, dt, -.5*dt**2], [0, 1, -dt], [0, 0, 1]])
            control = np.array([.5*dt**2, dt, 0.])
            x = x @ transition.T + mean_acc[:, None]*control
            noise = acceleration_sigma**2*np.outer(control, control)
            noise[2, 2] += .002**2*dt
            covariance = transition @ covariance @ transition.T + noise
        if np.isfinite(pressure_observation[i]):
            h = np.array([1., 0., 0.])
            gain = covariance[2] @ h/(h @ covariance[2] @ h+depth_sigma_m**2)
            x[2] += gain*(pressure_observation[i]-x[2, 0])
            update = np.eye(3)-np.outer(gain, h)
            covariance[2] = (update @ covariance[2] @ update.T
                             + depth_sigma_m**2*np.outer(gain, gain))
        if visual_velocity is not None and np.isfinite(visual_velocity[i]).all():
            for axis in range(3):
                h = np.array([0., 1., 0.])
                gain = covariance[axis] @ h/(h @ covariance[axis] @ h+visual_sigma_m_s**2)
                x[axis] += gain*(visual_velocity[i, axis]-x[axis, 1])
                update = np.eye(3)-np.outer(gain, h)
                covariance[axis] = (update @ covariance[axis] @ update.T
                                   + visual_sigma_m_s**2*np.outer(gain, gain))
            used_visual[i] = True
        output[i], velocity[i], raw[i], segment[i] = x[:, 0], x[:, 1], raw_p, number
    return raw, output, velocity, segment, used_visual


def pressure_scale(position_down, depth, times, minimum_span_m=.05):
    """Fit positive monocular scale on chronological 60%, check remaining 40%.

    This is pressure consistency, never independent position validation. A failed
    fit is returned with its candidate values, not silently applied to a map.
    """
    valid = np.isfinite(position_down) & np.isfinite(depth) & np.isfinite(times)
    x, y, t = np.asarray(position_down)[valid], np.asarray(depth)[valid], np.asarray(times)[valid]
    if len(x) < 8:
        return {'accepted': False, 'reason': 'fewer than 8 overlapping depth/visual samples',
                'overlapping_samples': len(x)}
    split = t.min()+.6*(t.max()-t.min())
    train = t < split
    if min(train.sum(), (~train).sum()) < 3 or np.ptp(x[train]) < 1e-8:
        return {'accepted': False, 'reason': 'insufficient split or visual excitation'}
    scale, offset = np.linalg.lstsq(np.column_stack([x[train], np.ones(train.sum())]),
                                    y[train], rcond=None)[0]
    residual = scale*x+offset-y
    rms = float(np.sqrt(np.mean(residual[~train]**2)))
    train_span = float(np.ptp(y[train]))
    accepted = bool(scale > 0 and train_span >= minimum_span_m
                    and rms < max(.05, .3*float(np.ptp(y))))
    return {'accepted': accepted, 'scale_m_per_slam_unit': float(scale),
            'offset_m': float(offset), 'training_depth_span_m': train_span,
            'held_out_rmse_m': rms, 'held_out_samples': int((~train).sum()),
            'training_samples': int(train.sum()), 'split_source_video_s': float(split),
            'reason': 'accepted provisional depth scale' if accepted else
                      'positive scale, vertical excitation or held-out consistency gate failed'}
