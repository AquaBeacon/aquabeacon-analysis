"""Audit transferred field results and compare conditional scatter with aquabeacon-sim."""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote

os.environ.setdefault("OMP_NUM_THREADS", "2")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "2")
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from aquabeacon_analysis.editable_plots import save_editable_figure
from aquabeacon_analysis.experiments import load_experiment
from aquabeacon_analysis.field_review import (
    centered_scatter,
    directional_scatter,
    nearest_indices,
    observation_coverage,
    valid_bracket_support,
)
from aquabeacon_analysis.ngt_binary import sha256

ROOT = Path(__file__).resolve().parents[1]
INPUTS = {}


def record(path):
    path = Path(path).resolve()
    key = os.path.relpath(path, ROOT).replace("\\", "/")
    if key not in INPUTS:
        INPUTS[key] = {"sha256": sha256(path), "bytes": path.stat().st_size}
    return path


def read(path):
    with record(path).open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def load(path):
    return json.loads(record(path).read_text(encoding="utf-8"))


def dump(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False),
                    encoding="utf-8")


def write_csv(path, rows):
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        fields = list(dict.fromkeys(k for r in rows for k in r))
        writer = csv.DictWriter(handle, fields)
        writer.writeheader()
        writer.writerows(rows)


def array(rows, columns):
    return np.array([[float(r[c]) for c in columns] for r in rows])


def stationary(cfg, out):
    spec = load(ROOT / cfg["stationary_windows"])
    rows, details, vectors = [], {}, {}
    abs_rows = read(ROOT / "results/20261101_nanyo_000/stationary_gnss_comparison/comparison.csv")
    for w in spec["windows"]:
        folder = ROOT / f'results/{w["experiment_id"]}/stationary_spectra/{w["id"]}'
        m = load(folder / "manifest.json")
        samples = read(folder / "matched_positions.csv")
        if len({r["shot_seq"] for r in samples}) != len(samples):
            raise AssertionError("Repeated acoustic shots in stationary comparison")
        xyz = array(samples, [f"corrected_{c}_m" for c in ("east", "north", "up")])
        fixed = array(samples, [f"fixed_parent_{c}_m" for c in ("east", "north", "up")])
        metrics = directional_scatter(xyz)
        fixed_metrics = centered_scatter(fixed)
        assert abs(metrics["rms_3d_m"] - m["metrics"]["corrected"]["rmse_3d_m"]) < 1e-10
        start = datetime.fromisoformat(w["start"]).timestamp()
        coverage = observation_coverage(sorted(float(s["utc_s"]) for s in samples), start, start+60)
        ranges = array(samples, ["range_m"])[:, 0]
        track = load(ROOT / f'results/{w["experiment_id"]}/light_tracking/{w["id"]}/manifest.json')
        current = {r["solution"]: r for r in abs_rows if r["window"] == w["id"]
                   and r["attitude"] == "corrected" and r["sampling"] == "linear"}
        row = {"window": w["id"], "experiment_id": w["experiment_id"], "start_jst": w["start"],
               "condition": w["condition"], "samples": len(samples),
               "depth_mean_m": m["metrics"]["pressure_depth"]["mean_m"],
               "mean_range_m": float(ranges.mean()), "range_centered_rms_m": float(ranges.std()),
               "relative_rms_m": metrics["rms_3d_m"], "fixed_parent_rms_m": fixed_metrics["rms_3d_m"],
               "reduction_percent": 100*(1-metrics["rms_3d_m"]/fixed_metrics["rms_3d_m"]),
               "radial_rms_m": metrics["radial_rms_m"], "tangential_rms_m": metrics["tangential_rms_m"],
               "tangential_variance_percent": 100*metrics["tangential_variance_fraction"],
               "equivalent_tangent_angle_deg": metrics["equivalent_tangent_angle_deg"],
               "range_records": m["metrics"]["range_records"],
               "shot_acceptance_percent": 100*len(samples)/m["metrics"]["range_records"],
               "max_gap_with_edges_s": coverage["maximum_gap_with_edges_s"],
               "leading_gap_s": coverage["leading_gap_s"],
               "absolute_ppk_ar_rms_m": float(current["ppk_ar"]["rmse_3d_m"]),
               "absolute_ppk_float_rms_m": float(current["ppk_float"]["rmse_3d_m"])}
        rows.append(row)
        details[w["id"]] = {"scatter": metrics, "coverage": coverage, "source_manifest": m,
                             "tracking_manifest": track}
        vectors[w["id"]] = np.asarray(metrics["mean_m"])
    # Pool comparison uses the corrected, unique acoustic events, not repeated 5 Hz cells.
    pool = ROOT / f'results/20261028_ngt_002/position_evaluation/{cfg["ngt_coordinate_run"]}'
    data = [r for r in read(pool / "positions_at_acoustic_events.csv")
            if r["magnetic_position_available"] == "True"]
    xyz = array(data, ["relative_magnetic_east_m", "relative_magnetic_north_m", "relative_down_m"])
    xyz[:, 2] *= -1
    met = directional_scatter(xyz)
    comparison = read(ROOT / "results/20261028_ngt_000/coordinate_reassessment/position_comparison.csv")
    assert abs(met["rms_3d_m"]-float(comparison[0]["corrected_centered_rms_3d_m"])) < 1e-10
    ranges = array(data, ["range_m"])[:, 0]
    row = {"window": "ngt_002", "experiment_id": "20261028_ngt_002", "condition": "pool_static",
           "samples": len(data), "relative_rms_m": met["rms_3d_m"],
           "mean_range_m": float(ranges.mean()), "range_centered_rms_m": float(ranges.std()),
           "radial_rms_m": met["radial_rms_m"], "tangential_rms_m": met["tangential_rms_m"],
           "tangential_variance_percent": 100*met["tangential_variance_fraction"],
           "equivalent_tangent_angle_deg": met["equivalent_tangent_angle_deg"]}
    vectors["ngt_002"] = np.asarray(met["mean_m"])
    details["ngt_002"] = {"scatter": met, "source_metrics": load(pool / "metrics.json")}
    write_csv(out / "stationary_summary.csv", rows)
    write_csv(out / "pool_position_comparison.csv", comparison)
    dump(out / "stationary_details.json", details)
    return [row, *rows], vectors


