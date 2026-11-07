"""Read-only Nanyo sources for independent inertial/depth displacement trials."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import numpy as np
from scipy.spatial.transform import Rotation

from aquabeacon_analysis.dead_reckoning_comparison import bounded_sample
from aquabeacon_analysis.parent_magnetic_attitude import sample_rotations


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _ordered(times: np.ndarray, source: Path) -> tuple[np.ndarray, int]:
    """Reject reversal, retain first duplicate, and expose duplicate count."""
    if not np.isfinite(times).all() or np.any(np.diff(times) < 0):
        raise ValueError(f"Nonfinite or reversed source clock: {source}")
    keep = np.r_[True, np.diff(times) > 0] if len(times) else np.zeros(0, bool)
    return keep, int((~keep).sum())


def load_nanyo(root: Path, experiment: str, query_utc_s: np.ndarray) -> dict:
    """Return true-ENU acceleration and pressure depth without position feedback."""
    allowed = {
        "20261031_nanyo_001", "20261031_nanyo_002",
        "20261101_nanyo_001", "20261101_nanyo_002", "20261101_nanyo_003",
        "20261101_nanyo_004", "20261101_nanyo_005",
    }
    if experiment not in allowed:
        raise ValueError(f"Unsupported Nanyo experiment: {experiment}")
    root = Path(root)
    query = np.asarray(query_utc_s, dtype=float)
    acquisition = experiment[:8] + "_nanyo_000"
    suffix = "35545" if experiment.startswith("20261031") else "68440"
    processed = root / "data/processed" / experiment
    imu_path = processed / f"synchronized/child/C_MAIN_IMU_100Hz_{suffix}.imu.csv"
    attitude_path = processed / "absolute_position/attitudes_10hz.csv"
    attitude_manifest = root / "results" / experiment / "absolute_position/manifest.json"
    config_path = root / "config" / acquisition / "absolute_position.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    axes_path = root / config["child_axes"]
    axes = np.asarray(json.loads(axes_path.read_text(encoding="utf-8"))["R_body_from_sensor"])
    segment_config_path = root / "config" / acquisition / "segments.json"
    segment_manifest_path = root / "results" / acquisition / "segmentation/manifest.json"
    depth_path = segment_manifest_path.parent / "selection.csv"
    segment_config = json.loads(segment_config_path.read_text(encoding="utf-8"))
    segment_manifest = json.loads(segment_manifest_path.read_text(encoding="utf-8"))

    imu = _rows(imu_path)
    imu_times = np.array([float(row["sync_utc_s"]) for row in imu])
    imu_keep, imu_duplicates = _ordered(imu_times, imu_path)
    fresh = np.array([(int(row["fresh_mask"]) & 2) != 0 for row in imu])
    acceleration = np.array([[float(row[k]) for k in ("ax", "ay", "az")] for row in imu])
    sensor_acceleration = bounded_sample(
        imu_times[imu_keep & fresh], acceleration[imu_keep & fresh], query, max_gap_s=0.1,
    )

    attitude = _rows(attitude_path)
    attitude_times = np.array([float(row["utc_s"]) for row in attitude])
    attitude_keep, attitude_duplicates = _ordered(attitude_times, attitude_path)
    q = np.array([[float(row[f"child_true_enu_q{k}"]) for k in "wxyz"] for row in attitude])
    available = np.array([row["child_attitude_available"].lower() == "true" for row in attitude])
    q[~available] = np.nan
    attitude_times, q = attitude_times[attitude_keep], q[attitude_keep]
    sampled_q = sample_rotations(attitude_times, q, query, 0.15)
    # The shared SLERP helper filters bad quaternions. Require the original
    # adjacent rows too, so a deleted invalid row can never be bridged.
    attitude_valid = np.isfinite(q).all(axis=1) & (np.abs(np.linalg.norm(q, axis=1) - 1) < .05)
    if len(attitude_times) >= 2:
        hi = np.searchsorted(attitude_times, query, side="left")
        clipped = np.clip(hi, 0, len(attitude_times) - 1)
        exact = (hi < len(attitude_times)) & (attitude_times[clipped] == query)
        left = np.clip(hi - 1, 0, len(attitude_times) - 1)
        valid = exact & attitude_valid[clipped]
        valid |= ((hi > 0) & (hi < len(attitude_times)) & attitude_valid[left]
                  & attitude_valid[clipped] & (attitude_times[clipped] - attitude_times[left] <= .15))
        sampled_q[~valid] = np.nan
        sampled_q[exact & valid] = q[clipped[exact & valid]]
    else:
        sampled_q[:] = np.nan
    good = np.isfinite(sampled_q).all(axis=1) & np.isfinite(sensor_acceleration).all(axis=1)
    linear = np.full((len(query), 3), np.nan)
    linear[good] = Rotation.from_quat(sampled_q[good][:, [1, 2, 3, 0]]).apply(
        sensor_acceleration[good] @ axes.T,
    ) - np.array([0., 0., 9.80665])

    depth_rows = _rows(depth_path)
    depth_times = np.array([float(row["sync_utc_s"]) for row in depth_rows])
    depth_keep, depth_duplicates = _ordered(depth_times, depth_path)
    depth_column = "relative_depth_m" if suffix == "35545" else "depth_m"
    depths = np.array([float(row[depth_column]) for row in depth_rows])
    depth = bounded_sample(depth_times[depth_keep], depths[depth_keep], query, max_gap_s=2.)
    paths = [imu_path, attitude_path, attitude_manifest, config_path, axes_path,
             depth_path, segment_config_path, segment_manifest_path,
             root / "experiments" / experiment / "experiment.json", Path(__file__).resolve()]
    return {
        "acceleration_enu": linear, "depth_m": depth, "provenance_paths": paths,
        "details": {
            "frame": "true ENU; processed absolute attitude already includes declination",
            "clock": "synchronized UTC seconds; no camera or clock refit",
            "acceleration_source": "fresh_mask bit1 specific force; derived linear acceleration, not SH2 sensor_id=4",
            "acceleration_equation": "R_true_ENU_body @ R_body_sensor @ specific_force - [0,0,9.80665]",
            "extra_accelerometer_bias_m_s2": [0., 0., 0.],
            "bias_policy": "No position-estimated bias or trajectory fit",
            "measurement_center": "BNO085 center approximated as body origin; CAD lever arm uncorrected and provisional",
            "acceleration_max_gap_s": .1, "attitude_max_gap_s": .15, "depth_max_gap_s": 2.,
            "imu_rows": len(imu), "fresh_acceleration_rows": int((imu_keep & fresh).sum()),
            "duplicates_excluded": {"imu": imu_duplicates, "attitude": attitude_duplicates, "depth": depth_duplicates},
            "clock_reversal_policy": "reject",
            "depth_source_column": depth_column, "depth_filter": "unfiltered pressure-derived depth",
            "density_kg_m3_assumed": segment_config["density_kg_m3_assumed"],
            "gravity_m_s2": segment_config["gravity_m_s2"],
            "pressure_baseline_mbar": segment_manifest["pressure_baseline_mbar"],
            "pressure_baseline_first_s": segment_config["baseline_first_s"],
            "depth_reference": "initial surface-pressure proxy; not calibrated absolute depth",
            "heading_accuracy_validated": False,
        },
    }
