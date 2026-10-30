"""Align pose changes, crop the marker interval, and audit magnetic north observability."""
from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from scipy.spatial.transform import Rotation

from aquabeacon_analysis.experiments import load_experiment, repository_path
from aquabeacon_analysis.magnetic_heading import fit_constant_bias, magnetic_candidates
from aquabeacon_analysis.ngt_binary import load_reader, sha256
from aquabeacon_analysis.ngt_sync import (
    angular_signal,
    correlation_offset,
    fit_rotation,
    numeric_csv,
    sample_with_gaps,
)


def read_stream(reader, path, kind):
    with path.open("rb") as f:
        header = reader.read_header(f)
        rows = [row for record in reader.records(f) if record["kind"] == kind
                for row in reader.rows(record)]
    return header, rows


def save_csv(path, rows):
    if not rows:
        raise ValueError(f"Empty crop: {path}")
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0])
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", required=True)
    parser.add_argument("--firmware-root", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    exp = load_experiment(root, args.experiment)
    cp = repository_path(root, exp.metadata["config_paths"][0])
    config = json.loads(cp.read_text(encoding="utf-8"))
    parameters = config["calibration_crop"]
    paths = {k: repository_path(root, p) for k, p in config["sources"].items()}
    if any(not p.is_relative_to(exp.raw_root) for p in paths.values()):
        raise ValueError("Raw source outside registered acquisition")
    solution_path = root / "calibration" / f"{args.experiment}_imu_frd.json"
    solution = json.loads(solution_path.read_text())
    body_r = np.array(solution["R_body_from_sensor"])
    reader = load_reader(args.firmware_root, "binary_log")
    head, events = read_stream(reader, paths["capture"], 13)
    ih, imu = read_stream(reader, paths["imu"], 1)
    if head["session"] != ih["session"]:
        raise ValueError("Capture/IMU session mismatch")
    begin, end = events[0]["marker_mono_us"] / 1e6, events[-1]["marker_mono_us"] / 1e6
    if events[-1]["event"] != 4:
        raise ValueError("Capture not completed")
    telemetry = exp.stage_path("interim") / "cameras/calibration_telemetry.csv"
    camera = numeric_csv(telemetry, ["video_time_s", "qw", "qx", "qy", "qz"])
    ct, cg = angular_signal(camera)
    fresh = [r for r in imu if r["fresh_mask"] & 4]
    bt = np.array([r["acquired_us"] / 1e6 for r in fresh])
    bg = np.array([[r[k] for k in ("gx", "gy", "gz")] for r in fresh])
    matches = []
    for win in parameters["motion_windows_video_s"]:
        fit = correlation_offset(ct, np.linalg.norm(cg, axis=1), bt,
                                 np.linalg.norm(bg, axis=1), win,
                                 np.arange(*parameters["offset_search_s"], .002))
        fit = {k: v for k, v in fit.items() if k not in ("offsets_s", "scores")}
        if (fit["correlation"] < .9 or fit["at_search_boundary"]
                or fit["correlation"] - fit["competing_correlation_outside_1s"] < .03):
            raise ValueError("Ambiguous motion alignment")
        matches.append(fit)
    offset = float(np.median([m["offset_s"] for m in matches]))
    if np.ptp([m["offset_s"] for m in matches]) > .03:
        raise ValueError("Motion windows do not share a clock offset")
    video_start, video_end = begin - offset, end - offset
    if not 0 <= video_start < video_end <= camera[-1, 0]:
        raise ValueError("Marker interval outside video")
    for match in matches:
        if not video_start <= match["window_s"][0] < match["window_s"][1] <= video_end:
            raise ValueError("Sync fit includes non-calibration motion")
    train = (ct >= 25) & (ct < 73)
    camera_r, quality = fit_rotation(sample_with_gaps(bt, bg, ct[train]+offset), cg[train])
    hold = (ct >= 75) & (ct < 100)
    x = sample_with_gaps(bt, bg, ct[hold]+offset)
    valid = np.isfinite(x).all(1) & (np.linalg.norm(x, axis=1) > .05)
    prediction, target = x[valid] @ camera_r.T, cg[hold][valid]
    check = {"samples": int(valid.sum()),
             "correlation": float(np.corrcoef(prediction.ravel(), target.ravel())[0, 1]),
             "rms_rad_s": float(np.sqrt(np.mean((prediction-target)**2)))}
    if quality["singular_ratio"] < .01 or check["correlation"] < .9:
        raise ValueError("Camera rotation fails held-out motion")
    out = exp.stage_path("results") / "calibration_clip"
    out.mkdir(parents=True, exist_ok=True)
    cropped = [{**r, "source_video_s": r["acquired_us"]/1e6-offset,
                "clip_time_s": r["acquired_us"]/1e6-begin} for r in imu
               if begin <= r["acquired_us"]/1e6 <= end]
    save_csv(out / "imu.csv", cropped)
    with telemetry.open(newline="", encoding="utf-8") as f:
        camrows = [{**r, "child_mono_s": float(r["video_time_s"])+offset,
                    "clip_time_s": float(r["video_time_s"])-video_start}
                   for r in csv.DictReader(f)
                   if video_start <= float(r["video_time_s"]) <= video_end]
    save_csv(out / "camera.csv", camrows)
    for kind, key in [(2, "bno_events"), (3, "health")]:
        h, rows = read_stream(reader, paths[key], kind)
        if h["session"] != head["session"]:
            raise ValueError("Source session mismatch")
        clock = "acquired_us" if kind == 2 else "mono_us"
        save_csv(out / f"{key}.csv", [{**r, "clip_time_s": r[clock]/1e6-begin}
                                     for r in rows if begin <= r[clock]/1e6 <= end])
    pose_groups, pose_summary = [], []
    for win in solution["pose_windows"]:
        group = [r for r in cropped if win["start_mono_s"]+1 <= r["acquired_us"]/1e6
                 <= win["end_mono_s"]-.25 and r["fresh_mask"] & 8]
        q = np.array([[r[k] for k in ("qw", "qx", "qy", "qz")] for r in group])
        m = np.array([[r[k] for k in ("mx", "my", "mz")] for r in group])
        _, world, _ = magnetic_candidates(q, m, body_r)
        mean = world.mean(0)
        pose_groups.append((q, m))
        pose_summary.append({"phase": win["phase"], "samples": len(group),
                             "world_field_uT": mean.tolist(),
                             "mean_field_norm_uT": float(np.mean(np.linalg.norm(m, axis=1))),
                             "world_field_azimuth_deg": float(np.degrees(np.arctan2(mean[1], mean[0]))),
                             "source_video_start_s": win["start_mono_s"]-offset,
                             "source_video_end_s": win["end_mono_s"]-offset})
        save_csv(out / f"pose_{win['phase']}_imu.csv",
                 [r for r in cropped if win["start_mono_s"] <= r["acquired_us"]/1e6
                  <= win["end_mono_s"]])
    qtrain = np.concatenate([g[0] for g in pose_groups[:3]])
    mtrain = np.concatenate([g[1] for g in pose_groups[:3]])
    bias, field, condition = fit_constant_bias(qtrain, mtrain)
    qhold, mhold = pose_groups[3]
    corrected = Rotation.from_quat(qhold[:, [1, 2, 3, 0]]).apply(mhold-bias).mean(0)
    hold_error = float(np.linalg.norm(corrected-field)/np.linalg.norm(field))
    angle_error = float(abs((np.degrees(np.arctan2(corrected[1], corrected[0])
                                        - np.arctan2(field[1], field[0])) + 180) % 360 - 180))
    # Conservative: this script publishes diagnostic candidates, never certified absolute headings.
    magnetic = {"accepted": False, "reference": "local magnetic north candidate; not true north",
                "poses": pose_summary, "constant_bias_trial_uT": bias.tolist(),
                "constant_bias_condition": condition, "fit_world_field_uT": field.tolist(),
                "held_out_pose": 4, "held_out_relative_field_error": hold_error,
                "held_out_field_azimuth_error_deg": angle_error,
                "relative_field_gate": .05, "heading_gate_deg": 5,
                "reason": "Raw field varies across poses; constant-bias trial fails independent pose. No magnetic or true-north calibration published.",
                "declination_deg": None}
    q = np.array([[r[k] for k in ("qw", "qx", "qy", "qz")] for r in cropped])
    m = np.array([[r[k] for k in ("mx", "my", "mz")] for r in cropped])
    headings, _, observable = magnetic_candidates(q, m, body_r)
    save_csv(out / "magnetic_heading_candidates.csv", [
        {"clip_time_s": row["clip_time_s"], "acquired_us": row["acquired_us"],
         "fresh_mask": row["fresh_mask"], "magnetic_heading_candidate_deg": float(h),
         "projection_observable": bool(v), "absolute_heading_valid": False,
         "true_heading_deg": "", "status": "magnetic_consistency_failed"}
        for row, h, v in zip(cropped, headings, observable, strict=True)])
    video = out / "calibration_only.mp4"
    command = ["ffmpeg", "-v", "error", "-nostdin", "-threads", "2", "-ss",
               str(video_start), "-i", str(paths["camera_original"]), "-t",
               str(video_end-video_start), "-map", "0:v:0", "-map", "0:a:0?",
               "-c:v", "libx264", "-threads", "2", "-preset", "veryfast", "-crf", "18",
               "-c:a", "aac", "-movflags", "+faststart", "-y", str(video)]
    old_path = out / "manifest.json"
    old = json.loads(old_path.read_text()) if old_path.exists() else {}
    reuse = (video.exists() and old.get("ffmpeg_command") == command
             and old.get("clip_sha256") == sha256(video)
             and old.get("inputs", {}).get("camera_original", {}).get("sha256")
             == sha256(paths["camera_original"]))
    if not reuse:
        process = subprocess.Popen(command)
        print(json.dumps({"pid": process.pid, "started_utc": datetime.now(UTC).isoformat(),
                          "owner": args.experiment,
                          "purpose": "frame-accurate calibration clip encode"}), flush=True)
        try:
            process.wait(timeout=300)
        except BaseException:
            process.terminate()
            process.wait(timeout=10)
            raise
        if process.returncode:
            raise RuntimeError("Video crop failed")
    manifest = {"experiment_id": args.experiment, "clock_model": "child_mono_s = video_s + offset_s",
                "offset_s": offset, "time_scale": 1, "drift_estimated": False,
                "source_video_interval_s": [video_start, video_end],
                "child_marker_interval_s": [begin, end], "motion_matches": matches,
                "R_camera_imu_from_bno": camera_r.tolist(),
                "R_body_from_camera_imu": (body_r @ camera_r.T).tolist(),
                "camera_rotation_fit": quality, "held_out_rotation_check": check,
                "camera_calibration_scope": "This capture interval only; camera IMU, not optical extrinsic or translation.",
                "magnetic": magnetic, "ffmpeg_command": command,
                "clip_sha256": sha256(video), "imu_rows": len(cropped),
                "camera_rows": len(camrows), "pid": os.getpid(),
                "inputs": {k: {"path": str(p), "sha256": sha256(p)} for k, p in paths.items()},
                "config_sha256": sha256(cp), "axis_solution_sha256": sha256(solution_path),
                "script_sha256": sha256(Path(__file__)),
                "magnetic_module_sha256": sha256(root / "src/aquabeacon_analysis/magnetic_heading.py"),
                "git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root,
                                                       text=True).strip()}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    camera_solution = {k: manifest[k] for k in (
        "experiment_id", "clock_model", "offset_s", "time_scale", "drift_estimated",
        "source_video_interval_s", "child_marker_interval_s", "R_camera_imu_from_bno",
        "R_body_from_camera_imu", "camera_rotation_fit", "held_out_rotation_check",
        "camera_calibration_scope", "inputs", "config_sha256", "axis_solution_sha256",
        "script_sha256", "git_commit")}
    camera_solution["status"] = "accepted_for_marker_interval_only"
    camera_solution["absolute_heading_valid"] = False
    (root / "calibration" / f"{args.experiment}_camera_imu.json").write_text(
        json.dumps(camera_solution, indent=2), encoding="utf-8")
    print(json.dumps({"interval": [video_start, video_end], "offset": offset,
                      "held_out": check, "magnetic_hold_error": hold_error,
                      "magnetic_azimuth_error_deg": angle_error}), flush=True)


if __name__ == "__main__":
    main()
