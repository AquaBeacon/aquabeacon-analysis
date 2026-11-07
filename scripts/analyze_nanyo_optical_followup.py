"""Prepare paired position evaluation, full parent-frame evidence, and camera priors."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

os.environ.setdefault("OMP_NUM_THREADS", "2")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "2")
import cv2
import numpy as np
from evaluate_position_fallback import ARGUMENTS, dump, finite, output_rows, scatter, write_csv
from improve_nanyo_light import ROOT, read, relative
from position_fallback_nanyo import load_nanyo, numbers
from scipy.spatial.transform import Rotation

from aquabeacon_analysis.nanyo_followup import bracket_values, longest_runs
from aquabeacon_analysis.ngt_binary import sha256
from aquabeacon_analysis.parent_magnetic_attitude import sample_rotations
from aquabeacon_analysis.position_fallback import run_fallback

CONFIG = ROOT/"config/20261103_field_review_000/optical_followup.json"


def evidence(eid, out, optical_run):
    source = ROOT/f"results/{eid}/optical_detection/{optical_run}"
    rows = read(source/"reviewed_camera_relative_candidates.csv")
    old = read(ROOT/f"results/{eid}/mapping_light_tracking/camera_relative_candidates.csv")
    yes = lambda field: np.array([r[field] == "True" for r in rows])
    categories = {
        "both_missing": ~yes("optical_candidate"),
        "recovered": yes("recovered_candidate"),
        "old_algorithm_only": yes("control_candidate") & ~yes("new_algorithm_candidate"),
        "identity_rejected": yes("identity_review_rejected"),
    }
    selected = []
    for label, mask in categories.items():
        for first, end in longest_runs(mask)[:2 if label == "both_missing" else 1]:
            selected.append((label, (first+end-1)//2, int(first), int(end)))
    proxy = ROOT/f"data/interim/{eid}/optical_detection/parent_30hz.mp4"
    cap = cv2.VideoCapture(str(proxy), cv2.CAP_FFMPEG, [cv2.CAP_PROP_N_THREADS, 2])
    cases = []
    try:
        for label, i, first, end in selected:
            row = rows[i]
            target = float(row["decoded_video_pts_s"])
            cap.set(cv2.CAP_PROP_POS_MSEC, max(0, target-.2)*1000)
            ok = False
            for _ in range(100):
                if not cap.grab():
                    break
                if cap.get(cv2.CAP_PROP_POS_MSEC)/1000 >= target-.001:
                    ok, frame = cap.retrieve()
                    break
            if not ok or abs(cap.get(cv2.CAP_PROP_POS_MSEC)/1000-target) > .001:
                raise ValueError(f"Evidence frame mismatch: {eid} {i}")
            name = f"parent_{label}_{i:05d}.jpg"
            cv2.imwrite(str(out/name), frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
            annotated = frame.copy()
            for observation, color in ((old[i], (0, 170, 255)), (row, (50, 255, 70))):
                if observation["optical_candidate"] == "True":
                    point = tuple(round(float(observation[k])) for k in ("x_px", "y_px"))
                    cv2.circle(annotated, point, 13, color, 2)
            cv2.putText(annotated, f"{eid}  {float(row['segment_time_s']):.1f}s  {label}",
                        (12, 26), cv2.FONT_HERSHEY_SIMPLEX, .65, (255, 255, 255), 2)
            marked = name.replace(".jpg", "_marked.jpg")
            cv2.imwrite(str(out/marked), annotated, [cv2.IMWRITE_JPEG_QUALITY, 92])
            cases.append({"case": label, "row_index": i, "episode_rows": [first, end],
                          "episode_duration_s": (end-first)/10,
                          "utc_s": float(row["decoded_utc_s"]),
                          "segment_time_s": float(row["segment_time_s"]),
                          "source_video": row["source_video"], "source_s": float(row["decoded_source_s"]),
                          "decoded_video_pts_s": target, "image": name, "marked_image": marked,
                          "image_sha256": sha256(out/name),
                          "track_status": row["track_status"],
                          "recovery_track_status": row["recovery_track_status"],
                          "automatic_classification": "selection category only, visual cause not inferred"})
    finally:
        cap.release()
    dump(out/"parent_frame_cases.json", cases)
    dump(out/"detection_status_counts.json", dict(Counter(r["track_status"] for r in rows)))
    return [source/"reviewed_camera_relative_candidates.csv", source/"manifest.json",
            source/"identity_review_manifest.json", proxy.with_suffix(".json")]


def camera_priors(eid, out, cfg, data, results):
    mapping_source = cfg["mapping_sources"][eid]
    source_out = ROOT/f"results/{eid}/{mapping_source}"
    source_work = ROOT/f"data/interim/{eid}/{mapping_source}"
    meta_path = source_out/"manifest.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    refined = mapping_source.startswith("mapping_refinement/")
    frame_path = source_out/("frames.csv" if refined else "frame_times.csv")
    frames = read(frame_path)
    ft = np.array([float(r["utc_s" if refined else "sync_utc_s"]) for r in frames])
    at_path = ROOT/f"data/processed/{eid}/absolute_position/attitudes_10hz.csv"
    attitudes = read(at_path)
    at = numbers(attitudes, ["utc_s"])[:, 0]
    q = numbers(attitudes, [f"child_true_enu_q{c}" for c in "wxyz"])
    q[np.array([r["child_attitude_available"] != "True" for r in attitudes])] = np.nan
    oq = sample_rotations(at, q, data["times"], .15)
    rotations = np.full((len(oq), 3, 3), np.nan)
    valid = np.isfinite(oq).all(1)
    rotations[valid] = Rotation.from_quat(oq[valid][:, [1, 2, 3, 0]]).as_matrix()
    geometry = data["metadata"]["configuration"]
    lever = np.array(geometry["lever_arms_frd_m"]["child_camera"])
    offset = np.einsum("nij,j->ni", rotations, lever)
    priors = {}
    for label in ("old", "new"):
        camera = results[label]["selected"]+data["parent_origin"]+offset
        priors[label] = bracket_values(data["times"], camera, ft)
    fq = sample_rotations(at, q, ft, .15)
    axes_path = ROOT/geometry["child_axes"]
    axes = np.array(json.loads(axes_path.read_text())["R_body_from_sensor"])
    clock_path = ROOT/f"data/processed/{eid}/videos/child.clock.json"
    clock = json.loads(clock_path.read_text()) if refined else None
    if not refined:
        timing_meta = source_out/"frame_times_manifest.json"
        timing = json.loads(timing_meta.read_text())
        log = source_work/"fps_timing_debug.log"
        if sha256(log) != timing["log_sha256"] or timing["precision_s"] > .001:
            raise ValueError("Saved child images require verified native-frame timestamps")
    frame_rows = []
    for i, frame in enumerate(frames):
        if refined:
            piece = next(p for p in clock["pieces"]
                         if p["joined_start_s"] <= float(frame["clip_pts_s"]) < p["joined_end_s"])
            session = piece["camera_key"]
        else:
            session = "child_"+Path(meta["source"].replace("\\", "/")).stem
        mount = axes @ np.array(geometry["mounts"][session]["R_dji_from_sensor"]).T @ np.array(
            geometry["R_dji_from_child_optical"])
        world_camera = np.full((3, 3), np.nan)
        if np.isfinite(fq[i]).all():
            world_camera = Rotation.from_quat(fq[i, [1, 2, 3, 0]]).as_matrix() @ mount
        row = {"image": frame["image"], "utc_s": ft[i],
               "segment_time_s": ft[i]-data["metadata"]["interval_s"][0],
               "child_camera_session": session}
        for label in priors:
            row[label+"_available"] = bool(np.isfinite(priors[label][i]).all())
            row.update({f"{label}_{axis}_m": value for axis, value in zip("enu", priors[label][i])})
        row.update({f"r{a}{b}": world_camera[a, b] for a in range(3) for b in range(3)})
        frame_rows.append(row)
    if refined:
        selected = frame_rows
    else:
        span = cfg["mapping_window_s"]
        candidates = []
        for start in np.arange(0, max(1, frame_rows[-1]["segment_time_s"]-span), 10):
            subset = [r for r in frame_rows if start <= r["segment_time_s"] < start+span]
            gain = sum(r["new_available"] and not r["old_available"] for r in subset)
            candidates.append(((gain, sum(r["new_available"] for r in subset)), subset))
        selected = max(candidates, key=lambda pair: pair[0])[1]
    if not 2 <= len(selected) <= cfg["maximum_images"]:
        raise ValueError("Bounded mapping selection is empty or too large")
    for row in selected:
        row["image_sha256"] = sha256(source_work/"images"/row["image"])
    write_csv(out/"mapping_frame_priors.csv", selected)
    dump(out/"mapping_input.json", {"source": mapping_source, "source_work": relative(source_work),
                                   "intrinsics": meta["intrinsics"], "source_images": len(frames),
                                   "selected_images": len(selected),
                                   "segment_interval_s": [selected[0]["segment_time_s"], selected[-1]["segment_time_s"]],
                                   "selection": cfg["selection"], "navigation_frame": "true ENU; child camera optical center",
                                   "missing_policy": "finite exact hit or two-sided <=0.15 s bracket; no extrapolation"})
    return [meta_path, frame_path, at_path, axes_path, source_work/"features.db",
            clock_path if refined else source_out/"frame_times_manifest.json"]


def prepare(eid, cfg):
    out = ROOT/f"results/{eid}/optical_followup/{cfg['run']}"
    out.mkdir(parents=True, exist_ok=False)
    position_config = ROOT/"config/20261103_field_review_000/position_fallback.json"
    geometry = json.loads(position_config.read_text())["geometry"]
    optical = ROOT/f"results/{eid}/optical_detection/{cfg['optical_run']}/reviewed_camera_relative_candidates.csv"
    inputs, results, datasets = [CONFIG, position_config, Path(__file__)], {}, {}
    for label, path in (("old", None), ("new", optical)):
        data = load_nanyo(eid, optical_path=path)
        result = run_fallback(*(data[key] for key in ARGUMENTS), config=geometry,
                              surface_seed_mask=np.array([r["event_sample"] for r in data["records"]]))
        datasets[label], results[label] = data, result
        inputs += data["input_paths"]
        write_csv(out/f"{label}_positions.csv", output_rows(data, result))
    np.testing.assert_array_equal(datasets["old"]["times"], data["times"])
    grid = np.array([r["grid_sample"] for r in data["records"]])
    events = np.array([r["event_sample"] for r in data["records"]])
    metrics, consistency = [], []
    for window in data["metadata"]["static_windows"]:
        inside = grid & (data["times"] >= window["start_s"]) & (data["times"] < window["end_s"])
        for method in ("joint", "optical_imu_depth", "selected"):
            common = inside & finite(results["old"][method]) & finite(results["new"][method])
            for sampling, subset in (("all_available", inside), ("paired_same_times", common)):
                for label in results:
                    metrics.append({"experiment_id": eid, "window": window["id"],
                                    "condition": window["condition"], "method": method,
                                    "sampling": sampling, "version": label,
                                    "window_grid_samples": int(inside.sum()), **scatter(results[label][method][subset])})
    for label in results:
        p = results[label]["optical_imu_depth"]
        predicted = np.linalg.norm(p+data["acoustic_offsets"]-data["transmitters"], axis=1)
        delta = predicted-data["ranges"]
        mask = events & np.isfinite(delta)
        consistency.append({"experiment_id": eid, "version": label, "samples": int(mask.sum()),
                            "median_signed_m": np.median(delta[mask]) if mask.any() else None,
                            "median_abs_m": np.median(abs(delta[mask])) if mask.any() else None,
                            "p95_abs_m": np.percentile(abs(delta[mask]), 95) if mask.any() else None,
                            "interpretation": "optical-depth versus acoustic range consistency; shared surface history, not independent ground truth"})
    write_csv(out/"paired_static_rms.csv", metrics)
    write_csv(out/"range_consistency.csv", consistency)
    inputs += evidence(eid, out, cfg["optical_run"])
    inputs += camera_priors(eid, out, cfg, data, results)
    inputs += [ROOT/"src/aquabeacon_analysis/nanyo_followup.py",
               ROOT/"src/aquabeacon_analysis/position_fallback.py",
               ROOT/"scripts/evaluate_position_fallback.py"]
    dump(out/"manifest.json", {"experiment_id": eid, "run": cfg["run"],
                               "created_utc": datetime.now(UTC).isoformat(), "pid": os.getpid(),
                               "git_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                               "accuracy_validated": False, "same_position_grid": True,
                               "metadata": data["metadata"],
                               "inputs_sha256": {relative(p): sha256(p) for p in set(inputs)}})
    print(eid, "prepared", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--experiment", action="append", required=True)
    args = parser.parse_args()
    config = json.loads(CONFIG.read_text())
    if set(args.experiment)-set(config["experiments"]):
        parser.error("Explicit configured Nanyo trials required")
    for experiment in dict.fromkeys(args.experiment):
        if (ROOT/f"results/{experiment}/optical_followup/{config['run']}").exists():
            parser.error("Preserving completed/incomplete preparation; choose a new configured run")
    cv2.setNumThreads(1)
    print("followup prepare PID", os.getpid(), datetime.now(UTC).isoformat(), flush=True)
    for experiment in dict.fromkeys(args.experiment):
        prepare(experiment, config)
