"""Recompute matched static geometry and exploratory frequency comparison for both days."""

import argparse
import csv
import json
import subprocess
from datetime import datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from scipy.spatial.transform import Rotation

from aquabeacon_analysis.camera_calibration import load_camera_intrinsics
from aquabeacon_analysis.editable_plots import save_editable_figure
from aquabeacon_analysis.nanyo_position import ray_sphere_position
from aquabeacon_analysis.ngt_binary import sha256
from aquabeacon_analysis.ngt_position import calibrated_rays
from aquabeacon_analysis.static_evaluation import static_metrics
from aquabeacon_analysis.stationary_spectra import (
    common_grid_series,
    paired_spectra,
    spectrum,
    trend_periodic_decomposition,
    variance_identity,
)

ACQ = "20261101_nanyo_000"


def read(path):
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write(path, rows, fieldnames=None):
    if not rows and fieldnames is None:
        return
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames or list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def dump(path, item):
    path.write_text(json.dumps(item, indent=2, ensure_ascii=False), encoding="utf-8")


def array(rows, keys):
    return np.array([[float(r.get(k) or "nan") for k in keys] for r in rows])


def rotations(rows, role):
    return Rotation.from_quat(array(rows, [f"{role}_true_enu_q{k}" for k in "xyzw"]))


def window_rows(path, lo, hi, time_key="utc_s"):
    with path.open(encoding="utf-8-sig", newline="") as f:
        return [r for r in csv.DictReader(f) if lo <= float(r[time_key]) < hi]


def pressure_depth_summary(acquisition, lo, hi, out):
    folder = Path(f"results/{acquisition}/segmentation")
    source, upstream = folder / "selection.csv", folder / "manifest.json"
    provenance = json.loads(upstream.read_text(encoding="utf-8"))
    rows = window_rows(source, lo, hi, "sync_utc_s")
    field = "depth_m" if rows and "depth_m" in rows[0] else "relative_depth_m"
    depth = array(rows, [field])[:, 0]
    valid = np.isfinite(depth)
    if not valid.any():
        raise ValueError(f"{acquisition}: no pressure depth within the requested window")
    times = array(rows, ["sync_utc_s"])[:, 0]
    write(
        out / "pressure_depth_samples.csv",
        [
            {
                "sync_utc_s": t,
                "offset_s": t - lo,
                "pressure_mbar": r["pressure_mbar"],
                "pressure_depth_m": d,
            }
            for r, t, d in zip(rows, times, depth, strict=True)
        ],
    )
    values = depth[valid]
    summary = {
        "mean_m": float(values.mean()),
        "minimum_m": float(values.min()),
        "maximum_m": float(values.max()),
        "std_m": float(values.std()),
        "valid_samples": int(valid.sum()),
        "total_samples": len(rows),
        "first_offset_s": float(times[valid][0] - lo),
        "last_offset_s": float(times[valid][-1] - lo),
        "maximum_sample_gap_s": float(np.diff(times[valid]).max()) if valid.sum() > 1 else None,
        "reference": "Child pressure sensor; initial surface-pressure proxy; positive down",
        "selection": "All finite unsmoothed pressure samples in [start,end), independent of tracking/ranging availability; arithmetic mean; no interpolation",
        "source": str(source),
        "source_field": field,
        "upstream_manifest": str(upstream),
        "pressure_baseline_mbar": provenance["pressure_baseline_mbar"],
        "baseline_first_s": provenance["config"]["baseline_first_s"],
        "density_kg_m3_assumed": provenance["config"]["density_kg_m3_assumed"],
        "gravity_m_s2": provenance["config"]["gravity_m_s2"],
        "calibrated_depth": False,
    }
    return summary, [source, upstream]


