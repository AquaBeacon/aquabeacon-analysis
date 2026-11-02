"""Track beacons at 10 Hz in both days' selected windows and localize once per shot."""

import argparse
import json
import subprocess
from collections import Counter
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
from analyze_nanyo import read, write
from analyze_nanyo_day2 import Day2Context
from estimate_nanyo_absolute import Context, lever_subset, q_wxyz

from aquabeacon_analysis.beacon_tracker import BeaconTracker
from aquabeacon_analysis.camera_calibration import load_camera_intrinsics
from aquabeacon_analysis.nanyo_position import ray_sphere_position
from aquabeacon_analysis.ngt_binary import sha256
from aquabeacon_analysis.ngt_optical import detect_candidate
from aquabeacon_analysis.ngt_position import calibrated_rays, nearest_indices


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--experiment", required=True, choices=["20261101_nanyo_000"])
    parser.add_argument("--window", action="append")
    args = parser.parse_args()
    configpath = Path(f"config/{args.experiment}/stationary_spectra.json")
    config = json.loads(configpath.read_text())
    reviewpath = Path(f"config/{args.experiment}/static_light_review.json")
    review = json.loads(reviewpath.read_text())
    contexts = {}
    for spec in config["windows"]:
        if args.window and spec["id"] not in args.window:
            continue
        eid, wid = spec["experiment_id"], spec["id"]
        day = eid[:8]
        if day not in contexts:
            contexts[day] = Day2Context() if day == "20261101" else Context()
        ctx = contexts[day]
        lo = datetime.fromisoformat(spec["start"]).timestamp()
        hi = lo + 60
        clock = max(
            [
                c
                for name, c in ctx.sync.items()
                if name.startswith("parent") and c["utc_at_video_zero_s"] <= lo
            ],
            key=lambda c: c["utc_at_video_zero_s"],
        )
        source = Path(clock["source_video"]["path"]).with_suffix(".LRF")
        folder = Path(f"results/{eid}/light_tracking/{wid}")
        folder.mkdir(parents=True, exist_ok=True)
        # Later manual anchors may only reinitialize a lost track; never steer a valid track.
        if wid in review["windows"]:
            seed = review["windows"][wid][0][1:]
        else:
            seed = {"day1_144110": [606, 308], "day1_150300": [540, 400]}[wid]
        tracker = BeaconTracker(seed)
        cap = cv2.VideoCapture(str(source))
        source_fps = cap.get(cv2.CAP_PROP_FPS)
        cap.set(cv2.CAP_PROP_ORIENTATION_AUTO, 0)
        source_start = (lo - clock["utc_at_video_zero_s"]) / clock["time_scale"]
        cap.set(cv2.CAP_PROP_POS_MSEC, source_start * 1000)
        rows, crops, overview, comparisons, next_utc = [], [], [], [], lo
        reseed_events = []
        writer = cv2.VideoWriter(
            str(folder / "tracking_review.mp4"), cv2.VideoWriter_fourcc(*"mp4v"), 10, (640, 360)
        )
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            sec = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000
            utc = clock["utc_at_video_zero_s"] + clock["time_scale"] * sec
            if utc >= hi:
                break
            if utc < next_utc - 1e-5:
                continue
            next_utc += 0.1
            reseed = None
            if tracker.missing >= 10 and wid in review["windows"]:
                for anchor in review["windows"][wid][1:]:
                    if abs(utc - lo - anchor[0]) < 0.05:
                        reseed = anchor[1:]
                        reseed_events.append({"offset_s": utc - lo, "seed_px": reseed})
            det = tracker.update(frame, reseed)
            baseline = detect_candidate(frame)
            row = {
                "utc_s": utc,
                "segment_time_s": utc - lo,
                "source_video": source.with_suffix(".MP4").name,
                "actual_source_s": sec,
                "frame_read": True,
                "x_px": det["x_px"],
                "y_px": det["y_px"],
                "optical_candidate": det["candidate"],
                "water_like_heuristic": True,
                **{k: v for k, v in det.items() if k not in ["x_px", "y_px", "candidate"]},
                "detection_only_candidate": baseline["status"] == "candidate",
                "detection_only_x_px": baseline["x_px"] if baseline["x_px"] is not None else np.nan,
                "detection_only_y_px": baseline["y_px"] if baseline["y_px"] is not None else np.nan,
            }
            rows.append(row)
            preview = frame.copy()
            if det["candidate"]:
                cv2.circle(preview, (round(det["x_px"]), round(det["y_px"])), 14, (0, 0, 255), 2)
            cv2.putText(
                preview,
                f"{utc - lo:.1f}s {det['track_status']}",
                (10, 30),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.8,
                (0, 0, 255),
                2,
            )
            writer.write(cv2.resize(preview, (640, 360)))
            if len(rows) % 20 == 1:
                overview.append(cv2.resize(preview, (320, 180)))
                compare = preview.copy()
                if baseline["status"] == "candidate":
                    cv2.drawMarker(
                        compare,
                        (round(baseline["x_px"]), round(baseline["y_px"])),
                        (0, 255, 255),
                        cv2.MARKER_TILTED_CROSS,
                        30,
                        3,
                    )
                comparisons.append(cv2.resize(compare, (320, 180)))
            if len(rows) % 5 == 1:
                center = tracker.point if tracker.point is not None else np.array(seed)
                x, y = np.round(center).astype(int)
                patch = cv2.getRectSubPix(preview, (160, 120), (float(x), float(y)))
                cv2.putText(
                    patch,
                    f"{utc - lo:.1f}s",
                    (3, 15),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.4,
                    (0, 0, 255),
                    1,
                )
                crops.append(patch)
        cap.release()
        writer.release()
        write(folder / "camera_relative_candidates.csv", rows)
        for name, frames, cols in [
            ("overview.jpg", overview, 5),
            ("all_crops.jpg", crops, 10),
            ("detection_vs_tracking.jpg", comparisons, 5),
        ]:
            while len(frames) % cols:
                frames.append(np.zeros_like(frames[0]))
            cv2.imwrite(
                str(folder / name),
                np.vstack([np.hstack(frames[i : i + cols]) for i in range(0, len(frames), cols)]),
            )
        range_path = Path(f"results/{eid}/evaluation/acoustic_depth.csv")
        ranges = [r for r in read(range_path) if lo <= float(r["sync_utc_s"]) < hi]
        t = np.array([float(r["sync_utc_s"]) for r in ranges])
        state = ctx.sample(t)
        ot = np.array([r["utc_s"] for r in rows])
        index, delta = nearest_indices(ot, t, 0.075)
        k, d, intrinsics = load_camera_intrinsics(Path.cwd(), "P_MAIN_CAM", (1280, 720), "proxy")
        positions = []
        for i, event in enumerate(ranges):
            opt = rows[index[i]] if index[i] >= 0 else {}
            rho = float(event["corrected_range_m"])
            good = (
                opt.get("optical_candidate", False)
                and event["acoustic_candidate"] == "True"
                and all(
                    np.isfinite(state[a][i]).all()
                    for a in ["parent", "child", "parent_opt", "gnss"]
                )
            )
            origin = np.full(3, np.nan)
            residual = np.nan
            if good:
                ray = calibrated_rays([[opt["x_px"], opt["y_px"]]], k, d)[0]
                origin, _, _, residual = ray_sphere_position(
                    state["gnss"][i],
                    state["parent"][i],
                    state["child"][i],
                    ray,
                    state["parent_opt"][i],
                    rho,
                    **lever_subset(ctx.lever),
                )
            r = {
                "utc_s": t[i],
                "segment_time_s": t[i] - lo,
                "source_video": source.with_suffix(".MP4").name,
                "source_video_s": opt.get("actual_source_s", np.nan),
                "x_px": opt.get("x_px", np.nan),
                "y_px": opt.get("y_px", np.nan),
                "optical_candidate": opt.get("optical_candidate", False),
                "range_candidate": event["acoustic_candidate"],
                "shot_seq": event["shot_seq"],
                "range_delta_s": float(delta[i]),
                "range_m": rho,
                "position_available": bool(np.isfinite(origin).all()),
                "acoustic_equation_residual_m": residual,
                "track_status": opt.get("track_status", "no_image"),
            }
            for label, vector in [("child_origin", origin), ("parent_antenna", state["gnss"][i])]:
                r.update(
                    {
                        f"{label}_{axis}_m": float(v)
                        for axis, v in zip(["east", "north", "up"], vector)
                    }
                )
            for role in ["parent", "child"]:
                r.update(
                    {
                        f"{role}_true_enu_{q}": float(v)
                        for q, v in zip(["qw", "qx", "qy", "qz"], q_wxyz(state[role][i]))
                    }
                )
            positions.append(r)
        write(folder / "positions.csv", positions)
        at = lo + np.arange(600) / 10
        astate = ctx.sample(at)
        attitude_rows = []
        for i, utc in enumerate(at):
            row = {"utc_s": utc}
            for role in ["parent", "child"]:
                row[f"{role}_attitude_available"] = bool(np.isfinite(astate[role][i]).all())
                row.update(
                    {
                        f"{role}_true_enu_{q}": float(v)
                        for q, v in zip(["qw", "qx", "qy", "qz"], q_wxyz(astate[role][i]))
                    }
                )
            attitude_rows.append(row)
        write(folder / "attitudes_10hz.csv", attitude_rows)
        manifest = {
            "window": spec,
            "configuration": ctx.cfg,
            "intrinsics": intrinsics,
            "seed_px": seed,
            "sample_hz": 10,
            "source_fps": source_fps,
            "rows": len(rows),
            "accepted": sum(r["optical_candidate"] for r in rows),
            "available_positions": sum(r["position_available"] for r in positions),
            "track_status_counts": dict(Counter(r["track_status"] for r in rows)),
            "smoothing_applied": False,
            "tracking_method": "LK forward/backward prediction, fixed 81x81 image NCC, local 15px light centroid; reviewed seed ROI 20px; failed seed waits for next reviewed initialization",
            "git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
            "later_manual_anchors_used": bool(reseed_events),
            "reseed_events": reseed_events,
            "geometry_time": "one row per acoustic event; optical within 75 ms; attitudes at acoustic UTC",
            "inputs_sha256": {
                str(p): sha256(p)
                for p in [
                    configpath,
                    reviewpath,
                    range_path,
                    Path(__file__),
                    Path("src/aquabeacon_analysis/beacon_tracker.py"),
                    Path("src/aquabeacon_analysis/beacon_halo.py"),
                    Path("src/aquabeacon_analysis/ngt_optical.py"),
                    Path("scripts/estimate_nanyo_absolute.py"),
                    Path("src/aquabeacon_analysis/nanyo_position.py"),
                ]
            },
            "upstream_configuration_inputs_sha256": {str(p): sha256(p) for p in ctx.inputs},
            "source_video": str(source),
            "source_video_size": source.stat().st_size,
        }
        paired = [r for r in rows if r["optical_candidate"] and r["detection_only_candidate"]]
        differences = [
            float(
                np.hypot(r["x_px"] - r["detection_only_x_px"], r["y_px"] - r["detection_only_y_px"])
            )
            for r in paired
        ]
        manifest["detection_only_comparison"] = {
            "candidate_count": sum(r["detection_only_candidate"] for r in rows),
            "paired_count": len(paired),
            "coordinate_difference_median_px": float(np.median(differences)) if paired else None,
            "disagreement_over_40_px": sum(v > 40 for v in differences),
            "definition": "Same exact images; disagreement is not ground-truth error. Yellow cross=baseline, red circle=tracking.",
        }
        (folder / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        print(
            wid,
            "tracked",
            manifest["accepted"],
            "/",
            len(rows),
            "positions",
            manifest["available_positions"],
            manifest["track_status_counts"],
            flush=True,
        )


if __name__ == "__main__":
    main()
