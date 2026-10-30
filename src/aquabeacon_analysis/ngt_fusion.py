"""Conservative NGT sensor fusion: local attitude and observable position channels.

No IMU acceleration is integrated into geographic position. Covariances below are
filter model covariances, not independently established sensor accuracy.
"""

from __future__ import annotations

import numpy as np

from .imu_localization import quaternion_multiply, slerp


def gap_interpolate(times, values, targets, max_gap_s=0.1):
    """Linear interpolation only inside a finite, short source bracket."""
    times, values, targets = map(np.asarray, (times, values, targets))
    one = values.ndim == 1
    if one:
        values = values[:, None]
    out = np.full((len(targets), values.shape[1]), np.nan)
    if len(times) < 2:
        return out[:, 0] if one else out
    hi = np.clip(np.searchsorted(times, targets, side="right"), 1, len(times) - 1)
    lo = hi - 1
    valid = ((targets >= times[0]) & (targets <= times[-1]) &
             ((times[hi] - times[lo]) <= max_gap_s))
    fraction = (targets - times[lo]) / (times[hi] - times[lo])
    out[valid] = (values[lo] * (1 - fraction[:, None]) +
                  values[hi] * fraction[:, None])[valid]
    return out[:, 0] if one else out


def fuse_local_attitude(camera_times, camera_q, imu_times, gyro, rotation,
                        correction_time_s=0.5, max_gap_s=0.1):
    """Gyro prediction, camera quaternion correction; reset rather than bridge gaps."""
    camera_q = np.asarray(camera_q)
    aligned = gap_interpolate(imu_times, gyro, camera_times, max_gap_s) @ rotation.T
    fused = camera_q.copy()
    used = np.zeros(len(camera_times), bool)
    reset = np.ones(len(camera_times), bool)
    for i in range(1, len(camera_times)):
        dt = camera_times[i] - camera_times[i - 1]
        if not (0 < dt <= max_gap_s and np.isfinite(aligned[i - 1:i + 1]).all()):
            continue
        omega = (aligned[i - 1] + aligned[i]) / 2
        angle = np.linalg.norm(omega) * dt
        step = np.r_[np.cos(angle / 2), omega * dt * np.sinc(angle / (2 * np.pi)) / 2]
        prediction = quaternion_multiply(fused[i - 1], step)
        fused[i] = slerp(prediction, camera_q[i], 1 - np.exp(-dt / correction_time_s))
        used[i], reset[i] = True, False
    return fused, used, reset


def stationarity(gyro, acceleration, gravity_m_s2=9.80665):
    """Inertial quiet flag, not proof of zero translational velocity."""
    return ((np.linalg.norm(gyro, axis=1) < 0.035) &
            (np.abs(np.linalg.norm(acceleration, axis=1) - gravity_m_s2) < 0.18))


def cv_filter(times, observations, sigma, valid, quiet, zero_rate,
              max_prediction_s=3.0, innovation_gate=25.0):
    """Independent-axis CV Kalman model, adaptive dynamics and bounded prediction.

    zero_rate is an explicitly justified velocity pseudo-observation. quiet alone
    only reduces process noise because constant velocity can be inertially quiet.
    A long loss clears state; reacquisition initializes from the measurement.
    """
    times, observations, sigma = map(np.asarray, (times, observations, sigma))
    if observations.ndim == 1:
        observations = observations[:, None]
        sigma = sigma[:, None]
    n, dim = observations.shape
    output = np.full((n, dim), np.nan)
    covariance = np.full((n, dim), np.nan)
    accepted, predicted, reset = (np.zeros(n, bool) for _ in range(3))
    state, cov, last_observed = None, None, -np.inf
    for i, t in enumerate(times):
        dt = t - times[i - 1] if i else 0
        if state is not None and (dt <= 0 or t - last_observed > max_prediction_s):
            state = None
        available = bool(valid[i] and np.isfinite(observations[i]).all() and
                         np.isfinite(sigma[i]).all() and (sigma[i] > 0).all())
        if state is None:
            if not available:
                continue
            state = np.column_stack([observations[i], np.zeros(dim)])
            cov = np.zeros((dim, 2, 2))
            cov[:, 0, 0], cov[:, 1, 1] = sigma[i] ** 2, 4
            last_observed = t
            accepted[i], reset[i] = True, True
        else:
            transition = np.array([[1, dt], [0, 1]])
            noise = (0.03 if quiet[i] else 0.8) ** 2
            process = noise * np.array([[dt**3 / 3, dt**2 / 2], [dt**2 / 2, dt]])
            state = state @ transition.T
            cov = transition @ cov @ transition.T + process
            if available:
                residual = observations[i] - state[:, 0]
                innovation = cov[:, 0, 0] + sigma[i] ** 2
                if np.sum(residual**2 / innovation) <= innovation_gate:
                    gain = cov[:, :, 0] / innovation[:, None]
                    state += gain * residual[:, None]
                    cov -= gain[:, :, None] * cov[:, None, 0, :]
                    accepted[i], last_observed = True, t
            if zero_rate[i]:
                gain = cov[:, :, 1] / (cov[:, 1, 1] + 0.02**2)[:, None]
                state -= gain * state[:, 1, None]
                cov -= gain[:, :, None] * cov[:, None, 1, :]
        if t - last_observed <= max_prediction_s:
            output[i], covariance[i] = state[:, 0], cov[:, 0, 0]
            predicted[i] = not accepted[i]
    return output, covariance, accepted, predicted, reset


def pressure_depth(pressure_mbar, baseline_mbar, density_kg_m3=997.0, gravity_m_s2=9.80665):
    """Depth positive down relative to explicitly selected pressure baseline."""
    return (np.asarray(pressure_mbar) - baseline_mbar) * 100 / (density_kg_m3 * gravity_m_s2)


def unobservable_child_position(n):
    """No bearing and no verified lever arms: neither world xy nor xyz is inferred."""
    return np.full((n, 3), np.nan)
