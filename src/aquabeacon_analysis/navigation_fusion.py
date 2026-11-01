"""Auditable inertial p/v/b filtering with independently gated axis observations."""

import numpy as np
from scipy.ndimage import median_filter


def hampel_positions(values, floors=(2.0, 2.0, 3.0), window=11):
    """Flag isolated local spikes, keeping the values and every original row."""
    values = np.asarray(values, float)
    center = median_filter(values, size=(window, 1), mode="nearest")
    deviation = np.abs(values - center)
    mad = 1.4826 * median_filter(deviation, size=(window, 1), mode="nearest")
    return deviation > np.maximum(np.asarray(floors), 5 * mad)


def inertial_filter(
    times,
    acceleration,
    position,
    position_sigma,
    *,
    velocity=None,
    velocity_sigma=None,
    acceleration_sigma=0.35,
    bias_walk=0.006,
    max_anchor_age=20.0,
    gate_sigma=5.0,
    max_horizontal_speed=np.inf,
):
    """Forward ENU filter. Missing IMU breaks propagation; no invented long bridges.

    States per axis are [position, velocity, acceleration bias]. Observation arrays
    contain events only, never repeatedly interpolated fixes. Noise and covariance
    are assumed model quantities, not empirically validated navigation accuracy.
    """
    t = np.asarray(times)
    count = len(t)
    xout = np.full((count, 3, 3), np.nan)
    pout = np.full((count, 3), np.nan)
    used = np.zeros((count, 3), bool)
    vused = np.zeros((count, 3), bool)
    innovation = np.full((count, 3), np.nan)
    anchor_age = np.full(count, np.nan)
    segment = np.full(count, -1, int)
    available = np.zeros(count, bool)
    x = cov = None
    last_anchor = -np.inf
    number = -1
    for i in range(count):
        dt = t[i] - t[i - 1] if i else 0
        if not np.isfinite(acceleration[i]).all():
            x = None
            continue
        reacquire = (
            x is not None
            and np.isfinite(position[i]).all()
            and (
                t[i] - last_anchor > max_anchor_age
                or np.linalg.norm(x[:2, 1]) > max_horizontal_speed
            )
        )
        if x is None or not 0 < dt <= 0.151 or reacquire:
            if not np.isfinite(position[i]).all():
                x = None
                continue
            x = np.zeros((3, 3))
            x[:, 0] = position[i]
            initial_v = np.asarray(velocity[i]) if velocity is not None else np.full(3, np.nan)
            x[:, 1] = np.where(np.isfinite(initial_v), initial_v, 0)
            cov = np.array([np.diag([s * s, 1.0, 0.15**2]) for s in position_sigma[i]])
            used[i] = True
            vused[i] = np.isfinite(initial_v)
            last_anchor = t[i]
            number += 1
        else:
            f = np.array([[1, dt, -dt * dt / 2], [0, 1, -dt], [0, 0, 1]])
            control = np.array([dt * dt / 2, dt, 0])
            a = (acceleration[i - 1] + acceleration[i]) / 2
            x = x @ f.T + a[:, None] * control
            q = acceleration_sigma**2 * np.outer(control, control)
            q[2, 2] += bias_walk**2 * dt
            cov = f @ cov @ f.T + q
            for column, measurements, sigmas, flags in [
                (1, velocity, velocity_sigma, vused),
                (0, position, position_sigma, used),
            ]:
                if measurements is None:
                    continue
                for axis in range(3):
                    z, sigma = measurements[i, axis], sigmas[i, axis]
                    if not np.isfinite(z) or not np.isfinite(sigma) or sigma <= 0:
                        continue
                    residual = z - x[axis, column]
                    variance = cov[axis, column, column] + sigma * sigma
                    if column == 0:
                        innovation[i, axis] = residual / np.sqrt(variance)
                    if residual * residual > gate_sigma**2 * variance:
                        continue
                    gain = cov[axis, :, column] / variance
                    x[axis] += gain * residual
                    h = np.eye(3)[column]
                    joseph = np.eye(3) - np.outer(gain, h)
                    cov[axis] = joseph @ cov[axis] @ joseph.T + sigma * sigma * np.outer(gain, gain)
                    flags[i, axis] = True
            if used[i, :2].all():
                last_anchor = t[i]
        xout[i] = x
        pout[i] = np.sqrt(np.maximum(cov[:, 0, 0], 0))
        anchor_age[i] = t[i] - last_anchor
        segment[i] = number
        available[i] = (
            anchor_age[i] <= max_anchor_age
            and np.max(pout[i, :2]) < 30
            and np.linalg.norm(x[:2, 1]) <= max_horizontal_speed
        )
    return {
        "state": xout,
        "position_sigma": pout,
        "position_used": used,
        "velocity_used": vused,
        "innovation_sigma": innovation,
        "anchor_age_s": anchor_age,
        "segment": segment,
        "available": available,
    }


def event_grid(times, event_times, values, max_delta=0.051):
    """Assign each event to one nearest grid sample, preserving unmatched events."""
    t, et = np.asarray(times), np.asarray(event_times)
    right = np.clip(np.searchsorted(t, et), 0, len(t) - 1)
    left = np.maximum(right - 1, 0)
    index = np.where(abs(t[left] - et) < abs(t[right] - et), left, right)
    delta = t[index] - et
    index[np.abs(delta) > max_delta] = -1
    output = np.full((len(t), *np.asarray(values).shape[1:]), np.nan)
    for j in np.argsort(-np.abs(delta)):
        if index[j] >= 0:
            output[index[j]] = values[j]
    return output, index
