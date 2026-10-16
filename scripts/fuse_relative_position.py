"""Robust batch fusion of acoustic-light positions, child IMU, and pressure depth."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import subprocess
from pathlib import Path

import numpy as np
from matplotlib import pyplot as plt
from scipy.signal import savgol_filter
from scipy.sparse import csr_matrix
from scipy.sparse.linalg import lsqr
from scipy.spatial.transform import Rotation


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def detect_immersion_reference(rows: list[dict[str, str]]) -> dict[str, float | int]:
    """Find the first sustained pressure rise above the initial air baseline."""
    times = np.array([float(row["experiment_time_s"]) for row in rows])
    pressure = np.array([float(row["pressure_mbar"]) for row in rows])
    initial = pressure[(times >= 0) & (times <= 60)]
    if len(initial) < 20:
        raise ValueError("insufficient pre-immersion pressure data")
    coarse = float(np.median(initial))
    onset_index = None
    for index in np.flatnonzero((times >= 30) & (pressure >= coarse + 8)):
        next_7 = pressure[index:index + 7]
        next_30 = pressure[(times >= times[index]) & (times <= times[index] + 30)]
        if len(next_7) == 7 and (next_7 >= coarse + 8).sum() >= 5 \
                and len(next_30) and np.max(next_30) >= coarse + 30:
            onset_index = int(index)
            break
    if onset_index is None:
        raise ValueError("sustained immersion onset not detected")
    baseline_samples = pressure[(times >= 0) & (times < times[onset_index] - 10)]
    if len(baseline_samples) < 20:
        raise ValueError("insufficient air baseline before immersion")
    baseline = float(np.median(baseline_samples))
    mad = float(np.median(abs(baseline_samples - baseline)))
    return {"onset_experiment_time_s": float(times[onset_index]),
            "onset_sync_utc_us": int(rows[onset_index]["sync_utc_us"]),
            "surface_pressure_mbar": baseline,
            "pre_immersion_mad_mbar": mad,
            "pre_immersion_samples": len(baseline_samples)}


def keep_strictly_increasing(times_s: np.ndarray) -> np.ndarray:
    previous_max = np.r_[-np.inf, np.maximum.accumulate(times_s[:-1])]
    return times_s > previous_max


def interpolate_series(times: np.ndarray, values: np.ndarray, target: np.ndarray, max_gap: float) -> np.ndarray:
    """Linear interpolation only inside source coverage and bounded gaps."""
    output = np.full((len(target),) + values.shape[1:], np.nan)
    hi = np.searchsorted(times, target, side="right")
    valid = (hi > 0) & (hi < len(times))
    indices = np.flatnonzero(valid)
    for index in indices:
        lo = hi[index] - 1
        if times[hi[index]] - times[lo] > max_gap:
            continue
        fraction = (target[index] - times[lo]) / (times[hi[index]] - times[lo])
        output[index] = values[lo] + fraction * (values[hi[index]] - values[lo])
    return output


def optical_observations(
    rows: list[dict[str, str]], angle_rows: list[dict[str, str]],
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Use the published attitude-corrected light bearing and acoustic range."""
    if len(rows) != len(angle_rows):
        raise ValueError("position and angle row counts differ")
    observations = np.full((len(rows), 3), np.nan)
    qc = np.zeros(len(rows), dtype=bool)
    for index, (row, angle) in enumerate(zip(rows, angle_rows, strict=True)):
        if abs(float(row["window_time_s"]) - float(angle["window_time_s"])) > 0.02:
            raise ValueError("position and attitude-corrected angle clocks disagree")
        if row["range_qc"] != "reported_valid" or angle["angle_status"] not in {
            "conditional_mount_and_fov", "calibrated_intrinsics_provisional_mount"
        }:
            continue
        az = float(angle["local_azimuth_rad"])
        el = float(angle["gravity_elevation_rad"])
        distance = float(row["range_m"])
        observations[index] = distance * np.array([math.cos(el) * math.cos(az),
                                                   math.cos(el) * math.sin(az), math.sin(el)])
        qc[index] = True
    return np.array([float(row["window_time_s"]) for row in rows]), observations, qc