def plot_position_overhead(spec, values, metrics, out):
    conditions = [
        ("corrected", "姿勢補正あり", "#007b94"),
        ("fixed_parent", "親機姿勢を固定", "#d28a25"),
        ("fixed_both", "親子姿勢を固定（追加感度）", "#706894"),
    ]
    centered = {name: data[:, :2] - data[:, :2].mean(axis=0) for name, data in values.items()}
    extent = 1.16 * max(
        0.05,
        *(float(np.max(np.abs(data))) for data in centered.values()),
        *(metrics[name]["horizontal_rmse_m"] for name, _, _ in conditions),
    )
    angle = np.linspace(0, 2 * np.pi, 361)
    with plt.rc_context({"font.family": "Yu Gothic", "font.size": 10}):
        fig, axes = plt.subplots(1, 3, figsize=(14, 6.3))
        fig.subplots_adjust(left=0.06, right=0.985, bottom=0.29, top=0.79, wspace=0.28)
        for ax, (name, title, color) in zip(axes, conditions, strict=True):
            data = centered[name]
            radius = metrics[name]["horizontal_rmse_m"]
            ax.axhline(0, color="#b0b7bd", lw=0.7, zorder=0)
            ax.axvline(0, color="#b0b7bd", lw=0.7, zorder=0)
            ax.plot(*data.T, ".", ms=6, alpha=0.75, color=color, label=f"有効観測 {len(data)}点")
            ax.plot(0, 0, "+", ms=13, mew=1.8, color="black", label="各系列の平均")
            ax.plot(
                radius * np.cos(angle),
                radius * np.sin(angle),
                "--",
                lw=1.4,
                color=color,
                label=f"水平RMSE {radius:.3f} m",
            )
            ax.set(
                xlabel="平均からの東方向偏差 [m]",
                ylabel="平均からの北方向偏差 [m]",
                title=title,
                xlim=(-extent, extent),
                ylim=(-extent, extent),
                aspect="equal",
            )
            ax.grid(alpha=0.2)
            ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.18), fontsize=9)
        condition = "・手動揺動" if "hand" in spec["condition"] else ""
        depth = metrics["pressure_depth"]
        fig.suptitle(
            f"{spec['start'][:10]} {spec['start'][11:19]} JST から60秒{condition}\n"
            f"静止位置の俯瞰分布 ｜ 子機圧力深度（区間平均）{depth['mean_m']:.2f} m",
            fontsize=14,
        )
        fig.supxlabel(
            "各系列の平均を原点とした真ENU。3条件は同じ観測点・同じ縮尺。円の半径は水平RMSE（信頼区間ではない）。",
            fontsize=9,
            y=0.025,
        )
        save_editable_figure(fig, out / "position_overhead.png", dpi=180)
        plt.close(fig)


