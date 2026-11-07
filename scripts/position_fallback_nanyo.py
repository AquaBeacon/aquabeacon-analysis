"""Read saved Nanyo observations without rerunning tracking or changing calibration."""
from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path

import numpy as np
from scipy.spatial.transform import Rotation

from aquabeacon_analysis.camera_calibration import load_camera_intrinsics
from aquabeacon_analysis.nanyo_position import ray_sphere_position
from aquabeacon_analysis.ngt_position import calibrated_rays, nearest_indices
from aquabeacon_analysis.ngt_sync import sample_with_gaps
from aquabeacon_analysis.parent_magnetic_attitude import sample_rotations

ROOT = Path(__file__).resolve().parents[1]


def read(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def numbers(rows, columns):
    return np.array([[float(r.get(c) or "nan") for c in columns] for r in rows])


def mixed_camera_rays(pixels, main_mask, proxy_k, proxy_d, main_k=None, main_d=None):
    """Keep LRF rays; map resized MP4 pixel centers back to native 1920x1080."""
    rays = calibrated_rays(pixels, proxy_k, proxy_d)
    main_mask = np.asarray(main_mask, bool)
    if main_mask.any():
        if main_k is None or main_d is None:
            raise ValueError("MP4-derived observations require main-video calibration")
        native_pixels = (np.asarray(pixels)[main_mask]+.5)*1.5-.5
        rays[main_mask] = calibrated_rays(native_pixels, main_k, main_d)
    return rays


def load_nanyo(eid, *, optical_path=None):
    """Return relative true-ENU geometry on a 10 Hz grid and unique shot times."""
    inputs = []

    def source(relative):
        path = ROOT / relative
        inputs.append(path)
        return path

    def load(relative):
        return json.loads(source(relative).read_text(encoding="utf-8"))

    registry = load(f"experiments/{eid}/experiment.json")
    acq = registry["parent_acquisition"]
    lo, hi = registry["utc_interval_s"]
    meta = load(f"results/{eid}/absolute_position/manifest.json")
    cfg = meta["configuration"]
    # Use the reviewed full-trial tracks, including explicit rejected/missing rows.
    if optical_path is None:
        optical_path = ROOT / f"results/{eid}/mapping_light_tracking/camera_relative_candidates.csv"
    optical_path = Path(optical_path).resolve()
    optical = read(source(optical_path))
    optical_meta = load(optical_path.parent / "manifest.json")
    if optical_meta.get("experiment_id") != eid:
        raise ValueError("Optical input manifest must identify the requested experiment")
    shots = read(source(f"results/{eid}/evaluation/acoustic_depth.csv"))
    shots = [r for r in shots if lo <= float(r["sync_utc_s"]) < hi]
    shot_t = numbers(shots, ["sync_utc_s"])[:, 0]
    if len(np.unique(shot_t)) != len(shot_t):
        raise ValueError(f"{eid}: repeated shot times need explicit resolution")
    grid_t = lo + np.arange(int(np.ceil((hi - lo) * 10))) / 10
    grid_t = grid_t[grid_t < hi]
    times = np.union1d(grid_t, shot_t)
    count = len(times)
    grid = np.isin(times, grid_t)
    events = np.isin(times, shot_t)
    optical_t = numbers(optical, ["utc_s"])[:, 0]
    oi, odelta = nearest_indices(optical_t, times, 0.075)
    ri, rdelta = nearest_indices(shot_t, times, 0.3)
    attitudes = read(source(f"data/processed/{eid}/absolute_position/attitudes_10hz.csv"))
    at = numbers(attitudes, ["utc_s"])[:, 0]
    matrices = {}
    for role in ("parent", "child"):
        q = numbers(attitudes, [f"{role}_true_enu_q{c}" for c in "wxyz"])
        q[np.array([r[f"{role}_attitude_available"] != "True" for r in attitudes])] = np.nan
        sampled = sample_rotations(at, q, times, 0.15)
        valid = np.isfinite(sampled).all(axis=1)
        matrix = np.full((count, 3, 3), np.nan)
        matrix[valid] = Rotation.from_quat(sampled[valid][:, [1, 2, 3, 0]]).as_matrix()
        matrices[role] = matrix
    depth_rows = read(source(f"results/{acq}/segmentation/selection.csv"))
    depth_meta = load(f"results/{acq}/segmentation/manifest.json")
    depth_key = "depth_m" if "depth_m" in depth_rows[0] else "relative_depth_m"
    depth_data = numbers(depth_rows, ["sync_utc_s", depth_key])
    depth = sample_with_gaps(depth_data[:, 0], depth_data[:, 1], times, 2.0)
    axes = np.array(load(cfg["parent_axes"])["R_body_from_spresense"])
    for key in ("child_axes", "parent_mag_mount", "rig", "child_mount"):
        source(cfg[key])
    k, d, intrinsics = load_camera_intrinsics(ROOT, "P_MAIN_CAM", (1280, 720), "proxy")
    has_main = any(r.get("recovered_candidate") == "True" for r in optical)
    main_k, main_d, main_intrinsics = None, None, None
    if has_main:
        if optical_meta.get("coordinate_basis") != (
                "encoded_unrotated_MP4_resampled_to_1280x720_with_pixel_center_mapping"):
            raise ValueError("Recovered optical input must identify its MP4 pixel basis")
        main_k, main_d, main_intrinsics = load_camera_intrinsics(
            ROOT, "P_MAIN_CAM", (1920, 1080), "main")
    source("calibration/camera_defaults.json")
    source(intrinsics["source"])
    pixels = np.full((count, 2), np.nan)
    main_mask = np.zeros(count, dtype=bool)
    ranges = np.full(count, np.nan)
    body_optical = np.full((count, 3, 3), np.nan)
    records = []
    for i, time in enumerate(times):
        o = optical[oi[i]] if oi[i] >= 0 else {}
        r = shots[ri[i]] if ri[i] >= 0 else {}
        optical_ok = o.get("optical_candidate") == "True" and o.get("water_like_heuristic") == "True"
        range_ok = r.get("acoustic_candidate") == "True"
        if optical_ok:
            pixels[i] = [float(o["x_px"]), float(o["y_px"])]
            main_mask[i] = o.get("recovered_candidate") == "True"
            mount = cfg["mounts"]["parent_" + Path(o["source_video"]).stem]
            body_optical[i] = (axes @ np.array(mount["R_dji_from_sensor"]).T
                               @ np.array(cfg["R_dji_from_parent_optical"]))
        if range_ok:
            ranges[i] = float(r["corrected_range_m"])
        records.append({"utc_s": time, "segment_time_s": time - lo,
                        "grid_sample": bool(grid[i]), "event_sample": bool(events[i]),
                        "shot_seq": r.get("shot_seq", ""),
                        "range_time_delta_s": rdelta[i], "optical_time_delta_s": odelta[i],
                        "optical_available": optical_ok, "range_available": range_ok,
                        "source_video": o.get("source_video", ""),
                        "source_video_s": o.get("actual_source_s", ""),
                        "optical_intrinsics_source_kind": "main" if main_mask[i] else "proxy",
                        "track_status": o.get("track_status", "missing"),
                        "parent_attitude_available": bool(np.isfinite(matrices["parent"][i]).all()),
                        "child_attitude_available": bool(np.isfinite(matrices["child"][i]).all())})
    camera_rays = mixed_camera_rays(pixels, main_mask, k, d, main_k, main_d)
    rp, rc = matrices["parent"], matrices["child"]
    rays = np.einsum("nij,nj->ni", rp @ body_optical, camera_rays)
    lever = {key: np.array(value) for key, value in cfg["lever_arms_frd_m"].items()}
    offsets = {key: np.einsum("nij,j->ni", rp if key.startswith("parent") else rc, value)
               for key, value in lever.items()}
    joint = np.full((count, 3), np.nan)
    for i in np.flatnonzero(np.isfinite(rays).all(axis=1) & np.isfinite(ranges)
                           & np.isfinite(rc).all(axis=(1, 2))):
        joint[i], *_ = ray_sphere_position(
            offsets["parent_antenna"][i], rp[i], rc[i], camera_rays[i], body_optical[i], ranges[i],
            **{key: value for key, value in lever.items() if key != "child_imu"})
    gnss_rows = read(source(f"results/{acq}/evaluation/ppk_positions.csv"))
    g = numbers(gnss_rows, ["utc_s", "east_m", "north_m", "up_m"])
    gnss = sample_with_gaps(g[:, 0], g[:, 1:], times, 1.5)
    parent_origin = gnss - offsets["parent_antenna"]
    spec = load("config/20261101_nanyo_000/stationary_spectra.json")
    windows = []
    for window in spec["windows"]:
        if window["experiment_id"] == eid:
            start = datetime.fromisoformat(window["start"]).timestamp()
            windows.append({**window, "start_s": start, "end_s": start + spec["duration_s"]})
    return {"times": times, "joint_positions": joint, "rays": rays,
            "cameras": offsets["parent_camera"], "transmitters": offsets["parent_acoustic"],
            "light_offsets": offsets["child_light"], "acoustic_offsets": offsets["child_acoustic"],
            "pressure_offsets": offsets["child_imu"], "ranges": ranges, "depths": depth,
            "surface_reference_z": offsets["parent_antenna"][:, 2],
            "camera_rays": camera_rays, "parent_origin": parent_origin,
            "records": records, "input_paths": [*inputs, Path(__file__)],
            "metadata": {"experiment_id": eid, "acquisition": acq,
                         "frame": "parent CAD origin to child CAD origin, true ENU [m]",
                         "interval_s": [lo, hi], "grid_hz": 10, "static_windows": windows,
                         "configuration": cfg, "intrinsics": intrinsics,
                         "additional_mp4_intrinsics": main_intrinsics,
                         "additional_mp4_pixel_mapping": "native=(resized+0.5)*1.5-0.5" if has_main else None,
                         "depth_baseline_mbar": depth_meta.get("pressure_baseline_mbar"),
                         "depth_point": "child IMU CAD reference, not surveyed pressure center",
                         "optical_source": str(optical_path.relative_to(ROOT)),
                         "baseline": "joint geometry using reviewed tracking and the same time grid",
                         "absolute_reference": "original PPK; separate from mapping Float selection",
                         "sampling": "nearest optical <=0.075s, nearest shot <=0.3s; attitude SLERP <=0.15s; depth <=2s brackets; no extrapolation"}}
