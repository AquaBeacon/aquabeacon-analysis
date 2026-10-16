"""Plot parent-camera light angles before and after parent IMU attitude application."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def values(rows: list[dict[str, str]], key: str, *, radians: bool = False) -> np.ndarray:
    output = np.array([float(row[key]) if row[key] else np.nan for row in rows])
    return np.degrees(output) if radians else output


def plot_experiment(experiment_id: str, root: Path) -> tuple[Path, Path]:
    directory = root / "results" / experiment_id / "light_detection"
    source = directory / "attitude_corrected_light_angles.csv"
    with source.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    if not rows:
        raise ValueError(f"no angle samples: {source}")
    time_s = values(rows, "video_time_s")
    if np.any(np.diff(time_s) <= 0):
        raise ValueError("video times must increase")
    before_az = values(rows, "camera_azimuth_rad", radians=True)
    before_el = values(rows, "camera_elevation_rad", radians=True)
    after_az = values(rows, "local_azimuth_deg")
    after_el = values(rows, "gravity_elevation_deg")
    samples = (
        (before_az, before_el, "Before: camera-relative", "#2563a6"),
        (after_az, after_el, "After: parent attitude applied", "#c65d32"),
    )
    fig, axes = plt.subplots(2, 2, figsize=(14, 7), sharex="col", layout="constrained")
    for column, (azimuth, elevation, title, color) in enumerate(samples):
        for row_index, (angle, label) in enumerate(((azimuth, "Azimuth (deg)"),
                                                     (elevation, "Elevation (deg)"))):
            ax = axes[row_index, column]
            ax.scatter(time_s, angle, s=9, c=color, alpha=0.85, linewidths=0)
            ax.axhline(0, color="#707880", linewidth=0.8, linestyle="--")
            ax.set_ylabel(label)
            ax.set_xlim(float(time_s[0]), float(time_s[-1]))
            ax.grid(True, alpha=0.22)
            if row_index == 0:
                ax.set_title(f"{title}  (n={np.isfinite(angle).sum()})", fontsize=11)
            else:
                ax.set_xlabel("Video time (s)")
    for ax in axes[1]:
        ax.set_ylim(-40, 40)
    fig.suptitle(f"P_MAIN to child light | {experiment_id}", fontsize=15)
    fig.text(
        0.5, -0.018,
        "Before: provisional 90 deg FOV. After: fused IMU, provisional mount, "
        "heading relative to initial view. Azimuth wraps at +/-180 deg; gaps stay missing.",
        ha="center", fontsize=9, color="#4b5563",
    )
    output = directory / "angle_before_after.png"
    fig.savefig(output, dpi=180, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    manifest_path = directory / "angle_plot_manifest.json"
    inputs = [
        source,
        directory / "attitude_angle_manifest.json",
        root / "calibration" / "parent_camera_provisional.json",
        root / "calibration" / "parent_camera_imu_provisional.json",
    ]
    manifest = {
        "experiment_id": experiment_id,
        "figure": str(output.relative_to(root)),
        "figure_sha256": sha256(output),
        "camera_angle_samples": int(np.isfinite(before_az).sum()),
        "attitude_angle_samples": int(np.isfinite(after_az).sum()),
        "x_axis": "video_time_s from trimmed P_MAIN proxy; not UTC",
        "before_frame": "camera-relative right-positive azimuth and up-positive elevation",
        "after_frame": "gravity-aligned elevation and initial-view-relative horizontal azimuth",
        "plot_policy": "observed samples as points; no interpolation across missing values",
        "input_sha256": {str(path.relative_to(root)): sha256(path) for path in inputs},
        "git_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip(),
        "not_accuracy_comparison": True,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return output, manifest_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", action="append", dest="sessions")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    sessions = args.sessions or ["20260922_cmain_022", "20260922_cmain_023", "20260922_cmain_024"]
    for session in sessions:
        output, manifest = plot_experiment(session, root)
        print(output)
        print(manifest)


if __name__ == "__main__":
    main()
