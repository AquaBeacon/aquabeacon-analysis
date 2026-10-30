"""Relative IMU attitudes and uncorrected parent dead reckoning."""

from __future__ import annotations

import csv
import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import matplotlib
import numpy as np

from .editable_plots import save_editable_figure

matplotlib.use("Agg")
import matplotlib.pyplot as plt

GRAVITY_M_S2 = 9.80665
CAMERA_RATE_HZ = 30.0


def read_numeric(
    path: Path, columns: tuple[str, ...], *, require_monotonic: bool = True
) -> dict[str, np.ndarray]:
    data = {column: [] for column in columns}
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        absent = set(columns) - set(reader.fieldnames or ())
        if absent:
            raise ValueError(f"{path}: missing columns {sorted(absent)}")
        for row in reader:
            for column in columns:
                data[column].append(float(row[column]))
    result = {key: np.asarray(value, dtype=np.float64) for key, value in data.items()}
    if not result[columns[0]].size or not all(np.isfinite(v).all() for v in result.values()):
        raise ValueError(f"{path}: empty or nonfinite required data")
    times = result[columns[0]]
    if require_monotonic and np.any(np.diff(times) <= 0):
        raise ValueError(f"{path}: timestamps must be strictly increasing")
    return result


def quaternion_array(data: dict[str, np.ndarray]) -> np.ndarray:
    q = np.column_stack([data[key] for key in ("qw", "qx", "qy", "qz")])
    norms = np.linalg.norm(q, axis=1)
    if np.any(norms < 0.5):
        raise ValueError("invalid quaternion norm")
    return q / norms[:, None]


