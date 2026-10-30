"""Export measured poses and final-map observation replay without changing sources."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

os.environ.setdefault("OPENBLAS_NUM_THREADS", "2")
os.environ.setdefault("OMP_NUM_THREADS", "2")

import numpy as np

from aquabeacon_analysis.ngt_replay_v2 import (
    compose_cad_quaternions,
    enu_quaternions,
    observation_times,
    read_image_names,
    read_points,
    sample_attitudes,
    utc_to_video,
)

ROOT = Path(__file__).resolve().parents[1]
EXPERIMENT = "20261028_ngt_004"


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def rows(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def finite(value):
    if isinstance(value, np.ndarray):
        return finite(value.tolist())
    if isinstance(value, dict):
        return {str(k): finite(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [finite(v) for v in value]
    if isinstance(value, (float, np.floating)):
        return float(value) if np.isfinite(value) else None
    if isinstance(value, (np.integer, np.bool_)):
        return value.item()
    return value


def save(path, value):
    path.write_text(json.dumps(finite(value), ensure_ascii=False, indent=2, allow_nan=False),
                    encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", required=True, choices=[EXPERIMENT])
    parser.parse_args()
    started = datetime.now(UTC).isoformat()
    out = ROOT / f"results/{EXPERIMENT}/presentation_v2/data"
    out.mkdir(parents=True, exist_ok=True)
    config_path = ROOT / "config/20261028_ngt/presentation_v2.json"
    config = load(config_path)
    source = ROOT / f"results/{EXPERIMENT}/wall_mapping/native120_restart"
    work = ROOT / f"data/interim/{EXPERIMENT}/wall_mapping/native120_restart"
    sync_path = ROOT / "results/20261028_ngt_000/synchronization/manifest.json"
    clock = load(sync_path)["cameras"]["parent"]
    positions_path = ROOT / f"results/{EXPERIMENT}/position_evaluation/positions_5hz.csv"
    positions = rows(positions_path)
    times = np.array([float(r["parent_video_s"]) for r in positions])
    inputs = [config_path, positions_path, positions_path.parent / "manifest.json", sync_path,
              source / "models.json", source / "summary.json", source / "manifest.json",
              source / "report_manifest.json", work / "optimizer_frame_priors.csv",
              source / "frame_evaluation.csv", source / "map.log",
              source / "extended_restarts.log", source / "config.json",
              source / "features_matching_config.json",
              source / "extended_restarts/config.json", source / "extended_restarts/manifest.json",
              Path(__file__), ROOT / "src/aquabeacon_analysis/ngt_replay_v2.py",
              ROOT / "calibration/child_mount_20261028_actual.json",
              ROOT / "calibration/20261029_child_axis_003_imu_frd.json",
              ROOT / "calibration/20261028_parent_lis3mdl_cad_axes.json",
              ROOT / "calibration/camera_defaults.json",
              ROOT / "calibration/parent_camera_underwater_20260924_provisional.json"]
    scene = {
        "schema_version": 2, "experiment_id": EXPERIMENT, "times": times,
        "time_basis": "parent_video_s; UTC = video_zero_UTC + scale * parent_video_s",
        "parent_video_clock": {k: clock[k] for k in ("utc_at_video_zero_s", "time_scale")},
        "display_frame": "ENU metres: existing declination0 east/north; parent up=0, child up=-pressure depth",
        "quaternion_convention": "wxyz; R_ENU_FRD maps column FRD vectors to displayed ENU",
        "heading": "magnetic north displayed on ENU north; declination 0; accuracy unvalidated",
        "render_interval": dict(zip(("start_parent_video_s", "end_parent_video_s"),
                                    config["render_interval_parent_video_s"], strict=True)),
        "full_timeline_retained": True,
        "position_policy": "Original 5Hz source samples; no interpolation, extrapolation or recentering",
        "attitude_policy": {"max_slerp_gap_s": config["max_attitude_slerp_gap_s"],
                            "nearest_matching_tolerance_s":
                                config["attitude_nearest_matching_tolerance_s"],
                            "no_extrapolation": True},
    }
    statistics = {}
    for body in ("parent", "child"):
        attitude_path = ROOT / f"data/processed/{EXPERIMENT}/{body}_magnetic_attitude/attitude.csv"
        attitude = rows(attitude_path)
        inputs.extend((attitude_path, attitude_path.parent / "manifest.json"))
        utc_key = "utc_us" if body == "child" else "utc_read_us"
        utc = np.array([float(r[utc_key]) / 1e6 for r in attitude])
        source_times = utc_to_video(utc, clock["utc_at_video_zero_s"], clock["time_scale"])
        logged_times = np.array([float(r["parent_video_time_s"]) for r in attitude])
        clock_residual = float(np.max(np.abs(source_times - logged_times)))
        if clock_residual > 2e-6:
            raise ValueError(f"{body} UTC/video clock mismatch {clock_residual}")
        q = np.array([[float(r[f"magnetic_ned_q{k}"] or "nan") for k in "wxyz"]
                      for r in attitude])
        valid = np.array([r["clock_valid"] == "True"
                          and r["absolute_attitude_available"] == "True" for r in attitude])
        valid &= np.isfinite(q).all(axis=1) & (np.abs(np.linalg.norm(q, axis=1) - 1) < .05)
        enu = np.full_like(q, np.nan)
        enu[valid] = enu_quaternions(q[valid])
        sampled, residual, methods = sample_attitudes(source_times, enu, valid, times,
            max_gap_s=config["max_attitude_slerp_gap_s"],
            nearest_tolerance_s=config["attitude_nearest_matching_tolerance_s"])
        attitude_valid = np.isfinite(sampled).all(axis=1)
        if body == "parent":
            xyz = np.array([[float(r["parent_east_m"]), float(r["parent_north_m"]), 0.]
                            for r in positions])
        else:
            xyz = np.array([[float(r["child_east_declination0_m"]),
                             float(r["child_north_declination0_m"]),
                             -float(r["pressure_depth_m"])]
                            if r["magnetic_position_available"] == "True" else [np.nan] * 3
                            for r in positions])
        position_valid = np.isfinite(xyz).all(axis=1)
        cad_q = np.full_like(sampled, np.nan)
        cad_q[attitude_valid] = compose_cad_quaternions(sampled[attitude_valid],
                                                       config[f"{body}_R_frd_from_cad"])
        scene[body] = {"position_enu_m": [v if ok else None for v, ok in
                                          zip(xyz, position_valid, strict=True)],
                       "quaternion_wxyz": [v if ok else None for v, ok in
                                            zip(sampled, attitude_valid, strict=True)],
                       "cad_quaternion_wxyz": [v if ok else None for v, ok in
                                                zip(cad_q, attitude_valid, strict=True)],
                       "position_valid": position_valid, "attitude_valid": attitude_valid,
                       "pose_valid": position_valid & attitude_valid,
                       "attitude_sample_method": methods, "attitude_time_residual_s": residual,
                       "R_frd_from_cad": config[f"{body}_R_frd_from_cad"],
                       "heading_accuracy_validated": False}
        statistics[body] = {"source_rows": len(attitude), "source_available": int(valid.sum()),
                            "position_samples": int(position_valid.sum()),
                            "attitude_samples": int(attitude_valid.sum()),
                            "pose_samples": int((position_valid & attitude_valid).sum()),
                            "clock_consistency_max_s": clock_residual,
                            "methods": {m: methods.count(m) for m in set(methods)}}
    scene["cad"] = {"bundle": "data/interim/cad/cad_20261028", "units": "metres",
                    "child_alignment_status": config["child_cad_alignment_status"],
                    "child_alignment_calibrated": config["child_cad_alignment_calibrated"],
                    "child_axis_evidence": config["child_cad_axis_evidence"],
                    "child_alignment_assumption": config["child_cad_alignment_assumption"],
                    "origin_policy": config["cad_origin_policy"],
                    "actual_child_camera_light_positions_swapped": True,
                    "camera_light_sensor_transforms_known": False}
    bundle = ROOT / scene["cad"]["bundle"]
    assets = load(bundle / "assets.json")
    if digest(bundle / "rig.json") != assets["profile_sha256"]:
        raise ValueError("Frozen CAD profile hash mismatch")
    for asset in assets["assets"].values():
        if digest(bundle / asset["file"]) != asset["sha256"]:
            raise ValueError("Frozen CAD GLB hash mismatch")
    inputs.extend(bundle / name for name in ("rig.json", "assets.json", "parent.glb", "child.glb"))
    frame_times = {r["image_name"]: float(r["parent_video_s"])
                   for r in rows(work / "optimizer_frame_priors.csv")}
    models = load(source / "models.json")
    xyz, rgb, first, reveal, last, observations, model_ids, point_ids = ([] for _ in range(8))
    for index, model in enumerate(models):
        selected = bool(model["structural"] and model["position_registered"]
                        and model.get("wall_period", False))
        model["replay_selected"] = selected
        model["replay_model_id"] = index
        model["replay_exclusion_reason"] = (None if selected else
            "Not jointly structural, position_registered and source wall_period; retained in ledger")
        if not selected:
            continue
        folder = ROOT / model["folder"]
        registered = folder / "position_registered"
        names = read_image_names(registered / "images.bin")
        offset = len(xyz)
        for pid, point, color, track in read_points(registered / "points3D.bin"):
            t0, t1, tn, count = observation_times(track, names, frame_times)
            xyz.append(point)
            rgb.append(color)
            first.append(t0)
            reveal.append(t1)
            last.append(tn)
            observations.append(count)
            model_ids.append(index)
            point_ids.append(pid)
        model["replay_point_offset"] = offset
        model["replay_point_count"] = len(xyz) - offset
        if model["replay_point_count"] != model["points"]:
            raise ValueError("Full point count differs from source model ledger")
        for directory in (folder, registered):
            inputs.extend(directory / name for name in ("images.bin", "points3D.bin", "cameras.bin"))
        inputs.extend((folder / "position_registration.json", folder / "camera_centers.csv"))
    arrays = {"xyz": np.array(xyz, dtype=np.float64), "rgb": np.array(rgb, dtype=np.uint8),
              "first_observation_parent_video_s": np.array(first),
              "reveal_parent_video_s": np.array(reveal),
              "last_observation_parent_video_s": np.array(last),
              "observation_count": np.array(observations, dtype=np.int32),
              "model_id": np.array(model_ids, dtype=np.int32),
              "source_point_id": np.array(point_ids, dtype=np.uint64)}
    if not np.isfinite(arrays["xyz"]).all():
        raise ValueError("Nonfinite original map coordinates; do not silently remove points")
    expected = load(source / "summary.json")["wall_period_positioned_points_sum"]
    if len(xyz) != expected:
        raise ValueError(f"Point count {len(xyz)} differs from declared {expected}")
    np.savez_compressed(out / "points.npz", **arrays)
    # Browser-friendly external data retains full float64 values and source IDs.
    save(out / "points.json", arrays)
    scene["points"] = {"file": "points.npz", "browser_file": "points.json", "count": len(xyz),
                       "coordinate_dtype": "float64", "color": "original COLMAP uint8 RGB",
                       "reveal_rule": config["point_reveal"],
                       "all_overlapping_fragments_preserved": True,
                       "online_birth_time_recovered": False,
                       "source_xyz_changed": False, "point_filtering_or_merging": False}
    scene["models"] = models
    scene["source_mapping_summary"] = load(source / "summary.json")
    windows = sorted(source.glob("window_*/summary.json"))
    extensions = sorted((source / "extended_restarts").glob("window_*/summary.json"))
    inputs.extend(windows + extensions)
    save(out / "window_trials.json", {"original": [load(p) for p in windows],
                                     "extended": [load(p) for p in extensions]})
    shutil.copyfile(source / "frame_evaluation.csv", out / "frame_evaluation.csv")
    scene["frame_coverage_file"] = "frame_evaluation.csv"
    scene["window_trials_file"] = "window_trials.json"
    save(out / "scene.json", scene)
    save(out / "config.json", config)
    # Verify exported data independently by re-reading original registered models.
    with np.load(out / "points.npz", allow_pickle=False) as exported:
        exported_xyz = exported["xyz"]
        exported_rgb = exported["rgb"]
        exported_ids = exported["source_point_id"]
        for model in models:
            if not model["replay_selected"]:
                continue
            start = model["replay_point_offset"]
            model_path = ROOT / model["folder"] / "position_registered/points3D.bin"
            for local_index, (pid, point, color, _) in enumerate(read_points(model_path)):
                i = start + local_index
                if (not np.array_equal(exported_xyz[i], point)
                        or not np.array_equal(exported_rgb[i], color)
                        or exported_ids[i] != pid):
                    raise ValueError("Saved point coordinates, colour or ID differs from source")
        if not np.all(exported["first_observation_parent_video_s"]
                      < exported["reveal_parent_video_s"]):
            raise ValueError("Reveal is not after first source observation")
    verification = {"passed": True, "point_count": len(xyz), "source_declared_points": expected,
                    "selected_fragments": sum(m["replay_selected"] for m in models),
                    "all_model_records_retained": len(models), "bodies": statistics,
                    "all_reveals_at_second_observation": True,
                    "reveal_range_parent_video_s": [min(reveal), max(reveal)],
                    "finite_json": True, "frozen_cad_hashes_verified": True,
                    "source_models_read_only": True,
                    "full_frame_ledger_exact_copy": digest(source / "frame_evaluation.csv")
                        == digest(out / "frame_evaluation.csv"),
                    "every_exported_point_xyz_rgb_id_matches_original_binary": True}
    save(out / "verification.json", verification)
    paths = sorted({p.resolve() for p in inputs if p.is_file()})
    manifest = {"experiment_id": EXPERIMENT, "created_utc": datetime.now(UTC).isoformat(),
                "git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"],
                                                       cwd=ROOT, text=True).strip(),
                "inputs_sha256": {str(p.relative_to(ROOT)).replace("\\", "/"): digest(p)
                                  for p in paths},
                "outputs_sha256": {p.name: digest(p) for p in out.iterdir()
                                   if p.is_file() and p.name not in ("manifest.json", "process.json")},
                "source_calibration_manifests_retained": True,
                "processing": "Read-only source decode; no SLAM, fitting, filtering or point rescaling",
                "limitations": ["Position scenario uses zero declination and uncalibrated centres",
                                "Measured magnetic attitude accuracy unvalidated",
                                config["child_cad_alignment_assumption"],
                                "Final optimized xyz replayed retrospectively by source observations"]}
    save(out / "manifest.json", manifest)
    save(out / "process.json", {"pid": os.getpid(), "started_utc": started,
                                "ended_utc": datetime.now(UTC).isoformat(),
                                "purpose": "task-owned read-only replay data preparation",
                                "child_processes": [], "temporary_servers": [],
                                "bounded_numeric_threads": 2})
    print(json.dumps(verification), flush=True)


if __name__ == "__main__":
    main()
