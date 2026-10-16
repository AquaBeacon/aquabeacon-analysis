"""Inspect BNO085 magnetic observations without treating them as a compass calibration."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from scipy.spatial.transform import Rotation


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def inspect_events(rows: list[dict[str, str]]) -> tuple[dict[str, object], np.ndarray, np.ndarray, np.ndarray]:
    """Measure field consistency in the Game Rotation Vector's gravity frame."""
    magnetic = [row for row in rows if row["sensor_id"] == "3"]
    game = [row for row in rows if row["sensor_id"] == "8"]
    if not magnetic or not game:
        raise ValueError("both magnetic-field (3) and game-rotation-vector (8) reports are required")
    magnetic_times = np.array([float(row["window_time_s"]) for row in magnetic])
    game_times = np.array([float(row["window_time_s"]) for row in game])
    field = np.array([[float(row[key]) for key in ("x", "y", "z")] for row in magnetic])
    norm = np.linalg.norm(field, axis=1)
    hi = np.clip(np.searchsorted(game_times, magnetic_times), 0, len(game_times) - 1)
    lo = np.maximum(hi - 1, 0)
    match = np.where(abs(game_times[lo] - magnetic_times) <= abs(game_times[hi] - magnetic_times), lo, hi)
    lag = abs(game_times[match] - magnetic_times)
    keep = (lag <= 0.03) & np.isfinite(norm) & (norm > 0)
    quaternion = np.array([[float(game[index][key]) for key in ("x", "y", "z", "w")]
                           for index in match[keep]])
    world_field = Rotation.from_quat(quaternion).apply(field[keep])
    heading = np.arctan2(world_field[:, 1], world_field[:, 0])
    concentration = float(abs(np.mean(np.exp(1j * heading))))
    reference = float(np.angle(np.mean(np.exp(1j * heading))))
    relative_deg = np.rad2deg(np.angle(np.exp(1j * (heading - reference))))
    summary = {
        "magnetic_reports": len(magnetic),
        "game_rotation_vector_reports": len(game),
        "paired_reports": int(keep.sum()),
        "magnetic_norm_uT_p5_p50_p95": np.percentile(norm[keep], [5, 50, 95]).tolist(),
        "game_frame_heading_concentration": concentration,
        "heading_relative_to_circular_mean_deg_p5_p50_p95": np.percentile(relative_deg, [5, 50, 95]).tolist(),
        "nearest_game_report_lag_ms_p95": float(np.percentile(lag[keep], 95) * 1000),
    }
    return summary, magnetic_times[keep], norm[keep], relative_deg


def diagnose(root: Path, experiment: str) -> dict[str, object]:
    input_path = next((root / "data" / "processed" / experiment / "tof_window" / "c_main")
                      .glob("C_MAIN_BNO_EVENTS_*.csv"))
    output = root / "results" / experiment / "calibrated_estimate"
    output.mkdir(parents=True, exist_ok=True)
    summary, times, norm, heading = inspect_events(read_rows(input_path))
    fig, axes = plt.subplots(2, 1, figsize=(11, 6), sharex=True)
    axes[0].plot(times, norm, linewidth=0.8, color="#2082a0")
    axes[0].set_ylabel("Magnetic field magnitude [uT]")
    axes[1].plot(times, heading, linewidth=0.8, color="#dc6e33")
    axes[1].set_ylabel("Horizontal field direction [deg]")
    axes[1].set_xlabel("ToF window time [s]")
    lower, upper = np.percentile(heading, [1, 99])
    if upper - lower < 120:
        axes[1].set_ylim(lower - 5, upper + 5)
    else:
        axes[1].set_ylim(-180, 180)
    fig.suptitle(f"{experiment}: BNO085 field in Game Rotation Vector frame")
    fig.text(0.5, 0.01, "Diagnostic only: GRV excludes magnetic heading; parent compass frame unavailable",
             ha="center", fontsize=9)
    fig.tight_layout(rect=(0, 0.04, 1, 0.96))
    fig.savefig(output / "magnetometer_diagnostic.png", dpi=150)
    plt.close(fig)
    metadata = {
        "experiment_id": experiment,
        "report_mapping": {"3": "calibrated magnetic field", "8": "game rotation vector"},
        "magnetic_field_unit": "microtesla, as logged by BNO085 firmware",
        "interpretation": "field rotated by magnetometer-independent game quaternion; changes combine magnetic disturbance and yaw drift",
        "heading_reference": "circular mean within this experiment, not geographic north",
        "heading_applied_to_pose": False,
        "reason_not_applied": "mount calibration, magnetic calibration quality, and parent heading reference are unverified; 022 varies strongly",
        "summary": summary,
        "source_csv": str(input_path.relative_to(root)),
        "source_sha256": sha256(input_path),
        "git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(),
    }
    (output / "magnetometer_manifest.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return metadata


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", action="append", dest="sessions")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    for experiment in args.sessions or ("20260922_cmain_022", "20260922_cmain_023", "20260922_cmain_024"):
        result = diagnose(root, experiment)
        print(experiment, result["summary"]["paired_reports"],
              f"concentration={result['summary']['game_frame_heading_concentration']:.3f}")


if __name__ == "__main__":
    main()
