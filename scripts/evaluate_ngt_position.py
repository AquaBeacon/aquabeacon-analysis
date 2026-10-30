"""Evaluate the NGT range/light position observations against the pool experiment plan."""
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path

os.environ.setdefault("OPENBLAS_NUM_THREADS", "2")
os.environ.setdefault("OMP_NUM_THREADS", "2")
import matplotlib
import numpy as np
from scipy.spatial.transform import Rotation

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from aquabeacon_analysis.camera_calibration import load_camera_intrinsics
from aquabeacon_analysis.editable_plots import save_editable_figure
from aquabeacon_analysis.ngt_binary import sha256
from aquabeacon_analysis.ngt_position import (
    bracket_interpolate,
    calibrated_rays,
    centered_scatter,
    missing_intervals,
    nearest_indices,
    trajectory_metrics,
)
from aquabeacon_analysis.parent_magnetic_attitude import sample_rotations

ROOT = Path(__file__).resolve().parents[1]
ACQUISITION = "20261028_ngt_000"


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def numbers(rows, columns):
    return np.array([[float(r.get(c) or "nan") for c in columns] for r in rows])


def write_csv(path, rows):
    if not rows:
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def clean_json(v):
    if isinstance(v, dict):
        return {k: clean_json(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, np.ndarray)):
        return [clean_json(x) for x in v]
    if isinstance(v, (float, np.floating)):
        return float(v) if np.isfinite(v) else None
    if isinstance(v, np.integer):
        return int(v)
    if isinstance(v, np.bool_):
        return bool(v)
    return v


def write_json(path, value):
    path.write_text(json.dumps(clean_json(value), ensure_ascii=False, indent=2), encoding="utf-8")


