"""SONY Spresense GPS positions and quality-aware camera-time interpolation."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt

WGS84_A_M = 6_378_137.0
WGS84_E2 = 6.6943799901413165e-3


def geodetic_to_enu(
    latitude_deg: np.ndarray,
    longitude_deg: np.ndarray,
    altitude_m: np.ndarray,
    origin_latitude_deg: float,
    origin_longitude_deg: float,
    origin_altitude_m: float,
) -> np.ndarray:
    """WGS84 geodetic coordinates to local east/north/up metres."""
    lat = np.deg2rad(latitude_deg)
    lon = np.deg2rad(longitude_deg)
    lat0 = np.deg2rad(origin_latitude_deg)
    lon0 = np.deg2rad(origin_longitude_deg)

    def ecef(phi: np.ndarray, lam: np.ndarray, height: np.ndarray) -> np.ndarray:
        radius = WGS84_A_M / np.sqrt(1 - WGS84_E2 * np.sin(phi) ** 2)
        return np.column_stack([
            (radius + height) * np.cos(phi) * np.cos(lam),
            (radius + height) * np.cos(phi) * np.sin(lam),
            (radius * (1 - WGS84_E2) + height) * np.sin(phi),
        ])

    xyz = ecef(lat, lon, altitude_m)
    origin = ecef(np.array([lat0]), np.array([lon0]), np.array([origin_altitude_m]))[0]
    difference = xyz - origin
    east = -np.sin(lon0) * difference[:, 0] + np.cos(lon0) * difference[:, 1]
    north = (-np.sin(lat0) * np.cos(lon0) * difference[:, 0]
             - np.sin(lat0) * np.sin(lon0) * difference[:, 1]
             + np.cos(lat0) * difference[:, 2])
    up = (np.cos(lat0) * np.cos(lon0) * difference[:, 0]
          + np.cos(lat0) * np.sin(lon0) * difference[:, 1]
          + np.sin(lat0) * difference[:, 2])
    return np.column_stack([east, north, up])


def classify_gps(
    times_s: np.ndarray,
    enu_m: np.ndarray,
    fix: np.ndarray,
    satellites: np.ndarray,
    *,
    max_isolated_speed_m_s: float = 2.0,
    quiet_neighbor_speed_m_s: float = 0.75,
    max_interpolation_gap_s: float = 2.0,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Retain raw fixes, segment isolated jumps and gaps, and expose reasons."""
    if len(times_s) < 2 or np.any(np.diff(times_s) <= 0):
        raise ValueError("GPS timestamps must be strictly increasing")
    valid = ((fix >= 3) & (satellites >= 7) &
             np.isfinite(enu_m).all(axis=1))
    delta_t = np.diff(times_s)
    step_speed = np.full(len(times_s), np.nan)
    step_speed[1:] = np.linalg.norm(np.diff(enu_m[:, :2], axis=0), axis=1) / delta_t
    jump = np.zeros(len(times_s), dtype=bool)
    for index in range(2, len(times_s) - 1):
        if (valid[index - 1] and valid[index] and
                step_speed[index] > max_isolated_speed_m_s and
                step_speed[index - 1] < quiet_neighbor_speed_m_s and
                step_speed[index + 1] < quiet_neighbor_speed_m_s):
            jump[index] = True
    segment = np.full(len(times_s), -1, dtype=int)
    status = np.full(len(times_s), "invalid_fix", dtype="U25")
    current = -1
    previous = None
    for index in range(len(times_s)):
        if not valid[index]:
            previous = None
            continue
        if (previous is None or jump[index] or
                times_s[index] - times_s[previous] > max_interpolation_gap_s):
            current += 1
        segment[index] = current
        status[index] = "segment_start_jump" if jump[index] else "valid"
        previous = index
    return segment, status, step_speed, jump


def interpolate_gps_segments(
    gps_times_s: np.ndarray,
    gps_enu_m: np.ndarray,
    gps_segment: np.ndarray,
    target_times_s: np.ndarray,
    *,
    max_interpolation_gap_s: float = 2.0,
) -> tuple[np.ndarray, np.ndarray]:
    """Interpolate horizontal GPS only within one trusted fix segment."""
    output = np.full((len(target_times_s), 2), np.nan)
    segment = np.full(len(target_times_s), -1, dtype=int)
    upper = np.searchsorted(gps_times_s, target_times_s, side="left")
    for index, right in enumerate(upper):
        if right == 0 or right >= len(gps_times_s):
            continue
        left = right - 1
        span = gps_times_s[right] - gps_times_s[left]
        if (gps_segment[left] < 0 or gps_segment[left] != gps_segment[right] or
                span > max_interpolation_gap_s):
            continue
        fraction = (target_times_s[index] - gps_times_s[left]) / span
        output[index] = (1 - fraction) * gps_enu_m[left, :2] + (
            fraction * gps_enu_m[right, :2]
        )
        segment[index] = gps_segment[left]
    return output, segment


def _read_gps(path: Path) -> dict[str, np.ndarray]:
    required = ("window_time_s", "sync_utc_us", "flags", "fix", "satellites",
                "lat_e7", "lon_e7", "altitude_mm")
    values = {key: [] for key in required}
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if set(required) - set(reader.fieldnames or ()):
            raise ValueError(f"{path}: GPS columns missing")
        for row in reader:
            for key in required:
                values[key].append(float(row[key]))
    result = {key: np.asarray(value, dtype=np.float64) for key, value in values.items()}
    if not result[required[0]].size or not all(np.isfinite(v).all() for v in result.values()):
        raise ValueError(f"{path}: empty or nonfinite GPS data")
    return result