def pressure_referenced_observations(
    optical: np.ndarray, depth_m: np.ndarray, parent_camera_depth_m: float,
) -> np.ndarray:
    """Put light/ToF horizontal observations at pressure-referenced child height."""
    return np.column_stack((optical[:, :2], parent_camera_depth_m - depth_m))


def estimate_yaw(
    times: np.ndarray, observations: np.ndarray, valid: np.ndarray,
    imu_acceleration: np.ndarray,
) -> tuple[float, float]:
    """Fit child-to-parent horizontal yaw from low-frequency acceleration, if observable."""
    if valid.sum() < 25:
        return 0.0, 0.0
    dt = float(np.median(np.diff(times)))
    if dt <= 0:
        return 0.0, 0.0
    window = min(21, len(times) if len(times) % 2 else len(times) - 1)
    if window < 7:
        return 0.0, 0.0
    x = np.interp(times, times[valid], observations[valid, 0])
    y = np.interp(times, times[valid], observations[valid, 1])
    optical_acc = np.column_stack([
        savgol_filter(x, window, 3, deriv=2, delta=dt),
        savgol_filter(y, window, 3, deriv=2, delta=dt),
    ])
    imu_acc = np.column_stack([
        savgol_filter(
            np.interp(
                times, times[np.isfinite(imu_acceleration[:, axis])],
                imu_acceleration[np.isfinite(imu_acceleration[:, axis]), axis],
            ), window, 3
        ) for axis in range(2)
    ])
    active = valid & np.all(np.isfinite(imu_acc), axis=1)
    a = optical_acc[active] - optical_acc[active].mean(axis=0)
    b = imu_acc[active] - imu_acc[active].mean(axis=0)
    denominator = float(np.sqrt(np.sum(a**2) * np.sum(b**2)))
    if denominator < 1e-10:
        return 0.0, 0.0
    dot = float(np.sum(a[:, 0] * b[:, 0] + a[:, 1] * b[:, 1]))
    cross = float(np.sum(a[:, 1] * b[:, 0] - a[:, 0] * b[:, 1]))
    coherence = math.hypot(dot, cross) / denominator
    return math.atan2(cross, dot), coherence


def solve_axis(
    times: np.ndarray, observed: np.ndarray, acceleration: np.ndarray,
    pressure_target: np.ndarray | None = None, pressure_sigma: float = 1.0,
    observed_sigma: float = 0.25,
) -> np.ndarray:
    """IRLS smoother with optical, acceleration, weak velocity, and optional depth factors."""
    count = len(times)
    rows: list[tuple[list[int], list[float], float, float, str]] = []
    for i, value in enumerate(observed):
        if np.isfinite(value):
            rows.append(([i], [1.0], float(value), observed_sigma, "optical"))
    if pressure_target is not None:
        for i, value in enumerate(pressure_target):
            if np.isfinite(value):
                rows.append(([i], [1.0], float(value), pressure_sigma, "pressure"))
    for i in range(1, count - 1):
        left, right = times[i] - times[i - 1], times[i + 1] - times[i]
        if max(left, right) > 1.2 or not np.isfinite(acceleration[i]):
            continue
        factor = 2.0 / (left + right)
        rows.append(([i - 1, i, i + 1],
                     [factor / left, -factor * (1 / left + 1 / right), factor / right],
                     float(acceleration[i]), 1.5, "imu"))
    for i in range(count - 1):
        dt = times[i + 1] - times[i]
        if dt <= 1.2:
            rows.append(([i, i + 1], [-1 / dt, 1 / dt], 0.0, 2.0, "velocity"))
    if not np.isfinite(observed).any():
        raise ValueError("no optical positions for axis")
    weights = np.ones(len(rows))
    solution = np.zeros(count)
    for _ in range(4):
        r_idx, c_idx, values, target = [], [], [], []
        for j, (indices, coefficients, rhs, sigma, _) in enumerate(rows):
            scale = math.sqrt(weights[j]) / sigma
            for index, coefficient in zip(indices, coefficients, strict=True):
                r_idx.append(j)
                c_idx.append(index)
                values.append(coefficient * scale)
            target.append(rhs * scale)
        matrix = csr_matrix((values, (r_idx, c_idx)), shape=(len(rows), count))
        solution = lsqr(matrix, np.array(target), atol=1e-8, btol=1e-8, iter_lim=3000)[0]
        for j, (indices, coefficients, rhs, sigma, kind) in enumerate(rows):
            if kind not in {"optical", "pressure"}:
                continue
            residual = abs(sum(solution[i] * c for i, c in zip(indices, coefficients, strict=True)) - rhs)
            weights[j] = min(1.0, 2.5 * sigma / max(residual, 1e-12))
    return solution