def build_observations(times, optical, ranges, attitude, gnss, pressure, telemetry,
                       cfg, sync, k, d, body_cam):
    t = np.asarray(times)
    oi, odelta = nearest_indices(numbers(optical, ["frame_pts_s"])[:, 0], t,
                                 cfg["nearest_optical_max_delta_s"])
    ri, rdelta = nearest_indices(numbers(ranges, ["parent_video_s"])[:, 0], t,
                                 cfg["nearest_range_max_delta_s"])
    pixels = np.full((len(t), 2), np.nan)
    op_valid, raw_optical, range_valid = [np.zeros(len(t), bool) for _ in range(3)]
    distance = np.full(len(t), np.nan)
    optical_reason, range_reason, shot_ids = [], [], []
    for i, time in enumerate(t):
        o = optical[oi[i]] if oi[i] >= 0 else {}
        r = ranges[ri[i]] if ri[i] >= 0 else {}
        raw_optical[i] = o.get("status") == "candidate"
        exclusion = next((x["reason"] for x in cfg["manual_exclusions"]
                          if x["interval_s"][0] <= time < x["interval_s"][1]), None)
        op_valid[i] = raw_optical[i] and exclusion is None
        optical_reason.append(exclusion or o.get("reason", "no_frame_within_tolerance"))
        if op_valid[i]:
            pixels[i] = [float(o["x_px"]), float(o["y_px"])]
        range_valid[i] = r.get("range_valid") == "True"
        if range_valid[i]:
            distance[i] = float(r["corrected_range_m"])
        range_reason.append(r.get("constraint_status", "no_event_within_tolerance"))
        shot_ids.append(r.get("shot_seq", ""))
    rays = calibrated_rays(pixels, k, d)
    m = np.array(cfg["optical_to_camera_imu_rotation"])
    body_rays = rays @ m.T @ body_cam.T
    at = numbers(attitude, ["parent_video_time_s"])[:, 0]
    aq = numbers(attitude, ["magnetic_ned_qw", "magnetic_ned_qx",
                            "magnetic_ned_qy", "magnetic_ned_qz"])
    flags = np.array([r["absolute_attitude_available"] == "True" and r["clock_valid"] == "True"
                      for r in attitude])
    aq[~flags] = np.nan
    q = sample_rotations(at, aq, t, max_gap_s=cfg["attitude_max_bracket_s"])
    ai, _ = nearest_indices(at, t, cfg["attitude_max_bracket_s"]/2)
    q[(ai < 0) | ~flags[np.maximum(ai, 0)]] = np.nan
    ned = np.full_like(rays, np.nan)
    available = np.isfinite(q).all(axis=1) & op_valid
    ned[available] = Rotation.from_quat(q[available][:, [1, 2, 3, 0]]).apply(body_rays[available])
    camera_position = distance[:, None]*rays
    relative = distance[:, None]*ned
    local_q = sample_rotations(numbers(telemetry, ["video_time_s"])[:, 0],
                              numbers(telemetry, ["qw", "qx", "qy", "qz"]), t, .1)
    local = np.full_like(relative, np.nan)
    local_ok = np.isfinite(local_q).all(axis=1) & np.isfinite(camera_position).all(axis=1)
    local[local_ok] = Rotation.from_quat(local_q[local_ok][:, [1, 2, 3, 0]]).apply(
        camera_position[local_ok] @ m.T)
    gp = numbers(gnss, ["east_m", "north_m", "up_m"])
    parent = bracket_interpolate(numbers(gnss, ["parent_video_s"])[:, 0], gp, t,
                                 cfg["gnss_max_bracket_s"])
    depth = bracket_interpolate(numbers(pressure, ["parent_video_s"])[:, 0],
                                numbers(pressure, ["raw_depth_m"]), t,
                                cfg["pressure_max_bracket_s"])[:, 0]
    # A named approximation: no surveyed centers and no magnetic declination applied.
    approximate_global = parent + relative[:, [1, 0, 2]]*np.array([1, 1, -1])
    rows = []
    for i, time in enumerate(t):
        r = {"parent_video_s": float(time),
             "utc_s": sync["utc_at_video_zero_s"]+sync["time_scale"]*time,
             "shot_seq": shot_ids[i], "range_time_delta_s": rdelta[i],
             "optical_time_delta_s": odelta[i], "raw_optical_candidate": bool(raw_optical[i]),
             "optical_available": bool(op_valid[i]), "optical_reason": optical_reason[i],
             "range_formula_available": bool(range_valid[i]), "range_reason": range_reason[i],
             "magnetic_attitude_available": bool(np.isfinite(q[i]).all()),
             "camera_position_available": bool(np.isfinite(camera_position[i]).all()),
             "magnetic_position_available": bool(np.isfinite(relative[i]).all()),
             "approximate_global_available": bool(np.isfinite(approximate_global[i]).all()),
             "range_m": distance[i], "light_x_px": pixels[i, 0], "light_y_px": pixels[i, 1],
             "camera_horizontal_angle_deg": np.degrees(np.arctan2(rays[i, 0], rays[i, 2])),
             "camera_elevation_deg": np.degrees(np.arctan2(-rays[i, 1], np.hypot(rays[i, 0], rays[i, 2]))),
             "off_axis_deg": np.degrees(np.arccos(np.clip(rays[i, 2], -1, 1))),
             "bearing_magnetic_deg": np.degrees(np.arctan2(ned[i, 1], ned[i, 0])) % 360,
             "depression_deg": np.degrees(np.arctan2(ned[i, 2], np.linalg.norm(ned[i, :2]))),
             "pressure_depth_m": depth[i], "optical_down_minus_pressure_m": relative[i, 2]-depth[i],
             "heading_accuracy_validated": False, "centers_calibrated": False}
        for columns, values in [
            (["relative_optical_right_m", "relative_optical_down_m", "relative_optical_forward_m"], camera_position[i]),
            (["relative_magnetic_north_m", "relative_magnetic_east_m", "relative_down_m"], relative[i]),
            (["relative_local_x_m", "relative_local_y_m", "relative_local_down_m"], local[i]),
            (["parent_east_m", "parent_north_m", "parent_up_m"], parent[i]),
            (["child_east_declination0_m", "child_north_declination0_m", "child_up_approx_m"], approximate_global[i]),
        ]:
            r.update(dict(zip(columns, values, strict=True)))
        rows.append(r)
    return rows