def _write_csv(path: Path, columns: dict[str, np.ndarray]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        writer.writerows(zip(*columns.values(), strict=True))


def produce_parent_gps_position(
    gps_path: Path,
    output_dir: Path,
    camera_times_s: np.ndarray,
    camera_utc_us: np.ndarray,
    imu_local_xy_m: np.ndarray,
) -> dict[str, object]:
    gps = _read_gps(gps_path)
    latitude = gps["lat_e7"] / 1e7
    longitude = gps["lon_e7"] / 1e7
    altitude = gps["altitude_mm"] / 1e3
    candidate = ((gps["fix"] >= 3) & (gps["satellites"] >= 7) &
                 (np.abs(latitude) <= 90) & (np.abs(longitude) <= 180) &
                 ((latitude != 0) | (longitude != 0)))
    if not np.any(candidate):
        raise ValueError(f"{gps_path}: no usable 3D GPS fix")
    origin_index = int(np.flatnonzero(candidate)[0])
    initial = (float(latitude[origin_index]), float(longitude[origin_index]),
               float(altitude[origin_index]))
    enu = geodetic_to_enu(latitude, longitude, altitude, *initial)
    plausible_coordinates = ((np.abs(latitude) <= 90) & (np.abs(longitude) <= 180) &
                             ((latitude != 0) | (longitude != 0)))
    enu[~plausible_coordinates] = np.nan
    times = gps["window_time_s"]
    segment, status, speed, jump = classify_gps(
        times, enu, gps["fix"], gps["satellites"]
    )
    xy, frame_segment = interpolate_gps_segments(times, enu, segment, camera_times_s)
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(output_dir / "parent_gps_observations.csv", {
        "window_time_s": times,
        "sync_utc_us": gps["sync_utc_us"].astype(np.int64),
        "latitude_deg": latitude,
        "longitude_deg": longitude,
        "altitude_m": altitude,
        "east_m": enu[:, 0], "north_m": enu[:, 1], "up_m": enu[:, 2],
        "fix": gps["fix"].astype(int),
        "satellites": gps["satellites"].astype(int),
        "flags": gps["flags"].astype(int),
        "step_speed_m_s": speed,
        "segment_id": segment,
        "status": status,
    })
    _write_csv(output_dir / "parent_gps_position.csv", {
        "window_time_s": camera_times_s,
        "sync_utc_us": camera_utc_us,
        "east_m": xy[:, 0], "north_m": xy[:, 1],
        "up_m": np.full(len(xy), np.nan),
        "segment_id": frame_segment,
        "status": np.where(frame_segment >= 0, "gps_interpolated", "unavailable"),
    })
    figure, axes = plt.subplots(1, 2, figsize=(11, 5))
    for number in np.unique(segment[segment >= 0]):
        mask = segment == number
        axes[0].plot(enu[mask, 0], enu[mask, 1], ".-", label=f"GPS segment {number}")
    axes[0].set(xlabel="East from first GPS fix (m)", ylabel="North (m)",
                title="SONY GPS horizontal observations")
    axes[0].legend()
    axes[1].plot(imu_local_xy_m[:, 0], imu_local_xy_m[:, 1], label="IMU only")
    axes[1].set(xlabel="Local X (m)", ylabel="Local Y (m)",
                title="Uncorrected IMU integral (different heading)")
    for axis in axes:
        axis.axis("equal")
        axis.grid(True)
    figure.suptitle("Different axes and scales; no direct accuracy comparison")
    figure.tight_layout()
    figure.savefig(output_dir / "parent_gps_imu_diagnostic.png", dpi=150)
    plt.close(figure)
    gps_valid = segment >= 0
    metadata = {
        "source": str(gps_path),
        "method": "GPS fixes converted to WGS84 ENU; horizontal linear interpolation within segments",
        "imu_horizontal_used": False,
        "imu_exclusion_reason": "camera-world heading relative to ENU is unverified",
        "origin_latitude_deg": initial[0],
        "origin_longitude_deg": initial[1],
        "origin_altitude_m": initial[2],
        "gps_samples": len(times),
        "valid_fixes": int(gps_valid.sum()),
        "estimated_rate_hz": float(1 / np.median(np.diff(times))),
        "segments": int(np.max(segment) + 1),
        "isolated_jumps": [{"window_time_s": float(times[i]),
                             "step_speed_m_s": float(speed[i])} for i in np.flatnonzero(jump)],
        "interpolated_camera_frames": int(np.sum(frame_segment >= 0)),
        "total_camera_frames": len(camera_times_s),
        "gps_horizontal_span_east_m": float(np.ptp(enu[gps_valid, 0])),
        "gps_horizontal_span_north_m": float(np.ptp(enu[gps_valid, 1])),
        "gps_altitude_span_m": float(np.ptp(altitude[gps_valid])),
        "position_reference": "SONY GPS antenna; camera/body lever arms uncalibrated",
        "vertical_policy": "raw GPS altitude retained for diagnostics; camera-time up is unavailable",
        "jump_rule": "step >2 m/s with both adjacent steps <0.75 m/s",
        "no_ground_truth": True,
    }
    (output_dir / "parent_gps_manifest.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return metadata
