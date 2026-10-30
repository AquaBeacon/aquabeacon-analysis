"""Estimate both camera clocks from IMU rotation, preserving independent checks."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from aquabeacon_analysis.editable_plots import save_editable_figure
from aquabeacon_analysis.experiments import load_experiment
from aquabeacon_analysis.ngt_sync import (
    angular_signal,
    correlation_offset,
    fit_rotation,
    numeric_csv,
    sample_with_gaps,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", required=True)
    args = parser.parse_args()
    exp = load_experiment(Path.cwd(), args.experiment)
    folder = exp.stage_path("interim") / "cameras"
    output = exp.stage_path("results") / "synchronization"
    output.mkdir(parents=True, exist_ok=True)
    print(json.dumps({"pid": os.getpid(), "started_utc": datetime.now(UTC).isoformat(),
                      "purpose": "IMU camera affine clock fits"}), flush=True)
    config_path = Path.cwd() / exp.metadata["config_paths"][0]
    config = json.loads(config_path.read_text())
    manifest = {"experiment_id": args.experiment,
                "git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"],
                                                      text=True).strip(),
                "config": str(config_path), "cameras": {},
                "method": "rotation-invariant gyro norm correlation; 3-axis Wahba validation",
                "clock_equation": "UTC seconds = utc_at_video_zero_s + time_scale * video_time_s",
                "limitations": ["Timing residual measures consistency, not external absolute accuracy.",
                                "DJI angular rate is quaternion derivative at LRF telemetry rate.",
                                "Body sensor delivery latency is included in empirical alignment.",
                                "Camera IMU frame is not a verified optical or CAD body frame."]}
    for role in ("parent", "child"):
        cam = numeric_csv(folder / f"{role}_telemetry.csv",
                          ["video_time_s", "qw", "qx", "qy", "qz",
                           "accel_x_g", "accel_y_g", "accel_z_g"])
        ct, cg = angular_signal(cam)
        if role == "parent":
            source = exp.stage_path("interim") / "decoded/spresence/parent_100hz_bin_means.csv"
            body = numeric_csv(source, ["sample_utc_us", "corrected_gx", "corrected_gy",
                                        "corrected_gz", "corrected_ax", "corrected_ay",
                                        "corrected_az"])
            windows = [(150, 240), (480, 700), (1000, 1100), (1260, 1500), (1700, 1900)]
        else:
            source = exp.stage_path("interim") / "decoded/child/C_MAIN_IMU_100Hz_83304.imu.csv"
            body = numeric_csv(source, ["utc_us", "gx", "gy", "gz", "ax", "ay", "az"])
            windows = [(540, 620), (700, 850), (950, 1100), (1200, 1400), (1400, 1510)]
        video = exp.raw_root / config[f"{role}_video"]
        probe = json.loads(subprocess.check_output([
            "ffprobe", "-v", "error", "-show_entries", "format_tags=creation_time",
            "-of", "json", str(video)], text=True, timeout=30))
        hint = probe["format"]["tags"]["creation_time"]
        epoch = datetime.fromisoformat(hint).timestamp()
        body = body[body[:, 0] > 1e15]
        bt = body[:, 0] / 1e6 - epoch
        c_norm, b_norm = np.linalg.norm(cg, axis=1), np.linalg.norm(body[:, 1:4], axis=1)
        results = []
        for window in windows:
            coarse = correlation_offset(ct, c_norm, bt, b_norm, window,
                                         np.arange(-25, 25.001, .1))
            fine = correlation_offset(ct, c_norm, bt, b_norm, window,
                                       np.arange(coarse["offset_s"] - .2,
                                                 coarse["offset_s"] + .201, .005))
            grid = (ct >= window[0]) & (ct < window[1])
            x = sample_with_gaps(bt, body[:, 1:4], ct[grid] + fine["offset_s"])
            fine["coarse_competing_peak"] = coarse["competing_correlation_outside_1s"]
            try:
                rotation, quality = fit_rotation(x, cg[grid])
                fine["rotation_fit"] = quality
                fine["R_camera_imu"] = rotation.tolist()
            except ValueError as error:
                fine["rotation_fit"] = {"rejected": str(error)}
            fine["used_for_clock_fit"] = bool(
                fine["correlation"] >= .9 and not fine["at_search_boundary"]
                and fine["rotation_fit"].get("correlation", 0) >= .85
                and fine["rotation_fit"].get("singular_ratio", 0) >= .01)
            results.append(fine)
        accepted = [row for row in results if row["used_for_clock_fit"]]
        centers = np.array([np.mean(row["window_s"]) for row in accepted])
        offsets = np.array([row["offset_s"] for row in accepted])
        if len(accepted) < 3 or np.ptp(centers) < 500:
            raise ValueError(f"{role}: insufficient independent motion for drift fit")
        slope, intercept = np.polyfit(centers, offsets, 1)
        scale = 1 + slope
        residual = offsets - (intercept + slope * centers)
        calibration = accepted[0]
        fit_win = calibration["window_s"]
        selection = (ct >= fit_win[0]) & (ct < fit_win[1])
        aligned = sample_with_gaps(bt, body[:, 1:4], intercept + scale * ct[selection])
        rotation, quality = fit_rotation(aligned, cg[selection])
        result = {"clock_hint_utc": hint, "utc_at_video_zero_s": epoch + intercept,
                  "utc_at_video_zero_iso": datetime.fromtimestamp(epoch + intercept,
                                                                   UTC).isoformat(),
                  "time_scale": scale, "drift_ppm": slope * 1e6,
                  "offset_from_creation_time_s": intercept,
                  "window_fit_residual_max_s": float(np.max(np.abs(residual))),
                  "window_fit_residual_rms_s": float(np.sqrt(np.mean(residual**2))),
                  "search_step_s": .005, "source": str(source),
                  "video": str(video), "telemetry": str(folder / f"{role}_telemetry.csv"),
                  "R_camera_imu": rotation.tolist(), "mounting_quality": quality,
                  "mounting_motion_window_video_s": fit_win, "windows": results}
        manifest["cameras"][role] = result
        np.savez(folder / f"{role}_sync_input.npz", camera=cam, ct=ct, cg=cg,
                 bt=bt, body=body, epoch=epoch)
        fig, axes = plt.subplots(2, 1, figsize=(10, 6))
        win = fit_win
        chosen = (ct >= win[0]) & (ct < win[1])
        axes[0].plot(ct[chosen], c_norm[chosen], label="Camera IMU quaternion rate", lw=1.2)
        axes[0].plot(ct[chosen], sample_with_gaps(bt, b_norm,
                                                intercept + scale * ct[chosen]),
                     label="Body IMU at corrected time", lw=.9, alpha=.8)
        axes[0].set(xlabel="Source video time [s]", ylabel="Angular speed [rad/s]",
                    title=f"{role}: synchronization motion")
        axes[0].legend()
        axes[1].plot(centers, offsets, "o", label="Independent motion windows")
        axes[1].plot([0, cam[-1, 0]], intercept + slope * np.array([0, cam[-1, 0]]),
                     label=f"Affine fit ({slope * 1e6:.1f} ppm)")
        axes[1].set(xlabel="Source video time [s]", ylabel="Correction to creation tag [s]")
        axes[1].legend()
        for axis in axes:
            axis.grid(True, alpha=.3)
        fig.tight_layout()
        save_editable_figure(fig, output / f"{role}_imu_sync.png", dpi=160)
        plt.close(fig)
        print(json.dumps({k: v for k, v in result.items()
                          if k not in ("windows", "R_camera_imu")}), flush=True)
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
