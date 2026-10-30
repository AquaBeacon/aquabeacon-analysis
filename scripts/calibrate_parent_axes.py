"""Parent capture audit, relative clock reconstruction, four-pose fit and video crop."""
from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from aquabeacon_analysis.child_axis_calibration import body_quaternions, fit_frd
from aquabeacon_analysis.experiments import load_experiment, repository_path
from aquabeacon_analysis.ngt_binary import decode_scal, load_reader, sha256
from aquabeacon_analysis.ngt_sync import (
    angular_signal,
    correlation_offset,
    fit_rotation,
    numeric_csv,
    sample_with_gaps,
)
from aquabeacon_analysis.parent_axis_calibration import parent_time, reference_clock_fit


def binary_rows(reader, path, kind):
    with path.open("rb") as f:
        header = reader.read_header(f)
        rows = [r for record in reader.records(f) if record["kind"] == kind
                for r in reader.rows(record)]
    return header, rows


def write_csv(path, rows):
    if not rows:
        raise ValueError(f"Empty output {path}")
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0])
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", required=True)
    parser.add_argument("--firmware-root", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps({"pid": os.getpid(), "started_utc": datetime.now(UTC).isoformat(),
                      "owner": args.experiment, "purpose": "parent axes and calibration clip"}),
          flush=True)
    root = Path(__file__).resolve().parents[1]
    exp = load_experiment(root, args.experiment)
    config_path = repository_path(root, exp.metadata["config_paths"][0])
    config = json.loads(config_path.read_text())
    paths = {k: repository_path(root, v) for k, v in config["sources"].items()}
    if any(not p.is_relative_to(exp.raw_root) for p in paths.values()):
        raise ValueError("Source outside registered raw root")
    reader = load_reader(args.firmware_root, "binary_log")
    sp = load_reader(args.firmware_root, "spresense_log")
    header, events = binary_rows(reader, paths["capture"], 13)
    hh, health = binary_rows(reader, paths["health"], 3)
    _, magnetic_rows = binary_rows(reader, paths["health"], 10)
    session = config["expected_parent_session"]
    if (header["role"] != 1 or header["session"] != session or hh["session"] != session
            or events[-1]["event"] != 4 or events[-1]["mask"] != 15
            or any(e["event"] == 5 or e["boot"] != session for e in events)):
        raise ValueError("Wrong or incomplete parent capture")
    refs = list(sp.iter_rows(paths["references"]))
    clock = reference_clock_fit(health, refs, session)
    coefficients = decode_scal(paths["imu_coefficients"].read_bytes())
    begin, end = events[0]["marker_mono_us"]/1e6, events[-1]["marker_mono_us"]/1e6
    out = exp.stage_path("results") / "axis_calibration"
    out.mkdir(parents=True, exist_ok=True)
    rows, framing, previous, missing = [], {}, None, 0
    # CRC-check every record while retaining only this capture plus 2 seconds.
    for row in sp.iter_rows(paths["imu"], report=framing):
        if previous is not None and row["seq"] != previous+1:
            missing += 1
        previous = row["seq"]
        if row["parent_session"] != session:
            continue
        t = float(parent_time(row["sample_mono_us"]/1e6, clock))
        if not begin-2 <= t <= end+2:
            continue
        if (row["calibration_generation"] != events[0]["imu_calibration"]
                or row["calibration_generation"] != coefficients["generation"]):
            raise ValueError("Mixed IMU calibration")
        rows.append({**row, "parent_mono_s": t})
    if missing:
        raise ValueError("Spresense source sequence discontinuity")
    print(json.dumps({"crc_checked_samples": framing["rows"], "retained": len(rows)}), flush=True)
    t = np.array([r["parent_mono_s"] for r in rows])
    a = np.array([[r["corrected_"+k] for k in ("ax", "ay", "az")] for r in rows])
    g = np.array([[r["corrected_"+k] for k in ("gx", "gy", "gz")] for r in rows])
    poses, windows, mag_means, mag_stats = [], [], [], []
    for phase in range(1, 5):
        starts = [e for e in events if e["phase"] == phase and e["event"] == 2]
        stops = [e for e in events if e["phase"] == phase and e["event"] == 3]
        if len(starts) != 1 or len(stops) != 1 or starts[0]["drops"] != stops[0]["drops"]:
            raise ValueError("Phase markers or drop counters invalid")
        lo, hi = starts[0]["marker_mono_us"]/1e6, stops[0]["marker_mono_us"]/1e6
        if hi-lo < 5:
            raise ValueError("Short pose")
        mask = (t >= lo+1) & (t <= hi-.25)
        poses.append((a[mask], g[mask], t[mask]))
        windows.append({"phase": phase, "parent_start_s": lo, "parent_end_s": hi})
        m = np.array([[r["corrected_"+k] for k in ("x", "y", "z")]
                      for r in magnetic_rows if lo+1 <= r["read_end_us"]/1e6 <= hi-.25
                      and r["valid"] and not r["hardware_overrun"]
                      and r["calibration_generation"] == events[0]["mag_calibration"]])
        if len(m) < 20:
            raise ValueError("Insufficient valid magnetic samples")
        mag_means.append(m.mean(0).tolist())
        mag_stats.append({"phase": phase, "samples": len(m), "mean_uT": m.mean(0).tolist(),
                          "norm_mean_uT": float(np.linalg.norm(m, axis=1).mean())})
    body_r, quality = fit_frd(poses)
    # The parent magnetometer is separate: never assume its frame equals Spresense.
    sys.path.insert(0, str(args.firmware_root.resolve() / "scripts"))
    import body_alignment
    try:
        mag_r, mag_quality = body_alignment.magnetic_fit(mag_means)
        magnetic = {"accepted": True, "R_body_from_mag": mag_r, "quality": mag_quality}
    except ValueError as error:
        magnetic = {"accepted": False, "reason": str(error), "R_body_from_mag": None}
    magnetic["poses"] = mag_stats
    magnetic["absolute_heading_valid"] = False
    cam_path = exp.stage_path("interim") / "cameras/calibration_telemetry.csv"
    cam = numeric_csv(cam_path, ["video_time_s", "qw", "qx", "qy", "qz"])
    ct, cg = angular_signal(cam)
    fits = []
    # Stride 10 retains measured samples for clock fitting; static fitting uses native rate.
    st, sg = t[::10], g[::10]
    for win in config["motion_windows_video_s"]:
        fit = correlation_offset(ct, np.linalg.norm(cg, axis=1), st,
                                 np.linalg.norm(sg, axis=1), win,
                                 np.arange(*config["offset_search_s"], .002))
        fit = {k: v for k, v in fit.items() if k not in ("offsets_s", "scores")}
        if (fit["correlation"] < .9 or fit["at_search_boundary"]
                or fit["competing_correlation_outside_1s"] is None
                or fit["correlation"]-fit["competing_correlation_outside_1s"] < .03):
            raise ValueError(f"Camera clock fit rejected: {fit}")
        fits.append(fit)
    offset = float(np.median([f["offset_s"] for f in fits]))
    if np.ptp([f["offset_s"] for f in fits]) > .03:
        raise ValueError("Camera clock windows inconsistent")
    bounds = [begin-offset, end-offset]
    for fit in fits:
        if not bounds[0] <= fit["window_s"][0] < fit["window_s"][1] <= bounds[1]:
            raise ValueError("Calibration fit outside markers")
    train = (ct >= config["rotation_train_video_s"][0]) & (ct < config["rotation_train_video_s"][1])
    rc, cq = fit_rotation(sample_with_gaps(st, sg, ct[train]+offset), cg[train])
    hold = (ct >= config["rotation_holdout_video_s"][0]) & (ct < config["rotation_holdout_video_s"][1])
    x = sample_with_gaps(st, sg, ct[hold]+offset)
    ok = np.isfinite(x).all(1) & (np.linalg.norm(x, axis=1) > .05)
    pred, target = x[ok] @ rc.T, cg[hold][ok]
    check = {"samples": int(ok.sum()),
             "correlation": float(np.corrcoef(pred.ravel(), target.ravel())[0, 1]),
             "rms_rad_s": float(np.sqrt(np.mean((pred-target)**2)))}
    if cq["singular_ratio"] < .01 or check["correlation"] < .9:
        raise ValueError(f"Held-out camera rotation fails: {check}")
    clipped = [{**r, "source_video_s": r["parent_mono_s"]-offset,
                "clip_time_s": r["parent_mono_s"]-begin} for r in rows
               if begin <= r["parent_mono_s"] <= end]
    write_csv(out / "imu.csv", clipped)
    for w in windows:
        write_csv(out / f"pose_{w['phase']}_imu.csv",
                  [r for r in clipped if w["parent_start_s"] <= r["parent_mono_s"] <= w["parent_end_s"]])
    with cam_path.open(newline="") as f:
        camrows = [{**r, "parent_mono_s": float(r["video_time_s"])+offset,
                    "clip_time_s": float(r["video_time_s"])-bounds[0]}
                   for r in csv.DictReader(f) if bounds[0] <= float(r["video_time_s"]) <= bounds[1]]
    write_csv(out / "camera.csv", camrows)
    qcam = np.array([[float(r[k]) for k in ("qw", "qx", "qy", "qz")] for r in camrows])
    qbody = body_quaternions(qcam, body_r @ rc.T)
    write_csv(out / "body_attitude.csv", [
        {"source_video_s": r["video_time_s"], "clip_time_s": r["clip_time_s"],
         "parent_mono_s": r["parent_mono_s"],
         **dict(zip(("qw", "qx", "qy", "qz"), q, strict=True)),
         "absolute_heading_valid": False,
         "frame": "body_FRD_to_arbitrary_camera_world"}
        for r, q in zip(camrows, qbody, strict=True)])
    write_csv(out / "magnetic.csv", [{**r, "clip_time_s": r["read_end_us"]/1e6-begin}
                                    for r in magnetic_rows if begin <= r["read_end_us"]/1e6 <= end])
    write_csv(out / "health.csv", [r for r in health if begin <= r["mono_us"]/1e6 <= end])
    video = out / "calibration_only.mp4"
    command = ["ffmpeg", "-v", "error", "-nostdin", "-threads", "2", "-ss", str(bounds[0]),
               "-i", str(paths["camera_original"]), "-t", str(bounds[1]-bounds[0]),
               "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "libx264", "-threads", "2",
               "-preset", "veryfast", "-crf", "18", "-c:a", "aac", "-movflags", "+faststart",
               "-y", str(video)]
    old_path = out / "manifest.json"
    old = json.loads(old_path.read_text()) if old_path.exists() else {}
    inputs = {k: {"path": str(p), "sha256": sha256(p)} for k, p in paths.items()}
    if not (video.exists() and old.get("ffmpeg_command") == command
            and old.get("video_sha256") == sha256(video)
            and old.get("inputs", {}).get("camera_original") == inputs["camera_original"]):
        process = subprocess.Popen(command)
        print(json.dumps({"pid": process.pid, "started_utc": datetime.now(UTC).isoformat(),
                          "purpose": "parent calibration video encode", "owner": args.experiment}), flush=True)
        try:
            process.wait(timeout=300)
        except BaseException:
            process.terminate()
            process.wait(timeout=10)
            raise
        if process.returncode:
            raise RuntimeError("Video encode failed")
    manifest = {"experiment_id": args.experiment, "device": events[0]["device"],
                "status": "offline_geometric_axes_camera_interval_accepted_magnetic_rejected",
                "clock": clock, "camera_parent_offset_s": offset,
                "camera_equation": "parent_mono_s = source_video_s + camera_parent_offset_s",
                "source_video_interval_s": bounds, "camera_fits": fits,
                "R_body_from_spresense": body_r.tolist(), "pose_quality": quality,
                "pose_windows": windows, "R_camera_imu_from_spresense": rc.tolist(),
                "R_body_from_camera_imu": (body_r @ rc.T).tolist(),
                "camera_fit_quality": cq, "camera_heldout": check, "magnetic": magnetic,
                "framing": framing, "source_seq_discontinuities": missing,
                "imu_coefficients": coefficients, "imu_rows": len(clipped),
                "camera_rows": len(camrows), "events": events, "inputs": inputs,
                "ffmpeg_command": command, "video_sha256": sha256(video),
                "config_sha256": sha256(config_path), "script_sha256": sha256(Path(__file__)),
                "telemetry_sha256": sha256(cam_path),
                "clock_module_sha256": sha256(root / "src/aquabeacon_analysis/parent_axis_calibration.py"),
                "axis_module_sha256": sha256(root / "src/aquabeacon_analysis/child_axis_calibration.py"),
                "binary_reader_sha256": sha256(args.firmware_root / "scripts/binary_log.py"),
                "reader_sha256": sha256(args.firmware_root / "scripts/spresense_log.py"),
                "mag_fit_code_sha256": sha256(args.firmware_root / "scripts/body_alignment.py"),
                "git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root,
                                                       text=True).strip(),
                "limits": ["No GNSS UTC in this capture. Packet latency is not absolute timing accuracy.",
                           "Camera IMU frame, not optical extrinsic; no measured translation.",
                           "Same installation required for reuse. No firmware import profile.",
                           "Parent magnetometer and IMU have separate frames. No north heading inferred on magnetic failure."]}
    old_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    solution = {k: v for k, v in manifest.items() if k not in ("ffmpeg_command", "events")}
    (root / "calibration" / f"{args.experiment}_axes.json").write_text(
        json.dumps(solution, indent=2), encoding="utf-8")
    print(json.dumps({"video_interval": bounds, "camera_heldout": check,
                      "pose_errors": [p["direction_residual_deg"] for p in quality],
                      "magnetic": magnetic}), flush=True)


if __name__ == "__main__":
    main()