def evaluate(rows, shot_rows, lo, hi, cfg):
    counts = {k: sum(r[k] for r in rows) for k in ["raw_optical_candidate", "optical_available",
              "range_formula_available", "magnetic_attitude_available", "camera_position_available",
              "magnetic_position_available", "approximate_global_available"]}
    times = numbers(rows, ["parent_video_s"])[:, 0]
    gaps = missing_intervals(times, [r["magnetic_position_available"] for r in rows], hi,
                             1/cfg["sample_hz"])
    relative = numbers(shot_rows, ["relative_magnetic_north_m", "relative_magnetic_east_m", "relative_down_m"])
    global_xyz = numbers(shot_rows, ["child_east_declination0_m", "child_north_declination0_m", "child_up_approx_m"])
    residual = numbers(shot_rows, ["optical_down_minus_pressure_m"])[:, 0]
    residual = residual[np.isfinite(residual)]
    result = {"interval_s": [lo, hi], "duration_s": hi-lo, "uniform_grid_rows": len(rows),
              "grid_counts": counts, "grid_availability_fraction": {k: n/len(rows) for k, n in counts.items()},
              "missing_intervals": gaps, "longest_gap_s": max((x["duration_s"] for x in gaps), default=0),
              "recorded_shots": len(shot_rows),
              "shot_range_formula_available": sum(r["range_formula_available"] for r in shot_rows),
              "shot_camera_positions": sum(r["camera_position_available"] for r in shot_rows),
              "shot_magnetic_positions": sum(r["magnetic_position_available"] for r in shot_rows),
              "relative_scatter": centered_scatter(relative),
              "local_gravity_relative_scatter": centered_scatter(numbers(shot_rows,
                  ["relative_local_x_m", "relative_local_y_m", "relative_local_down_m"])),
              "camera_relative_scatter": centered_scatter(numbers(shot_rows,
                  ["relative_optical_right_m", "relative_optical_down_m", "relative_optical_forward_m"])),
              "approximate_global_scatter": centered_scatter(global_xyz),
              "relative_observed_trajectory": trajectory_metrics(numbers(shot_rows, ["parent_video_s"])[:, 0], relative),
              "global_horizontal_observed_trajectory": trajectory_metrics(numbers(shot_rows, ["parent_video_s"])[:, 0], global_xyz[:, :2]),
              "parent_horizontal_trajectory": trajectory_metrics(times, numbers(rows, ["parent_east_m", "parent_north_m"])),
              "optical_down_minus_pressure": {"count": len(residual),
                  "median_m": float(np.median(residual)) if len(residual) else None,
                  "rms_m": float(np.sqrt(np.mean(residual**2))) if len(residual) else None},
              "ground_truth_rmse_m": None}
    return result


