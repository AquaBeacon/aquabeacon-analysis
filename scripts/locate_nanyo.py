"""Unverified light/range camera-frame position candidates, never global truth."""

import argparse
import json
from pathlib import Path

import cv2
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from analyze_nanyo import read, write

from aquabeacon_analysis.camera_calibration import load_camera_intrinsics
from aquabeacon_analysis.editable_plots import save_editable_figure
from aquabeacon_analysis.ngt_binary import sha256
from aquabeacon_analysis.ngt_optical import detect_candidate
from aquabeacon_analysis.ngt_position import calibrated_rays, nearest_indices


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", required=True)
    parser.add_argument("--reuse", action="store_true")
    args = parser.parse_args()
    root = Path.cwd()
    eid = args.experiment
    out = root / f"results/{eid}/relative_position"
    out.mkdir(parents=True, exist_ok=True)
    if args.reuse:
        _, _, prov = load_camera_intrinsics(root, "P_MAIN_CAM", (1280, 720), "proxy")
        summarize(eid, out, prov, read(out / "camera_relative_candidates.csv"))
        return
    seg = json.loads((root / f"experiments/{eid}/experiment.json").read_text())
    clocks = json.loads(
        (root / "results/20261031_nanyo_000/synchronization/manifest.json").read_text()
    )["cameras"]
    parents = sorted(
        [c for k, c in clocks.items() if k.startswith("parent")],
        key=lambda c: c["utc_at_video_zero_s"],
    )
    times = np.arange(*seg["utc_interval_s"], 0.5)
    ranges = read(root / f"results/{eid}/evaluation/acoustic_depth.csv")
    idx, _delta = nearest_indices([float(r["sync_utc_s"]) for r in ranges], times, 0.3)
    caps = {
        c["source_video"]["path"]: cv2.VideoCapture(
            str(Path(c["source_video"]["path"]).with_suffix(".LRF"))
        )
        for c in parents
    }
    k, d, prov = load_camera_intrinsics(root, "P_MAIN_CAM", (1280, 720), "proxy")
    rows, previews = [], []
    for i, utc in enumerate(times):
        clock = max(
            [c for c in parents if c["utc_at_video_zero_s"] <= utc],
            key=lambda c: c["utc_at_video_zero_s"],
        )
        cap = caps[clock["source_video"]["path"]]
        cap.set(cv2.CAP_PROP_ORIENTATION_AUTO, 0)
        source_s = (utc - clock["utc_at_video_zero_s"]) / clock["time_scale"]
        cap.set(cv2.CAP_PROP_POS_MSEC, source_s * 1000)
        ok, frame = cap.read()
        row = {
            "utc_s": utc,
            "segment_time_s": utc - seg["utc_interval_s"][0],
            "source_video": Path(clock["source_video"]["path"]).name,
            "requested_source_s": source_s,
            "frame_read": ok,
            "x_px": np.nan,
            "y_px": np.nan,
            "optical_candidate": False,
            "water_like_heuristic": False,
            "range_candidate": False,
            "range_m": np.nan,
            "x_right_candidate_m": np.nan,
            "y_down_candidate_m": np.nan,
            "z_forward_candidate_m": np.nan,
            "global_position_valid": False,
            "geometry_validated": False,
        }
        if ok:
            assert frame.shape[:2] == (720, 1280)
            row["actual_source_s"] = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000
            det = detect_candidate(frame)
            # Retain this as a heuristic, not a medium or beacon identity certification.
            blue = (frame[:, :, 0].astype(float) > frame[:, :, 2] * 1.25) & (frame[:, :, 0] > 30)
            row["water_like_heuristic"] = bool(blue.mean() > 0.7 and frame.mean() < 140)
            row["optical_candidate"] = det["status"] == "candidate"
            row["x_px"] = det["x_px"] if det["x_px"] is not None else np.nan
            row["y_px"] = det["y_px"] if det["y_px"] is not None else np.nan
            if idx[i] >= 0:
                r = ranges[idx[i]]
                row["range_candidate"] = r["acoustic_candidate"] == "True"
                row["range_m"] = float(r["corrected_range_m"])
            if row["optical_candidate"] and row["water_like_heuristic"] and row["range_candidate"]:
                ray = calibrated_rays([[row["x_px"], row["y_px"]]], k, d)[0]
                xyz = ray * row["range_m"]
                row.update(
                    zip(
                        ["x_right_candidate_m", "y_down_candidate_m", "z_forward_candidate_m"],
                        xyz.tolist(),
                        strict=True,
                    )
                )
            if i % max(1, len(times) // 12) == 0:
                img = frame.copy()
                if det["bbox"]:
                    x, y, w, h = det["bbox"]
                    cv2.rectangle(img, (x, y), (x + w, y + h), (0, 0, 255), 3)
                cv2.putText(
                    img,
                    f"{row['segment_time_s']:.1f}s candidate={np.isfinite(row['z_forward_candidate_m'])}",
                    (10, 35),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.8,
                    (0, 0, 255),
                    2,
                )
                previews.append(cv2.resize(img, (480, 270)))
        else:
            row["actual_source_s"] = np.nan
        rows.append(row)
    for cap in caps.values():
        cap.release()
    write(out / "camera_relative_candidates.csv", rows)
    fig, axes = plt.subplots(3, 1, figsize=(11, 7), sharex=True)
    for ax, col, label in zip(
        axes,
        ["x_right_candidate_m", "y_down_candidate_m", "z_forward_candidate_m"],
        ["Camera right [m]", "Camera down [m]", "Camera forward [m]"],
        strict=True,
    ):
        ax.plot([r["segment_time_s"] / 60 for r in rows], [r[col] for r in rows], lw=0.7)
        ax.set(ylabel=label)
        ax.grid(alpha=0.3)
    axes[-1].set(xlabel="Minutes from interval start")
    fig.suptitle("Conditional camera-frame positions: mounting/medium/identity not validated")
    fig.tight_layout()
    save_editable_figure(fig, out / "relative_candidates.png", dpi=150)
    plt.close(fig)
    while len(previews) % 3:
        previews.append(np.zeros_like(previews[0]))
    cv2.imwrite(
        str(out / "light_review.jpg"),
        np.vstack([np.hstack(previews[i : i + 3]) for i in range(0, len(previews), 3)]),
    )
    summarize(eid, out, prov, rows)


def summarize(eid, out, prov, rows):
    manifest = {
        "experiment_id": eid,
        "sample_hz": 2,
        "samples": len(rows),
        "candidate_positions": int(
            sum(np.isfinite(float(r["z_forward_candidate_m"])) for r in rows)
        ),
        "intrinsics": prov,
        "orientation": "native raster, autorotation disabled",
        "source_code_sha256": sha256(Path(__file__)),
        "assumptions": [
            "Distance and optical centers treated as coincident only for candidate output.",
            "Bright component identity and medium require manual review.",
            "No optical-to-body extrinsic, so no global/ENU child coordinates.",
            "Candidate coordinates are in the moving parent camera frame, not a trajectory in a fixed frame.",
        ],
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(eid, manifest["candidate_positions"], flush=True)


if __name__ == "__main__":
    main()