def simulation(cfg, sim_root, empirical, vectors, out):
    sys.path.insert(0, str(sim_root))
    from src.estimator import estimate_position, position_covariance
    from src.sensors import (
        effective_sigma,
        forward_observation,
        inverse_observation,
        optical_angular_sigma,
        optical_dropout_prob,
    )
    model = tomllib.loads(record(sim_root / "config.toml").read_text(encoding="utf-8"))
    for path in ("src/sensors.py", "src/estimator.py", "src/evaluation.py", "docs/MATH_SPEC.md"):
        record(sim_root / path)
    dump(out / "simulation_config_snapshot.json", model)
    sigma = np.array([model["noise"]["sigma_dist"], np.deg2rad(model["noise"]["sigma_az_deg"]),
                      np.deg2rad(model["noise"]["sigma_el_deg"])])
    optical = {**model["optical"], "sigma_ref": np.deg2rad(model["optical"]["sigma_ref_deg"]),
               "sigma_floor": np.deg2rad(model["optical"]["sigma_floor_deg"])}
    noise = np.random.default_rng(cfg["simulation_seed"]).normal(size=(cfg["monte_carlo_samples"], 3))
    rows, checks = [], []
    for obs in empirical:
        wid = obs["window"]
        # Approximate range/bearing center: observed body-vector direction and acoustic range.
        center = vectors[wid] / np.linalg.norm(vectors[wid]) * obs["mean_range_m"]
        z0 = forward_observation(center)
        growth = effective_sigma(z0[0], sigma, model["error_model"]["range_growth_per_m"],
                                 model["error_model"]["dist_growth_per_m"])
        sig_opt = optical_angular_sigma(z0[0], optical)
        cases = [("base_noise", sigma), ("range_growth", growth),
                 ("optical_c0_3_conditional", np.array([growth[0], sig_opt, sig_opt]))]
        for angle in cfg["simulation_angular_sensitivity_deg"]:
            cases.append((f"observed_range_scatter_angle_{angle:g}",
                          np.array([obs["range_centered_rms_m"], *([np.deg2rad(angle)]*2)])))
        for name, sig in cases:
            z = z0 + noise * sig
            estimates = inverse_observation(*z.T).T
            scatter = centered_scatter(estimates)["rms_3d_m"]
            true_rms = float(np.sqrt(np.mean(np.sum((estimates-center)**2, axis=1))))
            linear = float(np.sqrt(np.trace(position_covariance(center, sig))))
            if name == "base_noise":
                np.testing.assert_allclose(scatter, linear, rtol=0.015)
                for i in range(4):
                    np.testing.assert_allclose(estimate_position(z[i], sig), estimates[i], atol=1e-9)
                checks.append({"window": wid, "mc_linear_relative_difference": scatter/linear-1,
                               "estimator_roundtrip_checked": 4})
            rows.append({"window": wid, "case": name, "reference_range_m": z0[0],
                         "reference_az_deg": np.rad2deg(z0[1]), "reference_el_deg": np.rad2deg(z0[2]),
                         "sigma_range_m": sig[0], "sigma_az_deg": np.rad2deg(sig[1]),
                         "sigma_el_deg": np.rad2deg(sig[2]), "mc_centered_rms_m": scatter,
                         "mc_truth_rms_m": true_rms, "linearized_rms_m": linear,
                         "observed_centered_rms_m": obs["relative_rms_m"],
                         "observed_to_sim_scatter_ratio": obs["relative_rms_m"]/scatter,
                         "default_optical_dropout_probability": optical_dropout_prob(z0[0], optical),
                         "seed": cfg["simulation_seed"], "mc_samples": cfg["monte_carlo_samples"]})
    write_csv(out / "simulation_comparison.csv", rows)
    dump(out / "simulation_checks.json", checks)
    return rows


