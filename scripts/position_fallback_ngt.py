"""Read-only adapter for the rotation-corrected pool position observations.

The zero-center geometry deliberately reproduces the pool baseline: magnetic
parent attitude rotates the optical ray; child attitude is not needed to rotate
zero lever arms. No calibration, registry, video or existing result is changed.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from scipy.spatial.transform import Rotation

try:
    from evaluate_ngt_position import ACQUISITION, build_observations, read_csv
except ModuleNotFoundError:  # Importing as scripts.position_fallback_ngt in tests.
    from scripts.evaluate_ngt_position import ACQUISITION, build_observations, read_csv

from aquabeacon_analysis.ngt_position import calibrated_rays, nearest_indices
from aquabeacon_analysis.parent_magnetic_attitude import sample_rotations

ROOT = Path(__file__).resolve().parents[1]
RUN = "rotation_corrected_20261031"
EXPERIMENTS = tuple(f"20261028_ngt_{n:03}" for n in (2, 3, 4))


def numbers(rows, columns):
    """Read both CSV strings and computed numeric rows, preserving numeric zero."""
    return np.array([[float(r[c]) if r.get(c) not in (None, "") else np.nan
                      for c in columns] for r in rows])


def combined_timeline(grid, events):
    """Sorted exact union; event flags occur once even for repeated source rows."""
    grid, events = np.asarray(grid, float), np.asarray(events, float)
    if not np.isfinite(grid).all() or not np.isfinite(events).all():
        raise ValueError("Observation timestamps must be finite")
    times = np.union1d(grid, events)
    return times, np.isin(times, grid), np.isin(times, events)


def normalize_observations(rows, times, grid_flags, event_flags, attitude, cfg, k, d, body_cam):
    """Extract geometry without conditioning optical rays on range validity."""
    at = numbers(attitude, ["parent_video_time_s"])[:, 0]
    aq = numbers(attitude, ["magnetic_ned_qw", "magnetic_ned_qx", "magnetic_ned_qy", "magnetic_ned_qz"])
    valid = np.array([r["absolute_attitude_available"] == "True" and r["clock_valid"] == "True"
                      for r in attitude])
    aq[~valid] = np.nan
    q = sample_rotations(at, aq, times, cfg["attitude_max_bracket_s"])
    ai, _ = nearest_indices(at, times, cfg["attitude_max_bracket_s"] / 2)
    q[(ai < 0) | ~valid[np.maximum(ai, 0)]] = np.nan
    imu_ok = np.isfinite(q).all(axis=1)
    pixels = numbers(rows, ["light_x_px", "light_y_px"])
    camera_rays = calibrated_rays(pixels, k, d)
    camera_rays[~np.array([bool(r["optical_available"]) for r in rows])] = np.nan
    body_rays = camera_rays @ np.asarray(cfg["optical_to_camera_imu_rotation"]).T @ body_cam.T
    rays = np.full((len(rows), 3), np.nan)
    optical_ok = np.array([bool(r["optical_available"]) for r in rows]) & imu_ok
    ned = Rotation.from_quat(q[optical_ok][:, [1, 2, 3, 0]]).apply(body_rays[optical_ok]) if optical_ok.any() else np.empty((0, 3))
    rays[optical_ok] = ned[:, [1, 0, 2]] * [1, 1, -1]
    parent = numbers(rows, ["parent_east_m", "parent_north_m", "parent_up_m"])
    zero_offsets = np.full_like(parent, np.nan)
    zero_offsets[imu_ok] = 0
    records = []
    for i, row in enumerate(rows):
        records.append({**row, "grid_sample": bool(grid_flags[i]), "event_sample": bool(event_flags[i]),
                        "range_available": bool(row["range_formula_available"]),
                        "source": "ngt_corrected_range_light_pressure",
                        "coordinate_run": RUN,
                        "optical_source": f"data/processed/{ACQUISITION}/optical/light_candidates.csv",
                        "range_source": f"data/processed/{ACQUISITION}/fusion/range_constraints.csv"})
    joint = numbers(rows, ["relative_magnetic_east_m", "relative_magnetic_north_m", "relative_down_m"])
    joint[:, 2] *= -1
    ranges = numbers(rows, ["range_m"])[:, 0]
    ranges[~np.array([bool(r["range_formula_available"]) for r in rows])] = np.nan
    return {
        "times": numbers(rows, ["utc_s"])[:, 0],
        "joint_positions": joint, "parent_origin": parent,
        "rays": rays, "camera_rays": camera_rays,
        "cameras": zero_offsets.copy(), "transmitters": zero_offsets.copy(),
        "light_offsets": zero_offsets.copy(), "acoustic_offsets": zero_offsets.copy(),
        "pressure_offsets": zero_offsets.copy(), "ranges": ranges,
        "depths": numbers(rows, ["pressure_depth_m"])[:, 0],
        "surface_reference_z": np.zeros(len(rows)), "records": records,
    }


def load_ngt(eid):
    """Load trial 002/003/004 on the existing grid plus all acoustic event times."""
    if eid not in EXPERIMENTS:
        raise ValueError(f"Explicit corrected pool trial required: {EXPERIMENTS}")
    out = ROOT / f"results/{eid}/position_evaluation/{RUN}"
    manifest_path = out / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest["run"] != RUN or manifest["experiment_id"] != eid:
        raise ValueError("Pool source manifest does not identify the corrected trial")
    cfg = manifest["config"]
    frame = manifest["frame_calibration"]
    k = np.asarray(frame["parent_proxy_matrix_display"])
    d = np.asarray(frame["parent_proxy_distortion_display"])
    cfg = {**cfg, "optical_to_camera_imu_rotation": frame["R_camera_imu_from_display_optical"]}
    body_cam = np.asarray(manifest["R_body_from_camera_imu"])
    paths = {
        "optical": ROOT / f"data/processed/{ACQUISITION}/optical/light_candidates.csv",
        "ranges": ROOT / f"data/processed/{ACQUISITION}/fusion/range_constraints.csv",
        "attitude": ROOT / f"data/processed/{ACQUISITION}/parent_magnetic_attitude/attitude.csv",
        "gnss": ROOT / f"results/{ACQUISITION}/gnss_selection/selected_positions.csv",
        "pressure": ROOT / f"data/processed/{ACQUISITION}/fusion/child_depth.csv",
        "telemetry": ROOT / f"data/interim/{ACQUISITION}/cameras/parent_telemetry.csv",
    }
    analysis_path = ROOT / "config/20261028_ngt/analysis.json"
    segments = json.loads(analysis_path.read_text(encoding="utf-8"))["segments"]
    lo, hi = next(s["parent_video_interval_s"] for s in segments if s["experiment_id"] == eid)
    sync_path = ROOT / f"results/{ACQUISITION}/synchronization/manifest.json"
    sync = json.loads(sync_path.read_text(encoding="utf-8"))["cameras"]["parent"]
    tables = {key: read_csv(path) for key, path in paths.items()}
    grid_path = out / "positions_5hz.csv"
    grid = numbers(read_csv(grid_path), ["parent_video_s"])[:, 0]
    events = [float(r["parent_video_s"]) for r in tables["ranges"]
              if lo <= float(r["parent_video_s"]) < hi]
    times, grid_flags, event_flags = combined_timeline(grid, events)
    rows = build_observations(times, **tables, cfg=cfg, sync=sync, k=k, d=d, body_cam=body_cam)
    result = normalize_observations(rows, times, grid_flags, event_flags, tables["attitude"], cfg, k, d, body_cam)
    start, end = [sync["utc_at_video_zero_s"] + sync["time_scale"] * x for x in (lo, hi)]
    # Include source dependencies already recorded by the corrected baseline.
    provenance = [ROOT / p.replace("\\", "/") for p in manifest["inputs_sha256"]]
    result["input_paths"] = list(dict.fromkeys([
        *paths.values(), *provenance, manifest_path, grid_path, analysis_path, sync_path,
        Path(__file__), ROOT / "src/aquabeacon_analysis/parent_magnetic_attitude.py",
    ]))
    result["metadata"] = {
        "experiment_id": eid, "coordinate_run": RUN,
        "frame": "Relative to parent body origin: magnetic ENU east,north,up=-down; parent_origin is separate GNSS ENU",
        "magnetic_declination_deg": 0, "heading_accuracy_validated": False,
        "geometry": "Coincident optical/acoustic/GNSS/pressure centers; zero lever arms, unsurveyed",
        "child_imu_policy": "Not used: zero child lever arms retain existing pool baseline convention",
        "interval_utc_s": [start, end], "parent_video_interval_s": [lo, hi],
        "interval_s": [start, end], "grid_hz": cfg["sample_hz"],
        "sampling": "Sorted union of existing 5 Hz grid and unique recorded acoustic event timestamps; invalid events retained",
        "static_windows": [{"id": "ngt_002", "start_s": start, "end_s": end, "condition": "pool_static"}] if eid.endswith("002") else [],
    }
    return result
