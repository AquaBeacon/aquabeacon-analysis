"""Provisional true-ENU child positions from PPK, attitude, ray and acoustic range."""

import argparse
import json
import subprocess
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import itertools

import matplotlib.pyplot as plt
import numpy as np
from analyze_nanyo import read, write
from scipy.spatial.transform import Rotation

from aquabeacon_analysis.aerial_tracks import enu_to_geodetic
from aquabeacon_analysis.camera_calibration import load_camera_intrinsics
from aquabeacon_analysis.editable_plots import save_editable_figure
from aquabeacon_analysis.nanyo_position import (
    ENU_FROM_NED,
    ray_sphere_position,
    true_enu_from_magnetic_enu,
)
from aquabeacon_analysis.ngt_binary import sha256
from aquabeacon_analysis.ngt_position import calibrated_rays, nearest_indices
from aquabeacon_analysis.ngt_sync import numeric_csv, sample_with_gaps
from aquabeacon_analysis.parent_magnetic_attitude import sample_rotations

ACQ = "20261031_nanyo_000"


def n(rows, columns):
    return np.array([[float(r.get(k) or "nan") for k in columns] for r in rows])


class Context:
    def __init__(self):
        self.cfg_path = Path(f"config/{ACQ}/absolute_position.json")
        self.cfg = json.loads(self.cfg_path.read_text())
        self.inputs = [self.cfg_path]
        self.inputs.extend(
            [
                Path(f"results/{ACQ}/absolute_position/preparation.json"),
                Path(f"results/{ACQ}/absolute_position/optical_axes.json"),
            ]
        )
        self.sync = json.loads(Path(f"results/{ACQ}/synchronization/manifest.json").read_text())[
            "cameras"
        ]
        self.inputs.append(Path(f"results/{ACQ}/synchronization/manifest.json"))
        self.parent_axes = np.array(
            json.loads(Path(self.cfg["parent_axes"]).read_text())["R_body_from_spresense"]
        )
        self.child_axes = np.array(
            json.loads(Path(self.cfg["child_axes"]).read_text())["R_body_from_sensor"]
        )
        self.mag_mount = np.array(
            json.loads(Path(self.cfg["parent_mag_mount"]).read_text())["R_frd_from_mag"]
        )
        self.inputs.extend(
            Path(self.cfg[k]) for k in ["parent_axes", "child_axes", "parent_mag_mount"]
        )
        self.camera = {}
        for name in self.sync:
            p = Path(f"data/processed/{ACQ}/synchronization/{name}_attitude.csv")
            self.inputs.append(p)
            self.camera[name] = numeric_csv(p, ["sync_utc_s", "qw", "qx", "qy", "qz"])
        p = Path(f"data/interim/{ACQ}/geometry/child_absolute_raw.csv")
        self.inputs.append(p)
        self.child = numeric_csv(p, ["utc_us", "qw", "qx", "qy", "qz", "status", "accuracy_rad"])
        self.child[:, 0] /= 1e6
        p = Path(
            f"data/interim/{ACQ}/synchronization/parent/P_MAIN_HEALTH_RANGE_65433.mag_observation.csv"
        )
        self.inputs.append(p)
        mr = read(p)
        self.mag = n(
            mr,
            ["utc_read_us", "corrected_x", "corrected_y", "corrected_z", "calibration_generation"],
        )
        self.mag[:, 0] /= 1e6
        valid = np.array([r["valid"] == "True" and r["hardware_overrun"] == "False" for r in mr])
        self.mag[~valid, 1:4] = np.nan
        p = Path(f"results/{ACQ}/evaluation/ppk_positions.csv")
        self.inputs.append(p)
        self.gnss = n(
            read(p), ["utc_s", "east_m", "north_m", "up_m", "quality", "sd_e_m", "sd_n_m", "sd_u_m"]
        )
        self.origin = json.loads(Path(f"results/{ACQ}/evaluation/manifest.json").read_text())[
            "origin_llh"
        ]
        p = Path(f"results/{ACQ}/segmentation/selection.csv")
        self.inputs.append(p)
        self.depth = n(read(p), ["sync_utc_s", "relative_depth_m"])
        self.decl = self.cfg["declination_deg"]
        self.decl_enu = true_enu_from_magnetic_enu(self.decl)
        self.parent_from_dji = {
            k: self.parent_axes @ np.array(self.cfg["mounts"][k]["R_dji_from_sensor"]).T
            for k in self.sync
            if k.startswith("parent")
        }
        childname = next(k for k in self.sync if k.startswith("child"))
        self.child_from_dji = (
            self.child_axes @ np.array(self.cfg["mounts"][childname]["R_dji_from_sensor"]).T
        )
        self.child_from_optical = self.child_from_dji @ np.array(
            self.cfg["R_dji_from_child_optical"]
        )
        self.lever = {k: np.array(v) for k, v in self.cfg["lever_arms_frd_m"].items()}

    def sample(self, times):
        t = np.asarray(times)
        count = len(t)
        parent = np.full((count, 3, 3), np.nan)
        parent_opt = np.full_like(parent, np.nan)
        magnetic = sample_with_gaps(self.mag[:, 0], self.mag[:, 1:4], t, 0.15)
        magnetic_body = magnetic @ self.mag_mount.T
        for name, a in self.camera.items():
            if not name.startswith("parent"):
                continue
            q = sample_rotations(a[:, 0], a[:, 1:], t, 0.15)
            good = np.isfinite(q).all(1) & np.isfinite(magnetic_body).all(1)
            ids = np.flatnonzero(good)
            if not len(ids):
                continue
            world_from_body = (
                Rotation.from_quat(q[good][:, [1, 2, 3, 0]]).as_matrix()
                @ self.parent_from_dji[name].T
            )
            field = np.einsum("nij,nj->ni", world_from_body, magnetic_body[good])
            usable = np.linalg.norm(field[:, :2], axis=1) > 1
            yaw = -np.arctan2(field[:, 1], field[:, 0]) + np.radians(self.decl)
            corrected = (
                ENU_FROM_NED @ Rotation.from_euler("z", yaw[:, None]).as_matrix() @ world_from_body
            )
            parent[ids[usable]] = corrected[usable]
            parent_opt[ids[usable]] = self.parent_from_dji[name] @ np.array(
                self.cfg["R_dji_from_parent_optical"]
            )
        cq = sample_rotations(self.child[:, 0], self.child[:, 1:5], t, 0.15)
        good = np.isfinite(cq).all(1)
        child = np.full_like(parent, np.nan)
        child[good] = (
            self.decl_enu
            @ Rotation.from_quat(cq[good][:, [1, 2, 3, 0]]).as_matrix()
            @ self.child_axes.T
        )
        ci, _ = nearest_indices(self.child[:, 0], t, 0.1)
        status = np.where(ci >= 0, self.child[np.maximum(ci, 0), 5], np.nan)
        accuracy = np.where(ci >= 0, self.child[np.maximum(ci, 0), 6], np.nan)
        gnss = sample_with_gaps(self.gnss[:, 0], self.gnss[:, 1:4], t, 1.5)
        gi, _ = nearest_indices(self.gnss[:, 0], t, 0.51)
        gquality = np.where(gi >= 0, self.gnss[np.maximum(gi, 0), 4], np.nan)
        gsd = sample_with_gaps(self.gnss[:, 0], self.gnss[:, 5:8], t, 1.5)
        depth = sample_with_gaps(self.depth[:, 0], self.depth[:, 1], t, 2)
        return {
            "parent": parent,
            "child": child,
            "parent_opt": parent_opt,
            "gnss": gnss,
            "depth": depth,
            "child_status": status,
            "child_accuracy": accuracy,
            "gnss_quality": gquality,
            "gnss_sd": gsd,
        }


