"""Fuse GNSS/Spresense and optical/acoustic/depth/BNO navigation without SfM input."""

import argparse
import json
import subprocess
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from analyze_nanyo import read, write
from estimate_nanyo_absolute import Context, lever_subset, n
from scipy.optimize import least_squares
from scipy.spatial.transform import Rotation

from aquabeacon_analysis.camera_calibration import load_camera_intrinsics
from aquabeacon_analysis.editable_plots import save_editable_figure
from aquabeacon_analysis.nanyo_position import ray_sphere_position
from aquabeacon_analysis.navigation_fusion import event_grid, hampel_positions, inertial_filter
from aquabeacon_analysis.ngt_binary import sha256
from aquabeacon_analysis.ngt_position import calibrated_rays, nearest_indices
from aquabeacon_analysis.ngt_sync import numeric_csv, sample_with_gaps
from aquabeacon_analysis.parent_magnetic_attitude import sample_rotations

ACQ = "20261031_nanyo_000"


def dump(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")


def fill_rotations(times, matrices):
    good = np.isfinite(matrices).all((1, 2))
    q = Rotation.from_matrix(matrices[good]).as_quat()[:, [3, 0, 1, 2]]
    out = sample_rotations(times[good], q, times, 0.5)
    ok = np.isfinite(out).all(1)
    result = np.full_like(matrices, np.nan)
    result[ok] = Rotation.from_quat(out[ok][:, [1, 2, 3, 0]]).as_matrix()
    return result, ok & ~good


def sample_position(t, values, requested):
    return sample_with_gaps(t, values, requested, 0.15)


def filter_rows(times, result, rotations, imu_point, camera_point=None, antenna_point=None):
    rows = []
    p = result["state"][:, :, 0].copy()
    p[~result["available"]] = np.nan
    origin = p - np.einsum("nij,j->ni", rotations, imu_point)
    for i, t in enumerate(times):
        r = {
            "utc_s": t,
            "position_available": bool(result["available"][i]),
            "anchor_age_s": result["anchor_age_s"][i],
            "segment": int(result["segment"][i]),
            "sfm_position_used": False,
            "accuracy_validated": False,
            "position_update_axes": "".join(
                a for j, a in enumerate("ENU") if result["position_used"][i, j]
            ),
            "velocity_update_axes": "".join(
                a for j, a in enumerate("ENU") if result["velocity_used"][i, j]
            ),
        }
        for prefix, values in [
            ("origin", origin[i]),
            ("imu", p[i]),
            ("sigma", result["position_sigma"][i]),
            ("velocity", result["state"][i, :, 1]),
            ("acceleration_bias", result["state"][i, :, 2]),
        ]:
            r.update(
                {
                    f"{prefix}_{axis}": v
                    for axis, v in zip(["east", "north", "up"], values, strict=True)
                }
            )
        for name, point in [("camera", camera_point), ("antenna", antenna_point)]:
            if point is not None:
                v = origin[i] + rotations[i] @ point
                r.update({f"{name}_{axis}": v[j] for j, axis in enumerate(["east", "north", "up"])})
        if np.isfinite(rotations[i]).all():
            q = Rotation.from_matrix(rotations[i]).as_quat()[[3, 0, 1, 2]]
        else:
            q = np.full(4, np.nan)
        r.update(zip(["qw", "qx", "qy", "qz"], q, strict=True))
        rows.append(r)
    return rows


def parent_fusion(ctx, cfg, interval):
    acq = cfg.get("experiment_id", ACQ)
    output_name = cfg.get("output_name", "navigation_fusion")
    out = Path(f"results/{acq}/{output_name}")
    out.mkdir(parents=True, exist_ok=True)
    source = Path(f"data/interim/{acq}/synchronization/parent_100hz.csv")
    imu = numeric_csv(
        source,
        [
            "sample_utc_us",
            "corrected_ax",
            "corrected_ay",
            "corrected_az",
            "corrected_gx",
            "corrected_gy",
            "corrected_gz",
        ],
    )
    imu[:, 0] /= 1e6
    times = np.arange(interval[0] - 30, interval[1] + 1, 0.05)
    state = ctx.sample(times)
    rotation, filled = fill_rotations(times, state["parent"])
    samples = sample_with_gaps(imu[:, 0], imu[:, 1:], times, 0.15)
    accel = np.einsum("nij,nj->ni", rotation @ ctx.parent_axes, samples[:, :3]) - [0, 0, 9.80665]
    gyro = samples[:, 3:] @ ctx.parent_axes.T
    imu_point = np.array(cfg["parent_imu_frd_m"])
    lever = ctx.lever["parent_antenna"] - imu_point
    g = ctx.gnss[(ctx.gnss[:, 0] >= times[0]) & (ctx.gnss[:, 0] <= times[-1])]
    gr = sample_rotations(
        times[np.isfinite(rotation).all((1, 2))],
        Rotation.from_matrix(rotation[np.isfinite(rotation).all((1, 2))]).as_quat()[
            :, [3, 0, 1, 2]
        ],
        g[:, 0],
        0.15,
    )
    gok = np.isfinite(gr).all(1)
    rr = np.full((len(g), 3, 3), np.nan)
    rr[gok] = Rotation.from_quat(gr[gok][:, [1, 2, 3, 0]]).as_matrix()
    raw = g[:, 1:4] - np.einsum("nij,j->ni", rr, lever)
    flags = hampel_positions(g[:, 1:4], cfg["parent_hampel_floors_m"])
    floors = np.array([cfg["ppk_sigma_floor_by_quality"][str(int(q))] for q in g[:, 4]])
    sigma = np.maximum(g[:, 5:8], floors)
    accepted_input = raw.copy()
    accepted_input[flags] = np.nan
    positions, indices = event_grid(times, g[:, 0], accepted_input)
    psigma, _ = event_grid(times, g[:, 0], sigma)
    gnss_paths = [
        Path(
            f"data/interim/{acq}/synchronization/parent/P_MAIN_HEALTH_RANGE_{suffix}.gnss_observation.csv"
        )
        for suffix in cfg.get("parent_log_suffixes", ["65433"])
    ]
    nav = np.vstack(
        [
            numeric_csv(
                p, ["itow_ms", "utc_read_us", "fix", "vn_mm_s", "ve_mm_s", "vd_mm_s", "sacc_mm_s"]
            )
            for p in gnss_paths
        ]
    )
    nav = nav[np.argsort(nav[:, 1])]
    week_offset = round(np.median(nav[:, 1] / 1e6 - nav[:, 0] / 1000))
    nt = nav[:, 0] / 1000 + week_offset
    vv = nav[:, [4, 3, 5]] / 1000
    vv[:, 2] *= -1
    vv[nav[:, 2] < 3] = np.nan
    ni, _ = nearest_indices(times, nt, 0.051)
    inside = ni >= 0
    vv[inside] -= np.einsum("nij,nj->ni", rotation[ni[inside]], np.cross(gyro[ni[inside]], lever))
    vs = np.maximum(nav[:, 6, None] / 1000, np.array(cfg["gnss_velocity_sigma_floor_m_s"]))
    velocity, _ = event_grid(times, nt, vv)
    velocity_sigma, _ = event_grid(times, nt, vs)
    kwargs = {
        "velocity": velocity,
        "velocity_sigma": velocity_sigma,
        "max_anchor_age": cfg["parent_max_dr_s"],
        "acceleration_sigma": cfg["parent_acceleration_sigma_m_s2"],
    }
    result = inertial_filter(times, accel, positions, psigma, **kwargs)
    rows = filter_rows(
        times, result, rotation, imu_point, ctx.lever["parent_camera"], ctx.lever["parent_antenna"]
    )
    write(out / "parent_navigation.csv", rows)
    decisions = []
    for j, idx in enumerate(indices):
        if idx < 0:
            continue
        r = {"utc_s": g[j, 0], "ppk_quality": g[j, 4], "grid_delta_s": times[idx] - g[j, 0]}
        for axis, k in enumerate(["east", "north", "up"]):
            r[f"raw_antenna_{k}_m"] = g[j, axis + 1]
            r[f"hampel_rejected_{k}"] = bool(flags[j, axis])
            r[f"filter_used_{k}"] = bool(result["position_used"][idx, axis])
            r[f"innovation_sigma_{k}"] = result["innovation_sigma"][idx, axis]
        decisions.append(r)
    write(out / "parent_gnss_decisions.csv", decisions)
    holdout = []
    for offset, duration in [(200, 5), (400, 15), (800, 20)]:
        start = interval[0] + offset
        mask = (times >= start) & (times < start + duration)
        p = positions.copy()
        v = velocity.copy()
        p[mask] = np.nan
        v[mask] = np.nan
        trial = inertial_filter(times, accel, p, psigma, **{**kwargs, "velocity": v})
        ids = np.flatnonzero(
            mask & np.isfinite(raw[np.clip(np.searchsorted(g[:, 0], times), 0, len(g) - 1)]).all(1)
        )
        if not len(ids):
            continue
        end = ids[-1]
        full = result["state"][end, :, 0]
        pred = trial["state"][end, :, 0]
        holdout.append(
            {
                "start_utc_s": start,
                "duration_s": duration,
                "withheld_gnss_position_and_velocity": True,
                "endpoint_vs_full_filter_enu_m": (pred - full).tolist(),
                "endpoint_disagreement_m": float(np.linalg.norm(pred - full)),
                "available_at_end": bool(trial["available"][end]),
                "note": "Internal dropout sensitivity, not position ground truth",
            }
        )
    fig, axes = plt.subplots(3, 1, sharex=True, figsize=(11, 8))
    antenna = n(rows, ["antenna_east", "antenna_north", "antenna_up"])
    for j, ax in enumerate(axes):
        ax.plot(g[:, 0] - interval[0], g[:, j + 1], color=".65", lw=0.7, label="Raw PPK")
        ax.plot(times - interval[0], antenna[:, j], lw=1, label="PPK + Doppler + Spresense")
        ax.scatter(
            g[flags[:, j], 0] - interval[0],
            g[flags[:, j], j + 1],
            s=12,
            c="#bc493d",
            label="Local spike candidate",
        )
        ax.set_ylabel(["East [m]", "North [m]", "Up [m]"][j])
        ax.grid(alpha=0.2)
        ax.legend(fontsize=8)
    axes[-1].set_xlabel("Seconds from interval 1 start")
    fig.tight_layout()
    save_editable_figure(fig, out / "parent_comparison.png", dpi=150)
    plt.close(fig)
    manifest = {
        "experiment_id": acq,
        "sfm_position_used": False,
        "rtk_used": False,
        "origin_llh": ctx.origin,
        "rows": len(times),
        "available": int(result["available"].sum()),
        "hampel_rejected_axes": flags.sum(0).tolist(),
        "position_updates_by_axis": result["position_used"].sum(0).tolist(),
        "short_attitude_gaps_interpolated": int(filled.sum()),
        "dropout_trials": holdout,
        "gnss_velocity_time": "iTOW + integer UTC week offset; not host receive time",
        "utc_week_offset_s": week_offset,
        "config": cfg,
        "accuracy_validated": False,
        "inputs_sha256": {
            str(p): sha256(p)
            for p in [
                *ctx.inputs,
                source,
                *gnss_paths,
                Path(__file__),
                Path("src/aquabeacon_analysis/navigation_fusion.py"),
            ]
        },
    }
    dump(out / "parent_manifest.json", manifest)
    return times, antenna, rotation, result, manifest


def child_fusion(ctx, cfg, eid, parent):
    pt, pp, _, _, _ = parent
    acq = cfg.get("experiment_id", ACQ)
    output_name = cfg.get("output_name", "navigation_fusion")
    out = Path(f"results/{eid}/{output_name}")
    out.mkdir(parents=True, exist_ok=True)
    interval = json.loads(Path(f"experiments/{eid}/experiment.json").read_text(encoding="utf-8"))[
        "utc_interval_s"
    ]
    times = np.arange(*interval, 0.05)
    state = ctx.sample(times)
    rc, filled = fill_rotations(times, state["child"])
    suffix = cfg.get("child_log_suffix", "35545")
    source = Path(f"data/interim/{acq}/synchronization/child/C_MAIN_IMU_100Hz_{suffix}.imu.csv")
    rawimu = numeric_csv(source, ["utc_us", "fresh_mask", "ax", "ay", "az"])
    rawimu = rawimu[(rawimu[:, 1].astype(int) & 2) != 0]
    sample = sample_with_gaps(rawimu[:, 0] / 1e6, rawimu[:, 2:], times, 0.15)
    accel = np.einsum("nij,nj->ni", rc @ ctx.child_axes, sample) - [0, 0, 9.80665]
    optical_folder = cfg.get("optical_folder", "relative_position")
    optical_path = Path(f"results/{eid}/{optical_folder}/camera_relative_candidates.csv")
    optical = read(optical_path)
    ot = np.array(
        [
            next(
                c
                for c in ctx.sync.values()
                if Path(c["source_video"]["path"]).name == r["source_video"]
            )["utc_at_video_zero_s"]
            + next(
                c
                for c in ctx.sync.values()
                if Path(c["source_video"]["path"]).name == r["source_video"]
            )["time_scale"]
            * float(r["actual_source_s"])
            for r in optical
        ]
    )
    ranges_path = Path(f"results/{eid}/evaluation/acoustic_depth.csv")
    rr = read(ranges_path)
    range_times = n(rr, ["sync_utc_s"])[:, 0]
    if cfg.get("unique_range_events"):
        # Keep one nearest 10 Hz image per shot, plus 2 Hz bearing-only observations.
        shot_images, _ = nearest_indices(ot, range_times, 0.075)
        selected = np.unique(np.r_[np.arange(0, len(ot), 5), shot_images[shot_images >= 0]])
        optical = [optical[i] for i in selected]
        ot = ot[selected]
    os = ctx.sample(ot)
    gp = sample_position(pt, pp, ot)
    k, d, _ = load_camera_intrinsics(Path.cwd(), "P_MAIN_CAM", (1280, 720), "proxy")
    rays = calibrated_rays(n(optical, ["x_px", "y_px"]), k, d)
    ri, rdt = nearest_indices(range_times, ot, 0.075 if cfg.get("unique_range_events") else 0.3)
    if cfg.get("unique_range_events"):
        for shot in np.unique(ri[ri >= 0]):
            candidates_for_shot = np.flatnonzero(ri == shot)
            best = candidates_for_shot[np.argmin(abs(rdt[candidates_for_shot]))]
            ri[candidates_for_shot[candidates_for_shot != best]] = -1
    candidates = []
    surface_samples = []
    imu_point = ctx.lever["child_imu"]
    for i, row in enumerate(optical):
        event = rr[ri[i]] if ri[i] >= 0 else {}
        light = row["optical_candidate"] == "True" and row["water_like_heuristic"] == "True"
        range_ok = event.get("acoustic_candidate") == "True"
        rho = float(event.get("corrected_range_m") or "nan")
        usable = (
            light
            and np.isfinite(gp[i]).all()
            and all(np.isfinite(os[key][i]).all() for key in ["parent", "child", "parent_opt"])
        )
        origin = np.full(3, np.nan)
        if usable and range_ok:
            origin, *_ = ray_sphere_position(
                gp[i],
                os["parent"][i],
                os["child"][i],
                rays[i],
                os["parent_opt"][i],
                rho,
                **lever_subset(ctx.lever),
            )
        if np.isfinite(origin).all() and os["depth"][i] > 0.3:
            surface_samples.append(
                origin[2] + (os["child"][i] @ imu_point)[2] + os["depth"][i] - gp[i, 2]
            )
        candidates.append((usable, range_ok, rho, origin, event))
    surface = float(np.median(surface_samples)) if surface_samples else np.nan
    surface_sigma = (
        max(0.5, float(1.4826 * np.median(abs(np.array(surface_samples) - surface))))
        if surface_samples
        else np.nan
    )
    observations = []
    positions = np.full((len(ot), 3), np.nan)
    sigmas = np.full_like(positions, np.nan)
    for i, (usable, range_ok, rho, origin, event) in enumerate(candidates):
        depth = os["depth"][i]
        method = "none"
        reason = "missing optical/attitude/parent position or depth"
        angle = residual = joint_angle = joint_residual = np.nan
        used_range = False
        if usable and np.isfinite(depth) and depth > 0.3:
            rp, cr = os["parent"][i], os["child"][i]
            c = gp[i] + rp @ (ctx.lever["parent_camera"] - ctx.lever["parent_antenna"])
            tx = gp[i] + rp @ (ctx.lever["parent_acoustic"] - ctx.lever["parent_antenna"])
            u = rp @ os["parent_opt"][i] @ rays[i]
            light_from_imu = cr @ (ctx.lever["child_light"] - imu_point)
            acoustic_from_imu = cr @ (ctx.lever["child_acoustic"] - imu_point)
            z = gp[i, 2] + surface - depth
            length = (z + light_from_imu[2] - c[2]) / u[2] if abs(u[2]) > 0.15 else np.nan
            if 0 < length < 50:
                initial = c + length * u - light_from_imu
                used_range = range_ok and rho > abs(z + acoustic_from_imu[2] - tx[2]) - 0.5
                sigma_angle = max(0.25, length * np.sin(np.radians(cfg["child_bearing_sigma_deg"])))
                sigma_range = max(0.3, 0.04 * rho) if range_ok else 1

                def residuals(
                    x,
                    light_from_imu=light_from_imu,
                    c=c,
                    u=u,
                    sigma_angle=sigma_angle,
                    z=z,
                    used_range=used_range,
                    acoustic_from_imu=acoustic_from_imu,
                    tx=tx,
                    rho=rho,
                    sigma_range=sigma_range,
                ):
                    vec = x + light_from_imu - c
                    cross = np.cross(vec, u) / sigma_angle
                    result = [*cross, (x[2] - z) / cfg["depth_sigma_m"]]
                    if used_range:
                        result.append(
                            (np.linalg.norm(x + acoustic_from_imu - tx) - rho) / sigma_range
                        )
                    return result

                fit = least_squares(residuals, initial, loss="soft_l1", f_scale=1)
                point = fit.x
                residual = (
                    np.linalg.norm(point + acoustic_from_imu - tx) - rho if range_ok else np.nan
                )
                angle = np.degrees(
                    np.arctan2(
                        np.linalg.norm(np.cross(point + light_from_imu - c, u)),
                        np.dot(point + light_from_imu - c, u),
                    )
                )
                joint_angle, joint_residual = angle, residual
                if used_range and (abs(residual) > max(2, 0.2 * rho) or angle > 15):
                    used_range = False
                    point = initial
                    reason = (
                        "acoustic/optical/depth conflict; retained raw range, used ray and depth"
                    )
                else:
                    reason = "geometrically usable provisional observation"
                residual = (
                    np.linalg.norm(point + acoustic_from_imu - tx) - rho if range_ok else np.nan
                )
                vec = point + light_from_imu - c
                angle = np.degrees(np.arctan2(np.linalg.norm(np.cross(vec, u)), np.dot(vec, u)))
                positions[i] = point
                sigmas[i] = [sigma_angle, sigma_angle, cfg["depth_sigma_m"]]
                method = "ray_range_depth" if used_range else "ray_depth"
            else:
                reason = "ray nearly horizontal, negative intersection or beyond 50 m"
        r = {
            "utc_s": ot[i],
            "method": method,
            "reason": reason,
            "available": bool(np.isfinite(positions[i]).all()),
            "range_candidate": range_ok,
            "range_used": used_range,
            "range_m": rho,
            "range_delta_s": rdt[i],
            "shot_seq": event.get("shot_seq", ""),
            "range_residual_m": residual,
            "ray_residual_deg": angle,
            "joint_candidate_range_residual_m": joint_residual,
            "joint_candidate_ray_residual_deg": joint_angle,
            "relative_depth_m": depth,
            "surface_up_m": gp[i, 2] + surface,
            "surface_relative_to_parent_antenna_m": surface,
            "surface_offset_uncertainty_proxy_m": surface_sigma,
            "sfm_position_used": False,
            "accuracy_validated": False,
        }
        r.update(
            {f"imu_{axis}_m": positions[i, j] for j, axis in enumerate(["east", "north", "up"])}
        )
        observations.append(r)
    last_range_anchor = -np.inf
    for i, row in enumerate(observations):
        if row["method"] == "ray_range_depth":
            last_range_anchor = ot[i]
        allowed = row["available"] and (
            row["range_used"] or ot[i] - last_range_anchor <= cfg["ray_depth_max_after_range_s"]
        )
        row["eligible_for_filter"] = bool(allowed)
        if not allowed:
            positions[i] = np.nan
            if row["available"]:
                row["reason"] += "; ray-depth-only candidate lacks a recent range anchor"
    p, oi = event_grid(times, ot, positions)
    sigma, _ = event_grid(times, ot, sigmas)
    depth_obs, _di = event_grid(times, ctx.depth[:, 0], ctx.depth[:, 1], max_delta=0.051)
    parent_at_grid = sample_position(pt, pp, times)
    valid_depth = np.isfinite(depth_obs) & np.isfinite(parent_at_grid[:, 2])
    p[valid_depth, 2] = parent_at_grid[valid_depth, 2] + surface - depth_obs[valid_depth]
    sigma[valid_depth, 2] = cfg["depth_sigma_m"]
    result = inertial_filter(
        times,
        accel,
        p,
        sigma,
        max_anchor_age=cfg["child_max_dr_s"],
        acceleration_sigma=cfg["child_acceleration_sigma_m_s2"],
        bias_walk=0.008,
        max_horizontal_speed=cfg["child_max_horizontal_speed_m_s"],
    )
    rows = filter_rows(times, result, rc, imu_point, ctx.lever["child_camera"])
    parent_sigma = sample_position(pt, parent[3]["position_sigma"], times)
    for i, r in enumerate(rows):
        r["surface_offset_uncertainty_proxy_m"] = surface_sigma
        r["absolute_up_sigma_model_m"] = float(
            np.sqrt(r["sigma_up"] ** 2 + surface_sigma**2 + parent_sigma[i, 2] ** 2)
        )
        r["absolute_east_sigma_model_m"] = float(np.hypot(r["sigma_east"], parent_sigma[i, 0]))
        r["absolute_north_sigma_model_m"] = float(np.hypot(r["sigma_north"], parent_sigma[i, 1]))
    for r, idx in zip(observations, oi, strict=True):
        r["filter_used_horizontal"] = bool(idx >= 0 and result["position_used"][idx, :2].all())
    write(out / "child_navigation.csv", rows)
    write(out / "child_observations.csv", observations)
    processed = Path(f"data/processed/{eid}/{output_name}")
    processed.mkdir(parents=True, exist_ok=True)
    write(processed / "child_navigation.csv", rows)
    old = read(Path(f"results/{eid}/absolute_position/positions.csv"))
    oldxyz = n(old, ["child_origin_east_m", "child_origin_north_m", "child_origin_up_m"])
    xyz = n(rows, ["origin_east", "origin_north", "origin_up"])
    fig, axes = plt.subplots(3, 1, sharex=True, figsize=(11, 8))
    for j, ax in enumerate(axes):
        ax.plot(
            n(old, ["utc_s"])[:, 0] - interval[0],
            oldxyz[:, j],
            ".",
            ms=2,
            color=".6",
            label="Previous ray + range + raw PPK",
        )
        ax.plot(times - interval[0], xyz[:, j], lw=0.8, label="Sensor fusion (no SfM)")
        ax.set_ylabel(["East [m]", "North [m]", "Up [m]"][j])
        ax.legend(fontsize=8)
        ax.grid(alpha=0.2)
    axes[-1].set_xlabel("Interval time [s]")
    fig.tight_layout()
    save_editable_figure(fig, out / "child_comparison.png", dpi=150)
    plt.close(fig)
    from collections import Counter

    manifest = {
        "experiment_id": eid,
        "rows": len(times),
        "available": int(result["available"].sum()),
        "sfm_position_used": False,
        "accuracy_validated": False,
        "origin_llh": ctx.origin,
        "observation_methods": dict(Counter(r["method"] for r in observations)),
        "horizontal_position_updates": int(result["position_used"][:, :2].all(1).sum()),
        "dr_samples": int((result["available"] & (result["anchor_age_s"] > 1)).sum()),
        "surface_relative_to_parent_antenna_m": surface,
        "surface_offset_uncertainty_proxy_m": surface_sigma,
        "water_level_basis": "Robust constant waterline offset relative to parent antenna from ray/range/depth. Absolute water level follows fused parent height; no surveyed sea-level or constant absolute-height constraint.",
        "pressure_reference": "CAD child IMU point approximates pressure sensor location",
        "initial_velocity": "Zero if no velocity observation; not a zero-velocity detection",
        "config": cfg,
        "short_attitude_gaps_interpolated": int(filled.sum()),
        "inputs_sha256": {
            str(q): sha256(q)
            for q in [
                *ctx.inputs,
                source,
                optical_path,
                ranges_path,
                Path(f"results/{acq}/{output_name}/parent_navigation.csv"),
                Path(__file__),
                Path("src/aquabeacon_analysis/navigation_fusion.py"),
            ]
        },
    }
    dump(out / "manifest.json", manifest)
    print(
        eid,
        manifest["available"],
        manifest["observation_methods"],
        "water level",
        surface,
        surface_sigma,
        flush=True,
    )
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", required=True, choices=[ACQ, "20261101_nanyo_000"])
    parser.add_argument("--mapping-selection", action="store_true")
    args = parser.parse_args()
    acq = args.experiment
    if acq == ACQ:
        ctx = Context()
    else:
        from analyze_nanyo_day2 import Day2Context

        ctx = Day2Context()
    if args.mapping_selection:
        selection_path = Path(f"results/{acq}/mapping_gnss_selection/selected_positions.csv")
        ctx.gnss = numeric_csv(
            selection_path,
            ["utc_s", "east_m", "north_m", "up_m", "quality", "sd_e_m", "sd_n_m", "sd_u_m"],
        )
        ctx.inputs.extend([selection_path, selection_path.parent / "manifest.json"])
    rig = json.loads(Path(ctx.cfg["rig"]).read_text(encoding="utf-8"))
    point = next(s["reference_position_body_m"] for s in rig["sensors"] if s["id"] == "parent_imu")
    cfg = {
        "experiment_id": acq,
        "parent_imu_frd_m": (np.diag([1, -1, -1]) @ point).tolist(),
        "parent_hampel_floors_m": [2, 2, 3],
        "ppk_sigma_floor_by_quality": {"1": [0.4, 0.4, 1], "2": [1, 1, 3], "4": [3, 3, 6]},
        "gnss_velocity_sigma_floor_m_s": [0.15, 0.15, 0.2],
        "parent_acceleration_sigma_m_s2": 0.4,
        "child_acceleration_sigma_m_s2": 0.35,
        "parent_max_dr_s": 5,
        "child_max_dr_s": 3,
        "ray_depth_max_after_range_s": 5,
        "child_max_horizontal_speed_m_s": 3,
        "depth_sigma_m": 0.1,
        "child_bearing_sigma_deg": 5,
        "scope": "Provisional sensor navigation. SfM camera positions are prohibited as filter inputs.",
        "parameters_are_assumptions_not_validated_noise": True,
    }
    if acq != ACQ:
        cfg.update(parent_log_suffixes=["62440", "62441"], child_log_suffix="68440")
    if args.mapping_selection:
        cfg.update(
            output_name="mapping_navigation",
            optical_folder="mapping_light_tracking",
            unique_range_events=True,
            gnss_selection=f"results/{acq}/mapping_gnss_selection/manifest.json",
        )
    output_name = cfg.get("output_name", "navigation_fusion")
    cfg_path = Path(f"config/{acq}/{output_name}.json")
    dump(cfg_path, cfg)
    ctx.inputs.append(cfg_path)
    ids = [f"{acq[:8]}_nanyo_{i:03d}" for i in range(1, 3 if acq == ACQ else 6)]
    intervals = [
        json.loads(Path(f"experiments/{e}/experiment.json").read_text(encoding="utf-8"))[
            "utc_interval_s"
        ]
        for e in ids
    ]
    parent = parent_fusion(ctx, cfg, [intervals[0][0], intervals[-1][1]])
    results = [child_fusion(ctx, cfg, e, parent) for e in ids]
    dump(
        Path(f"results/{acq}/{output_name}/manifest.json"),
        {
            "experiment_id": acq,
            "segments": results,
            "parent": parent[-1],
            "config_sha256": sha256(cfg_path),
            "git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
            "sfm_position_used": False,
            "accuracy_validated": False,
        },
    )


if __name__ == "__main__":
    main()
