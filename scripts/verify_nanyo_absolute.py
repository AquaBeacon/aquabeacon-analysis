"""Verify frame recovery, geomagnetic reference and saved localization geometry."""

import argparse
import json
import subprocess
import zipfile
from pathlib import Path

import cv2
import numpy as np
from analyze_nanyo import read
from estimate_nanyo_absolute import Context
from pygeomag import GeoMag
from scipy.spatial.transform import Rotation

from aquabeacon_analysis.camera_calibration import load_camera_intrinsics
from aquabeacon_analysis.ngt_binary import sha256
from aquabeacon_analysis.ngt_position import calibrated_rays
from aquabeacon_analysis.ngt_sync import numeric_csv
from aquabeacon_analysis.time_sync import resolve_ffmpeg


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", required=True, choices=["20261031_nanyo_000"])
    args = parser.parse_args()
    eid = args.experiment
    cfg = json.loads(Path(f"config/{eid}/absolute_position.json").read_text())
    with zipfile.ZipFile(f"data/interim/{eid}/geometry/WMM2025COF.zip") as z:
        tests = [
            list(map(float, line.split()))
            for line in z.read("WMM2025COF/WMM2025_TestValues.txt").decode().splitlines()
            if line and not line.startswith("#")
        ]
    geo = GeoMag(base_year=2025)
    errors = []
    for row in tests:
        value = geo.calculate(glat=row[2], glon=row[3], alt=row[1], time=row[0])
        errors.append(abs(value.d - row[4]))
        assert errors[-1] < 0.0051
    manifest = {
        "WMM_official_tests": len(tests),
        "WMM_declination_max_difference_deg": max(errors),
        "segments": [],
    }
    field = geo.calculate(glat=32.9645, glon=132.5055, alt=0.03, time=2026 + 303 / 365)
    assert abs(field.d - cfg["declination_deg"]) < 1e-10
    ctx = Context()
    accel_path = Path(f"data/interim/{eid}/synchronization/parent_100hz.csv")
    accel = numeric_csv(
        accel_path, ["sample_utc_us", "corrected_ax", "corrected_ay", "corrected_az"]
    )[::10]
    attitude = ctx.sample(accel[:, 0] / 1e6)["parent"]
    specific_force = np.einsum("nij,nj->ni", attitude @ ctx.parent_axes, accel[:, 1:])
    valid_force = specific_force[np.isfinite(specific_force).all(1)]
    gravity_median = np.median(valid_force, axis=0)
    assert 9 < gravity_median[2] < 10.5 and np.linalg.norm(gravity_median[:2]) < 0.5
    manifest["parent_specific_force_true_enu_median_m_s2"] = gravity_median.tolist()
    manifest["parent_specific_force_up_p05_p95_m_s2"] = np.percentile(
        valid_force[:, 2], [5, 95]
    ).tolist()
    manifest["attitude_check_note"] = (
        "Checks vertical convention and tilt consistency only; magnetic heading accuracy is unvalidated."
    )
    manifest["inputs_sha256"] = {str(p): sha256(p) for p in [*ctx.inputs, accel_path]}
    k, d, _ = load_camera_intrinsics(Path.cwd(), "P_MAIN_CAM", (1280, 720), "proxy")
    parent_axes = np.array(
        json.loads(Path(cfg["parent_axes"]).read_text())["R_body_from_spresense"]
    )
    lever = {key: np.array(value) for key, value in cfg["lever_arms_frd_m"].items()}
    for suffix in ["001", "002"]:
        segment = f"20261031_nanyo_{suffix}"
        out = Path(f"results/{segment}/absolute_position")
        rows = read(out / "positions.csv")
        valid = [r for r in rows if r["position_available"] == "True"]
        residual = []
        optical_residual = []
        for r in valid:
            rp = Rotation.from_quat(
                [float(r["parent_true_enu_" + c]) for c in ["qx", "qy", "qz", "qw"]]
            ).as_matrix()
            rc = Rotation.from_quat(
                [float(r["child_true_enu_" + c]) for c in ["qx", "qy", "qz", "qw"]]
            ).as_matrix()
            origin = np.array(
                [float(r["child_origin_" + c + "_m"]) for c in ["east", "north", "up"]]
            )
            gnss = np.array(
                [float(r["parent_antenna_" + c + "_m"]) for c in ["east", "north", "up"]]
            )
            transmitter = gnss + rp @ (lever["parent_acoustic"] - lever["parent_antenna"])
            receiver = origin + rc @ lever["child_acoustic"]
            residual.append(abs(np.linalg.norm(receiver - transmitter) - float(r["range_m"])))
            camera = gnss + rp @ (lever["parent_camera"] - lever["parent_antenna"])
            light = origin + rc @ lever["child_light"]
            name = next(
                key for key in cfg["mounts"] if key == "parent_" + Path(r["source_video"]).stem
            )
            body_opt = (
                parent_axes
                @ np.array(cfg["mounts"][name]["R_dji_from_sensor"]).T
                @ np.array(cfg["R_dji_from_parent_optical"])
            )
            ray = calibrated_rays([[float(r["x_px"]), float(r["y_px"])]], k, d)[0]
            direction = rp @ body_opt @ ray
            optical_residual.append(np.linalg.norm(np.cross(light - camera, direction)))
        assert max(residual) < 1e-8 and max(optical_residual) < 1e-8
        mapping = json.loads(Path(f"results/{segment}/visual_mapping/manifest.json").read_text())
        start, _end = mapping["source_interval_s"]
        check = Path(f"data/interim/{segment}/frame_timing_verification")
        check.mkdir(exist_ok=True, parents=True)
        command = [
            str(resolve_ffmpeg()),
            "-v",
            "error",
            "-y",
            "-threads",
            "2",
            "-ss",
            str(start),
            "-i",
            mapping["source"],
            "-vf",
            "fps=1,scale=960:540",
            "-q:v",
            "2",
            "-start_number",
            "0",
            "-frames:v",
            "2",
            str(check / "%06d.jpg"),
        ]
        subprocess.run(command, check=True, timeout=60)
        hashes = []
        clahe = cv2.createCLAHE(2.0, (8, 8))
        for index in [0, 1]:
            p = check / f"{index:06d}.jpg"
            img = cv2.imread(str(p))
            lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
            lab[:, :, 0] = clahe.apply(lab[:, :, 0])
            cv2.imwrite(str(p), cv2.cvtColor(lab, cv2.COLOR_LAB2BGR))
            expected = Path(f"data/interim/{segment}/visual_mapping/images") / p.name
            assert sha256(p) == sha256(expected)
            hashes.append(sha256(p))
        manifest["segments"].append(
            {
                "experiment_id": segment,
                "position_rows": len(rows),
                "available": len(valid),
                "acoustic_residual_max_m": max(residual),
                "optical_ray_residual_max_m": max(optical_residual),
                "reproduced_first_two_image_sha256": hashes,
            }
        )
    manifest["status"] = "passed"
    manifest["code_sha256"] = sha256(Path(__file__))
    Path(f"results/{eid}/absolute_position/geometry_verification.json").write_text(
        json.dumps(manifest, indent=2)
    )
    print(json.dumps(manifest), flush=True)


if __name__ == "__main__":
    main()