def q_wxyz(matrix):
    return (
        Rotation.from_matrix(matrix).as_quat()[[3, 0, 1, 2]]
        if np.isfinite(matrix).all()
        else np.full(4, np.nan)
    )


def estimate(ctx, eid):
    out = Path(f"results/{eid}/absolute_position")
    out.mkdir(parents=True, exist_ok=True)
    optical_path = Path(f"results/{eid}/relative_position/camera_relative_candidates.csv")
    optical = read(optical_path)
    t = []
    for r in optical:
        clock = next(
            c
            for c in ctx.sync.values()
            if Path(c["source_video"]["path"]).name == r["source_video"]
        )
        t.append(clock["utc_at_video_zero_s"] + clock["time_scale"] * float(r["actual_source_s"]))
    t = np.array(t)
    state = ctx.sample(t)
    range_path = Path(f"results/{eid}/evaluation/acoustic_depth.csv")
    rr = read(range_path)
    ri, delta = nearest_indices(n(rr, ["sync_utc_s"])[:, 0], t, 0.3)
    k, d, provenance = load_camera_intrinsics(Path.cwd(), "P_MAIN_CAM", (1280, 720), "proxy")
    rays = calibrated_rays(n(optical, ["x_px", "y_px"]), k, d)
    rows = []
    lever = ctx.lever
    for i, r in enumerate(optical):
        event = rr[ri[i]] if ri[i] >= 0 else {}
        range_ok = event.get("acoustic_candidate") == "True"
        light_ok = r["optical_candidate"] == "True" and r["water_like_heuristic"] == "True"
        rho = float(event.get("corrected_range_m") or "nan")
        available = (
            light_ok
            and range_ok
            and all(
                np.isfinite(state[key][i]).all()
                for key in ["parent", "child", "gnss", "parent_opt"]
            )
        )
        origin, cam = np.full(3, np.nan), np.full(3, np.nan)
        length, residual = np.nan, np.nan
        if available:
            origin, cam, length, residual = ray_sphere_position(
                state["gnss"][i],
                state["parent"][i],
                state["child"][i],
                rays[i],
                state["parent_opt"][i],
                rho,
                **lever_subset(lever),
            )
        available = bool(np.isfinite(origin).all())
        naive = (
            state["gnss"][i] + state["parent"][i] @ state["parent_opt"][i] @ rays[i] * rho
            if available
            else np.full(3, np.nan)
        )
        row = {
            "utc_s": t[i],
            "segment_time_s": float(r["segment_time_s"]),
            "source_video": r["source_video"],
            "source_video_s": float(r["actual_source_s"]),
            "x_px": r["x_px"],
            "y_px": r["y_px"],
            "optical_candidate": light_ok,
            "range_candidate": range_ok,
            "shot_seq": event.get("shot_seq", ""),
            "range_delta_s": delta[i],
            "range_m": rho,
            "position_available": available,
            "accuracy_validated": False,
            "child_attitude_status": state["child_status"][i],
            "child_reported_accuracy_rad": state["child_accuracy"][i],
            "parent_mag_calibrated": False,
            "ppk_quality": state["gnss_quality"][i],
            "pressure_depth_m": state["depth"][i],
            "optical_distance_m": length,
            "acoustic_equation_residual_m": residual,
            "cad_correction_norm_m": float(np.linalg.norm(origin - naive)),
            "angular_5deg_sensitivity_m": rho * np.sin(np.radians(5)),
            "sound_speed_50m_s_sensitivity_m": rho * 50 / 1500,
            "lever_arm_sensitivity_m": ctx.cfg["lever_arm_sensitivity_m"],
        }
        for prefix, values in [
            ("child_origin", origin),
            ("child_camera", cam),
            ("parent_antenna", state["gnss"][i]),
            ("ppk_sd", state["gnss_sd"][i]),
        ]:
            row.update(
                {f"{prefix}_{axis}_m": float(v) for axis, v in zip(["east", "north", "up"], values)}
            )
        for role in ["parent", "child"]:
            row.update(
                {
                    f"{role}_true_enu_{axis}": float(v)
                    for axis, v in zip(["qw", "qx", "qy", "qz"], q_wxyz(state[role][i]))
                }
            )
        if available:
            llh = enu_to_geodetic(origin[None, :], ctx.origin)[0]
        else:
            llh = np.full(3, np.nan)
        row.update(zip(["latitude_deg", "longitude_deg", "height_ellipsoid_m"], map(float, llh)))
        rows.append(row)
    xyz = n(rows, ["child_origin_east_m", "child_origin_north_m", "child_origin_up_m"])
    valid = np.isfinite(xyz).all(1)
    speed = np.full(len(t), np.nan)
    ids = np.flatnonzero(valid)
    for left, right in itertools.pairwise(ids):
        if 0 < t[right] - t[left] <= 1.5:
            speed[right] = np.linalg.norm(xyz[right] - xyz[left]) / (t[right] - t[left])
    for r, v in zip(rows, speed):
        r["step_speed_m_s"] = float(v)
        r["motion_outlier_candidate"] = bool(v > 3)
    write(out / "positions.csv", rows)
    processed = Path(f"data/processed/{eid}/absolute_position")
    processed.mkdir(parents=True, exist_ok=True)
    write(processed / "positions.csv", rows)
    features = []
    for row in rows:
        if row["position_available"]:
            features.append(
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "Point",
                        "coordinates": [
                            row["longitude_deg"],
                            row["latitude_deg"],
                            row["height_ellipsoid_m"],
                        ],
                    },
                    "properties": {
                        key: row[key]
                        for key in [
                            "utc_s",
                            "segment_time_s",
                            "ppk_quality",
                            "accuracy_validated",
                            "motion_outlier_candidate",
                        ]
                    },
                }
            )
    (out / "positions.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": features})
    )
    interval = json.loads(Path(f"experiments/{eid}/experiment.json").read_text())["utc_interval_s"]
    attitude_t = np.arange(*interval, 0.1)
    attitude_state = ctx.sample(attitude_t)
    attitude_rows = []
    for i, time in enumerate(attitude_t):
        row = {
            "utc_s": time,
            "segment_time_s": time - interval[0],
            "child_status": attitude_state["child_status"][i],
            "child_reported_accuracy_rad": attitude_state["child_accuracy"][i],
            "accuracy_validated": False,
        }
        for role in ["parent", "child"]:
            row[f"{role}_attitude_available"] = bool(np.isfinite(attitude_state[role][i]).all())
            row.update(
                {
                    f"{role}_true_enu_{axis}": float(v)
                    for axis, v in zip(["qw", "qx", "qy", "qz"], q_wxyz(attitude_state[role][i]))
                }
            )
        attitude_rows.append(row)
    write(processed / "attitudes_10hz.csv", attitude_rows)
    # Include all sampled rows and explicit gaps in time plots.
    fig, axes = plt.subplots(3, 1, sharex=True, figsize=(11, 7))
    for j, ax in enumerate(axes):
        ax.plot(
            [float(r["segment_time_s"]) for r in rows],
            state["gnss"][:, j],
            lw=0.6,
            label="Parent antenna PPK",
        )
        ax.plot(
            [float(r["segment_time_s"]) for r in rows],
            xyz[:, j],
            ".",
            ms=2,
            label="Child origin estimate",
        )
        ax.set_ylabel(["East [m]", "North [m]", "Up [m]"][j])
        ax.grid(alpha=0.2)
        ax.legend()
    axes[-1].set_xlabel("Interval time [s]")
    fig.tight_layout()
    save_editable_figure(fig, out / "trajectory_components.png", dpi=150)
    plt.close(fig)
    fig, ax = plt.subplots(figsize=(8, 7))
    ax.plot(state["gnss"][:, 0], state["gnss"][:, 1], color="gray", lw=0.7, label="Parent PPK")
    sc = ax.scatter(
        xyz[:, 0],
        xyz[:, 1],
        c=np.array([float(r["segment_time_s"]) for r in rows]),
        s=6,
        cmap="viridis",
        label="Child estimate",
    )
    ax.set(
        xlabel="True east [m]",
        ylabel="True north [m]",
        title=f"{eid}: provisional georeferenced positions",
    )
    ax.axis("equal")
    ax.legend()
    ax.grid(alpha=0.2)
    fig.colorbar(sc, ax=ax, label="Interval time [s]")
    fig.tight_layout()
    save_editable_figure(fig, out / "overhead.png", dpi=150)
    plt.close(fig)
    inputs = [
        *ctx.inputs,
        optical_path,
        range_path,
        Path(__file__),
        Path("src/aquabeacon_analysis/nanyo_position.py"),
    ]
    manifest = {
        "experiment_id": eid,
        "rows": len(rows),
        "available": int(valid.sum()),
        "frame": "true local ENU, meters",
        "origin_llh": ctx.origin,
        "accuracy_validated": False,
        "rtk_used": False,
        "configuration": ctx.cfg,
        "intrinsics": provenance,
        "motion_outlier_candidates": int(np.sum(speed > 3)),
        "acoustic_residual_max_m": float(
            np.nanmax(np.abs(n(rows, ["acoustic_equation_residual_m"])))
        ),
        "cad_correction_median_m": float(np.nanmedian(n(rows, ["cad_correction_norm_m"]))),
        "parent_mag_status": "Generation 0 raw-equivalent field, valid non-overrun only; no invented bias correction",
        "child_attitude_status": "All SH2 quality levels retained; reference conversion validated, heading accuracy not validated",
        "git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "inputs_sha256": {str(p): sha256(p) for p in inputs},
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(
        eid,
        "positions",
        valid.sum(),
        "motion flags",
        manifest["motion_outlier_candidates"],
        flush=True,
    )


def lever_subset(lever):
    return {k: v for k, v in lever.items() if k != "child_imu"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--experiment",
        action="append",
        required=True,
        choices=["20261031_nanyo_001", "20261031_nanyo_002"],
    )
    args = parser.parse_args()
    ctx = Context()
    for eid in args.experiment:
        estimate(ctx, eid)


if __name__ == "__main__":
    main()