def heldout_optical_check(
    times: np.ndarray, observed: np.ndarray, valid: np.ndarray,
    acceleration: np.ndarray, depth_at_grid: np.ndarray,
    parent_camera_depth_m: float, pressure_sigma_m: float = 0.03,
    use_pressure: bool = True,
) -> dict[str, float | int | str]:
    """Compare every fifth withheld optical point with linear interpolation and fusion."""
    heldout = np.flatnonzero(valid)[::5]
    training = valid.copy()
    training[heldout] = False
    training_observed = observed.copy()
    training_observed[~training] = np.nan
    target = parent_camera_depth_m - depth_at_grid
    predicted = np.column_stack([
        solve_axis(times, training_observed[:, axis], acceleration[:, axis],
                   target if use_pressure and axis == 2 else None, pressure_sigma_m)
        for axis in range(3)
    ])
    baseline = np.column_stack([
        np.interp(times, times[training], observed[training, axis]) for axis in range(3)
    ])
    fused_error = np.linalg.norm(predicted[heldout] - observed[heldout], axis=1)
    baseline_error = np.linalg.norm(baseline[heldout] - observed[heldout], axis=1)
    return {
        "heldout_optical_points": len(heldout),
        "fused_mean_disagreement_m": float(np.mean(fused_error)),
        "fused_median_disagreement_m": float(np.median(fused_error)),
        "linear_mean_disagreement_m": float(np.mean(baseline_error)),
        "linear_median_disagreement_m": float(np.median(baseline_error)),
        "interpretation": "agreement with withheld uncalibrated optical observations, not ground-truth accuracy",
    }


def render_diagnostic(
    destination: Path, times: np.ndarray, observed: np.ndarray, fused: np.ndarray,
    published: np.ndarray, depth_target: np.ndarray,
) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.5))
    valid = np.all(np.isfinite(observed), axis=1)
    displayed = fused.copy()
    displayed[~published] = np.nan
    axes[0].plot(displayed[:, 0], displayed[:, 1], color="#155e75", lw=1.5,
                 label="Fused (conditional)")
    axes[0].scatter(observed[valid, 0], observed[valid, 1], s=8, alpha=0.5,
                    color="#d97706", label="ToF + light observations")
    axes[0].set(xlabel="Local x [m]", ylabel="Local y [m]", title="Horizontal relative position")
    axes[0].axis("equal")
    axes[0].grid(alpha=0.25)
    axes[0].legend(fontsize=8)
    axes[1].plot(times, displayed[:, 2], color="#155e75", lw=1.5,
                 label="Fused up")
    depth_display = depth_target.copy()
    depth_display[~published] = np.nan
    axes[1].plot(times, depth_display, color="#7c3aed",
                 lw=1, alpha=0.7, label="Pressure-referenced observation up")
    axes[1].set(xlabel="ToF window time [s]", ylabel="Relative up [m]",
                title="Vertical position in parent-camera frame")
    axes[1].grid(alpha=0.25)
    axes[1].legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(destination, dpi=150)
    plt.close(fig)