def figures(out, rows, shots, label):
    t = numbers(rows, ["parent_video_s"])[:, 0]
    st = numbers(shots, ["parent_video_s"])[:, 0]
    fig, axes = plt.subplots(3, 1, figsize=(11, 8), layout="constrained", sharex=True)
    for key, name, y in [("optical_available", "Optical candidate with review exclusions", 0),
                         ("range_formula_available", "Acoustic formula candidate", 1),
                         ("magnetic_position_available", "Range + ray + magnetic attitude", 2)]:
        good = np.array([r[key] for r in rows])
        axes[0].plot(t, np.where(good, y, np.nan), ".", ms=3, label=name)
    axes[0].set(yticks=[0, 1, 2], yticklabels=["Optical", "Acoustic", "Position"], title=label)
    axes[1].plot(st, numbers(shots, ["range_m"])[:, 0], ".", ms=3, label="Acoustic range")
    axes[1].plot(t, numbers(rows, ["pressure_depth_m"])[:, 0], label="Pressure depth")
    axes[1].plot(st, numbers(shots, ["relative_down_m"])[:, 0], ".", ms=3, label="Optical/range down")
    axes[1].set_ylabel("Distance / depth [m]")
    for k, name in [("camera_horizontal_angle_deg", "Camera horizontal"),
                    ("camera_elevation_deg", "Camera elevation"), ("off_axis_deg", "Off-axis")]:
        axes[2].plot(t, numbers(rows, [k])[:, 0], label=name)
    axes[2].set(xlabel="Parent source video time [s]", ylabel="Angle [deg]")
    for ax in axes:
        ax.grid(alpha=.25)
        ax.legend(fontsize=8, loc="upper left")
    save_editable_figure(fig, out / "availability_and_sensors.png", dpi=150)
    plt.close(fig)
    fig, axes = plt.subplots(1, 2, figsize=(11, 5), layout="constrained")
    axes[0].plot(numbers(shots, ["relative_magnetic_east_m"])[:, 0],
                 numbers(shots, ["relative_magnetic_north_m"])[:, 0], ".", ms=3, label="Relative position")
    axes[0].set(xlabel="Magnetic east [m]", ylabel="Magnetic north [m]", title="Range/ray with zero lever arms")
    for x, y, name in [("parent_east_m", "parent_north_m", "Parent selected GNSS"),
                        ("child_east_declination0_m", "child_north_declination0_m", "Child: declination=0 assumption")]:
        axes[1].plot(numbers(shots, [x])[:, 0], numbers(shots, [y])[:, 0], ".", ms=3, label=name)
    axes[1].set(xlabel="East scenario [m]", ylabel="North scenario [m]", title="GNSS + relative offset")
    for ax in axes:
        ax.axis("equal")
        ax.grid(alpha=.25)
        ax.legend(fontsize=8)
    save_editable_figure(fig, out / "positions.png", dpi=150)
    plt.close(fig)
    fig, ax = plt.subplots(figsize=(11, 4), layout="constrained")
    for name, series_label in [("relative_magnetic_north_m", "Magnetic north"),
                         ("relative_magnetic_east_m", "Magnetic east"), ("relative_down_m", "Down")]:
        # Event-only marks cannot visually bridge intervals with no recorded event.
        ax.plot(st, numbers(shots, [name])[:, 0], ".", ms=3, label=series_label)
    ax.set(xlabel="Parent source video time [s]", ylabel="Relative position [m]", title=label)
    ax.grid(alpha=.25)
    ax.legend()
    save_editable_figure(fig, out / "relative_components.png", dpi=150)
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", required=True, action="append",
                        choices=[f"20261028_ngt_{i:03}" for i in [2, 3, 4]])
    args = parser.parse_args()
    started = datetime.now(UTC).isoformat()
    print(json.dumps({"pid": os.getpid(), "started_utc": started,
                      "owner": "position_evaluation", "purpose": "NGT range-light position evaluation"}), flush=True)
    cfg_path = ROOT / "config/20261028_ngt/position_evaluation.json"
    cfg = json.loads(cfg_path.read_text())
    defaults_path = ROOT / cfg["camera_defaults"]
    defaults = json.loads(defaults_path.read_text())
    calibration_path = ROOT / defaults["underwater_intrinsics"]["P_MAIN_CAM"]
    k, d, intrinsic_provenance = load_camera_intrinsics(ROOT, "P_MAIN_CAM", (1280, 720), "proxy")
    axis_path = ROOT / cfg["parent_body_axes"]
    axes = json.loads(axis_path.read_text())
    sync_path = ROOT / f"results/{ACQUISITION}/synchronization/manifest.json"
    sync = json.loads(sync_path.read_text())["cameras"]["parent"]
    body_cam = np.array(axes["R_body_from_spresense"]) @ np.array(sync["R_camera_imu"]).T
    paths = {
        "optical": ROOT / f"data/processed/{ACQUISITION}/optical/light_candidates.csv",
        "ranges": ROOT / f"data/processed/{ACQUISITION}/fusion/range_constraints.csv",
        "attitude": ROOT / f"data/processed/{ACQUISITION}/parent_magnetic_attitude/attitude.csv",
        "gnss": ROOT / f"results/{ACQUISITION}/gnss_selection/selected_positions.csv",
        "pressure": ROOT / f"data/processed/{ACQUISITION}/fusion/child_depth.csv",
        "telemetry": ROOT / f"data/interim/{ACQUISITION}/cameras/parent_telemetry.csv",
    }
    tables = {key: read_csv(p) for key, p in paths.items()}
    segments = json.loads((ROOT / "config/20261028_ngt/analysis.json").read_text())["segments"]
    result_all = {}
    for experiment in dict.fromkeys(args.experiment):
        segment = next(s for s in segments if s["experiment_id"] == experiment)
        lo, hi = segment["parent_video_interval_s"]
        t = np.arange(lo, hi, 1/cfg["sample_hz"])
        recorded_shots = [r for r in tables["ranges"] if lo <= float(r["parent_video_s"]) < hi]
        shot_times = numbers(recorded_shots, ["parent_video_s"])[:, 0]
        rows = build_observations(t, **tables, cfg=cfg, sync=sync, k=k, d=d, body_cam=body_cam)
        shot_rows = build_observations(shot_times, **tables, cfg=cfg, sync=sync, k=k, d=d, body_cam=body_cam)
        out = ROOT / f"results/{experiment}/position_evaluation"
        out.mkdir(parents=True, exist_ok=True)
        write_csv(out / "positions_5hz.csv", rows)
        write_csv(out / "positions_at_acoustic_events.csv", shot_rows)
        metrics = evaluate(rows, shot_rows, lo, hi, cfg)
        write_csv(out / "outages.csv", metrics["missing_intervals"] or
                  [{"start_s": "", "end_s": "", "duration_s": 0}])
        # Bins condition on an OBSERVED estimate; they cannot establish a physical range limit.
        bins = []
        for column, edges, denominator_flag in [
            ("range_m", [0, .75, 1, 1.25, 1.5, 2, 3, 5, 10], "range_formula_available"),
            ("off_axis_deg", [0, 5, 10, 15, 20, 25, 30, 40, 90], "optical_available"),
        ]:
            for lower, upper in pairwise(edges):
                subset = [r for r in shot_rows if r[denominator_flag] and lower <= r[column] < upper]
                count = sum(r["magnetic_position_available"] for r in subset)
                bins.append({"conditioning_variable": column, "lower": lower, "upper": upper,
                             "observed_denominator": len(subset), "position_count": count,
                             "conditional_fraction": count/len(subset) if subset else np.nan})
        write_csv(out / "conditional_range_angle_bins.csv", bins)
        write_json(out / "metrics.json", metrics)
        figures(out, rows, shot_rows, segment["label"])
        files = [*paths.values(), cfg_path, defaults_path, calibration_path, axis_path, sync_path,
                 ROOT / cfg["parent_magnetic_mount"], Path(__file__),
                 ROOT / "src/aquabeacon_analysis/ngt_position.py",
                 ROOT / "src/aquabeacon_analysis/camera_calibration.py",
                 ROOT / "config/20261028_ngt/analysis.json",
                 ROOT / f"experiments/{experiment}/experiment.json",
                 ROOT / "scripts/check_ngt_optical_axes.py",
                 ROOT / "results/20261028_ngt_000/position_evaluation/optical_axis_audit.json"]
        manifest = {"experiment_id": experiment, "started_utc": started,
                    "created_utc": datetime.now(UTC).isoformat(), "git_commit": subprocess.check_output(
                        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                    "config": cfg, "camera_matrix": k.tolist(), "distortion": d.tolist(),
                    "intrinsic_provenance": intrinsic_provenance,
                    "R_body_from_camera_imu": body_cam.tolist(),
                    "inputs": [{"path": str(p.relative_to(ROOT)), "sha256": sha256(p)} for p in files],
                    "frames": {"optical": "right/down/forward", "relative": "magnetic NED",
                               "global": "GNSS ENU plus magnetic offset, assumes zero declination/lever arms"},
                    "statistics": "one row per recorded acoustic event for scatter; uniform 5Hz for duration coverage",
                    "input_preservation": "invalid events and optical outages retained; no smoothing or range outlier rejection",
                    "ground_truth": None, "source_video_read_only": True}
        write_json(out / "manifest.json", manifest)
        shutil.copy2(__file__, out / "generate.py")
        shutil.copy2(ROOT / "src/aquabeacon_analysis/ngt_position.py", out / "ngt_position.py")
        result_all[experiment] = metrics
        print(experiment, json.dumps(clean_json({k: metrics[k] for k in ["recorded_shots",
              "shot_range_formula_available", "shot_magnetic_positions", "relative_scatter", "longest_gap_s"]})), flush=True)
    out = ROOT / f"results/{ACQUISITION}/position_evaluation"
    write_json(out / "summary.json", result_all)


if __name__ == "__main__":
    main()
