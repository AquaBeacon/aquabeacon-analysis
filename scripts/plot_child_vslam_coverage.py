"""Plot saved-pose coverage over elapsed camera time for child-only VSLAM runs."""

from __future__ import annotations

import csv
from pathlib import Path

import matplotlib.pyplot as plt

from aquabeacon_analysis.editable_plots import save_editable_figure


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    fig, axes = plt.subplots(3, 1, figsize=(10, 4.8), sharex=False, layout="constrained")
    for ax, suffix in zip(axes, ("022", "023", "024")):
        path = (root / f"results/20260922_cmain_{suffix}/vislam_depth/"
                f"calibrated_mono_{suffix}_full/camera_trajectory_status.csv")
        with path.open(encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream))
        times = [(int(row["utc_ns"]) - int(rows[0]["utc_ns"])) * 1e-9 for row in rows]
        status = [row["pose_status"] == "saved" for row in rows]
        ax.fill_between(times, 0, 1, where=status, step="mid", color="#147d64")
        ax.set(xlim=(0, times[-1]), ylim=(0, 1), yticks=[], ylabel=suffix)
        ax.grid(axis="x", color="#dddddd")
    axes[-1].set_xlabel("Elapsed child camera time (s)")
    fig.suptitle("Child-only calibrated monocular VSLAM: saved pose coverage")
    output = root / "results/child_vslam_20260922_coverage.png"
    save_editable_figure(fig, output, dpi=180)
    print(output)


if __name__ == "__main__":
    main()
