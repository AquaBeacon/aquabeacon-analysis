"""Independently check GNSS timing, full ray/sphere geometry, metrics and report assets."""

import argparse
import csv
import hashlib
import json
import pickle
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

import numpy as np
from scipy.spatial.transform import Rotation

from aquabeacon_analysis.camera_calibration import load_camera_intrinsics
from aquabeacon_analysis.gps_parent import geodetic_to_enu
from aquabeacon_analysis.nanyo_position import ray_sphere_position
from aquabeacon_analysis.ngt_binary import sha256
from aquabeacon_analysis.ngt_position import calibrated_rays

ACQ = "20261101_nanyo_000"
AXES = ["east", "north", "up"]


def read(path):
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def js(path):
    return json.loads(path.read_text(encoding="utf-8"))


def array(rows, keys):
    return np.array([[float(r[k]) for k in keys] for r in rows])


class References(HTMLParser):
    def __init__(self):
        super().__init__()
        self.refs = []

    def handle_starttag(self, tag, attrs):
        for key, value in attrs:
            if key in ("href", "src") and value:
                self.refs.append(value)


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--experiment", required=True, choices=[ACQ])
    args = parser.parse_args()
    out = Path(f"results/{args.experiment}/stationary_gnss_comparison")
    summary = js(out / "manifest.json")
    checks, directories, max_errors = [], [out], []
    upstream_metadata_changes = {}

    def check(value, message):
        if not value:
            raise AssertionError(message)
        checks.append(message)

    def close(actual, expected, message, atol=1e-9):
        np.testing.assert_allclose(actual, expected, atol=atol, rtol=0, err_msg=message)
        checks.append(message)

    def hashes(manifest):
        for p, value in manifest.get("inputs_sha256", manifest.get("input_sha256", {})).items():
            check(sha256(Path(p)) == value, "input hash " + p)

    def metrics(points, expected, label):
        residual = points - np.mean(points, axis=0)
        var = np.mean(residual * residual, axis=0)
        check(len(points) == expected["n"], label + " sample count")
        close(points.mean(0), expected["mean_m"], label + " mean")
        close(np.sqrt(var), expected["axis_rmse_m"], label + " axis RMSE")
        close(np.sqrt(var[:2].sum()), expected["horizontal_rmse_m"], label + " horizontal RMSE")
        close(np.sqrt(var.sum()), expected["rmse_3d_m"], label + " 3D RMSE")

    hashes(summary)
    k, distortion, _ = load_camera_intrinsics(Path.cwd(), "P_MAIN_CAM", (1280, 720), "proxy")
    total_shots = 0
    for m in summary["windows"]:
        w, cfg = m["window"], m["geometry_configuration"]
        folder = Path(f"results/{w['experiment_id']}/stationary_gnss_comparison/{w['id']}")
        directories.append(folder)
        hashes(m)
        check(js(folder / "manifest.json") == m, w["id"] + " summary matches window manifest")
        upstream = folder.parents[1] / "stationary_spectra" / w["id"]
        original = read(upstream / "matched_positions.csv")
        rows = read(folder / "position_samples.csv")
        times = array(original, ["utc_s"])[:, 0]
        lo = datetime.fromisoformat(w["start"]).timestamp()
        check(((times >= lo) & (times < lo + 60)).all(), w["id"] + " half-open 60s window")
        check(len({r["shot_seq"] for r in original}) == len(original), w["id"] + " unique shots")
        total_shots += m["common_shots"]
        source_manifest = js(upstream / "manifest.json")
        for name, historic_hash in source_manifest["inputs_sha256"].items():
            source_path = Path(name)
            current_hash = sha256(source_path)
            if current_hash == historic_hash:
                check(True, "upstream unchanged " + name)
                continue
            check(
                source_path.parts[0] == "experiments" and source_path.name == "experiment.json",
                "only experiment metadata may differ from historical static provenance",
            )
            historic_metadata = js(source_path)
            additions = [
                p
                for p in historic_metadata["config_paths"]
                if p.endswith(("/mapping_light_review.json", "/mapping_navigation.json"))
            ]
            historic_metadata["config_paths"] = [
                p for p in historic_metadata["config_paths"] if p not in additions
            ]
            historic_bytes = (
                json.dumps(historic_metadata, indent=2, ensure_ascii=False).replace("\n", "\r\n")
                + "\r\n"
            ).encode("utf-8")
            check(
                hashlib.sha256(historic_bytes).hexdigest() == historic_hash,
                "exact historical hash recovered by removing mapping-only config declarations "
                + name,
            )
            upstream_metadata_changes[name] = {
                "historical_sha256": historic_hash,
                "current_sha256": current_hash,
                "added_config_declarations": additions,
                "proof": "Removing only the two mapping config declarations reproduces the exact historical bytes/hash; no static input data or numerical setting changed. Historical manifests are retained unchanged.",
            }
        check(
            source_manifest["metrics"]["pressure_depth"] == m["pressure_depth"],
            w["id"] + " depth unchanged",
        )
        paths = [Path(f"results/{w['experiment_id']}/light_tracking/{w['id']}/attitudes_10hz.csv")]
        attitudes = [r for r in read(paths[0]) if lo <= float(r["utc_s"]) < lo + 60]
        means = {
            role: Rotation.from_quat(
                array(
                    [r for r in attitudes if r[f"{role}_attitude_available"] == "True"],
                    [f"{role}_true_enu_q{k}" for k in "xyzw"],
                )
            )
            .mean()
            .as_matrix()
            for role in ["parent", "child"]
        }
        rp = Rotation.from_quat(
            array(original, [f"parent_true_enu_q{k}" for k in "xyzw"])
        ).as_matrix()
        rc = Rotation.from_quat(
            array(original, [f"child_true_enu_q{k}" for k in "xyzw"])
        ).as_matrix()
        rays = calibrated_rays(array(original, ["x_px", "y_px"]), k, distortion)
        axes = np.array(js(Path(cfg["parent_axes"]))["R_body_from_spresense"])
        levers = {k: np.array(v) for k, v in cfg["lever_arms_frd_m"].items() if k != "child_imu"}
        for solution, source_path in m["gnss_files"].items():
            gr = read(Path(source_path))
            gt = array(gr, ["utc_s"])[:, 0]
            gp = array(gr, [f"{a}_m" for a in AXES])
            lookup = {float(r["utc_s"]): i for i, r in enumerate(gr)}
            geometry_errors = []
            for condition in ["corrected", "fixed_parent", "fixed_both"]:
                key = f"{solution}__{condition}"
                selected = [
                    r for r in rows if r["solution"] == solution and r["attitude"] == condition
                ]
                check(len(selected) == len(original), key + " preserves all candidate shots")
                close(array(selected, ["utc_s"])[:, 0], times, key + " identical times")
                check(
                    [r["shot_seq"] for r in selected] == [r["shot_seq"] for r in original],
                    key + " identical shots",
                )
                valid = np.array([r["common_available"] == "True" for r in selected])
                near_valid = np.array([r["nearest_common_available"] == "True" for r in selected])
                positions = array(selected, [f"child_absolute_{a}_m" for a in AXES])
                for i, row in enumerate(selected):
                    li, ri = lookup[float(row["left_utc_s"])], lookup[float(row["right_utc_s"])]
                    available = row["gnss_available"] == "True"
                    if not available:
                        check(np.isnan(positions[i]).all(), key + " missing GNSS remains missing")
                        continue
                    check(
                        gt[li] <= times[i] <= gt[ri] and gt[ri] - gt[li] <= 1.5,
                        key + " real interpolation bracket",
                    )
                    check(
                        int(gr[li]["quality"]) in [1, 2, 4] and int(gr[ri]["quality"]) in [1, 2, 4],
                        key + " native quality",
                    )
                    fraction = 0 if li == ri else (times[i] - gt[li]) / (gt[ri] - gt[li])
                    gnss = gp[li] if li == ri else gp[li] + fraction * (gp[ri] - gp[li])
                    close(
                        [float(row[f"gnss_{a}_m"]) for a in AXES],
                        gnss,
                        key + " native interpolation",
                    )
                    p_rot = rp[i] if condition == "corrected" else means["parent"]
                    c_rot = means["child"] if condition == "fixed_both" else rc[i]
                    mount = cfg["mounts"]["parent_" + Path(original[i]["source_video"]).stem]
                    body_opt = (
                        axes
                        @ np.array(mount["R_dji_from_sensor"]).T
                        @ np.array(cfg["R_dji_from_parent_optical"])
                    )
                    expected, _, _, residual = ray_sphere_position(
                        gnss,
                        p_rot,
                        c_rot,
                        rays[i],
                        body_opt,
                        float(original[i]["range_m"]),
                        **levers,
                    )
                    close(positions[i], expected, key + " direct ray/sphere geometry", atol=1e-8)
                    geometry_errors.append(float(abs(positions[i] - expected).max()))
                    close(residual, 0, key + " acoustic equation", atol=1e-8)
                    relative = positions[i] - (gnss - p_rot @ levers["parent_antenna"])
                    close(
                        relative,
                        [float(original[i][f"{condition}_{a}_m"]) for a in AXES],
                        key + " GNSS cancels from relative position",
                    )
                    ni = lookup[float(row["nearest_utc_s"])]
                    if near_valid[i]:
                        check(abs(gt[ni] - times[i]) <= 0.51, key + " nearest GNSS time")
                        near = [float(row[f"child_nearest_{a}_m"]) for a in AXES]
                        close(near, positions[i] - gnss + gp[ni], key + " nearest native GNSS")
                metrics(positions[valid], m["metrics"][key], key)
                metrics(
                    positions[near_valid],
                    m["nearest_sensitivity"]["linear_same_shots"][key],
                    key + " sensitivity subset",
                )
                metrics(
                    array(selected, [f"child_nearest_{a}_m" for a in AXES])[near_valid],
                    m["nearest_sensitivity"]["metrics"][key],
                    key + " nearest sensitivity",
                )
                dg = array(selected, [f"gnss_{a}_m" for a in AXES])[valid]
                ds = array(selected, [f"sensor_offset_{a}_m" for a in AXES])[valid]
                dg, ds = dg - dg.mean(0), ds - ds.mean(0)
                b = m["variance_budgets"][key]
                close(
                    2 * np.mean(np.sum(dg * ds, axis=1)),
                    b["twice_covariance_3d_m2"],
                    key + " signed covariance",
                )
                close(
                    b["gnss_variance_3d_m2"]
                    + b["sensor_offset_variance_3d_m2"]
                    + b["twice_covariance_3d_m2"],
                    m["metrics"][key]["rmse_3d_m"] ** 2,
                    key + " variance sum identity",
                )
                if solution == "ppk_ar" and condition == "corrected":
                    close(
                        positions[valid],
                        array(original, [f"child_origin_{a}_m" for a in AXES])[valid],
                        key + " original absolute baseline",
                        atol=1e-8,
                    )
            used = sorted(
                {
                    lookup[float(r[k])]
                    for r in rows
                    if r["solution"] == solution and r["common_available"] == "True"
                    for k in ["left_utc_s", "right_utc_s"]
                }
            )
            llh = array(
                [gr[i] for i in used], ["latitude_deg", "longitude_deg", "height_ellipsoid_m"]
            )
            close(
                geodetic_to_enu(*llh.T, *m["origin_llh"]),
                gp[used],
                solution + " common source ENU",
                atol=1e-6,
            )
            max_errors.append(max(geometry_errors))
        for stem in ["absolute_overhead", "absolute_timeseries"]:
            for suffix in [".png", ".svg", ".figure.pickle", ".plot.json"]:
                check((folder / (stem + suffix)).is_file(), str(folder / (stem + suffix)))
            master = folder / (stem + ".figure.pickle")
            check(
                js(folder / (stem + ".plot.json"))["master_sha256"] == sha256(master),
                str(master) + " master hash",
            )
        with (folder / "absolute_overhead.figure.pickle").open("rb") as f:
            figure = pickle.load(f)
        check(len(figure.axes) == 6, w["id"] + " six overhead panels")
        for ax, (solution, condition) in zip(
            figure.axes,
            [(s, c) for s in m["gnss_files"] for c in ["corrected", "fixed_parent", "fixed_both"]],
            strict=True,
        ):
            selected = [
                r
                for r in rows
                if r["solution"] == solution
                and r["attitude"] == condition
                and r["common_available"] == "True"
            ]
            points = array(selected, ["child_absolute_east_m", "child_absolute_north_m"])
            points -= points.mean(0)
            close(
                np.column_stack([ax.lines[2].get_xdata(), ax.lines[2].get_ydata()]),
                points,
                w["id"] + " plotted positions " + solution + condition,
            )
            close(ax.get_xlim(), figure.axes[0].get_xlim(), w["id"] + " common x scale")
            close(ax.get_ylim(), ax.get_xlim(), w["id"] + " equal horizontal axes")
            check(float(ax.get_aspect()) == 1, w["id"] + " equal plot aspect")
        import matplotlib.pyplot as plt

        plt.close(figure)
    for day, scores in summary["aggregates"].items():
        selected = [
            m
            for m in summary["windows"]
            if m["acquisition"] == day and "hand" not in m["window"]["condition"]
        ]
        for key, value in scores.items():
            close(
                value,
                np.sqrt(np.mean([m["metrics"][key]["rmse_3d_m"] ** 2 for m in selected])),
                day + " equal-window aggregation " + key,
            )
    directories.append(Path("results/20261031_nanyo_000/stationary_gnss_comparison"))
    hashes(js(directories[-1] / "manifest.json"))
    # The verification file is written last; check this one self-reference separately.
    report_refs = 0
    for directory in directories:
        p = directory / "report.html"
        refs = References()
        refs.feed(p.read_text(encoding="utf-8"))
        for ref in refs.refs:
            u = urlsplit(ref)
            if u.scheme or u.netloc or not u.path:
                continue
            target = (p.parent / unquote(u.path)).resolve()
            check(
                target == (out / "verification.json").resolve() or target.exists(),
                "report reference " + str(target),
            )
            report_refs += 1
    old = js(out / "verification.json") if (out / "verification.json").exists() else {}
    result = {
        "numeric_checks_passed": True,
        "check_count": len(checks),
        "windows": len(summary["windows"]),
        "common_unique_shots": total_shots,
        "report_references": report_refs,
        "maximum_direct_geometry_difference_m": max(max_errors),
        "upstream_metadata_changes": upstream_metadata_changes,
        "checks": sorted(set(checks)),
        "visual_checks": old.get("visual_checks", []),
        "code_quality_checks": old.get("code_quality_checks", {}),
        "verified_at_utc": datetime.now(UTC).isoformat(),
        "verifier_sha256": sha256(Path(__file__)),
    }
    (out / "verification.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"PASS: {len(summary['windows'])} windows, {total_shots} shots, {len(checks)} checks",
        flush=True,
    )


if __name__ == "__main__":
    main()
