"""Read-only pool BNO085/pressure adapter for dead-reckoning comparisons."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from scipy.spatial.transform import Rotation

from aquabeacon_analysis.dead_reckoning_comparison import bounded_sample


def load_pool(root: Path, experiment: str, query_utc_s: np.ndarray, *,
              use_static_bias: bool = True) -> dict:
    """Return provisional magnetic-ENU linear acceleration and relative depth.

    Logged accelerometer values include gravity. The existing child-only sensor
    preparation supplies fresh-sample masking, attitude gaps and sensor axes.
    Absolute magnetic heading and coincident measurement centers are assumptions.
    """
    if experiment not in {f"20261028_ngt_{n:03}" for n in (2, 3, 4)}:
        raise ValueError("Pool body IMU/pressure comparison supports explicit NGT 002–004 only")
    root = Path(root)
    folder = root / "results/20261028_ngt_000/child_sensor_fusion"
    manifest_path = folder / "sensor_manifest.json"
    sensor_path = folder / "sensors.npz"
    sync_path = root / "results/20261028_ngt_000/synchronization/manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    sync = json.loads(sync_path.read_text(encoding="utf-8"))
    clock = sync["cameras"]["child"]
    utc_origin = float(clock["utc_at_video_zero_s"])
    time_scale = float(clock["time_scale"])
    if not np.isfinite([utc_origin, time_scale]).all() or time_scale <= 0:
        raise ValueError("Invalid synchronized child video UTC affine clock")
    with np.load(sensor_path, allow_pickle=False) as sensors:
        video_s = sensors["video_s"].copy()
        acceleration = sensors["acceleration"].copy()
        q = sensors["magnetic_xyzw"].copy()
        body_from_sensor = sensors["body_from_sensor"].copy()
        prepared_bias = sensors["accelerometer_bias"].copy()
        health = sensors["health"].copy()
    bias = prepared_bias if use_static_bias else np.zeros(3)
    gravity = float(manifest["gravity_m_s2"])
    density = float(manifest["pressure_density_kg_m3"])
    native_utc = utc_origin + time_scale * video_s
    valid = (np.isfinite(acceleration).all(axis=1) & np.isfinite(q).all(axis=1)
             & (np.linalg.norm(q, axis=1) > 1e-12))
    ned = np.full_like(acceleration, np.nan, dtype=float)
    body_acceleration = (acceleration[valid] - bias) @ body_from_sensor.T
    ned[valid] = Rotation.from_quat(q[valid]).apply(body_acceleration) + [0, 0, gravity]
    enu = ned[:, [1, 0, 2]] * [1, 1, -1]
    query = np.asarray(query_utc_s, dtype=float)
    sampled = bounded_sample(native_utc, enu, query, max_gap_s=.1)

    finite_pressure = np.isfinite(health[:, :2]).all(axis=1)
    if not finite_pressure.any():
        raise ValueError("Pool health source contains no finite pressure observations")
    baseline_index = int(np.flatnonzero(finite_pressure)[0])
    baseline = float(health[baseline_index, 1])
    depth = (health[:, 1] - baseline) * 100 / (density * gravity)
    depth[~finite_pressure] = np.nan
    pressure_utc = utc_origin + time_scale * health[:, 0]
    sampled_depth = bounded_sample(pressure_utc, depth, query, max_gap_s=2.)

    paths = [sensor_path, manifest_path, sync_path,
             root / "calibration/20261029_child_axis_003_imu_frd.json", Path(__file__)]
    for relative in manifest["inputs_sha256"]:
        path = root / relative.replace("\\", "/")
        if path.is_file():
            paths.append(path)
    # Include synchronization sources rather than importing their data as inputs.
    for key in ("source", "telemetry"):
        if key in clock:
            path = Path(clock[key])
            if not path.is_absolute():
                path = root / path
            if path.is_file():
                paths.append(path)
    details = {
        "experiment_id": experiment,
        "frame": "Provisional magnetic ENU; east,north,up; declination 0 degrees",
        "heading_accuracy_validated": False,
        "centers": "Nominal coincident pool measurement centers; no lever-arm correction",
        "acceleration_source": "BNO085 accelerometer specific force including gravity; no logged linear acceleration",
        "acceleration_equation": "a_NED=R_NED_body R_body_sensor (a_sensor-bias)+[0,0,g]; ENU=[NED_y,NED_x,-NED_z]",
        "acceleration_bias_m_s2": bias.tolist(),
        "acceleration_bias_frame": "BNO085 sensor axes",
        "static_bias_applied": bool(use_static_bias),
        "bias_provenance": {
            "source": "results/20261028_ngt_000/child_sensor_fusion/sensor_manifest.json",
            "child_video_interval_s": manifest["bias_source_video_s"],
            "samples": manifest["bias_samples"],
            "condition": "Static 002 samples with gyro norm below 0.035 rad/s; training samples are not held-out validation",
        },
        "body_from_sensor": body_from_sensor.tolist(),
        "gravity_m_s2": gravity,
        "pressure_density_kg_m3": density,
        "pressure_baseline_mbar": baseline,
        "pressure_baseline_child_video_s": float(health[baseline_index, 0]),
        "pressure_depth_convention": "Positive down relative to first finite health pressure; fixed baseline cancels in displacement",
        "clock": {"utc_at_video_zero_s": utc_origin, "time_scale": time_scale,
                  "equation": "utc_s=utc_at_video_zero_s+time_scale*child_video_s"},
        "sensor_preparation_clock": manifest["clock"],
        "native_imu_rows": len(native_utc),
        "native_acceleration_valid_rows": int(valid.sum()),
        "native_acceleration_invalid_rows": int((~valid).sum()),
        "native_pressure_rows": len(health),
        "native_pressure_valid_rows": int(finite_pressure.sum()),
        "query_rows": len(query),
        "query_acceleration_valid_rows": int(np.isfinite(sampled).all(axis=1).sum()),
        "query_pressure_valid_rows": int(np.isfinite(sampled_depth).sum()),
        "acceleration_max_bracket_gap_s": .1,
        "pressure_max_bracket_gap_s": 2.,
        "missing_policy": "Bounded immediate-neighbor interpolation; preserve invalid source samples and gaps; no extrapolation",
    }
    return {"acceleration_enu": sampled, "depth_m": sampled_depth,
            "provenance_paths": list(dict.fromkeys(paths)), "details": details}
