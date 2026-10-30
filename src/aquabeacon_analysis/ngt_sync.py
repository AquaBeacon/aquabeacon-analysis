"""IMU-based camera clocks for explicitly selected NGT acquisitions.

Video PTS is the primary relative time. Creation tags are search hints only.
The sign convention is utc_s = video_time_s + utc_at_video_zero_s.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import numpy as np
from scipy.ndimage import uniform_filter1d

from aquabeacon_analysis.imu_localization import camera_angular_velocity
from aquabeacon_analysis.time_sync import _djmd_samples, _parse_djmd


def extract_telemetry(source: Path, destination: Path) -> dict:
    """Read original DJI telemetry without trusting its calendar clock."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    first = last = None
    with destination.open("w", newline="", encoding="utf-8") as handle:
        writer = None
        for pts, packet in _djmd_samples(source):
            row = {"video_time_s": pts, **_parse_djmd(packet)}
            if writer is None:
                writer = csv.DictWriter(handle, fieldnames=list(row))
                writer.writeheader()
                first = pts
            writer.writerow(row)
            count += 1
            last = pts
    result = {"source": str(source), "destination": str(destination), "rows": count,
              "first_video_time_s": first, "last_video_time_s": last,
              "time_basis": "djmd sample presentation timestamp; no UTC correction yet"}
    destination.with_suffix(".json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def numeric_csv(path: Path, columns: list[str]) -> np.ndarray:
    """Load only numeric columns; keep missing fields as NaN."""
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader)
        indices = [header.index(name) for name in columns]
        rows = [[float(row[i]) if row[i] else np.nan for i in indices] for row in reader]
    return np.asarray(rows, dtype=float).reshape(-1, len(columns))


def sample_with_gaps(times: np.ndarray, values: np.ndarray, query: np.ndarray,
                     max_gap_s: float = 0.1) -> np.ndarray:
    """Interpolate only inside valid brackets; never bridge outages."""
    times, unique = np.unique(times, return_index=True)
    values = values[unique]
    if len(times) < 2:
        return np.full((len(query),) + values.shape[1:], np.nan)
    idx = np.clip(np.searchsorted(times, query, side="right"), 1, len(times) - 1)
    ok = ((query >= times[0]) & (query <= times[-1])
          & ((times[idx] - times[idx - 1]) <= max_gap_s))
    fraction = (query - times[idx - 1]) / (times[idx] - times[idx - 1])
    if values.ndim == 2:
        fraction = fraction[:, None]
    result = values[idx - 1] + fraction * (values[idx] - values[idx - 1])
    result[~ok] = np.nan
    return result


def angular_signal(camera: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Camera quaternion derivative in its own IMU axes, rad/s."""
    times, quaternions = camera[:, 0], camera[:, 1:5]
    valid = np.isfinite(quaternions).all(axis=1) & (np.linalg.norm(quaternions, axis=1) > .5)
    times, quaternions = times[valid], quaternions[valid]
    quaternions /= np.linalg.norm(quaternions, axis=1)[:, None]
    rates = camera_angular_velocity(quaternions, times)
    rates[np.diff(times) > .1] = np.nan
    return (times[:-1] + times[1:]) / 2, rates


def correlation_offset(camera_t: np.ndarray, camera_signal: np.ndarray,
                       body_t: np.ndarray, body_signal: np.ndarray,
                       camera_window_s: tuple[float, float], offsets_s: np.ndarray,
                       rate_hz: float = 30., highpass_s: float | None = None) -> dict:
    """Search body_time = camera_time + offset using scalar IMU activity.

Time arguments should be near a common epoch to avoid large floating values.
The mean and scale are removed per candidate on exactly its valid samples.
"""
    grid = np.arange(*camera_window_s, 1 / rate_hz)
    cam = sample_with_gaps(camera_t, camera_signal, grid, .15)
    scores, coverage = [], []
    for offset in offsets_s:
        body = sample_with_gaps(body_t, body_signal, grid + offset, .15)
        a, b = cam.copy(), body.copy()
        mask = np.isfinite(a) & np.isfinite(b)
        if highpass_s:
            width = max(3, round(rate_hz * highpass_s))
            a -= uniform_filter1d(np.nan_to_num(a), width)
            b -= uniform_filter1d(np.nan_to_num(b), width)
        coverage.append(float(mask.mean()))
        if mask.sum() < 100 or mask.mean() < .8:
            scores.append(np.nan)
        else:
            av, bv = a[mask], b[mask]
            if np.std(av) < 1e-6 or np.std(bv) < 1e-6:
                scores.append(np.nan)
            else:
                scores.append(float(np.corrcoef(av, bv)[0, 1]))
    scores = np.asarray(scores)
    if not np.isfinite(scores).any():
        raise ValueError("No valid overlapping motion window")
    best = int(np.nanargmax(scores))
    outside_peak = np.abs(offsets_s - offsets_s[best]) > 1.
    competitor = float(np.nanmax(scores[outside_peak])) if outside_peak.any() else None
    return {"offset_s": float(offsets_s[best]), "correlation": float(scores[best]),
            "coverage": coverage[best], "window_s": list(camera_window_s),
            "competing_correlation_outside_1s": competitor,
            "at_search_boundary": best in (0, len(offsets_s) - 1),
            "offsets_s": offsets_s.tolist(), "scores": scores.tolist()}


def fit_rotation(source: np.ndarray, target: np.ndarray) -> tuple[np.ndarray, dict]:
    """Proper Wahba rotation, with centering to tolerate constant gyro bias."""
    valid = np.isfinite(source).all(axis=1) & np.isfinite(target).all(axis=1)
    source, target = source[valid], target[valid]
    valid = (np.linalg.norm(source, axis=1) > .05) & (np.linalg.norm(target, axis=1) > .05)
    source, target = source[valid], target[valid]
    if len(source) < 100:
        raise ValueError("Insufficient rotational excitation")
    u, s, vt = np.linalg.svd((source - source.mean(0)).T @ (target - target.mean(0)))
    rotation = vt.T @ np.diag([1, 1, np.linalg.det(vt.T @ u.T)]) @ u.T
    prediction = source @ rotation.T
    return rotation, {"samples": len(source), "singular_values": s.tolist(),
                      "singular_ratio": float(s[-1] / s[0]),
                      "correlation": float(np.corrcoef(prediction.ravel(), target.ravel())[0, 1]),
                      "rms_rad_s": float(np.sqrt(np.mean((prediction - target) ** 2)))}
