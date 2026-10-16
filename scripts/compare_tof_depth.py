"""Compare C_MAIN and P_MAIN acoustic ranges with C_MAIN pressure depth."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import subprocess
from pathlib import Path
from statistics import median

MEDWIN_SOURCE = (
    "https://calhoun.nps.edu/server/api/core/bitstreams/"
    "35d133d5-6956-4912-8597-5f2dc8fc5ad2/content"
)


def medwin_sound_speed(temp_c: float, salinity_ppt: float, depth_m: float) -> float:
    """Medwin (1975), m/s; T=0..35 C, S=0..45 ppt, depth=0..1000 m."""
    if not (0 <= temp_c <= 35 and 0 <= salinity_ppt <= 45 and 0 <= depth_m <= 1000):
        raise ValueError("Medwin input outside stated domain")
    return (1449.2 + 4.6 * temp_c - 0.055 * temp_c**2
            + 0.00029 * temp_c**3
            + (1.34 - 0.010 * temp_c) * (salinity_ppt - 35)
            + 0.016 * depth_m)


def pressure_depth_m(pressure_mbar: float, surface_mbar: float, density: float) -> float:
    return (pressure_mbar - surface_mbar) * 100 / (density * 9.80665)


def pearson(x: list[float], y: list[float]) -> float:
    mx, my = sum(x) / len(x), sum(y) / len(y)
    xx = sum((v - mx) ** 2 for v in x)
    yy = sum((v - my) ** 2 for v in y)
    return sum((a - mx) * (b - my) for a, b in zip(x, y, strict=True)) / math.sqrt(xx * yy)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as stream:
        return list(csv.DictReader(stream))


def match_shots(child_rows: list[dict[str, str]], parent_rows: list[dict[str, str]],
                *, max_offset_us: int = 1_000_000) -> dict[str, dict[str, str]]:
    """Match unique shot IDs only when their recorded UTC times also agree."""
    parent_by_shot = {}
    for row in parent_rows:
        shot = row["shot_seq"]
        parent_by_shot.setdefault(shot, []).append(row)
    matched = {}
    used_parent_ids = set()
    seen_child_shots = set()
    for row in child_rows:
        shot = row["shot_seq"]
        if shot in seen_child_shots:
            raise ValueError(f"duplicate C_MAIN shot_seq: {shot}")
        seen_child_shots.add(shot)
        candidates = sorted(parent_by_shot.get(shot, []),
                            key=lambda parent: abs(int(parent["sync_utc_us"])
                                                   - int(row["sync_utc_us"])))
        parent = next((candidate for candidate in candidates
                       if id(candidate) not in used_parent_ids
                       and abs(int(candidate["sync_utc_us"]) - int(row["sync_utc_us"]))
                       <= max_offset_us), None)
        if parent:
            matched[shot] = parent
            used_parent_ids.add(id(parent))
    return matched


def find_parent_source(child_rows: list[dict[str, str]], processed_root: Path
                       ) -> tuple[Path | None, dict[str, dict[str, str]]]:
    best_source = None
    best_matches = {}
    for source in sorted(processed_root.glob("*_pmain_*/tof_window/p_main/*HEALTH_RANGE*.csv")):
        matches = match_shots(child_rows, read_csv(source))
        if len(matches) > len(best_matches):
            best_source, best_matches = source, matches
    return (best_source, best_matches) if len(best_matches) >= 5 else (None, {})


def analyze(source: Path, output: Path, *, salinity_ppt: float,
            surface_mbar: float, density: float, git_commit: str,
            processed_root: Path, config_path: Path) -> dict:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    rows = read_csv(source)
    if not rows:
        raise ValueError(f"empty source: {source}")
    parent_source, parent_matches = find_parent_source(rows, processed_root)
    output.mkdir(parents=True, exist_ok=True)
    paired = []
    detailed = []
    for row in rows:
        temp = float(row["temp_c"])
        depth = pressure_depth_m(float(row["pressure_mbar"]), surface_mbar, density)
        speed = medwin_sound_speed(temp, salinity_ppt, depth)
        valid = row["tof_valid"] == "1"
        tof = float(row["tof_us"]) if valid else None
        old_range = tof * 0.0015 if valid else None
        corrected_range = tof * speed * 1e-6 if valid else None
        parent = parent_matches.get(row["shot_seq"])
        parent_valid = parent is not None and parent["tof_valid"] == "1"
        parent_tof = float(parent["tof_us"]) if parent_valid else None
        parent_range = parent_tof * speed * 1e-6 if parent_valid else None
        record = {
            "window_time_s": row["window_time_s"],
            "sync_utc_us": row["sync_utc_us"],
            "shot_seq": row["shot_seq"],
            "tof_valid": row["tof_valid"],
            "tof_us": row["tof_us"],
            "temp_c": row["temp_c"],
            "pressure_mbar": row["pressure_mbar"],
            "pressure_depth_m": depth,
            "sound_speed_m_s": speed,
            "range_1500_m": old_range,
            "range_corrected_m": corrected_range,
            "parent_sync_utc_us": parent["sync_utc_us"] if parent else None,
            "parent_time_offset_ms": (
                (int(parent["sync_utc_us"]) - int(row["sync_utc_us"])) / 1000
                if parent else None
            ),
            "parent_tof_valid": parent["tof_valid"] if parent else None,
            "parent_tof_us": parent["tof_us"] if parent else None,
            "parent_range_corrected_m": parent_range,
        }
        detailed.append(record)
        if valid:
            paired.append(record)
    if len(paired) < 2:
        raise ValueError(f"too few valid ToF rows: {source}")
    with (output / "paired_observations.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(detailed[0]))
        writer.writeheader()
        writer.writerows(detailed)

    corrected = [r["range_corrected_m"] for r in paired]
    old = [r["range_1500_m"] for r in paired]
    depths = [r["pressure_depth_m"] for r in paired]
    speeds = [r["sound_speed_m_s"] for r in paired]
    both_valid = [r for r in detailed if r["range_corrected_m"] is not None
                  and r["parent_range_corrected_m"] is not None]
    summary = {
        "experiment_id": source.parents[2].name,
        "input": str(source),
        "input_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "git_commit": git_commit,
        "config_path": str(config_path),
        "config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
        "calibration_used": None,
        "method": "Medwin 1975; one-way ToF, no electronic delay correction",
        "source_url": MEDWIN_SOURCE,
        "temperature_column": "temp_c (assumed ambient water temperature; unverified)",
        "salinity_ppt_assumed": salinity_ppt,
        "surface_pressure_mbar_assumed": surface_mbar,
        "water_density_kg_m3_assumed": density,
        "rows": len(rows),
        "valid_tof_rows": len(paired),
        "sound_speed_m_s_min_median_max": [min(speeds), median(speeds), max(speeds)],
        "range_1500_m_median": median(old),
        "range_corrected_m_median": median(corrected),
        "range_correction_m_median": median([a - b for a, b in zip(corrected, old, strict=True)]),
        "pressure_depth_m_median": median(depths),
        "median_corrected_range_minus_depth_m": median(
            [a - b for a, b in zip(corrected, depths, strict=True)]
        ),
        "pearson_corrected_range_vs_depth": pearson(corrected, depths),
        "parent_source": str(parent_source) if parent_source else None,
        "parent_input_sha256": (hashlib.sha256(parent_source.read_bytes()).hexdigest()
                                if parent_source else None),
        "parent_shot_matched_rows": len(parent_matches),
        "parent_valid_matched_rows": sum(r["parent_range_corrected_m"] is not None
                                         for r in detailed),
        "both_tof_valid_rows": len(both_valid),
    }
    if both_valid:
        parent_ranges = [r["parent_range_corrected_m"] for r in both_valid]
        child_ranges = [r["range_corrected_m"] for r in both_valid]
        same_depths = [r["pressure_depth_m"] for r in both_valid]
        offsets = [r["parent_time_offset_ms"] for r in both_valid]
        summary.update({
            "parent_range_corrected_m_median_both_valid": median(parent_ranges),
            "median_parent_minus_child_range_m": median(
                [a - b for a, b in zip(parent_ranges, child_ranges, strict=True)]
            ),
            "median_parent_range_minus_depth_m": median(
                [a - b for a, b in zip(parent_ranges, same_depths, strict=True)]
            ),
            "pearson_parent_range_vs_depth": pearson(parent_ranges, same_depths),
            "pearson_parent_vs_child_range": pearson(parent_ranges, child_ranges),
            "parent_minus_child_utc_ms_min_median_max": [min(offsets), median(offsets), max(offsets)],
        })
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    fig, axes = plt.subplots(3 if both_valid else 2, 1,
                             figsize=(11, 10 if both_valid else 7), constrained_layout=True)
    times = [float(r["window_time_s"]) for r in detailed]
    valid_times = [float(r["window_time_s"]) for r in paired]
    axes[0].plot(times, [r["pressure_depth_m"] for r in detailed],
                 color="#096d91", label="Pressure-derived depth")
    axes[0].scatter(valid_times, corrected, color="#d27622", s=14,
                    label="C_MAIN ToF range, corrected")
    if both_valid:
        parent_records = [r for r in detailed if r["parent_range_corrected_m"] is not None]
        axes[0].scatter([float(r["window_time_s"]) for r in parent_records],
                        [r["parent_range_corrected_m"] for r in parent_records],
                        color="#713da2", s=12, marker="x", label="P_MAIN ToF range, corrected")
    axes[0].set(title=summary["experiment_id"], xlabel="Time in ToF window (s)", ylabel="Meters")
    axes[0].legend()
    axes[1].scatter(depths, corrected, color="#d27622", s=17, alpha=0.65)
    if both_valid:
        axes[1].scatter(same_depths, parent_ranges, color="#713da2", s=14,
                        marker="x", alpha=0.65, label="P_MAIN")
        axes[1].legend()
    axes[1].set(title=f"Valid pairs: {len(paired)} | Pearson r = {summary['pearson_corrected_range_vs_depth']:.2f}",
                xlabel="Pressure-derived depth (m)", ylabel="Corrected ToF range (m)")
    if both_valid:
        axes[2].scatter(child_ranges, parent_ranges, color="#713da2", s=17, alpha=0.7)
        low = min(child_ranges + parent_ranges)
        high = max(child_ranges + parent_ranges)
        axes[2].plot([low, high], [low, high], color="gray", linestyle="--", linewidth=1)
        axes[2].set(title=f"Matched valid shots: {len(both_valid)} | P_MAIN − C_MAIN median = {summary['median_parent_minus_child_range_m']:.3f} m",
                    xlabel="C_MAIN corrected range (m)", ylabel="P_MAIN corrected range (m)")
    for axis in axes:
        axis.grid(alpha=0.25)
    fig.savefig(output / "comparison.png", dpi=160)
    plt.close(fig)
    if both_valid:
        parent_fig, parent_axis = plt.subplots(figsize=(11, 4.5), constrained_layout=True)
        parent_axis.plot(times, [r["pressure_depth_m"] for r in detailed],
                         color="#096d91", linewidth=1.7, label="Depth")
        parent_axis.scatter([float(r["window_time_s"]) for r in parent_records],
                            [r["parent_range_corrected_m"] for r in parent_records],
                            color="#713da2", s=22, label="ToF range")
        parent_axis.set(title=f"{summary['experiment_id']} | C_MAIN depth vs P_MAIN ToF range",
                        xlabel="Time in ToF window (s)", ylabel="Meters")
        parent_axis.grid(alpha=0.25)
        parent_axis.legend()
        parent_fig.savefig(output / "pressure_vs_p_main.png", dpi=160)
        plt.close(parent_fig)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--processed-root", type=Path, default=Path("data/processed"))
    parser.add_argument("--results-root", type=Path, default=Path("results"))
    parser.add_argument("--config", type=Path,
                        default=Path("config/tof_depth_comparison_20260922.json"))
    parser.add_argument("--salinity-ppt", type=float)
    parser.add_argument("--surface-pressure-mbar", type=float)
    parser.add_argument("--density-kg-m3", type=float)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    salinity = args.salinity_ppt if args.salinity_ppt is not None else config["salinity_ppt"]
    surface = (args.surface_pressure_mbar if args.surface_pressure_mbar is not None
               else config["surface_pressure_mbar"])
    density = args.density_kg_m3 if args.density_kg_m3 is not None else config["density_kg_m3"]
    commit = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                            text=True, check=False).stdout.strip()
    for source in sorted(args.processed_root.glob("*/tof_window/c_main/*HEALTH_RANGE*.csv")):
        summary = analyze(source, args.results_root / source.parents[2].name / "tof_depth_comparison",
                          salinity_ppt=salinity, surface_mbar=surface,
                          density=density, git_commit=commit,
                          processed_root=args.processed_root, config_path=args.config)
        print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
