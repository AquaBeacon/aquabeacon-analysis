"""SPP/PPK and acoustic evaluation without RTK or surveyed ground truth."""

import argparse
import csv
import json
from collections import Counter
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from ppk_ngt_geonet import read_positions

from aquabeacon_analysis.editable_plots import save_editable_figure
from aquabeacon_analysis.gps_parent import geodetic_to_enu
from aquabeacon_analysis.ngt_binary import sha256
from aquabeacon_analysis.ngt_gnss import audit_ubx
from aquabeacon_analysis.ngt_position import bracket_interpolate


def read(p):
    with p.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write(p, rows):
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", required=True)
    args = parser.parse_args()
    eid = args.experiment
    root = Path.cwd()
    out = root / f"results/{eid}/evaluation"
    out.mkdir(parents=True, exist_ok=True)
    work = root / f"data/interim/{eid}/gnss"
    ubx = root / "data/raw/Nanyo_ocean_test/parent/P_MAIN_GNSS_65433.ubx"
    audit, spp = audit_ubx(ubx)
    assert not any(r["carrier_solution"] for r in spp)
    origin = [spp[0][k] for k in ["latitude_deg", "longitude_deg", "height_ellipsoid_m"]]
    enu = geodetic_to_enu(
        *np.array(
            [[r[k] for k in ["latitude_deg", "longitude_deg", "height_ellipsoid_m"]] for r in spp]
        ).T,
        *origin,
    )
    for r, xyz in zip(spp, enu, strict=True):
        r.update(zip(["east_m", "north_m", "up_m"], xyz.tolist(), strict=True))
    sync = json.loads((root / f"results/{eid}/synchronization/manifest.json").read_text())
    clock = next(v for k, v in sync["cameras"].items() if k.startswith("parent"))
    ppk = read_positions(work / "ar_continuous.pos", origin, clock)
    floating = read_positions(work / "float.pos", origin, clock)
    segments = json.loads((root / f"results/{eid}/segmentation/manifest.json").read_text())[
        "segments"
    ]
    pressure = read(root / f"results/{eid}/segmentation/selection.csv")
    pt = np.array([float(r["sync_utc_s"]) for r in pressure])
    pd = np.array([float(r["relative_depth_m"]) for r in pressure])
    manifest = {
        "experiment_id": eid,
        "rtk_used": False,
        "origin_llh": origin,
        "ubx_audit": audit,
        "spp_epochs": len(spp),
        "ppk_quality_counts": dict(Counter(r["quality"] for r in ppk)),
        "ppk_float_run_quality_counts": dict(Counter(r["quality"] for r in floating)),
        "base_station": "950437",
        "base_distance_km_approx": 5.25,
        "base_coordinates": "GEONET RINEX header; no surveyed truth or frame-epoch transform",
        "base_interval_s": 30,
        "ppk_max_base_age_s": 15,
        "accuracy_limit": "Moving boat: dispersion includes real motion; no absolute error metric.",
        "acoustic_calibration": "Logged profile 0, offset 0 us, sound speed 1500 m/s; no NGT correction",
        "segments": [],
        "inputs": [
            {"path": str(p), "sha256": sha256(p)}
            for p in [
                ubx,
                work / "rover.obs",
                work / "rover.nav",
                work / "ar_continuous.pos",
                work / "float.pos",
                *list((work / "base_950437").glob("*.26*")),
                work / "ar_continuous.conf",
                work / "float.conf",
                work / "rtklib/rnx2rtkp.exe",
                Path(__file__),
            ]
        ],
    }
    for s in [
        {"experiment_id": eid, "start_utc_s": spp[0]["utc_s"], "end_utc_s": spp[-1]["utc_s"] + 1},
        *segments,
    ]:
        folder = root / f"results/{s['experiment_id']}/evaluation"
        folder.mkdir(parents=True, exist_ok=True)
        tracks = {
            label: [r for r in rows if s["start_utc_s"] <= r["utc_s"] < s["end_utc_s"]]
            for label, rows in [("spp", spp), ("ppk", ppk), ("ppk_float", floating)]
        }
        for label, rows in tracks.items():
            write(folder / f"{label}_positions.csv", rows)
        fig, axes = plt.subplots(1, 2, figsize=(12, 5))
        for label in ["spp", "ppk"]:
            r = tracks[label]
            axes[0].plot(
                [x["east_m"] for x in r], [x["north_m"] for x in r], lw=0.8, label=label.upper()
            )
        axes[0].set(
            xlabel="East [m]",
            ylabel="North [m]",
            aspect="equal",
            title="Parent trajectory (WGS84 ENU)",
        )
        r = tracks["ppk"]
        for q, label in [(1, "PPK fixed"), (2, "PPK float"), (4, "DGNSS")]:
            selected = [x for x in r if x["quality"] == q]
            axes[1].scatter(
                [x["east_m"] for x in selected], [x["north_m"] for x in selected], s=5, label=label
            )
        axes[1].set(
            xlabel="East [m]",
            ylabel="North [m]",
            aspect="equal",
            title="Postprocessed solution classes",
        )
        for ax in axes:
            ax.legend()
            ax.grid(alpha=0.3)
        fig.tight_layout()
        save_editable_figure(fig, folder / "gnss_overhead.png", dpi=150)
        plt.close(fig)
        p_time = np.array([r["utc_s"] for r in tracks["ppk"]])
        s_time = np.array([r["utc_s"] for r in tracks["spp"]])
        sxyz = bracket_interpolate(
            s_time,
            np.array([[r[k] for k in ["east_m", "north_m", "up_m"]] for r in tracks["spp"]]),
            p_time,
            2,
        )
        delta = (
            np.array([[r[k] for k in ["east_m", "north_m", "up_m"]] for r in tracks["ppk"]]) - sxyz
        )
        fig, axes = plt.subplots(3, 1, figsize=(10, 7), sharex=True)
        axes[0].plot((p_time - s["start_utc_s"]) / 60, np.linalg.norm(delta[:, :2], axis=1))
        axes[0].set(ylabel="SPP–PPK horizontal [m]")
        axes[1].step(
            (p_time - s["start_utc_s"]) / 60, [r["quality"] for r in tracks["ppk"]], where="mid"
        )
        axes[1].set(ylabel="PPK Q (1 fixed/2 float/4 DGPS)", yticks=[1, 2, 4])
        axes[2].plot((s_time - s["start_utc_s"]) / 60, [r["h_accuracy_m"] for r in tracks["spp"]])
        axes[2].set(ylabel="Receiver hAcc [m]", xlabel="Minutes from interval start")
        for ax in axes:
            ax.grid(alpha=0.3)
        fig.tight_layout()
        save_editable_figure(fig, folder / "gnss_quality.png", dpi=150)
        plt.close(fig)
        info = {
            "experiment_id": s["experiment_id"],
            "ppk_quality_counts": dict(Counter(r["quality"] for r in tracks["ppk"])),
            "spp_ppk_horizontal_difference_median_m": float(
                np.nanmedian(np.linalg.norm(delta[:, :2], axis=1))
            ),
            "spp_ppk_horizontal_difference_p95_m": float(
                np.nanpercentile(np.linalg.norm(delta[:, :2], axis=1), 95)
            ),
        }
        if s["experiment_id"] != eid:
            logs = root / f"data/processed/{s['experiment_id']}/synchronized"
            rows = read(logs / "parent/P_MAIN_HEALTH_RANGE_65433.range_measurement.csv")
            t = np.array([float(r["sync_utc_s"]) for r in rows])
            depth = bracket_interpolate(pt, pd, t, 3)[:, 0]
            ranges = np.array(
                [
                    float(r["corrected_range_m"]) if int(r["range_state"]) == 3 else np.nan
                    for r in rows
                ]
            )
            valid = np.isfinite(ranges) & (ranges > 0)
            output = []
            for i, r in enumerate(rows):
                output.append(
                    {
                        **r,
                        "pressure_depth_m": depth[i],
                        "acoustic_candidate": bool(valid[i]),
                        "range_minus_depth_m": ranges[i] - depth[i],
                        "range_at_1450_m_s_m": ranges[i] * 1450 / 1500,
                        "range_at_1550_m_s_m": ranges[i] * 1550 / 1500,
                        "same_origin_sphere_depth_feasible": bool(
                            valid[i] and ranges[i] >= depth[i]
                        ),
                        "horizontal_radius_if_surface_parent_m": np.sqrt(
                            max(0, ranges[i] ** 2 - depth[i] ** 2)
                        )
                        if valid[i] and ranges[i] >= depth[i]
                        else np.nan,
                    }
                )
            write(folder / "acoustic_depth.csv", output)
            info.update(
                shot_count=len(rows),
                candidate_count=int(valid.sum()),
                candidate_fraction=float(valid.mean()),
                range_below_depth_count=int((valid & (ranges < depth)).sum()),
                range_state_counts=dict(Counter(r["range_state"] for r in rows)),
            )
            fig, axes = plt.subplots(2, 1, figsize=(11, 6), sharex=True)
            minutes = (t - s["start_utc_s"]) / 60
            axes[0].plot(minutes, depth, label="Pressure depth (surface proxy)", lw=1)
            axes[0].scatter(minutes, ranges, s=4, label="Acoustic state 3, 1500 m/s")
            axes[0].set(ylabel="Depth / range [m]")
            axes[0].legend()
            axes[1].scatter(minutes, ranges - depth, s=4)
            axes[1].axhline(0, color="black", lw=0.7)
            axes[1].set(ylabel="Range minus depth [m]", xlabel="Minutes from interval start")
            for ax in axes:
                ax.grid(alpha=0.3)
            fig.tight_layout()
            save_editable_figure(fig, folder / "acoustic_depth.png", dpi=150)
            plt.close(fig)
        manifest["segments"].append(info)
        (folder / "metrics.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    alternate_file = work / "base_041133/ar_continuous.pos"
    alternate = read_positions(alternate_file, origin, clock)
    write(out / "ppk_base_041133.csv", alternate)
    primary_times = np.array([r["utc_s"] for r in ppk])
    alternate_xyz = bracket_interpolate(
        np.array([r["utc_s"] for r in alternate]),
        np.array([[r[k] for k in ["east_m", "north_m", "up_m"]] for r in alternate]),
        primary_times,
        2,
    )
    difference = alternate_xyz - np.array(
        [[r[k] for k in ["east_m", "north_m", "up_m"]] for r in ppk]
    )
    horizontal = np.linalg.norm(difference[:, :2], axis=1)
    comparison = {
        "base_041133_quality_counts": dict(Counter(r["quality"] for r in alternate)),
        "horizontal_base_solution_difference_median_m": float(np.nanmedian(horizontal)),
        "horizontal_base_solution_difference_p95_m": float(np.nanpercentile(horizontal, 95)),
        "limit": "Station sensitivity, not surveyed error.",
    }
    (out / "base_comparison.json").write_text(json.dumps(comparison, indent=2))
    manifest["inputs"].extend(
        {"path": str(p), "sha256": sha256(p)}
        for p in [alternate_file, *list((work / "base_041133").glob("*.26*"))]
    )
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest["segments"], indent=2))


if __name__ == "__main__":
    main()
