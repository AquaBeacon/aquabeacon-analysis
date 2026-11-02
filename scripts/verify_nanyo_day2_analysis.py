"""Verify day2 artifacts and, optionally, run repository/workspace checks."""

import argparse
import csv
import json
import pickle
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote

import cv2
import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from aquabeacon_analysis.ngt_binary import sha256

ACQ = "20261101_nanyo_000"


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--experiment", required=True, choices=[ACQ])
    parser.add_argument("--tests", action="store_true")
    args = parser.parse_args()
    out = Path(f"results/{ACQ}/analysis")
    destination = out / "verification.json"
    previous = read_json(destination) if destination.exists() else {}
    config = read_json(Path(f"config/{ACQ}/stationary_spectra.json"))
    issues, checks, hashes, windows, reports = [], [], {}, [], []

    def check(name, condition):
        checks.append({"check": name, "passed": bool(condition)})
        if not condition:
            issues.append(name)

    def verify_hashes(mapping):
        for path, expected in mapping.items():
            p = Path(path)
            if str(p) not in hashes:
                hashes[str(p)] = sha256(p) if p.exists() else None
            check(f"hash: {p}", hashes[str(p)] == expected)

    for w in config["windows"]:
        wid, eid = w["id"], w["experiment_id"]
        track = Path(f"results/{eid}/light_tracking/{wid}")
        static = Path(f"results/{eid}/stationary_spectra/{wid}")
        tm, sm = read_json(track / "manifest.json"), read_json(static / "manifest.json")
        review = read_json(track / "matched_shot_review.json")
        verify_hashes(review["inputs_sha256"])
        verify_hashes(tm["inputs_sha256"])
        verify_hashes(tm["upstream_configuration_inputs_sha256"])
        verify_hashes(sm["inputs_sha256"])
        observations = read_csv(track / "camera_relative_candidates.csv")
        positions = read_csv(track / "positions.csv")
        matched = read_csv(static / "matched_positions.csv")
        check(f"{wid}: 600 images", len(observations) == tm["rows"] == 600)
        offsets = np.array([float(r["segment_time_s"]) for r in observations])
        check(
            f"{wid}: timestamps within 60 seconds",
            offsets[0] >= 0 and offsets[-1] < 60 and np.all(np.diff(offsets) > 0),
        )
        check(
            f"{wid}: unique acoustic events",
            len({r["shot_seq"] for r in positions}) == len(positions),
        )
        good = [r for r in positions if r["position_available"] == "True"]
        check(
            f"{wid}: reported accepted count",
            len(good)
            == len(matched)
            == tm["available_positions"]
            == sm["metrics"]["matched_shots"],
        )
        check(
            f"{wid}: all accepted shots re-extracted",
            review["reviewed_image_count"] == len(good)
            and review["max_source_time_difference_s"] < 0.04,
        )
        check(
            f"{wid}: 75ms optical association",
            all(abs(float(r["range_delta_s"])) <= 0.075 for r in good),
        )
        check(
            f"{wid}: unavailable images have no predicted coordinates",
            all(
                r["optical_candidate"] == "True"
                or not np.isfinite([float(r["x_px"]), float(r["y_px"])]).any()
                for r in observations
            ),
        )
        check(
            f"{wid}: accepted images are finite",
            all(
                r["optical_candidate"] != "True"
                or np.isfinite([float(r["x_px"]), float(r["y_px"])]).all()
                for r in observations
            ),
        )
        check(
            f"{wid}: image acceptance count",
            sum(r["optical_candidate"] == "True" for r in observations) == tm["accepted"],
        )
        metric = sm["metrics"]
        depth = metric["pressure_depth"]
        lo = datetime.fromisoformat(w["start"]).timestamp()
        expected_depth = [
            r
            for r in read_csv(Path(depth["source"]))
            if lo <= float(r["sync_utc_s"]) < lo + config["duration_s"]
        ]
        depth_rows = read_csv(static / "pressure_depth_samples.csv")
        check(
            f"{wid}: pressure samples cover full requested window",
            len(depth_rows) == len(expected_depth),
        )
        check(
            f"{wid}: pressure timestamps unchanged",
            [r["sync_utc_s"] for r in depth_rows] == [r["sync_utc_s"] for r in expected_depth],
        )
        depth_values = np.array([float(r["pressure_depth_m"]) for r in depth_rows])
        expected_values = np.array([float(r[depth["source_field"]]) for r in expected_depth])
        pressure = np.array([float(r["pressure_mbar"]) for r in expected_depth])
        check(
            f"{wid}: depth agrees with source and pressure conversion",
            np.allclose(depth_values, expected_values, equal_nan=True)
            and np.allclose(
                depth_values,
                (pressure - depth["pressure_baseline_mbar"])
                * 100
                / (depth["density_kg_m3_assumed"] * depth["gravity_m_s2"]),
                equal_nan=True,
            ),
        )
        finite_depth = depth_values[np.isfinite(depth_values)]
        check(
            f"{wid}: depth summary statistics",
            depth["valid_samples"] == len(finite_depth)
            and np.allclose(
                [depth["mean_m"], depth["minimum_m"], depth["maximum_m"], depth["std_m"]],
                [finite_depth.mean(), finite_depth.min(), finite_depth.max(), finite_depth.std()],
            ),
        )
        # Only open local figure masters generated by this analysis, never downloaded pickles.
        figure_path = static / "position_overhead.figure.pickle"
        with figure_path.open("rb") as stream:
            figure = pickle.load(stream)
        metadata = read_json(static / "position_overhead.plot.json")
        check(f"{wid}: overhead master hash", metadata["master_sha256"] == sha256(figure_path))
        check(f"{wid}: three overhead conditions", len(figure.axes) == 3)
        for axis, name in zip(
            figure.axes, ["corrected", "fixed_parent", "fixed_both"], strict=True
        ):
            xy = np.array([[float(r[f"{name}_{a}_m"]) for a in ["east", "north"]] for r in matched])
            centered = xy - xy.mean(axis=0)
            point_line = next(
                line for line in axis.lines if line.get_label().startswith("有効観測")
            )
            circle_line = next(
                line for line in axis.lines if line.get_label().startswith("水平RMSE")
            )
            radius = np.sqrt(np.mean(np.sum(centered**2, axis=1)))
            check(
                f"{wid}: overhead points {name}",
                np.allclose(point_line.get_xydata(), centered, atol=1e-10),
            )
            check(
                f"{wid}: overhead RMS circle {name}",
                np.allclose(np.linalg.norm(circle_line.get_xydata(), axis=1), radius)
                and np.isclose(radius, metric[name]["horizontal_rmse_m"]),
            )
            check(
                f"{wid}: overhead equal and common scale {name}",
                axis.get_aspect() == 1
                and np.allclose(axis.get_xlim(), axis.get_ylim())
                and np.allclose(axis.get_xlim(), figure.axes[0].get_xlim()),
            )
        plt.close(figure)
        baseline_path = out / "depth_update_baseline.json"
        if baseline_path.exists():
            before = read_json(baseline_path)[wid]
            check(
                f"{wid}: existing RMSE metrics unchanged",
                all(metric[k] == v for k, v in before.items()),
            )
        check(
            f"{wid}: independent geometry recomputation",
            metric["position_source_geometry_max_difference_m"] < 1e-8,
        )
        identity = metric["variance_identity"]
        residual = identity["corrected_variance_m2"] - sum(
            identity[k]
            for k in ["fixed_variance_m2", "correction_variance_m2", "twice_covariance_m2"]
        )
        check(f"{wid}: covariance identity", abs(residual) < 1e-10)
        for name, dec in metric["orthogonal_decomposition"].items():
            check(f"{wid}: orthogonal closure {name}", abs(dec["closure_error_m2"]) < 1e-10)
        for name, spec in sm["spectra"].items():
            if "fft_run_parseval_variance" in spec:
                check(
                    f"{wid}: Parseval {name}",
                    np.isclose(
                        spec["fft_run_parseval_variance"],
                        spec["fft_run_detrended_variance"],
                        rtol=1e-10,
                        atol=1e-12,
                    ),
                )
        for c in sm["coherence"].values():
            if c["status"] == "ok":
                check(f"{wid}: Welch averaging", c["welch_windows"] >= 3)
        cross_rows = read_csv(static / "cross_spectra.csv")
        expected_cross_rows = 9 * sum(c["status"] == "ok" for c in sm["coherence"].values())
        check(f"{wid}: no stale coherence rows", len(cross_rows) == expected_cross_rows)
        cap = cv2.VideoCapture(str(track / "tracking_review.mp4"))
        check(f"{wid}: review video frame count", int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) == 600)
        cap.release()
        for name in ["timeseries", "spectra", "native_spectra", "coherence", "position_overhead"]:
            for suffix in [".png", ".svg", ".figure.pickle", ".plot.json"]:
                check(f"{wid}: figure {name}{suffix}", (static / (name + suffix)).is_file())
        series = read_csv(static / "series.csv")
        for name in ["corrected_east", "fixed_parent_east"]:
            rows = [r for r in series if r["series"] == name]
            check(
                f"{wid}: invalid shot rows retained in {name}",
                len(rows) == len(positions)
                and sum(np.isfinite(float(r["value"])) for r in rows) == len(good),
            )
        if wid == "day2_101500":
            check("10:15 acoustic startup gap retained", float(positions[0]["segment_time_s"]) > 14)
            check("10:15 pre-ranging pressure retained", float(depth_rows[0]["offset_s"]) < 1)
        windows.append(
            {
                "id": wid,
                "images": len(observations),
                "accepted_images": tm["accepted"],
                "matched_shots": len(good),
                "coherence_pairs_available": sum(
                    c["status"] == "ok" for c in sm["coherence"].values()
                ),
            }
        )
        reports.extend([static / "report.md", static / "report.html"])

    for i in range(1, 6):
        eid = f"20261101_nanyo_{i:03d}"
        folder = Path(f"results/{eid}/visual_mapping")
        m = read_json(folder / "manifest.json")
        check(
            f"{eid}: registered model count",
            m["registered_frames"] == sum(v["registered_images"] for v in m["models"]),
        )
        check(f"{eid}: mapping acquisition", m["acquisition"] == ACQ)
        check(f"{eid}: model image review", (folder / "model_views.jpg").exists())
        for model in m["models"]:
            check(
                f"{eid}: point cloud {model['id']}", (folder / f"model_{model['id']}.ply").exists()
            )
        reports.append(Path(f"results/{eid}/report.md"))
    reports.extend([out / "report.md", out / "report.html"])
    for path in reports:
        check(f"report exists: {path}", path.exists())
        content = path.read_text(encoding="utf-8")
        links = (
            re.findall(r"(?:href|src)=\"([^\"]+)\"", content)
            if path.suffix == ".html"
            else re.findall(r"\]\(([^)]+)\)", content)
        )
        for link in links:
            if link.startswith(("http:", "https:", "#")):
                continue
            target = (path.parent / unquote(link.split("#")[0])).resolve()
            check(f"link: {path} -> {link}", target == destination.resolve() or target.exists())
    commands = previous.get("commands", [])
    if args.tests:
        commands = []
        for name, command, cwd in [
            ("pytest", [sys.executable, "-m", "pytest", "-q"], Path.cwd()),
            ("ruff", [sys.executable, "-m", "ruff", "check", "."], Path.cwd()),
            (
                "workspace",
                [sys.executable, "workspace-tools/workspace.py", "check"],
                Path.cwd().parent,
            ),
        ]:
            run = subprocess.run(
                command,
                check=False,
                cwd=cwd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            log = out / f"{name}.log"
            log.write_text(run.stdout + run.stderr, encoding="utf-8")
            commands.append(
                {
                    "name": name,
                    "command": command,
                    "exit_code": run.returncode,
                    "log": str(log),
                    "tail": (run.stdout + run.stderr).splitlines()[-4:],
                }
            )
    result = {
        "experiment_id": ACQ,
        "created_utc": datetime.now(UTC).isoformat(),
        "git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "verifier_sha256": sha256(Path(__file__)),
        "artifact_checks_passed": not issues,
        "artifact_check_count": len(checks),
        "issues": issues,
        "windows": windows,
        "commands": commands,
        "all_command_checks_passed": bool(commands) and all(c["exit_code"] == 0 for c in commands),
        "known_preexisting_test_failure": "tests/test_editable_plots.py::test_analysis_plot_outputs_use_editable_saver: existing anchor_nanyo_maps.py direct 3D savefig; recorded in pre-analysis handoff",
        "checks": checks,
        "current_input_sha256": hashes,
        "visual_review": previous.get(
            "visual_review",
            "Record reviewed image sheets and plots separately; automated checks do not certify target identity or ground truth.",
        ),
    }
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                k: result[k]
                for k in [
                    "artifact_checks_passed",
                    "artifact_check_count",
                    "issues",
                    "windows",
                    "all_command_checks_passed",
                ]
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if issues:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