def fuse(experiment: str, root: Path, variant: str = "") -> dict[str, object]:
    result = root / "results" / experiment
    directory = result / (variant or "relative_position")
    optical_path = directory / "parent_child_position.csv"
    angle_path = (directory / "attitude_corrected_light_angles.csv" if variant
                  else result / "light_detection" / "attitude_corrected_light_angles.csv")
    pressure_path = result / "tof_depth_comparison" / "paired_observations.csv"
    interim_pressure_path = next((root / "data" / "interim" / experiment / "c_main").glob("C_MAIN_HEALTH_RANGE_*.csv"))
    child_path = next((root / "data" / "processed" / experiment / "tof_window" / "c_main").glob("C_MAIN_IMU_100Hz_*.csv"))
    fusion_config_path = root / "calibration" / "relative_fusion_provisional.json"
    input_paths = [
        optical_path, directory / "manifest.json",
        angle_path, (directory / "attitude_angle_manifest.json" if variant
                     else result / "light_detection" / "attitude_angle_manifest.json"),
        pressure_path, interim_pressure_path,
        result / "tof_depth_comparison" / "summary.json",
        child_path, fusion_config_path,
    ]
    rows = read_rows(optical_path)
    angles = read_rows(angle_path)
    fusion_config = json.loads(fusion_config_path.read_text(encoding="utf-8"))
    if fusion_config["status"] != "hypothetical_not_measured":
        raise ValueError("relative fusion geometry must be marked provisional")
    times, observed, optical_valid = optical_observations(rows, angles)
    immersion = detect_immersion_reference(read_rows(interim_pressure_path))
    pressure = read_rows(pressure_path)
    pressure_times = np.array([float(row["window_time_s"]) for row in pressure])
    depths = (np.array([float(row["pressure_mbar"]) for row in pressure])
              - immersion["surface_pressure_mbar"]) * 100 / (1025 * 9.80665)
    depth_at_grid = interpolate_series(pressure_times, depths, times, 2.0)
    child = read_rows(child_path)
    raw_times = np.array([float(row["window_time_s"]) for row in child])
    keep = keep_strictly_increasing(raw_times)
    child = [row for row, valid in zip(child, keep, strict=True) if valid]
    child_times = raw_times[keep]
    child_acc = np.array([[float(row[k]) for k in ("ax", "ay", "az")] for row in child])
    child_q = Rotation.from_quat(np.array([[float(row[k]) for k in ("qx", "qy", "qz", "qw")] for row in child]))
    world_acc = child_q.apply(child_acc) - [0.0, 0.0, 9.80665]
    acceleration = interpolate_series(child_times, world_acc, times, 0.1)
    yaw, coherence = estimate_yaw(times, observed, optical_valid, acceleration)
    horizontal_imu_used = coherence >= fusion_config["horizontal_imu_yaw_coherence_min"]
    if horizontal_imu_used:
        c, s = math.cos(yaw), math.sin(yaw)
        acceleration[:, :2] = acceleration[:, :2] @ np.array([[c, s], [-s, c]])
    else:
        acceleration[:, :2] = np.nan
    depth_match = optical_valid & np.isfinite(depth_at_grid)
    if depth_match.sum() < 10:
        raise ValueError("too few common optical and pressure samples")
    parent_camera_depth_assumed = fusion_config["parent_camera_depth_below_surface_m"]
    optical_pressure_disagreement = (
        observed[depth_match, 2] + depth_at_grid[depth_match] - parent_camera_depth_assumed
    )
    pressure_bias = float(np.median(optical_pressure_disagreement))
    pressure_offset_mad = float(np.median(abs(optical_pressure_disagreement - pressure_bias)))
    pressure_sigma = fusion_config["pressure_depth_constraint_sigma_m"]
    pressure_target = parent_camera_depth_assumed - depth_at_grid
    display_observed = pressure_referenced_observations(
        observed, depth_at_grid, parent_camera_depth_assumed
    )
    validation = heldout_optical_check(
        times, observed, optical_valid, acceleration, depth_at_grid,
        parent_camera_depth_assumed, pressure_sigma,
    )
    validation_without_pressure = heldout_optical_check(
        times, observed, optical_valid, acceleration, depth_at_grid,
        parent_camera_depth_assumed, pressure_sigma,
        use_pressure=False,
    )
    pressure_used = True
    raw_geometry_comparable = optical_valid & np.isfinite(depth_at_grid)
    raw_geometry_conflicts = sum(
        float(row["range_m"]) + fusion_config["tof_endpoint_offset_allowance_m_for_diagnostic"]
        < depth_at_grid[index] - parent_camera_depth_assumed
        for index, row in enumerate(rows) if raw_geometry_comparable[index]
    )
    fused = np.column_stack([
        solve_axis(times, observed[:, axis], acceleration[:, axis],
                   pressure_target if axis == 2 else None, pressure_sigma)
        for axis in range(3)
    ])
    nearest_observation_s = np.array([
        np.min(abs(times[optical_valid] - t)) for t in times
    ])
    # Do not publish a long IMU-only extrapolation as a measured position.
    published = (nearest_observation_s <= fusion_config["max_time_from_optical_observation_s"]
                 ) & np.isfinite(depth_at_grid)
    output_rows = []
    for index, row in enumerate(rows):
        status = "observed_and_fused" if optical_valid[index] else "short_gap_fused"
        if not published[index]:
            status = "pressure_or_light_gap_unavailable"
        geometry_conflict = (
            optical_valid[index] and np.isfinite(depth_at_grid[index])
            and float(row["range_m"]) + fusion_config["tof_endpoint_offset_allowance_m_for_diagnostic"]
            < depth_at_grid[index] - parent_camera_depth_assumed
        )
        result_row: dict[str, object] = {
            "experiment_id": experiment, "video_time_s": row["video_time_s"],
            "window_time_s": row["window_time_s"], "sync_utc_us": row["sync_utc_us"],
            "status": status, "light_status": row["light_status"],
            "range_qc": row["range_qc"], "range_m": row["range_m"],
            "depth_range_geometry_conflict": (
                int(geometry_conflict) if optical_valid[index] and np.isfinite(depth_at_grid[index])
                else ""
            ),
            "pressure_depth_m": f"{depth_at_grid[index]:.6f}" if np.isfinite(depth_at_grid[index]) else "",
            "optical_x_m": f"{observed[index, 0]:.6f}" if optical_valid[index] else "",
            "optical_y_m": f"{observed[index, 1]:.6f}" if optical_valid[index] else "",
            "optical_z_up_m": f"{observed[index, 2]:.6f}" if optical_valid[index] else "",
            "pressure_referenced_observation_x_m": (
                f"{display_observed[index, 0]:.6f}"
                if optical_valid[index] and np.isfinite(display_observed[index, 2]) else ""
            ),
            "pressure_referenced_observation_y_m": (
                f"{display_observed[index, 1]:.6f}"
                if optical_valid[index] and np.isfinite(display_observed[index, 2]) else ""
            ),
            "pressure_referenced_observation_z_up_m": (
                f"{display_observed[index, 2]:.6f}"
                if optical_valid[index] and np.isfinite(display_observed[index, 2]) else ""
            ),
            "fused_x_m": f"{fused[index, 0]:.6f}" if published[index] else "",
            "fused_y_m": f"{fused[index, 1]:.6f}" if published[index] else "",
            "fused_z_up_m": f"{fused[index, 2]:.6f}" if published[index] else "",
            "optical_fused_distance_m": (
                f"{np.linalg.norm(fused[index] - observed[index]):.6f}"
                if published[index] and optical_valid[index] else ""
            ),
            "pressure_fused_z_residual_m": (
                f"{fused[index, 2] - pressure_target[index]:.6f}"
                if published[index] and np.isfinite(pressure_target[index]) else ""
            ),
            "nearest_optical_time_s": f"{nearest_observation_s[index]:.6f}",
        }
        output_rows.append(result_row)
    pressure_rows = [
        {"experiment_id": experiment, "window_time_s": row["window_time_s"],
         "sync_utc_us": row["sync_utc_us"], "pressure_mbar": row["pressure_mbar"],
         "surface_pressure_mbar": f"{immersion['surface_pressure_mbar']:.6f}",
         "pressure_depth_immersion_m": f"{depth:.6f}"}
        for row, depth in zip(pressure, depths, strict=True)
    ]
    with (directory / "pressure_depth_immersion.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(pressure_rows[0]))
        writer.writeheader()
        writer.writerows(pressure_rows)
    output_path = directory / "fused_position.csv"
    with output_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(output_rows[0]))
        writer.writeheader()
        writer.writerows(output_rows)
    render_diagnostic(
        directory / "fused_comparison.png", times, observed, fused, published,
        pressure_target,
    )
    metadata = {
        "experiment_id": experiment, "samples": len(rows),
        "optical_observations": int(optical_valid.sum()),
        "published_fused_samples": int(published.sum()),
        "parent_camera_depth_m_assumed": parent_camera_depth_assumed,
        "pressure_immersion_reference": immersion,
        "pressure_optical_median_disagreement_m": pressure_bias,
        "pressure_optical_disagreement_mad_m": pressure_offset_mad,
        "pressure_sigma_m": pressure_sigma,
        "pressure_used_in_position": pressure_used,
        "pressure_policy": "user-provided reliability ordering: immersion-referenced pressure is primary for vertical position despite optical/ToF conflict",
        "vertical_model": "parent camera depth minus C_MAIN pressure depth; C_MAIN pressure sensor and child light assumed at equal height",
        "horizontal_model": ("ToF times cosine of underwater-calibrated light elevation and local azimuth"
                             if variant else "ToF times cosine of provisional light elevation and local azimuth")
                            + "; not a geometrically consistent slant range where pressure conflicts",
        "display_observation": "pressure_referenced_observation_* combines ToF+light horizontal x/y with immersion-referenced pressure z in the same parent-camera frame as fused_*; optical_z_up_m preserves the conditional light-elevation diagnostic",
        "raw_depth_vs_range_geometry_conflicts_with_0p2m_allowance": int(raw_geometry_conflicts),
        "child_light_camera_separation_m_user_approximate": fusion_config["child_light_camera_separation_m"],
        "child_light_to_acoustic_receiver_vector": "unknown; no lever-arm correction applied",
        "pressure_reference": "pre-immersion median from interim C_MAIN pressure; depth=(pressure-surface)*100/(1025*9.80665) m",
        "child_imu_yaw_fit_rad": yaw,
        "child_imu_yaw_coherence": coherence,
        "horizontal_imu_used": horizontal_imu_used,
        "heldout_check": validation,
        "heldout_check_without_pressure": validation_without_pressure,
        "child_imu_nonincreasing_rows_excluded": int((~keep).sum()),
        "camera_intrinsics_status": "provisional_underwater_calibrated" if variant else "hypothetical_not_measured",
        "optical_to_imu_status": "hypothetical_not_measured",
        "parent_motion": "fixed-position assumption; camera depth 0.03 m hypothetical",
        "smoother": "IRLS weighted least squares; optical sigma 0.25m, pressure depth sigma 0.03m, IMU acceleration sigma 1.5m/s2, weak velocity sigma 2m/s; pressure is the vertical primary observation",
        "long_gap_rule": "fused position suppressed beyond configured maximum from nearest valid optical observation",
        "input_sha256": {str(path.relative_to(root)): hash_file(path) for path in input_paths},
        "git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(),
    }
    (directory / "fused_position_manifest.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (directory / "pressure_immersion_reference.json").write_text(
        json.dumps({"experiment_id": experiment, **immersion,
                    "method": "first sustained +8 mbar rise with +30 mbar in 30 s; baseline median before onset minus 10 s",
                    "source_sha256": hash_file(interim_pressure_path),
                    "git_commit": metadata["git_commit"]}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return metadata


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", action="append", dest="sessions")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    for experiment in args.sessions or (
        "20260922_cmain_022", "20260922_cmain_023", "20260922_cmain_024"
    ):
        metadata = fuse(experiment, root)
        print(experiment, metadata["optical_observations"], metadata["published_fused_samples"])


if __name__ == "__main__":
    main()