def mapping_audit(cfg, out):
    trial_rows, fragment_rows = [], []
    for eid in cfg["nanyo_trials"]:
        base = ROOT / f"results/{eid}"
        vm = load(base / "visual_mapping/manifest.json")
        selected = load(base / "mapping_selected/manifest.json")
        tracking = load(base / "mapping_light_tracking/manifest.json")
        nav_meta = load(base / "mapping_navigation/manifest.json")
        nav = read(base / "mapping_navigation/child_navigation.csv")
        nt = array(nav, ["utc_s"])[:, 0]
        good = np.array([r["position_available"] == "True" and float(r["anchor_age_s"]) <= 1
                         for r in nav])
        frame_times = {r["image"]: float(r["sync_utc_s"])
                       for r in read(base / "visual_mapping/frame_times.csv")}
        for model in vm["models"]:
            poses = read(base / f'visual_mapping/model_{model["id"]}_poses.csv')
            t = np.array([frame_times[r["image"]] for r in poses])
            support = valid_bracket_support(nt, good, t, cfg["map_navigation_max_bracket_s"])
            distance = abs(t-nt[good][nearest_indices(nt[good], t)]) if good.any() else np.full(len(t), np.nan)
            fragment_rows.append({"experiment_id": eid, "visual_model": model["id"],
                                  "registered_images": len(t), "points": model["points"],
                                  "reprojection_px": model["mean_reprojection_error_px"],
                                  "recent_navigation_brackets": int(support.sum()),
                                  "first_utc_s": float(t.min()), "last_utc_s": float(t.max()),
                                  "minimum_gap_to_recent_navigation_s": float(distance.min()) if good.any() else None})
        registry = load(ROOT / f"experiments/{eid}/experiment.json")
        trial_rows.append({"experiment_id": eid, "start_utc_s": registry["utc_interval_s"][0],
                           "end_utc_s": registry["utc_interval_s"][1],
                           "initial_sampled_frames": vm["sampled_frames"],
                           "initial_registered_frames": vm["registered_frames"],
                           "initial_models": len(vm["models"]),
                           "accepted_fragments": selected["accepted_fragments"],
                           "candidate_fragments": selected["total_fragments"],
                           "map_points": selected["points"], "photo_patches": selected["photo_patches"],
                           "navigation_valid_percent": 100*nav_meta["available"]/nav_meta["rows"],
                           "light_accepted": tracking["accepted"], "light_frames": tracking["rows"],
                           "light_valid_percent": 100*tracking["accepted"]/tracking["rows"]})
    write_csv(out / "mapping_trials.csv", trial_rows)
    write_csv(out / "mapping_anchor_audit.csv", fragment_rows)
    return trial_rows, fragment_rows


