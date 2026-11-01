"""Decode logged absolute attitude, freeze nominal CAD geometry and true-north model."""

import argparse
import importlib.util
import io
import json
import subprocess
import zipfile
from collections import Counter
from datetime import datetime
from pathlib import Path

import numpy as np
import requests
from analyze_nanyo import write
from pygeomag import GeoMag
from scipy.spatial.transform import Rotation

from aquabeacon_analysis.nanyo_position import robust_rotation_mean
from aquabeacon_analysis.ngt_binary import sha256
from aquabeacon_analysis.ngt_sync import numeric_csv, sample_with_gaps


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", required=True, choices=["20261031_nanyo_000"])
    parser.add_argument("--firmware-root", required=True)
    args = parser.parse_args()
    eid = args.experiment
    work = Path(f"data/interim/{eid}/geometry")
    work.mkdir(parents=True, exist_ok=True)
    out = Path(f"results/{eid}/absolute_position")
    out.mkdir(parents=True, exist_ok=True)
    source = Path(args.firmware_root) / "scripts/binary_log.py"
    spec = importlib.util.spec_from_file_location("absolute_binary_reader", source)
    reader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reader)
    raw = Path("data/raw/Nanyo_ocean_test/child/C_MAIN_BNO_EVENTS_35545.bin")
    rows = []
    with raw.open("rb") as f:
        header = reader.read_header(f)
        for rec in reader.records(f):
            if rec["kind"] == 16:
                rows.extend(reader.rows(rec))
    write(work / "child_absolute_raw.csv", rows)
    t = np.array([r["utc_us"] / 1e6 for r in rows])
    q = np.array([[r[k] for k in ["qw", "qx", "qy", "qz"]] for r in rows])
    assert np.max(np.abs(np.linalg.norm(q, axis=1) - 1)) < 0.01
    assert np.all(np.diff(t) > 0)
    a = numeric_csv(
        Path(f"data/interim/{eid}/synchronization/child/C_MAIN_IMU_100Hz_35545.imu.csv"),
        ["utc_us", "ax", "ay", "az", "mx", "my", "mz"],
    )
    v = sample_with_gaps(a[:, 0] / 1e6, a[:, 1:], t, 0.1)
    ok = np.isfinite(v).all(1)
    r = Rotation.from_quat(q[:, [1, 2, 3, 0]])
    gravity = np.median(r[ok].apply(v[ok, :3]), 0)
    inverse_gravity = np.median(r[ok].inv().apply(v[ok, :3]), 0)
    magnetic = np.median(r[ok].apply(v[ok, 3:]), 0)
    assert gravity[2] > 9 and np.linalg.norm(gravity[:2]) < 0.5
    assert magnetic[1] > 10 and abs(magnetic[0]) < 2
    url = "https://www.ncei.noaa.gov/sites/default/files/2024-12/WMM2025COF.zip"
    archive = work / "WMM2025COF.zip"
    if not archive.exists():
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        archive.write_bytes(response.content)
    with zipfile.ZipFile(io.BytesIO(archive.read_bytes())) as z:
        name = next(n for n in z.namelist() if n.endswith("/WMM.COF"))
        (work / "WMM.COF").write_bytes(z.read(name))
    date = datetime(2026, 10, 31)
    decimal_year = 2026 + (date - datetime(2026, 1, 1)).days / 365
    lines = (work / "WMM.COF").read_text().splitlines()
    h = lines[0].split()
    coefficients = [
        tuple(t(v) for t, v in zip((int, int, float, float, float, float), line.split()))
        for line in lines[1:]
        if line.strip() and not line.startswith("9999")
    ]
    field = GeoMag(coefficients_data=((float(h[0]), h[1], h[2]), coefficients)).calculate(
        glat=32.9645, glon=132.5055, alt=0.03, time=decimal_year
    )
    syncpath = Path(f"results/{eid}/synchronization/manifest.json")
    sync = json.loads(syncpath.read_text())["cameras"]
    mounts = {}
    for name, c in sync.items():
        windows = [w for w in c["windows"] if w["accepted"]]
        mean, residual, keep = robust_rotation_mean([w["R_camera_imu"] for w in windows], 3)
        mounts[name] = {
            "R_dji_from_sensor": mean.tolist(),
            "window_residual_deg": residual.tolist(),
            "used": keep.tolist(),
            "method": "Same-run accepted gyro windows, mean trimmed at 3 degrees; source sensor is BNO or Spresense",
        }
    rigpath = Path("calibration/rigs/cad_20261028.json")
    rig = json.loads(rigpath.read_text(encoding="utf-8"))
    sensors = {s["id"]: s for s in rig["sensors"]}
    parts = {p["id"]: p for p in rig["components"]}
    pm = np.diag([1, -1, -1])
    cm = np.array([[0, 1, 0], [1, 0, 0], [0, 0, -1]])
    # Nominal mechanical reference points, explicitly not calibrated phase centers.
    antenna = np.array(parts["parent_hand_service/gnss_antenna"]["T_body_component"])[:3, 3]
    light_mount = np.array(
        parts["child_horizontal_plate_frame/side_light_holder_cap"]["T_body_component"]
    )[:3, 3]
    old_camera = np.array(sensors["child_camera"]["reference_position_body_m"])
    geometry = {
        "parent_antenna": (pm @ antenna).tolist(),
        "parent_camera": (
            pm @ np.array(sensors["parent_camera"]["reference_position_body_m"])
        ).tolist(),
        "parent_acoustic": (
            pm @ np.array(sensors["parent_td04a"]["reference_position_body_m"])
        ).tolist(),
        "child_camera": (cm @ light_mount).tolist(),
        "child_light": (cm @ old_camera).tolist(),
        "child_acoustic": (
            cm @ np.array(sensors["child_td04a"]["reference_position_body_m"])
        ).tolist(),
        "child_imu": (cm @ np.array(sensors["child_imu"]["reference_position_body_m"])).tolist(),
    }
    cfg = {
        "experiment_id": eid,
        "user_same_mount_confirmed": "2026-10-31 current chat",
        "parent_axes": "calibration/20261029_parent_axis_011_axes.json",
        "child_axes": "calibration/20261029_child_axis_003_imu_frd.json",
        "parent_mag_mount": "calibration/20261028_parent_lis3mdl_cad_axes.json",
        "saved_child_camera": "calibration/20261029_child_axis_003_camera_imu.json",
        "saved_child_camera_policy": "Calibration-interval-only rotation not applied; current-run gyro alignment used.",
        "rig": str(rigpath),
        "child_mount": "calibration/child_mount_20261028_actual.json",
        "R_parent_frd_from_cad": pm.tolist(),
        "R_child_frd_from_cad": cm.tolist(),
        "lever_arms_frd_m": geometry,
        "lever_arm_status": "CAD reference points used as nominal measurement centers, unmeasured",
        "child_swap_policy": "Camera at old light-holder reference; light at old camera-case reference. No rotation inferred from swap.",
        "lever_arm_sensitivity_m": 0.05,
        "declination_deg": field.d,
        "declination_positive": "east",
        "declination_model": "NOAA WMM2025",
        "declination_location": [32.9645, 132.5055, 0.03],
        "decimal_year": decimal_year,
        "heading_accuracy_validated": False,
        "position_accuracy_validated": False,
        "rtk_used": False,
        "attitude_max_gap_s": 0.15,
        "gnss_max_gap_s": 1.5,
        "optical_range_max_delta_s": 0.3,
        "mounts": mounts,
    }
    cfgpath = Path(f"config/{eid}/absolute_position.json")
    cfgpath.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    inputs = [
        source,
        raw,
        syncpath,
        rigpath,
        Path(__file__),
        work / "WMM.COF",
        archive,
        *[
            Path(cfg[k])
            for k in [
                "parent_axes",
                "child_axes",
                "parent_mag_mount",
                "saved_child_camera",
                "child_mount",
            ]
        ],
    ]
    manifest = {
        "child_rows": len(rows),
        "status_counts": dict(Counter(r["status"] for r in rows)),
        "raw_frame": "sensor to magnetic ENU, scalar first wxyz; no body/declination applied in firmware",
        "gravity_world_median": gravity.tolist(),
        "gravity_inverse_median": inverse_gravity.tolist(),
        "magnetic_world_median": magnetic.tolist(),
        "header": header,
        "field": field.__dict__,
        "WMM_source": url,
        "crc": "All source records checked by strict reader",
        "input_sha256": {str(p): sha256(p) for p in inputs},
        "git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
    }
    (out / "preparation.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print("Absolute rows", len(rows), "declination", field.d, "geometry", geometry, flush=True)


if __name__ == "__main__":
    main()