def quaternion_multiply(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    aw, ax, ay, az = np.moveaxis(a, -1, 0)
    bw, bx, by, bz = np.moveaxis(b, -1, 0)
    return np.stack(
        [aw * bw - ax * bx - ay * by - az * bz,
         aw * bx + ax * bw + ay * bz - az * by,
         aw * by - ax * bz + ay * bw + az * bx,
         aw * bz + ax * by - ay * bx + az * bw],
        axis=-1,
    )


def quaternion_conjugate(q: np.ndarray) -> np.ndarray:
    result = q.copy()
    result[..., 1:] *= -1
    return result


def slerp(a: np.ndarray, b: np.ndarray, fraction: np.ndarray | float) -> np.ndarray:
    """Shortest-arc spherical interpolation of normalized wxyz quaternions."""
    a, b = np.broadcast_arrays(a, b)
    amount = np.broadcast_to(np.asarray(fraction), a.shape[:-1])
    dot = np.sum(a * b, axis=-1)
    b = np.where(dot[..., None] < 0, -b, b)
    dot = np.clip(np.abs(dot), -1, 1)
    angle = np.arccos(dot)
    sine = np.sin(angle)
    linear = sine < 1e-7
    weight_a = np.divide(np.sin((1 - amount) * angle), sine,
                         out=np.zeros_like(angle), where=~linear)
    weight_b = np.divide(np.sin(amount * angle), sine,
                         out=np.zeros_like(angle), where=~linear)
    weight_a = np.where(linear, 1 - amount, weight_a)
    weight_b = np.where(linear, amount, weight_b)
    result = weight_a[..., None] * a + weight_b[..., None] * b
    return result / np.linalg.norm(result, axis=-1, keepdims=True)


def quaternion_angle_deg(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    dot = np.abs(np.sum(a * b, axis=-1))
    return np.rad2deg(2 * np.arccos(np.clip(dot, 0, 1)))


def interpolate_quaternions(
    times_s: np.ndarray, q: np.ndarray, target_times_s: np.ndarray
) -> np.ndarray:
    if target_times_s[0] < times_s[0] or target_times_s[-1] > times_s[-1]:
        raise ValueError("quaternion interpolation outside source time span")
    upper = np.searchsorted(times_s, target_times_s, side="right")
    upper = np.clip(upper, 1, len(times_s) - 1)
    lower = upper - 1
    fraction = (target_times_s - times_s[lower]) / (times_s[upper] - times_s[lower])
    return slerp(q[lower], q[upper], fraction)


def interval_average(times: np.ndarray, values: np.ndarray, edges: np.ndarray) -> np.ndarray:
    indices = np.searchsorted(times, edges)
    counts = np.diff(indices)
    if np.any(counts == 0):
        raise ValueError("IMU samples absent from a camera frame interval")
    sums = np.vstack([np.zeros(values.shape[1]), np.cumsum(values, axis=0)])
    return np.diff(sums[indices], axis=0) / counts[:, None]


def fuse_camera_and_gyro(
    camera_q: np.ndarray,
    camera_times_s: np.ndarray,
    imu_times_s: np.ndarray,
    imu_gyro_rad_s: np.ndarray,
    imu_to_camera: np.ndarray,
    *,
    correction_time_s: float = 0.5,
) -> tuple[np.ndarray, np.ndarray]:
    """Integrate aligned gyro, then anchor each frame toward camera attitude."""
    if correction_time_s <= 0:
        raise ValueError("correction time must be positive")
    gyro_intervals = interval_average(imu_times_s, imu_gyro_rad_s, camera_times_s)
    gyro_intervals = gyro_intervals @ imu_to_camera.T
    fused = np.empty_like(camera_q)
    gyro_only = np.empty_like(camera_q)
    fused[0] = camera_q[0]
    gyro_only[0] = camera_q[0]
    for index, omega in enumerate(gyro_intervals, start=1):
        dt = camera_times_s[index] - camera_times_s[index - 1]
        angle = np.linalg.norm(omega) * dt
        if angle < 1e-12:
            step = np.array([1.0, 0.0, 0.0, 0.0])
        else:
            step = np.r_[np.cos(angle / 2), omega * (dt / angle) * np.sin(angle / 2)]
        gyro_only[index] = quaternion_multiply(gyro_only[index - 1], step)
        prediction = quaternion_multiply(fused[index - 1], step)
        camera_weight = 1 - np.exp(-dt / correction_time_s)
        fused[index] = slerp(prediction, camera_q[index], camera_weight)
    return fused, gyro_only


def align_body_attitude_to_camera(
    body_q: np.ndarray,
    camera_q: np.ndarray,
    body_to_camera: np.ndarray,
) -> np.ndarray:
    """Use first-frame gauge and motion-fitted axes to compare two attitudes."""
    q_cb = rotation_quaternion(body_to_camera)
    q_bc = quaternion_conjugate(q_cb)
    world_alignment = quaternion_multiply(
        quaternion_multiply(camera_q[0], q_cb), quaternion_conjugate(body_q[0])
    )
    return quaternion_multiply(quaternion_multiply(world_alignment, body_q), q_bc)


def fuse_child_attitudes(
    camera_q: np.ndarray,
    aligned_body_q: np.ndarray,
    *,
    full_weight_until_deg: float = 5.0,
    reject_at_deg: float = 20.0,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Blend two absolute attitudes, reducing body weight when they disagree."""
    if reject_at_deg <= full_weight_until_deg:
        raise ValueError("rejection angle must exceed full-weight angle")
    available = np.isfinite(aligned_body_q).all(axis=1)
    angle_deg = np.full(len(camera_q), np.nan)
    angle_deg[available] = quaternion_angle_deg(camera_q[available], aligned_body_q[available])
    weight = np.zeros(len(camera_q))
    weight[available] = 0.5 * np.clip(
        (reject_at_deg - angle_deg[available]) /
        (reject_at_deg - full_weight_until_deg), 0, 1
    )
    fused = camera_q.copy()
    fused[available] = slerp(
        camera_q[available], aligned_body_q[available], weight[available]
    )
    status = np.full(len(camera_q), "camera_only_gap", dtype="U26")
    status[available & (weight == 0)] = "camera_only_disagreement"
    status[available & (weight > 0) & (weight < 0.5)] = "body_downweighted"
    status[available & (weight == 0.5)] = "fused_equal"
    return fused, angle_deg, weight, status


def rotate_vectors(q: np.ndarray, vectors: np.ndarray) -> np.ndarray:
    xyz = q[..., 1:]
    uv = np.cross(xyz, vectors)
    return vectors + 2 * (q[..., :1] * uv + np.cross(xyz, uv))


def rotation_quaternion(matrix: np.ndarray) -> np.ndarray:
    """Return wxyz for a proper rotation matrix."""
    trace = np.trace(matrix)
    if trace > 0:
        scale = np.sqrt(trace + 1) * 2
        return np.array([
            scale / 4,
            (matrix[2, 1] - matrix[1, 2]) / scale,
            (matrix[0, 2] - matrix[2, 0]) / scale,
            (matrix[1, 0] - matrix[0, 1]) / scale,
        ])
    index = int(np.argmax(np.diag(matrix)))
    j, k = (index + 1) % 3, (index + 2) % 3
    scale = np.sqrt(1 + matrix[index, index] - matrix[j, j] - matrix[k, k]) * 2
    result = np.empty(4)
    result[0] = (matrix[k, j] - matrix[j, k]) / scale
    result[index + 1] = scale / 4
    result[j + 1] = (matrix[j, index] + matrix[index, j]) / scale
    result[k + 1] = (matrix[k, index] + matrix[index, k]) / scale
    return result / np.linalg.norm(result)


def camera_angular_velocity(q: np.ndarray, times_s: np.ndarray) -> np.ndarray:
    previous = q[:-1].copy()
    previous[:, 1:] *= -1
    relative = quaternion_multiply(previous, q[1:])
    relative[relative[:, 0] < 0] *= -1
    v = relative[:, 1:]
    magnitude = np.linalg.norm(v, axis=1)
    angle = 2 * np.arctan2(magnitude, relative[:, 0])
    factor = np.divide(angle, magnitude, out=np.full_like(angle, 2.0), where=magnitude > 1e-10)
    return v * (factor / np.diff(times_s))[:, None]


def fit_sony_to_camera(
    camera_q: np.ndarray,
    camera_times_s: np.ndarray,
    sony_times_s: np.ndarray,
    sony_gyro_rad_s: np.ndarray,
) -> tuple[np.ndarray, dict[str, float]]:
    """Wahba fit of Sony axes to camera axes using synchronized angular motion."""
    center = (camera_times_s[1:] + camera_times_s[:-1]) / 2
    cam_gyro = camera_angular_velocity(camera_q, camera_times_s)
    sony = np.column_stack([
        np.interp(center, sony_times_s, sony_gyro_rad_s[:, axis]) for axis in range(3)
    ])
    upper = np.clip(np.searchsorted(sony_times_s, center), 1, len(sony_times_s) - 1)
    valid_bracket = sony_times_s[upper] - sony_times_s[upper - 1] <= 0.1
    active = (np.linalg.norm(cam_gyro, axis=1) > 0.05) & (
        center >= sony_times_s[0]) & (center <= sony_times_s[-1]
    ) & valid_bracket
    if active.sum() < 100:
        raise ValueError("insufficient overlapping angular motion for Sony/camera alignment")
    x, y = sony[active], cam_gyro[active]
    cross_covariance = (x - x.mean(axis=0)).T @ (y - y.mean(axis=0))
    u, singular_values, vt = np.linalg.svd(cross_covariance)
    rotation = vt.T @ np.diag([1, 1, np.linalg.det(vt.T @ u.T)]) @ u.T
    predicted = x @ rotation.T
    residual = predicted - y
    score = float(np.sqrt(np.mean(residual**2)))
    correlation = float(np.corrcoef(predicted.ravel(), y.ravel())[0, 1])
    if singular_values[-1] / singular_values[0] < 0.005 or correlation < 0.8:
        raise ValueError("Sony/camera angular alignment is poorly constrained")
    return rotation, {
        "gyro_rms_rad_s": score,
        "gyro_correlation": correlation,
        "active_samples": int(active.sum()),
        "singular_value_ratio": float(singular_values[-1] / singular_values[0]),
    }


def fit_mounting_with_time_refinement(
    camera_q: np.ndarray,
    camera_times_s: np.ndarray,
    imu_times_s: np.ndarray,
    imu_gyro_rad_s: np.ndarray,
) -> tuple[np.ndarray, float, dict[str, float]]:
    """Refine a small residual clock offset using 3-axis angular motion."""
    baseline_rotation, baseline = fit_sony_to_camera(
        camera_q, camera_times_s, imu_times_s, imu_gyro_rad_s
    )
    best_rotation, best_offset, best = baseline_rotation, 0.0, baseline
    for offset in np.arange(-0.1, 0.1001, 1 / 120):
        if abs(offset) < 1e-10:
            continue
        try:
            rotation, quality = fit_sony_to_camera(
                camera_q, camera_times_s, imu_times_s + offset, imu_gyro_rad_s
            )
        except ValueError:
            continue
        if quality["gyro_rms_rad_s"] < best["gyro_rms_rad_s"]:
            best_rotation, best_offset, best = rotation, float(offset), quality
    if best["gyro_rms_rad_s"] > 0.9 * baseline["gyro_rms_rad_s"]:
        best_rotation, best_offset, best = baseline_rotation, 0.0, baseline
    return best_rotation, best_offset, {
        **best,
        "residual_imu_time_offset_applied_s": best_offset,
        "before_refinement_gyro_rms_rad_s": baseline["gyro_rms_rad_s"],
        "before_refinement_gyro_correlation": baseline["gyro_correlation"],
    }


def bin_average(times: np.ndarray, values: np.ndarray, centers: np.ndarray) -> np.ndarray:
    """Average high-rate Sony samples in nonoverlapping camera-rate bins."""
    edges = np.empty(centers.size + 1)
    edges[1:-1] = (centers[1:] + centers[:-1]) / 2
    edges[0] = centers[0] - (edges[1] - centers[0])
    edges[-1] = centers[-1] + (centers[-1] - edges[-2])
    indices = np.searchsorted(times, edges)
    counts = np.diff(indices)
    if np.any(counts == 0):
        raise ValueError("Sony samples absent from a camera time bin")
    sums = np.vstack([np.zeros(3), np.cumsum(values, axis=0)])
    return np.diff(sums[indices], axis=0) / counts[:, None]


def integrate_acceleration(
    times_s: np.ndarray,
    acceleration_m_s2: np.ndarray,
    initial_velocity_m_s: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Trapezoidal integration with no hidden drift correction."""
    if len(times_s) != len(acceleration_m_s2) or np.any(np.diff(times_s) <= 0):
        raise ValueError("invalid integration series")
    velocity = np.zeros_like(acceleration_m_s2)
    position = np.zeros_like(acceleration_m_s2)
    if initial_velocity_m_s is not None:
        velocity[0] = initial_velocity_m_s
    for index in range(1, len(times_s)):
        dt = times_s[index] - times_s[index - 1]
        velocity[index] = velocity[index - 1] + (
            acceleration_m_s2[index - 1] + acceleration_m_s2[index]
        ) * (dt / 2)
        position[index] = position[index - 1] + (
            velocity[index - 1] + velocity[index]
        ) * (dt / 2)
    return velocity, position


def write_csv(path: Path, columns: dict[str, np.ndarray]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        writer.writerows(zip(*columns.values(), strict=True))


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _single(folder: Path, pattern: str) -> Path:
    matches = list(folder.glob(pattern))
    if len(matches) != 1:
        raise ValueError(f"expected one {pattern} under {folder}, found {len(matches)}")
    return matches[0]


def _write_attitude(path: Path, data: dict[str, np.ndarray], q: np.ndarray) -> None:
    columns = {
        "window_time_s": data["window_time_s"],
        "sync_utc_us": data["sync_utc_us"],
        "qw": q[:, 0], "qx": q[:, 1], "qy": q[:, 2], "qz": q[:, 3],
    }
    write_csv(path, columns)


def localize_experiment(
    experiment_dir: Path, results_root: Path, repository_root: Path
) -> Path:
    from .attitude_animation import save_attitude_animation
    from .gps_parent import produce_parent_gps_position

    experiment_id = experiment_dir.name
    folder = experiment_dir / "tof_window"
    parent_camera_path = _single(folder / "camera_telemetry", "P_MAIN*_refined.csv")
    child_camera_path = _single(folder / "camera_telemetry", "C_MAIN*_refined.csv")
    sony_path = _single(folder / "spresense", "I*.CSV")
    gps_path = _single(folder / "spresense", "G*.CSV")
    child_body_path = _single(folder / "c_main", "C_MAIN_IMU_100Hz_*.csv")
    camera_columns = ("window_time_s", "refined_sync_utc_us", "qw", "qx", "qy", "qz",
                      "accel_x_g", "accel_y_g", "accel_z_g")
    body_columns = ("window_time_s", "sync_utc_us", "qw", "qx", "qy", "qz",
                    "gx", "gy", "gz")
    sony_columns = ("window_time_s", "sync_utc_us", "gx", "gy", "gz", "ax", "ay", "az")
    parent = read_numeric(parent_camera_path, camera_columns)
    child_camera = read_numeric(child_camera_path, camera_columns)
    child_body = read_numeric(child_body_path, body_columns, require_monotonic=False)
    sony = read_numeric(sony_path, sony_columns)
    parent_q = quaternion_array(parent)
    child_camera_q = quaternion_array(child_camera)
    child_body_q = quaternion_array(child_body)
    parent_time = parent["window_time_s"]
    sony_time = sony["window_time_s"]
    if np.max(np.diff(parent_time)) > 0.1:
        raise ValueError("P_MAIN camera telemetry gap exceeds 0.1 s")
    sony_gyro = np.column_stack([sony[key] for key in ("gx", "gy", "gz")])
    rotation, sony_time_offset, alignment = fit_mounting_with_time_refinement(
        parent_q, parent_time, sony_time, sony_gyro
    )
    sony_aligned_time = sony_time + sony_time_offset
    body_time_raw = child_body["window_time_s"]
    body_increasing = np.r_[True, body_time_raw[1:] > np.maximum.accumulate(body_time_raw)[:-1]]
    body_times = body_time_raw[body_increasing]
    body_q_clean = child_body_q[body_increasing]
    body_gyro = np.column_stack([
        child_body[key][body_increasing] for key in ("gx", "gy", "gz")
    ])
    child_rotation, child_time_offset, child_alignment = fit_mounting_with_time_refinement(
        child_camera_q, child_camera["window_time_s"], body_times, body_gyro
    )
    body_times = body_times + child_time_offset
    child_cam_times = child_camera["window_time_s"]
    child_overlap = (child_cam_times >= body_times[0]) & (child_cam_times <= body_times[-1])
    child_times = child_cam_times[child_overlap]
    child_camera_matched_q = child_camera_q[child_overlap]
    child_body_interp_q = interpolate_quaternions(body_times, body_q_clean, child_times)
    body_upper = np.clip(np.searchsorted(body_times, child_times), 1, len(body_times) - 1)
    child_body_available = (body_times[body_upper] - body_times[body_upper - 1]) <= 0.1
    child_body_aligned_q = align_body_attitude_to_camera(
        child_body_interp_q, child_camera_matched_q, child_rotation
    )
    child_difference_deg = quaternion_angle_deg(
        child_camera_matched_q[child_body_available],
        child_body_aligned_q[child_body_available],
    )
    child_body_aligned_q[~child_body_available] = np.nan
    child_fused_q, child_angle_deg, child_body_weight, child_fusion_status = (
        fuse_child_attitudes(child_camera_matched_q, child_body_aligned_q)
    )
    sony_accel = np.column_stack([sony[key] for key in ("ax", "ay", "az")])
    matched = (parent_time >= sony_aligned_time[0] + 1 / CAMERA_RATE_HZ) & (
        parent_time <= sony_aligned_time[-1] - 1 / CAMERA_RATE_HZ
    )
    times = parent_time[matched]
    utc = parent["refined_sync_utc_us"][matched].astype(np.int64)
    q = parent_q[matched]
    fused_q, gyro_only_q = fuse_camera_and_gyro(
        q, times, sony_aligned_time, sony_gyro, rotation
    )
    camera_accel = np.column_stack([
        parent[key][matched] for key in ("accel_x_g", "accel_y_g", "accel_z_g")
    ]) * GRAVITY_M_S2
    sony_binned = bin_average(sony_aligned_time, sony_accel, times)
    sony_in_camera = sony_binned @ rotation.T
    # DJI's camera-frame accelerometer maps static gravity to world -Z.
    # The Sony mounting rotation is inferred from gyro, then uses the same convention.
    gravity_correction = np.array([0.0, 0.0, GRAVITY_M_S2])
    cam_linear = rotate_vectors(q, camera_accel) + gravity_correction
    sony_linear = rotate_vectors(q, sony_in_camera) + gravity_correction
    fused_linear = (cam_linear + sony_linear) / 2
    attitude_fused_linear = rotate_vectors(
        fused_q, (camera_accel + sony_in_camera) / 2
    ) + gravity_correction
    tracks = {}
    for name, linear in (("camera", cam_linear), ("sony", sony_linear),
                         ("mean", fused_linear), ("fused", attitude_fused_linear)):
        tracks[name] = integrate_acceleration(times, linear)
    output = results_root / experiment_id / "localization"
    output.mkdir(parents=True, exist_ok=True)
    gps_metadata = produce_parent_gps_position(
        gps_path, output, times, utc, tracks["fused"][1][:, :2]
    )
    columns = {"window_time_s": times, "sync_utc_us": utc}
    for axis, label in enumerate("wxyz"):
        columns[f"parent_camera_q{label}"] = q[:, axis]
        columns[f"parent_fused_q{label}"] = fused_q[:, axis]
    for name, linear in (("camera", cam_linear), ("sony", sony_linear),
                         ("mean", fused_linear), ("fused", attitude_fused_linear)):
        velocity, position = tracks[name]
        for axis, label in enumerate("xyz"):
            columns[f"{name}_a{label}_m_s2"] = linear[:, axis]
            columns[f"{name}_v{label}_m_s"] = velocity[:, axis]
            columns[f"{name}_{label}_m"] = position[:, axis]
    write_csv(output / "parent_imu.csv", columns)
    _write_attitude(output / "parent_camera_attitude.csv", {
        "window_time_s": parent_time, "sync_utc_us": parent["refined_sync_utc_us"]
    }, parent_q)
    _write_attitude(output / "parent_fused_attitude.csv", {
        "window_time_s": times, "sync_utc_us": utc
    }, fused_q)
    _write_attitude(output / "parent_sony_gyro_attitude.csv", {
        "window_time_s": times, "sync_utc_us": utc
    }, gyro_only_q)
    _write_attitude(output / "child_body_attitude.csv", child_body, child_body_q)
    _write_attitude(output / "child_camera_attitude.csv", {
        "window_time_s": child_camera["window_time_s"],
        "sync_utc_us": child_camera["refined_sync_utc_us"],
    }, child_camera_q)
    write_csv(output / "child_body_aligned_attitude.csv", {
        "window_time_s": child_times,
        "sync_utc_us": child_camera["refined_sync_utc_us"][child_overlap].astype(np.int64),
        "available": child_body_available.astype(np.int8),
        "qw": child_body_aligned_q[:, 0],
        "qx": child_body_aligned_q[:, 1],
        "qy": child_body_aligned_q[:, 2],
        "qz": child_body_aligned_q[:, 3],
    })
    write_csv(output / "child_fused_attitude.csv", {
        "window_time_s": child_times,
        "sync_utc_us": child_camera["refined_sync_utc_us"][child_overlap].astype(np.int64),
        "status": child_fusion_status,
        "body_camera_angle_deg": child_angle_deg,
        "body_weight": child_body_weight,
        "qw": child_fused_q[:, 0],
        "qx": child_fused_q[:, 1],
        "qy": child_fused_q[:, 2],
        "qz": child_fused_q[:, 3],
    })
    sony_to_world_q = quaternion_multiply(parent_q, np.broadcast_to(
        rotation_quaternion(rotation), parent_q.shape
    ))
    _write_attitude(output / "parent_sony_attitude.csv", {
        "window_time_s": parent_time, "sync_utc_us": parent["refined_sync_utc_us"]
    }, sony_to_world_q)
    input_paths = [parent_camera_path, child_camera_path, sony_path, gps_path,
                   child_body_path]
    conditions = [*sorted((repository_root / "config").glob("*.yaml")),
                  *sorted((repository_root / "calibration").glob("*.yaml")),
                  repository_root / "calibration" / "README.md"]
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repository_root,
                            capture_output=True, text=True, check=True).stdout.strip()
    metadata = {
        "experiment_id": experiment_id,
        "generated_utc": datetime.now(UTC).isoformat(),
        "git_commit": commit,
        "inputs_sha256": {str(p.resolve().relative_to(repository_root)): _hash(p)
                          for p in input_paths},
        "conditions_sha256": {str(p.relative_to(repository_root)): _hash(p) for p in conditions},
        "extrinsic_calibration_available": False,
        "frame": "local gravity-aligned camera world; +Z up; horizontal heading unverified",
        "pose_origin": "P_MAIN camera IMU; body lever arm uncalibrated",
        "sony_to_parent_camera_rotation_matrix": rotation.tolist(),
        "child_body_to_camera_rotation_matrix": child_rotation.tolist(),
        "alignment": alignment,
        "child_alignment": child_alignment,
        "parent_fusion": {
            "method": "Sony gyro prediction in camera axes, camera quaternion correction",
            "camera_correction_time_constant_s": 0.5,
            "fused_camera_angle_median_deg": float(np.median(
                quaternion_angle_deg(fused_q, q)
            )),
            "gyro_only_camera_angle_end_deg": float(quaternion_angle_deg(
                gyro_only_q[-1], q[-1]
            )),
        },
        "child_motion_alignment": {
            "method": "angular-velocity fitted fixed rotation and first-frame gauge",
            "body_camera_angle_median_deg": float(np.median(child_difference_deg)),
            "body_camera_angle_p95_deg": float(np.percentile(child_difference_deg, 95)),
            "missing_body_camera_frames": int((~child_body_available).sum()),
        },
        "child_fusion": {
            "method": "slerp camera and motion-aligned body quaternions",
            "equal_weight_until_deg": 5.0,
            "reject_body_at_deg": 20.0,
            "status_counts": {name: int(np.sum(child_fusion_status == name))
                              for name in np.unique(child_fusion_status)},
            "median_camera_fused_angle_deg": float(np.median(
                quaternion_angle_deg(child_camera_matched_q, child_fused_q)
            )),
        },
        "gps_position": gps_metadata,
        "time_sync": "refined camera UTC and sensor UTC, plus recorded gyro residual offsets",
        "gravity_m_s2": GRAVITY_M_S2,
        "initial_position_m": [0, 0, 0],
        "initial_velocity_m_s": [0, 0, 0],
        "accel_bias_m_s2": [0, 0, 0],
        "gyro_bias_rad_s": [0, 0, 0],
        "drift_correction": "none",
        "parent_dr_samples": len(times),
        "child_body_nonmonotonic_intervals": int(np.sum(
            np.diff(child_body["window_time_s"]) <= 0
        )),
        "parent_dr_duration_s": float(times[-1] - times[0]),
        "sonycamera_specific_force_rms_m_s2": float(np.sqrt(np.mean(
            (sony_in_camera - camera_accel) ** 2
        ))),
        "terminal_displacement_m": {
            name: float(np.linalg.norm(track[1][-1])) for name, track in tracks.items()
        },
        "terminal_track_disagreement_m": float(np.linalg.norm(
            tracks["camera"][1][-1] - tracks["sony"][1][-1]
        )),
        "warnings": [
            "Relative coordinates only; no RTK-GNSS or independent ground truth.",
            "Uncalibrated accelerometer bias and initial velocity cause quadratic position drift.",
            "Parent body/camera lever arm and heading are uncalibrated; do not treat as ENU position.",
            "parent_sony_attitude.csv is camera attitude expressed on Sony axes; parent_sony_gyro_attitude.csv is the independent gyro integral.",
            "Fused parent attitude uses camera correction; gyro-only parent is a drift diagnostic.",
            "Child body/camera alignment is motion fitted and first-frame gauged, not physical extrinsic calibration.",
            "C_MAIN body attitude preserves source row order, including nonmonotonic timestamps.",
        ],
    }
    if child_alignment["gyro_correlation"] < 0.9 or np.percentile(child_difference_deg, 95) > 10:
        metadata["warnings"].append(
            "Child body/camera motion mismatch remains after fixed-axis and residual-time alignment."
        )
    if gps_metadata["isolated_jumps"]:
        metadata["warnings"].append(
            "SONY GPS has an isolated position jump; interpolation is stopped across segments."
        )
    (output / "imu_manifest.json").write_text(json.dumps(metadata, indent=2) + "\n",
                                               encoding="utf-8")
    figure, axis = plt.subplots(figsize=(7, 6))
    for name, (_, position) in tracks.items():
        axis.plot(position[:, 0], position[:, 1], label=name)
    axis.set(xlabel="local X (m)", ylabel="local Y (m)",
             title=f"{experiment_id} uncorrected IMU integral (drifted)")
    axis.axis("equal")
    axis.grid(True)
    axis.legend()
    figure.tight_layout()
    save_editable_figure(figure, output / "parent_imu_xy.png", dpi=150)
    plt.close(figure)
    report = (
        f"# {experiment_id} IMU localization\n\n"
        f"Parent trajectory: {len(times)} samples over {times[-1] - times[0]:.2f} s. "
        f"Camera/Sony terminal disagreement: "
        f"{metadata['terminal_track_disagreement_m']:.1f} m.\n\n"
        "The trajectory is an uncorrected IMU double integral from zero position and velocity. "
        "Its large drift is expected and does not indicate real displacement. "
        "The local horizontal axes have no verified east/north alignment. "
        "Parent body position additionally requires a calibrated camera-to-body lever arm.\n\n"
        "Parent and child attitude CSVs contain normalized sensor-frame quaternions. "
        "Sony orientation uses the fitted Sony-to-camera rotation and camera orientation. "
        "A body-frame attitude for either device requires measured extrinsics.\n\n"
        f"SONY/camera gyro correlation: {alignment['gyro_correlation']:.3f}; "
        f"specific-force disagreement RMS: "
        f"{metadata['sonycamera_specific_force_rms_m_s2']:.3f} m/s².\n\n"
        "Parent fused attitude combines aligned Sony gyro prediction with camera quaternion "
        "correction (0.5 s time constant). "
        f"Median fused/camera separation: "
        f"{metadata['parent_fusion']['fused_camera_angle_median_deg']:.2f}°. "
        f"Gyro-only end separation: "
        f"{metadata['parent_fusion']['gyro_only_camera_angle_end_deg']:.2f}°.\n\n"
        "Child body and camera are aligned by angular motion and first-frame gauge for comparison. "
        f"Median separation: {metadata['child_motion_alignment']['body_camera_angle_median_deg']:.2f}°; "
        f"95th percentile: {metadata['child_motion_alignment']['body_camera_angle_p95_deg']:.2f}°.\n"
        "Child fused attitude blends camera and body quaternions when they agree; "
        "body weight decreases above 5° disagreement and reaches zero at 20°. "
        f"Median camera/fused separation: "
        f"{metadata['child_fusion']['median_camera_fused_angle_deg']:.2f}°.\n"
        f"Residual IMU time offsets applied: parent {sony_time_offset:+.4f} s, "
        f"child {child_time_offset:+.4f} s.\n\n"
        "SONY GPS fixes provide horizontal WGS84 ENU position at about 1 Hz; "
        "the camera-time CSV interpolates only between fixes in one valid segment. "
        f"GPS segments: {gps_metadata['segments']}; isolated jumps: "
        f"{len(gps_metadata['isolated_jumps'])}. "
        "IMU horizontal acceleration is not rotated into ENU because heading is unverified. "
        "Raw GPS altitude is diagnostic only. This is an antenna position without ground truth.\n"
    )
    (output / "imu_report.md").write_text(report, encoding="utf-8")
    save_attitude_animation(
        output / "parent_attitude_3d.html",
        f"{experiment_id} 親機の3D姿勢",
        "P_MAINカメラ、SONY角速度との融合、SONY角速度のみを同じ初期姿勢で比較",
        times,
        {"P_MAINカメラ": q, "融合姿勢": fused_q, "SONY角速度のみ": gyro_only_q},
    )
    save_attitude_animation(
        output / "child_attitude_3d.html",
        f"{experiment_id} 子機の3D姿勢",
        "C_MAINカメラ・融合姿勢・本体IMUを角速度で軸合わせし、初期姿勢を揃えて比較",
        child_times,
        {"C_MAINカメラ": child_camera_matched_q, "子機融合姿勢": child_fused_q,
         "C_MAIN本体IMU": child_body_aligned_q},
    )
    return output


def localize_all(
    processed_root: Path, results_root: Path, repository_root: Path,
    sessions: list[str] | None = None,
) -> list[Path]:
    from .experiments import select_experiment_directories

    directories = select_experiment_directories(processed_root, sessions, "tof_window/c_main")
    return [localize_experiment(path, results_root, repository_root) for path in directories]