def run_window(spec, config, config_path):
    eid = spec["experiment_id"]
    base = Path(f"results/{eid}")
    out = base / "stationary_spectra" / spec["id"]
    out.mkdir(parents=True, exist_ok=True)
    lo = datetime.fromisoformat(spec["start"]).timestamp()
    hi = lo + config["duration_s"]
    source = base / "absolute_position/positions.csv"
    manifest_path = base / "absolute_position/manifest.json"
    tracking = base / "light_tracking" / spec["id"]
    tracked = (tracking / "manifest.json").exists()
    if tracked:
        source = tracking / "positions.csv"
        manifest_path = tracking / "manifest.json"
    cfg = json.loads(manifest_path.read_text())["configuration"]
    rows = window_rows(source, lo, hi)
    valid = [r for r in rows if r["position_available"] == "True"]
    # Each physical shot contributes once: nearest valid image to that shot.
    grouped = {}
    for r in valid:
        key = r["shot_seq"]
        if key not in grouped or abs(float(r["range_delta_s"])) < abs(
            float(grouped[key]["range_delta_s"])
        ):
            grouped[key] = r
    good = sorted(grouped.values(), key=lambda r: float(r["utc_s"]))
    if len(good) < 4:
        raise ValueError(f"{spec['id']}: fewer than four matched observations")
    attitudes_path = Path(f"data/processed/{eid}/absolute_position/attitudes_10hz.csv")
    if tracked:
        attitudes_path = tracking / "attitudes_10hz.csv"
    attitudes = window_rows(attitudes_path, lo, hi)
    means = {}
    series = {}
    for role in ["parent", "child"]:
        av = [r for r in attitudes if r[f"{role}_attitude_available"] == "True"]
        rot = rotations(av, role)
        means[role] = rot.mean()
        rv = np.degrees((rot.mean().inv() * rot).as_rotvec())
        t = array(av, ["utc_s"])[:, 0] - lo
        for j, axis in enumerate(["roll", "pitch", "yaw"]):
            series[f"{role}_{axis}"] = (t, rv[:, j], "deg")
    k, distortion, intrinsics = load_camera_intrinsics(
        Path.cwd(), "P_MAIN_CAM", (1280, 720), "proxy"
    )
    rays = calibrated_rays(array(good, ["x_px", "y_px"]), k, distortion)
    axes_path = Path(cfg["parent_axes"])
    axes = np.array(json.loads(axes_path.read_text())["R_body_from_spresense"])
    lever = {k: np.array(v) for k, v in cfg["lever_arms_frd_m"].items() if k != "child_imu"}
    rp, rc = rotations(good, "parent"), rotations(good, "child")
    values = {"corrected": [], "fixed_parent": [], "fixed_both": []}
    exported = []
    for i, row in enumerate(good):
        mount = cfg["mounts"]["parent_" + Path(row["source_video"]).stem]
        body_opt = (
            axes
            @ np.array(mount["R_dji_from_sensor"]).T
            @ np.array(cfg["R_dji_from_parent_optical"])
        )
        item = dict(row)
        for name, p_rot, c_rot in [
            ("corrected", rp[i].as_matrix(), rc[i].as_matrix()),
            ("fixed_parent", means["parent"].as_matrix(), rc[i].as_matrix()),
            ("fixed_both", means["parent"].as_matrix(), means["child"].as_matrix()),
        ]:
            origin, _, _, _ = ray_sphere_position(
                np.zeros(3), p_rot, c_rot, rays[i], body_opt, float(row["range_m"]), **lever
            )
            relative = origin + p_rot @ lever["parent_antenna"]
            values[name].append(relative)
            for j, axis in enumerate(["east", "north", "up"]):
                item[f"{name}_{axis}_m"] = relative[j]
        exported.append(item)
    values = {k: np.array(v) for k, v in values.items()}
    existing = array(good, [f"child_origin_{a}_m" for a in ["east", "north", "up"]]) - (
        array(good, [f"parent_antenna_{a}_m" for a in ["east", "north", "up"]])
        - rp.apply(lever["parent_antenna"])
    )
    np.testing.assert_allclose(values["corrected"], existing, atol=1e-8, rtol=0)
    tpos = array(good, ["utc_s"])[:, 0] - lo
    position_times = array(rows, ["utc_s"])[:, 0] - lo
    observed_by_shot = {r["shot_seq"]: i for i, r in enumerate(good)}
    for name, data in values.items():
        for j, axis in enumerate(["east", "north", "up"]):
            full = np.array(
                [
                    data[observed_by_shot[r["shot_seq"]], j]
                    if r["shot_seq"] in observed_by_shot and r["position_available"] == "True"
                    else np.nan
                    for r in rows
                ]
            )
            series[f"{name}_{axis}"] = (position_times, full, "m")
    # Preserve all original candidate/missing rows alongside the unique-shot evaluation.
    write(out / "all_position_samples.csv", rows)
    write(out / "matched_positions.csv", exported)
    range_path = base / "evaluation/acoustic_depth.csv"
    ranges = window_rows(range_path, lo, hi, "sync_utc_s")
    tr = array(ranges, ["sync_utc_s"])[:, 0] - lo
    yr = np.array(
        [
            float(r["corrected_range_m"]) if r["acoustic_candidate"] == "True" else np.nan
            for r in ranges
        ]
    )
    series["range"] = (tr, yr, "m")
    series["pressure_depth"] = (tr, array(ranges, ["pressure_depth_m"])[:, 0], "m")
    optical_path = base / "relative_position/camera_relative_candidates.csv"
    if tracked:
        optical_path = tracking / "camera_relative_candidates.csv"
    optical = window_rows(optical_path, lo, hi)
    to = array(optical, ["utc_s"])[:, 0] - lo
    for axis in ["x", "y"]:
        y = np.array(
            [
                float(r[f"{axis}_px"])
                if r["optical_candidate"] == "True" and r["water_like_heuristic"] == "True"
                else np.nan
                for r in optical
            ]
        )
        series[f"light_{axis}"] = (to, y, "px")
    series_rows, spectra_rows, spectral_info = [], [], {}
    for name, (t, y, unit) in series.items():
        for x, value in zip(t, y, strict=True):
            series_rows.append(
                {"series": name, "offset_s": float(x), "value": float(value), "unit": unit}
            )
        info, arrays = spectrum(t, y, config["maximum_comparison_hz"])
        spectral_info[name] = info
        for method, fkey, pkey in [
            ("FFT_Hann", "fft_frequency_hz", "fft_psd"),
            ("Lomb_normalized", "lomb_frequency_hz", "lomb_normalized_power"),
        ]:
            if fkey in arrays:
                for f, p in zip(arrays[fkey], arrays[pkey], strict=True):
                    spectra_rows.append(
                        {
                            "series": name,
                            "method": method,
                            "frequency_hz": float(f),
                            "power": float(p),
                            "power_unit": f"{unit}^2/Hz"
                            if method == "FFT_Hann"
                            else "dimensionless",
                        }
                    )
    write(out / "series.csv", series_rows)
    write(out / "spectra.csv", spectra_rows)
    grid = np.arange(0, config["duration_s"], 1 / config["common_coherence_hz"])
    common = {
        name: common_grid_series(t, y, grid, config["common_coherence_hz"])
        for name, (t, y, _) in series.items()
    }
    pairs = [
        (p, q)
        for p in [
            "parent_roll",
            "parent_pitch",
            "parent_yaw",
            "child_roll",
            "child_pitch",
            "child_yaw",
        ]
        for q in [
            "range",
            "light_x",
            "light_y",
            "fixed_parent_east",
            "fixed_parent_north",
            "corrected_east",
            "corrected_north",
        ]
    ]
    pair_info, cross_rows = {}, []
    for first, second in pairs:
        info, arrays = paired_spectra(
            common[first], common[second], config["common_coherence_hz"], config["welch_window_s"]
        )
        pair_info[f"{first}__{second}"] = info
        if arrays:
            band = (arrays["frequency_hz"] >= 0.05) & (arrays["frequency_hz"] <= 0.45)
            ids = np.flatnonzero(band & np.isfinite(arrays["coherence"]))
            if len(ids):
                index = ids[np.argmax(arrays["coherence"][ids])]
                info.update(
                    max_coherence=float(arrays["coherence"][index]),
                    frequency_hz=float(arrays["frequency_hz"][index]),
                )
            for i, f in enumerate(arrays["frequency_hz"]):
                cross_rows.append(
                    {
                        "first": first,
                        "second": second,
                        "frequency_hz": f,
                        "coherence": arrays["coherence"][i],
                        "phase_y_minus_x_rad": arrays["phase_y_minus_x_rad"][i],
                        "windows": info["welch_windows"],
                    }
                )
    write(
        out / "cross_spectra.csv",
        cross_rows,
        ["first", "second", "frequency_hz", "coherence", "phase_y_minus_x_rad", "windows"],
    )
    write(
        out / "coherence_grid.csv",
        [{"offset_s": t, **{k: v[i] for k, v in common.items()}} for i, t in enumerate(grid)],
    )
    metrics = {name: static_metrics(data) for name, data in values.items()}
    metrics["variance_identity"] = variance_identity(values["fixed_parent"], values["corrected"])
    metrics["range_rmse_m"] = float(np.nanstd(yr))
    attitude_candidates = [
        name
        for name in ["parent_roll", "parent_pitch", "parent_yaw"]
        if "lomb_peak_hz" in spectral_info[name]
    ]
    if attitude_candidates:
        dominant = max(attitude_candidates, key=lambda name: spectral_info[name]["detrended_rmse"])
        frequency = spectral_info[dominant]["lomb_peak_hz"]
        metrics["attitude_selected_frequency"] = {
            "series": dominant,
            "frequency_hz": frequency,
            "period_s": 1 / frequency,
        }
        metrics["orthogonal_decomposition"] = {
            name: trend_periodic_decomposition(tpos, value, frequency)
            for name, value in values.items()
        }
    metrics["matched_shots"] = len(good)
    metrics["original_valid_positions"] = len(valid)
    metrics["candidate_positions"] = len(rows)
    metrics["range_records"] = len(ranges)
    metrics["range_valid_records"] = int(np.isfinite(yr).sum())
    metrics["first_position_offset_s"] = float(tpos[0])
    metrics["last_position_offset_s"] = float(tpos[-1])
    metrics["maximum_position_gap_s"] = float(np.diff(tpos).max())
    metrics["rmse_reduction_percent"] = 100 * (
        1 - metrics["corrected"]["rmse_3d_m"] / metrics["fixed_parent"]["rmse_3d_m"]
    )
    metrics["position_source_geometry_max_difference_m"] = float(
        np.max(np.abs(existing - values["corrected"]))
    )
    experiment = json.loads(Path(f"experiments/{eid}/experiment.json").read_text(encoding="utf-8"))
    acquisition = experiment["parent_acquisition"]
    metrics["pressure_depth"], depth_inputs = pressure_depth_summary(acquisition, lo, hi, out)
    plot_position_overhead(spec, values, metrics, out)
    # Sensitivity uses fixed subwindows, never selects the best RMSE window.
    metrics["subwindows"] = []
    for a, b in [(0, 30), (30, 60), (10, 50)]:
        m = (tpos >= a) & (tpos < b)
        if m.sum() >= 4:
            metrics["subwindows"].append(
                {
                    "offset_interval_s": [a, b],
                    "n": int(m.sum()),
                    **{k: static_metrics(v[m])["rmse_3d_m"] for k, v in values.items()},
                }
            )
    fig, axs = plt.subplots(5, 1, figsize=(11, 12), sharex=True)
    for name in ["parent_roll", "parent_pitch", "parent_yaw"]:
        t, y, _ = series[name]
        axs[0].plot(t, y, label=name, lw=0.8)
    for name in ["light_x", "light_y"]:
        t, y, _ = series[name]
        axs[1].plot(t, y - np.nanmean(y), ".", label=name, ms=3)
    axs[2].plot(tr, yr - np.nanmean(yr), ".", label="range", ms=4)
    for ax, axis in zip(axs[3:], ["east", "north"], strict=True):
        for prefix in ["corrected", "fixed_parent"]:
            t, y, _ = series[f"{prefix}_{axis}"]
            ax.plot(t, y - np.nanmean(y), ".", ms=4, label=f"{prefix}_{axis}")
    for ax, label in zip(
        axs,
        [
            "Parent rotation [deg]",
            "Image residual [px]",
            "Range residual [m]",
            "East residual [m]",
            "North residual [m]",
        ],
        strict=True,
    ):
        ax.set_ylabel(label)
        ax.legend(loc="upper right", fontsize=8)
        ax.grid(alpha=0.25)
    axs[-1].set(
        xlabel=f"Seconds after {spec['start']} (observations; no gap filling)", xlim=(0, 60)
    )
    fig.suptitle(spec["id"] + " / " + spec["condition"])
    fig.tight_layout()
    save_editable_figure(fig, out / "timeseries.png", dpi=160)
    plt.close(fig)
    fig, axs = plt.subplots(4, 2, figsize=(12, 12), sharex=True)
    panels = [
        ["range"],
        ["parent_roll", "parent_pitch", "parent_yaw"],
        ["light_x", "light_y"],
        ["corrected_east", "corrected_north", "fixed_parent_east", "fixed_parent_north"],
    ]
    for i, names in enumerate(panels):
        for j, method in enumerate(["FFT_Hann", "Lomb_normalized"]):
            for name in names:
                a = [r for r in spectra_rows if r["series"] == name and r["method"] == method]
                axs[i, j].plot(
                    [r["frequency_hz"] for r in a], [r["power"] for r in a], label=name, lw=1
                )
            axs[i, j].set(
                ylabel="PSD [unit²/Hz]" if j == 0 else "Normalized LS power", xlim=(0, 0.5)
            )
            axs[i, j].grid(alpha=0.25)
            axs[i, j].legend(fontsize=7)
            if not any(r["series"] in names and r["method"] == method for r in spectra_rows):
                axs[i, j].text(
                    0.5,
                    0.5,
                    "Insufficient contiguous observations",
                    transform=axs[i, j].transAxes,
                    ha="center",
                    fontsize=10,
                )
    axs[0, 0].set_title("Hann FFT: longest contiguous run")
    axs[0, 1].set_title("Lomb-Scargle: observed samples, linear detrend")
    for ax in axs[-1]:
        ax.set_xlabel("Frequency [Hz]")
    fig.suptitle(spec["id"])
    fig.tight_layout()
    save_editable_figure(fig, out / "spectra.png", dpi=160)
    plt.close(fig)
    fig, axs = plt.subplots(3, 1, figsize=(11, 9), sharex=True)
    for ax, names in zip(
        axs,
        [
            ["parent_roll", "parent_pitch", "parent_yaw"],
            ["child_roll", "child_pitch", "child_yaw"],
            ["light_x", "light_y"],
        ],
        strict=True,
    ):
        for name in names:
            a = [r for r in spectra_rows if r["series"] == name and r["method"] == "FFT_Hann"]
            ax.semilogy(
                [r["frequency_hz"] for r in a], [max(r["power"], 1e-12) for r in a], label=name
            )
        ax.axvspan(0.45, 5, color="gray", alpha=0.12, label="Beyond common acoustic band")
        if not any(r["series"] in names and r["method"] == "FFT_Hann" for r in spectra_rows):
            ax.text(
                0.5,
                0.5,
                "No continuous run >= 8 seconds; spectrum withheld",
                transform=ax.transAxes,
                ha="center",
            )
        ax.set(ylabel="PSD [unit²/Hz]", xlim=(0, 5))
        ax.legend(fontsize=8)
        ax.grid(alpha=0.25)
    axs[-1].set_xlabel("Frequency [Hz]; attitude and image only, 10 Hz sampling")
    fig.tight_layout()
    save_editable_figure(fig, out / "native_spectra.png", dpi=160)
    plt.close(fig)
    fig, ax = plt.subplots(figsize=(11, 7))
    gridfreq = np.arange(1, 8) / 16
    matrix = np.full((len(pairs), 7), np.nan)
    for i, (p, q) in enumerate(pairs):
        for r in cross_rows:
            if r["first"] == p and r["second"] == q and 0 < r["frequency_hz"] < 0.5:
                matrix[i, round(r["frequency_hz"] * 16) - 1] = r["coherence"]
    mesh = ax.imshow(matrix, aspect="auto", vmin=0, vmax=1, interpolation="nearest", cmap="viridis")
    ax.set_xticks(range(7), [f"{v:.3f}" for v in gridfreq])
    ax.set_yticks(range(len(pairs)), [f"{a} / {b}" for a, b in pairs], fontsize=7)
    ax.set(
        xlabel="Frequency [Hz]",
        title="Descriptive coherence; blank = fewer than 3 complete Welch windows",
    )
    fig.colorbar(mesh, ax=ax, label="Magnitude-squared coherence")
    fig.tight_layout()
    save_editable_figure(fig, out / "coherence.png", dpi=160)
    plt.close(fig)
    inputs = [
        source,
        manifest_path,
        attitudes_path,
        range_path,
        optical_path,
        axes_path,
        config_path,
        Path(__file__),
        Path("src/aquabeacon_analysis/stationary_spectra.py"),
        Path("src/aquabeacon_analysis/nanyo_position.py"),
        Path(f"experiments/{eid}/experiment.json"),
        *depth_inputs,
    ]
    result = {
        "window": spec,
        "duration_s": config["duration_s"],
        "metrics": metrics,
        "spectra": spectral_info,
        "coherence": pair_info,
        "configuration": config,
        "geometry_configuration": cfg,
        "intrinsics": intrinsics,
        "inputs_sha256": {str(p): sha256(p) for p in inputs},
        "git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "accuracy_validated": False,
        "tracked_input": tracked,
        "wave_causality_identified": False,
        "position_selection": "upstream available; one nearest optical observation per shot; no additional outlier rejection",
        "overhead_position_plot": {
            "series": ["corrected", "fixed_parent", "fixed_both"],
            "coordinates": "True ENU horizontal residuals about each series arithmetic mean",
            "scale": "Equal east/north scale and common limits across the three conditions in each window",
            "circle": "Horizontal RMSE radius; not a confidence interval",
            "source": "matched_positions.csv; all matched shots; no smoothing, outlier rejection or gap connections",
        },
    }
    dump(out / "manifest.json", result)
    lines = [
        f"# 静止区間の周波数評価：{spec['id']}",
        "",
        f"{spec['start']} から60秒。条件：{spec['condition']}。",
        "",
        f"有効な重複なし測距・位置対応 {len(good)}点、元の有効画像位置 {len(valid)}点。測距有効 {np.isfinite(yr).sum()}/{len(ranges)}件。",
        f"最初の位置は開始後{tpos[0]:.2f}秒、最後は{tpos[-1]:.2f}秒。最大間隔{np.diff(tpos).max():.2f}秒。",
        "",
        "RMSEは各系列自身の平均周り・分母N。真値誤差ではなく、実運動・誤検出・同期・校正誤差を含む。",
        "",
        "| 条件 | 平均深度 m | 東RMSE m | 北RMSE m | 上RMSE m | 水平RMSE m | 3D RMSE m |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for label, name in [
        ("姿勢補正あり", "corrected"),
        ("親機姿勢を固定（前回と同じ比較）", "fixed_parent"),
        ("親子姿勢を両方固定（追加感度）", "fixed_both"),
    ]:
        m = metrics[name]
        lines.append(
            f"| {label} | {metrics['pressure_depth']['mean_m']:.2f} | "
            + " | ".join(
                f"{v:.4f}" for v in [*m["axis_rmse_m"], m["horizontal_rmse_m"], m["rmse_3d_m"]]
            )
            + " |"
        )
    lines += [
        "",
        f"深度は子機水圧の60秒区間平均。{metrics['pressure_depth']['valid_samples']}標本、最小{metrics['pressure_depth']['minimum_m']:.3f} m、最大{metrics['pressure_depth']['maximum_m']:.3f} m。",
        "収録初期180秒の水面圧代理値、仮定密度1025 kg/m³から求めた未校正の相対深度。海底深度や音響斜距離ではない。追跡・測距の成否によらず区間内の有限な未平滑化圧力データを使い、欠測は補間しない。",
        "[深度の全標本](pressure_depth_samples.csv)。10:15の測距開始前も、圧力が記録された時刻は深度集計に含む。",
        "",
        f"親機姿勢補正による3D RMSE変化：{metrics['rmse_reduction_percent']:.1f}%減。測距RMSE {metrics['range_rmse_m']:.4f} m。",
        "同じ観測・距離・校正を使い、親機の回転だけを10 Hz姿勢の区間平均へ固定して再計算。子機姿勢を保持した比較と、両方固定の比較を区別した。",
        "",
        "![静止位置の俯瞰分布](position_overhead.png)",
        "GNSS静止評価と同様、各系列の平均を原点とした東・北の分布。3条件は同じ採用点・同じ縮尺で、点間を結ばず欠測を保持。円の半径は水平RMSEであり、3D RMSEや信頼区間ではない。系列の平均差・絶対位置誤差はこの図では評価しない。",
        "[俯瞰図SVG](position_overhead.svg) / [編集マスター](position_overhead.figure.pickle)",
        "",
        "![時系列](timeseries.png)",
        "",
        "| 系列 | 標本数 | RMSE（元の単位） | 線形傾き/秒 | LSピーク Hz | 周期 s | FFT連続長 s | FFTピーク Hz |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, info in spectral_info.items():
        if name.startswith("fixed_both"):
            continue

        def fmt(key, info=info):
            return f"{info[key]:.4f}" if key in info else "—"

        lines.append(
            f"| {name} | {info['n']} | {fmt('rmse_about_mean')} | {fmt('linear_slope_per_s')} | {fmt('lomb_peak_hz')} | {fmt('lomb_period_s')} | {fmt('fft_duration_s')} | {fmt('fft_peak_hz')} |"
        )
    lines += [
        "",
        "LSはLomb–Scargle。不均一な実観測時刻を使用。FFTは1.5標本周期を超える欠測を跨がず、最長の連続部分へHann窓を適用した。",
        "スペクトルのみ線形トレンドを除去。RMSEはトレンド除去・平滑化なし。ピーク検索は2周期以上・共通上限0.45 Hz。数値グリッドの細かさを周波数分解能と解釈しない。",
        "SO(3)平均周りの回転ベクトル成分をroll/pitch/yaw相当として使用し、±180°の折返しをFFTへ入れない。",
        "",
        "![FFTとLS](spectra.png)",
        "![姿勢・画像の高周波側](native_spectra.png)",
        "0.45 Hzより高い帯域は10 Hz姿勢・光点だけの診断。day2の1 Hz測距・位置との同一帯域比較には使わない。10 Hz取得より高い運動のエイリアシングは否定できない。",
        "FFT図が空欄の系列は8秒以上の連続観測がなく算出を見送った。Lomb–Scargleは欠測を埋めず実観測点を使う。低周波の検出可能性と連続長を表で確認する。",
        "",
        "![コヒーレンス](coherence.png)",
        "",
        "コヒーレンスは共通1 Hz・16秒Hann窓・50%重複、完全窓3本以上に限定。高レート系列は0.4 Hzの4次Butterworthで低域化してから間引く。",
        "重複窓は独立ではなく、最大値は多数の組合せからの探索値。位相はCSVに保存。高コヒーレンスだけで波由来を証明したり、残差をセンサ雑音と呼ぶことはできない。",
        "",
        "分散の恒等式：補正後 = 固定姿勢 + 補正差。各系列の平均を除き、Var(C)=Var(F)+Var(D)+2Cov(F,D)を計算。",
        f"`{json.dumps(metrics['variance_identity'], ensure_ascii=False)}`",
        "負の共分散は変動の打消しを示す。各項は独立した波・雑音の寄与率ではない。",
        "",
        "[入力時系列](series.csv) / [FFT・LS](spectra.csv) / [周波数別コヒーレンスと位相](cross_spectra.csv) / [同一観測の位置比較](matched_positions.csv) / [来歴と窓感度](manifest.json)",
        "",
        "波高計・独立位置真値がなく、波と測定雑音の一意な分離は未達。手動揺動は自然波と分けて扱う。",
    ]
    if tracked:
        lines += [
            "",
            (
                f"追跡済み光源を使用。[全時刻の追跡確認](../../light_tracking/{spec['id']}/tracking_review.mp4) / "
                f"[拡大確認画像](../../light_tracking/{spec['id']}/all_crops.jpg)。"
            ),
            "10 Hzで画像を追跡し、測距イベントごとに75 ms以内の画像を1枚対応付けた。初期点と追跡喪失後の再初期化は画像確認に基づく。平滑化・予測位置の出力はしない。",
        ]
    if "orthogonal_decomposition" in metrics:
        selected = metrics["attitude_selected_frequency"]
        lines += [
            "",
            f"姿勢だけで選んだ主周期：{selected['series']}、{selected['frequency_hz']:.4f} Hz（{selected['period_s']:.2f}秒）。",
            "同一観測の位置から、線形トレンド→その周波数のsin/cos→残りを直交射影で分けた。3項の分散和は元の平均周り分散に一致する。",
            "これは探索的な成分分解であり、主周期成分を波だけ、残りを雑音だけと認定しない。",
            "",
            "| 条件 | トレンドRMS m | 姿勢主周期成分RMS m | 残りRMS m |",
            "|---|---:|---:|---:|",
        ]
        for name, m in metrics["orthogonal_decomposition"].items():
            lines.append(
                f"| {name} | {m['rms_m']['trend']:.4f} | {m['rms_m']['attitude_frequency']:.4f} | {m['rms_m']['remainder']:.4f} |"
            )
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(
        spec["id"],
        {k: metrics[k] for k in ["matched_shots", "range_rmse_m", "rmse_reduction_percent"]},
        flush=True,
    )
    return result


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--experiment", required=True, choices=[ACQ])
    parser.add_argument("--window", action="append")
    args = parser.parse_args()
    config_path = Path(f"config/{ACQ}/stationary_spectra.json")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    results = [
        run_window(w, config, config_path)
        for w in config["windows"]
        if not args.window or w["id"] in args.window
    ]
    out = Path(f"results/{ACQ}/stationary_spectra")
    out.mkdir(parents=True, exist_ok=True)
    dump(out / "comparison.json", results)
    write(
        out / "comparison.csv",
        [
            {
                "window": r["window"]["id"],
                "condition": r["window"]["condition"],
                "matched_shots": r["metrics"]["matched_shots"],
                "pressure_depth_mean_m": r["metrics"]["pressure_depth"]["mean_m"],
                "pressure_depth_minimum_m": r["metrics"]["pressure_depth"]["minimum_m"],
                "pressure_depth_maximum_m": r["metrics"]["pressure_depth"]["maximum_m"],
                "pressure_depth_samples": r["metrics"]["pressure_depth"]["valid_samples"],
                "corrected_3d_rmse_m": r["metrics"]["corrected"]["rmse_3d_m"],
                "fixed_parent_3d_rmse_m": r["metrics"]["fixed_parent"]["rmse_3d_m"],
                "range_rmse_m": r["metrics"]["range_rmse_m"],
                "reduction_percent": r["metrics"]["rmse_reduction_percent"],
            }
            for r in results
        ],
    )


if __name__ == "__main__":
    main()