def inventory(cfg, out):
    ids = [*cfg["acquisitions"], *cfg["nanyo_trials"], *cfg["ngt_trials"],
           "20261102_nanyo_combined_000"]
    inventory_rows, reports, broken = [], [], []
    current = {"coordinate_reassessment", "stationary_spectra", "stationary_gnss_comparison",
               "mapping_replay", "mapping_gnss_selection", "multipath", "child_only_comprehensive_report"}
    legacy = {"position_evaluation", "research_report", "navigation_replay", "absolute_position",
              "position_visualization", "presentation", "presentation_v2", "static_evaluation"}
    for eid in ids:
        reg = load(ROOT / f"experiments/{eid}/experiment.json")
        for stage in ("results", "data/processed"):
            folder = ROOT / stage / eid
            files = list(folder.rglob("*")) if folder.exists() else []
            files = [p for p in files if p.is_file() and "mapping_refinement" not in p.parts
                     and "research_review" not in p.parts and "acoustic_audit_20261103" not in p.parts]
            inventory_rows.append({"experiment_id": eid, "stage": stage, "files": len(files),
                                   "bytes": sum(p.stat().st_size for p in files),
                                   "registered_raw_sources": ";".join(reg.get("raw_sources", [])),
                                   "registered_raw_sources_present": all((ROOT / p).exists()
                                                                          for p in reg.get("raw_sources", []))})
        for p in sorted((ROOT / "results" / eid).rglob("report.md")):
            if "research_review" in p.parts or "mapping_refinement" in p.parts:
                continue
            parts = set(p.parts)
            status = "current_topic" if current & parts else "supporting"
            if legacy & parts and "rotation_corrected_20261031" not in parts:
                status = "historical_or_superseded"
            content = p.read_text(encoding="utf-8-sig")
            links = re.findall(r"!?\[[^\]]*\]\(([^\n)]+)\)", content)
            missing = 0
            for link in links:
                if re.match(r"(?:https?://|mailto:|#|data:)", link):
                    continue
                target = unquote(link.split("#", 1)[0].strip("<>"))
                if not target:
                    continue
                if not (p.parent / target.replace("\\", "/")).exists():
                    missing += 1
                    broken.append({"report": str(p.relative_to(ROOT)).replace("\\", "/"),
                                   "target": target, "status": "missing_local_reference"})
            reports.append({"experiment_id": eid, "role": status,
                            "report": str(p.relative_to(ROOT)).replace("\\", "/"),
                            "title": next((s.lstrip("# ") for s in content.splitlines() if s.startswith("#")), ""),
                            "missing_local_links": missing, "sha256": sha256(p)})
    write_csv(out / "data_inventory.csv", inventory_rows)
    write_csv(out / "existing_report_catalog.csv", reports)
    write_csv(out / "existing_missing_links.csv", broken)
    dump(out / "inventory_summary.json", {"experiments": len(ids), "reports": len(reports),
                                           "missing_link_occurrences": len(broken),
                                           "missing_unique_targets": len({r["target"] for r in broken}),
                                           "scope": "Presence/metadata only; not a hash audit of all media"})


def plots(empirical, sim, mapping, out):
    plt.rcParams.update({"font.family": ["Yu Gothic", "Meiryo", "DejaVu Sans"],
                         "font.size": 10, "axes.spines.top": False, "axes.spines.right": False})
    labels = [r["window"].replace("day", "D").replace("_", " ") for r in empirical]
    x = np.arange(len(labels))
    fig, ax = plt.subplots(figsize=(10, 4.8))
    ax.bar(x-.18, [r["relative_rms_m"] for r in empirical], width=.36, label="実測 平均周り3D RMS")
    base = {r["window"]: r for r in sim if r["case"] == "base_noise"}
    ax.bar(x+.18, [base[r["window"]]["mc_centered_rms_m"] for r in empirical],
           width=.36, label="基本雑音モデル 条件付きRMS")
    ax.set(xticks=x, xticklabels=labels, ylabel="平均周り3D RMS [m]",
           xlabel="静止窓（D2 102000は手動揺動）")
    ax.legend()
    ax.grid(axis="y", alpha=.25)
    fig.tight_layout()
    save_editable_figure(fig, out / "observed_simulation_scatter.png")
    plt.close(fig)
    fig, ax = plt.subplots(figsize=(10, 4.8))
    ax.bar(x-.18, [r["radial_rms_m"] for r in empirical], width=.36, label="平均視線の方向")
    ax.bar(x+.18, [r["tangential_rms_m"] for r in empirical], width=.36, label="平均視線に直交する方向")
    ax.set(xticks=x, xticklabels=labels, ylabel="位置変動のRMS [m]",
           xlabel="位置変動の分解（個別センサーの誤差分離ではない）")
    ax.legend()
    ax.grid(axis="y", alpha=.25)
    fig.tight_layout()
    save_editable_figure(fig, out / "directional_scatter.png")
    plt.close(fig)
    fig, ax = plt.subplots(figsize=(10, 4.8))
    ax.bar(np.arange(len(mapping))-.18, [r["light_valid_percent"] for r in mapping],
           width=.36, label="10 Hz光源追跡の採用率")
    ax.bar(np.arange(len(mapping))+.18, [r["navigation_valid_percent"] for r in mapping],
           width=.36, label="20 Hz航法出力の有効率")
    ax.set(xticks=np.arange(len(mapping)),
           xticklabels=[r["experiment_id"].replace("20261031_nanyo_", "D1 ").replace("20261101_nanyo_", "D2 ") for r in mapping],
           ylim=(0, 100), ylabel="割合 [%]", xlabel="分母が異なるため成功率の差とは解釈しない")
    ax.legend()
    ax.grid(axis="y", alpha=.25)
    fig.tight_layout()
    save_editable_figure(fig, out / "navigation_availability.png")
    plt.close(fig)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--experiment", required=True, choices=["20261103_field_review_000"])
    p.add_argument("--sim-root", type=Path, default=ROOT.parent / "aquabeacon-sim")
    p.add_argument("--skip-plots", action="store_true",
                   help="Refresh numerical tables/provenance while preserving existing figures")
    a = p.parse_args()
    exp = load_experiment(ROOT, a.experiment)
    cfg = load(ROOT / exp.metadata["config_paths"][0])
    out = exp.stage_path("results") / "research_review"
    out.mkdir(parents=True, exist_ok=True)
    empirical, vectors = stationary(cfg, out)
    sim = simulation(cfg, a.sim_root.resolve(), empirical, vectors, out)
    mapping, _ = mapping_audit(cfg, out)
    inventory(cfg, out)
    if not a.skip_plots:
        plots(empirical, sim, mapping, out)
    record(Path(__file__))
    record(ROOT / "src/aquabeacon_analysis/field_review.py")
    record(ROOT / f"experiments/{a.experiment}/experiment.json")
    dump(out / "manifest.json", {
        "experiment_id": a.experiment, "created_utc": datetime.now(UTC).isoformat(),
        "figures_regenerated": not a.skip_plots,
        "config": cfg, "inputs": INPUTS,
        "git_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "sim_git_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=a.sim_root, text=True).strip(),
        "interpretation": "Descriptive scatter and conditional model comparison, not absolute accuracy",
        "outputs": {p.name: sha256(p) for p in out.iterdir() if p.is_file() and
                    (p.stem in {"stationary_summary", "stationary_details", "pool_position_comparison",
                                "simulation_comparison", "simulation_checks", "simulation_config_snapshot",
                                "mapping_trials", "mapping_anchor_audit", "data_inventory",
                                "existing_report_catalog", "existing_missing_links", "inventory_summary"}
                     or p.name.startswith(("observed_simulation_scatter.", "directional_scatter.",
                                           "navigation_availability.")))},
    })
    print(json.dumps({"output": str(out), "stationary_windows": len(empirical),
                      "simulation_cases": len(sim), "mapping_trials": len(mapping),
                      "hashed_inputs": len(INPUTS)}, indent=2))


if __name__ == "__main__":
    main()
