"""Explicit day2 GNSS/acoustic/optical/position pipeline; raw inputs are read-only."""

import argparse
import gzip
import io
import json
import re
import subprocess
import tarfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import cv2
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from analyze_nanyo import read, write
from estimate_nanyo_absolute import Context, estimate
from ppk_ngt_geonet import Inputs, get, observations, read_positions
from pygeomag import GeoMag

from aquabeacon_analysis.camera_calibration import load_camera_intrinsics
from aquabeacon_analysis.editable_plots import save_editable_figure
from aquabeacon_analysis.gps_parent import geodetic_to_enu
from aquabeacon_analysis.nanyo_position import robust_rotation_mean, true_enu_from_magnetic_enu
from aquabeacon_analysis.ngt_binary import sha256
from aquabeacon_analysis.ngt_gnss import audit_ubx
from aquabeacon_analysis.ngt_optical import detect_candidate
from aquabeacon_analysis.ngt_position import calibrated_rays, nearest_indices
from aquabeacon_analysis.ngt_sync import numeric_csv, sample_with_gaps

ACQ = "20261101_nanyo_000"
ROOT = Path.cwd()
RAW = ROOT / "data/raw/Nanyo_ocean_test_day2"
WORK = ROOT / f"data/interim/{ACQ}"
RESULT = ROOT / f"results/{ACQ}"


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")


def provenance(paths):
    return {str(p): sha256(p) for p in paths}


def segments():
    return json.loads((RESULT / "segmentation/manifest.json").read_text())["segments"]


def run(command, log, timeout=600):
    dump(log.with_suffix(".command.json"), command)
    with log.open("w") as f:
        subprocess.run(command, stdout=f, stderr=f, check=True, timeout=timeout)


def gnss():
    folder = WORK / "gnss"
    folder.mkdir(parents=True, exist_ok=True)
    binaries = ROOT / "data/interim/20261031_nanyo_000/gnss/rtklib"
    sources = [RAW / f"parent/P_MAIN_GNSS_{s}.ubx" for s in ["62440", "62441"]]
    rover = folder / "rover.ubx"
    if not rover.exists():
        with rover.open("wb") as f:
            for p in sources:
                f.write(p.read_bytes())
    if not (folder / "rover.obs").exists():
        run(
            [
                str(binaries / "convbin.exe"),
                "-r",
                "ubx",
                "-v",
                "3.04",
                "-od",
                "-os",
                "-o",
                str(folder / "rover.obs"),
                "-n",
                str(folder / "rover.nav"),
                str(rover),
            ],
            folder / "convert.log",
        )
    base_paths = []
    for station in ["950437", "041133"]:
        target = folder / f"base_{station}"
        target.mkdir(exist_ok=True)
        if not list(target.glob("*.26o")):
            fields = {
                "category": "gis",
                "sel_obs[0]": station,
                "hour_time": "UTC",
                "hour_st_year": "2026",
                "hour_st_mon": "11",
                "hour_st_day": "01",
                "hour_st_time": "00",
                "hour_ed_year": "2026",
                "hour_ed_mon": "11",
                "hour_ed_day": "01",
                "hour_ed_time": "02",
                "hour_satellite": "GRJE",
                "hour_rinex_ver": "3.02",
                "hour_filename": "normal",
                "hour_navfile": "normal",
            }
            dump(target / "request.json", fields)
            page = get("https://terras.gsi.go.jp/hour_download.php", fields).decode()
            (target / "response.html").write_text(page, encoding="utf-8")
            form_inputs = Inputs()
            form_inputs.feed(page)
            files = re.findall(
                r"one_file_download\('([^']+)','([^']+)','([^']+)','([^']+)'\)", page
            )
            if not files:
                raise ValueError(f"No GEONET files for {station}; see saved response")
            for code, name, kind, tmp in files:
                form = dict(form_inputs.fields)
                form.update(
                    satellite_type="GRJE",
                    data_type=kind,
                    observation_date="2026-11-01",
                    d_mode="[hour] GRJE 3.02 2026/11/01.00 2026/11/01.03",
                    d_obs_cd=code,
                    d_file=name,
                    d_tmp_dir=tmp,
                    d_satellite="DOWNLOAD",
                    d_rv="3.02",
                )
                content = get("https://terras.gsi.go.jp/download.php", form)
                (target / Path(name).name).write_bytes(content)
                if name.endswith(".tar.gz"):
                    with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as archive:
                        for member in archive.getmembers():
                            if member.isfile():
                                (target / Path(member.name).name).write_bytes(
                                    archive.extractfile(member).read()
                                )
                else:
                    (target / Path(name[:-3]).name).write_bytes(gzip.decompress(content))
        base = next(target.glob("*.26o"))
        info = observations(base)
        dump(target / "observations.json", info)
        assert info["first_utc_s"] <= segments()[0]["start_utc_s"]
        assert info["last_utc_s"] >= segments()[-1]["end_utc_s"]
        nav = sorted(p for p in target.glob("*.26*") if p.suffix in [".26n", ".26l", ".26q"])
        base_paths += [base, *nav]
        for mode in ["ar_continuous", "float"] if station == "950437" else ["ar_continuous"]:
            output = folder / f"{mode}.pos" if station == "950437" else target / f"{mode}.pos"
            config = folder / f"{mode}.conf"
            armode = "continuous" if mode == "ar_continuous" else "off"
            config.write_text(
                "\n".join(
                    [
                        "pos1-posmode =kinematic",
                        "pos1-frequency =l1+l2+l5",
                        "pos1-soltype =combined",
                        "pos1-navsys =73",
                        "pos1-elmask =15",
                        f"pos2-armode ={armode}",
                        "pos2-arthres =3",
                        "pos2-arlockcnt =5",
                        "pos2-maxage =15",
                        "ant2-postype =rinexhead",
                        "out-solformat =llh",
                        "out-timesys =utc",
                        "out-timeform =hms",
                        "out-height =ellipsoidal",
                        "",
                    ]
                )
            )
            if not output.exists():
                run(
                    [
                        str(binaries / "rnx2rtkp.exe"),
                        "-k",
                        str(config),
                        "-c",
                        "-t",
                        "-u",
                        "-o",
                        str(output),
                        str(folder / "rover.obs"),
                        str(base),
                        *map(str, nav),
                    ],
                    output.with_suffix(".log"),
                )
            print(station, mode, output.stat().st_size, flush=True)
    dump(
        folder / "manifest.json",
        {
            "experiment_id": ACQ,
            "rtk_used": False,
            "date_utc": "2026-11-01",
            "base_stations": ["950437", "041133"],
            "inputs_sha256": provenance(
                [
                    *sources,
                    *base_paths,
                    binaries / "convbin.exe",
                    binaries / "rnx2rtkp.exe",
                    Path(__file__),
                ]
            ),
            "git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        },
    )


def evaluate():
    out = RESULT / "evaluation"
    out.mkdir(parents=True, exist_ok=True)
    spp, audits = [], {}
    for suffix in ["62440", "62441"]:
        audit, rows = audit_ubx(RAW / f"parent/P_MAIN_GNSS_{suffix}.ubx")
        audits[suffix] = audit
        spp.extend(rows)
    spp = sorted(spp, key=lambda r: r["utc_s"])
    origin = [spp[0][k] for k in ["latitude_deg", "longitude_deg", "height_ellipsoid_m"]]
    xyz = geodetic_to_enu(
        *np.array(
            [[r[k] for k in ["latitude_deg", "longitude_deg", "height_ellipsoid_m"]] for r in spp]
        ).T,
        *origin,
    )
    for row, point in zip(spp, xyz, strict=True):
        row.update(zip(["east_m", "north_m", "up_m"], point.tolist()))
    clock = next(
        v
        for k, v in json.loads((RESULT / "synchronization/manifest.json").read_text())[
            "cameras"
        ].items()
        if k.startswith("parent")
    )
    tracks = {"spp": spp}
    for name, file in [
        ("ppk", "ar_continuous.pos"),
        ("ppk_float", "float.pos"),
        ("ppk_base_041133", "base_041133/ar_continuous.pos"),
    ]:
        tracks[name] = read_positions(WORK / "gnss" / file, origin, clock)
    manifest = {
        "experiment_id": ACQ,
        "origin_llh": origin,
        "ubx_audit": audits,
        "rtk_used": False,
        "carrier_solution_counts": dict(Counter(r["carrier_solution"] for r in spp)),
        "segments": [],
        "inputs_sha256": provenance([WORK / "gnss/manifest.json", Path(__file__)]),
    }
    pressure = numeric_csv(RESULT / "segmentation/selection.csv", ["sync_utc_s", "depth_m"])
    for seg in [
        {"experiment_id": ACQ, "start_utc_s": spp[0]["utc_s"], "end_utc_s": spp[-1]["utc_s"] + 1},
        *segments(),
    ]:
        eid, lo, hi = seg["experiment_id"], seg["start_utc_s"], seg["end_utc_s"]
        folder = ROOT / f"results/{eid}/evaluation"
        folder.mkdir(parents=True, exist_ok=True)
        cut = {k: [r for r in values if lo <= r["utc_s"] < hi] for k, values in tracks.items()}
        for name, rows in cut.items():
            if rows:
                write(folder / f"{name}_positions.csv", rows)
        info = {
            "experiment_id": eid,
            "ppk_quality_counts": dict(Counter(r["quality"] for r in cut["ppk"])),
        }
        fig, ax = plt.subplots(figsize=(8, 6))
        for name in ["spp", "ppk"]:
            ax.plot(
                [r["east_m"] for r in cut[name]],
                [r["north_m"] for r in cut[name]],
                ".",
                ms=2,
                label=name.upper(),
            )
        ax.set(xlabel="East [m]", ylabel="North [m]", aspect="equal", title=eid)
        ax.legend()
        ax.grid(alpha=0.25)
        fig.tight_layout()
        save_editable_figure(fig, folder / "gnss_overhead.png", dpi=150)
        plt.close(fig)
        if eid != ACQ:
            files = sorted(
                (ROOT / f"data/processed/{eid}/synchronized/parent").glob("*.range_measurement.csv")
            )
            rows = sorted(
                [dict(r, log_file=p.name) for p in files for r in read(p)],
                key=lambda r: float(r["sync_utc_s"]),
            )
            t = np.array([float(r["sync_utc_s"]) for r in rows])
            depth = sample_with_gaps(pressure[:, 0], pressure[:, 1], t, 3)
            ranges = np.array(
                [float(r["corrected_range_m"]) if r["range_state"] == "3" else np.nan for r in rows]
            )
            valid = np.isfinite(ranges) & (ranges > 0)
            for i, row in enumerate(rows):
                row.update(
                    pressure_depth_m=float(depth[i]),
                    acoustic_candidate=bool(valid[i]),
                    range_minus_depth_m=float(ranges[i] - depth[i]),
                )
            write(folder / "acoustic_depth.csv", rows)
            info.update(
                shot_count=len(rows),
                candidate_count=int(valid.sum()),
                candidate_fraction=float(valid.mean()),
                range_state_counts=dict(Counter(r["range_state"] for r in rows)),
                median_interval_s=float(np.median(np.diff(t))),
                sound_speed_m_s=sorted({r["sound_speed_m_s"] for r in rows}),
                offset_us=sorted({r["offset_us"] for r in rows}),
            )
            fig, ax = plt.subplots(figsize=(11, 4))
            ax.plot(t - lo, depth, label="Pressure depth (surface proxy)")
            ax.plot(t - lo, np.where(valid, ranges, np.nan), ".", ms=3, label="Acoustic state 3")
            ax.set(xlabel="Interval time [s]", ylabel="Depth / range [m]", title=eid)
            ax.legend()
            ax.grid(alpha=0.25)
            fig.tight_layout()
            save_editable_figure(fig, folder / "acoustic_depth.png", dpi=150)
            plt.close(fig)
            groups = {}
            shot_files = (ROOT / f"data/processed/{eid}/synchronized/child").glob("*.shot.csv")
            for p in shot_files:
                for r in read(p):
                    key = tuple(
                        r[k]
                        for k in [
                            "frequency_hz",
                            "acoustic_pattern_id",
                            "pattern_gap1_us",
                            "pattern_gap2_us",
                            "ranging_rate_hz",
                        ]
                    )
                    counts = groups.setdefault(key, [0, 0])
                    counts[0] += 1
                    counts[1] += int(r["range_state"] == "3" and float(r["tof_us"]) > 0)
            write(
                folder / "acoustic_pattern_groups.csv",
                [
                    dict(
                        zip(["frequency_hz", "pattern_id", "gap1_us", "gap2_us", "rate_hz"], key),
                        shots=v[0],
                        candidates=v[1],
                        candidate_fraction=v[1] / v[0],
                    )
                    for key, v in groups.items()
                ],
            )
        dump(folder / "metrics.json", info)
        manifest["segments"].append(info)
        print(info, flush=True)
    primary = tracks["ppk"]
    alternate = tracks["ppk_base_041133"]
    a = sample_with_gaps(
        np.array([r["utc_s"] for r in alternate]),
        np.array([[r[k] for k in ["east_m", "north_m", "up_m"]] for r in alternate]),
        np.array([r["utc_s"] for r in primary]),
        2,
    )
    diff = a - np.array([[r[k] for k in ["east_m", "north_m", "up_m"]] for r in primary])
    manifest["base_difference_horizontal_median_p95_m"] = np.nanpercentile(
        np.linalg.norm(diff[:, :2], axis=1), [50, 95]
    ).tolist()
    dump(out / "manifest.json", manifest)


class Day2Context(Context):
    def __init__(self):
        self.cfg_path = ROOT / f"config/{ACQ}/absolute_position.json"
        old = json.loads((ROOT / "config/20261031_nanyo_000/absolute_position.json").read_text())
        keys = [
            "parent_axes",
            "child_axes",
            "parent_mag_mount",
            "rig",
            "child_mount",
            "lever_arms_frd_m",
            "lever_arm_status",
            "R_dji_from_parent_optical",
            "R_dji_from_child_optical",
            "lever_arm_sensitivity_m",
        ]
        self.cfg = {k: old[k] for k in keys}
        self.cfg.update(
            experiment_id=ACQ,
            user_same_mount_confirmed="2026-11-02 current chat: same as day1; child 30 fps",
            intrinsics_policy="User default shared underwater calibration; native LRF 1280x720, no 90-degree rotation",
            optical_axes_policy="Same device and same mounting explicitly confirmed; day1 visual-axis convention retained",
            child_video_fps_user=30,
            heading_accuracy_validated=False,
            position_accuracy_validated=False,
            rtk_used=False,
            attitude_max_gap_s=0.15,
        )
        syncpath = RESULT / "synchronization/manifest.json"
        self.sync = json.loads(syncpath.read_text())["cameras"]
        self.cfg["mounts"] = {}
        for name, clock in self.sync.items():
            matrices = [w["R_camera_imu"] for w in clock["windows"] if w["accepted"]]
            mean, residual, keep = robust_rotation_mean(matrices, 3)
            self.cfg["mounts"][name] = {
                "R_dji_from_sensor": mean.tolist(),
                "residual_deg": residual.tolist(),
                "used": keep.tolist(),
                "method": "day2 same-session accepted gyro windows; trim 3 deg",
            }
        self.origin = json.loads((RESULT / "evaluation/manifest.json").read_text())["origin_llh"]
        date = datetime(2026, 11, 1, tzinfo=UTC)
        year = 2026 + (date - datetime(2026, 1, 1, tzinfo=UTC)).days / 365
        self.decl = (
            GeoMag()
            .calculate(
                glat=self.origin[0], glon=self.origin[1], alt=self.origin[2] / 1000, time=year
            )
            .d
        )
        self.cfg.update(declination_deg=self.decl, declination_model="WMM2025", decimal_year=year)
        dump(self.cfg_path, self.cfg)
        self.inputs = [self.cfg_path, syncpath, Path(__file__)]
        for k in ["parent_axes", "child_axes", "parent_mag_mount", "rig", "child_mount"]:
            self.inputs.append(Path(self.cfg[k]))
        self.parent_axes = np.array(
            json.loads(Path(self.cfg["parent_axes"]).read_text())["R_body_from_spresense"]
        )
        self.child_axes = np.array(
            json.loads(Path(self.cfg["child_axes"]).read_text())["R_body_from_sensor"]
        )
        self.mag_mount = np.array(
            json.loads(Path(self.cfg["parent_mag_mount"]).read_text())["R_frd_from_mag"]
        )
        self.camera = {}
        for name in self.sync:
            p = ROOT / f"data/processed/{ACQ}/synchronization/{name}_attitude.csv"
            self.inputs.append(p)
            self.camera[name] = numeric_csv(p, ["sync_utc_s", "qw", "qx", "qy", "qz"])
        p = WORK / "synchronization/child/C_MAIN_BNO_EVENTS_68440.absolute_attitude.csv"
        self.inputs.append(p)
        self.child = numeric_csv(p, ["utc_us", "qw", "qx", "qy", "qz", "status", "accuracy_rad"])
        self.child[:, 0] /= 1e6
        self.mag = []
        for suffix in ["62440", "62441"]:
            p = WORK / f"synchronization/parent/P_MAIN_HEALTH_RANGE_{suffix}.mag_observation.csv"
            self.inputs.append(p)
            rows = read(p)
            a = np.array(
                [
                    [
                        float(r[k])
                        for k in [
                            "utc_read_us",
                            "corrected_x",
                            "corrected_y",
                            "corrected_z",
                            "calibration_generation",
                        ]
                    ]
                    for r in rows
                ]
            )
            a[:, 0] /= 1e6
            good = [r["valid"] == "True" and r["hardware_overrun"] == "False" for r in rows]
            a[~np.array(good), 1:4] = np.nan
            self.mag.append(a)
        self.mag = np.vstack(self.mag)
        self.mag = self.mag[np.argsort(self.mag[:, 0])]
        p = RESULT / "evaluation/ppk_positions.csv"
        self.inputs.append(p)
        self.gnss = numeric_csv(
            p, ["utc_s", "east_m", "north_m", "up_m", "quality", "sd_e_m", "sd_n_m", "sd_u_m"]
        )
        p = RESULT / "segmentation/selection.csv"
        self.inputs.append(p)
        self.depth = numeric_csv(p, ["sync_utc_s", "depth_m"])
        self.decl_enu = true_enu_from_magnetic_enu(self.decl)
        self.parent_from_dji = {
            k: self.parent_axes @ np.array(self.cfg["mounts"][k]["R_dji_from_sensor"]).T
            for k in self.sync
            if k.startswith("parent")
        }
        self.lever = {k: np.array(v) for k, v in self.cfg["lever_arms_frd_m"].items()}


def optical(eid, hz=2):
    out = ROOT / f"results/{eid}/relative_position"
    out.mkdir(parents=True, exist_ok=True)
    seg = json.loads((ROOT / f"experiments/{eid}/experiment.json").read_text())
    clocks = json.loads((RESULT / "synchronization/manifest.json").read_text())["cameras"]
    parents = sorted(
        [c for k, c in clocks.items() if k.startswith("parent")],
        key=lambda c: c["utc_at_video_zero_s"],
    )
    times = np.arange(*seg["utc_interval_s"], 1 / hz)
    ranges = read(ROOT / f"results/{eid}/evaluation/acoustic_depth.csv")
    idx, _ = nearest_indices([float(r["sync_utc_s"]) for r in ranges], times, 0.3)
    k, d, prov = load_camera_intrinsics(ROOT, "P_MAIN_CAM", (1280, 720), "proxy")
    rows, previews, caps = [], [], {}
    for i, utc in enumerate(times):
        clock = max(
            [c for c in parents if c["utc_at_video_zero_s"] <= utc],
            key=lambda c: c["utc_at_video_zero_s"],
        )
        source = RAW / "parent/DJI" / Path(clock["source_video"]["path"]).with_suffix(".LRF").name
        if source not in caps:
            caps[source] = cv2.VideoCapture(str(source))
            caps[source].set(cv2.CAP_PROP_ORIENTATION_AUTO, 0)
        cap = caps[source]
        source_s = (utc - clock["utc_at_video_zero_s"]) / clock["time_scale"]
        cap.set(cv2.CAP_PROP_POS_MSEC, source_s * 1000)
        ok, frame = cap.read()
        row = {
            "utc_s": utc,
            "segment_time_s": utc - seg["utc_interval_s"][0],
            "source_video": source.with_suffix(".MP4").name,
            "requested_source_s": source_s,
            "actual_source_s": cap.get(cv2.CAP_PROP_POS_MSEC) / 1000 if ok else source_s,
            "frame_read": ok,
            "x_px": np.nan,
            "y_px": np.nan,
            "optical_candidate": False,
            "water_like_heuristic": False,
            "range_candidate": False,
            "range_m": np.nan,
            "x_right_candidate_m": np.nan,
            "y_down_candidate_m": np.nan,
            "z_forward_candidate_m": np.nan,
        }
        if ok:
            assert frame.shape[:2] == (720, 1280), frame.shape
            det = detect_candidate(frame)
            blue = (frame[:, :, 0].astype(float) > frame[:, :, 2] * 1.25) & (frame[:, :, 0] > 30)
            row.update(
                optical_candidate=det["status"] == "candidate",
                water_like_heuristic=bool(blue.mean() > 0.7 and frame.mean() < 140),
                x_px=det["x_px"] if det["x_px"] is not None else np.nan,
                y_px=det["y_px"] if det["y_px"] is not None else np.nan,
            )
            if idx[i] >= 0:
                row.update(
                    range_candidate=ranges[idx[i]]["acoustic_candidate"] == "True",
                    range_m=float(ranges[idx[i]]["corrected_range_m"]),
                )
            if row["optical_candidate"] and row["water_like_heuristic"] and row["range_candidate"]:
                xyz = calibrated_rays([[row["x_px"], row["y_px"]]], k, d)[0] * row["range_m"]
                row.update(
                    zip(
                        ["x_right_candidate_m", "y_down_candidate_m", "z_forward_candidate_m"],
                        xyz.tolist(),
                    )
                )
            if i % max(1, len(times) // 12) == 0:
                preview = frame.copy()
                if det["bbox"]:
                    x, y, w, h = det["bbox"]
                    cv2.rectangle(preview, (x, y), (x + w, y + h), (0, 0, 255), 3)
                cv2.putText(
                    preview,
                    f"{row['segment_time_s']:.1f}s candidate={row['optical_candidate']} water={row['water_like_heuristic']}",
                    (10, 35),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.8,
                    (0, 0, 255),
                    2,
                )
                previews.append(cv2.resize(preview, (480, 270)))
        rows.append(row)
    for cap in caps.values():
        cap.release()
    write(out / "camera_relative_candidates.csv", rows)
    if previews:
        while len(previews) % 3:
            previews.append(np.zeros_like(previews[0]))
        cv2.imwrite(
            str(out / "light_review.jpg"),
            np.vstack([np.hstack(previews[i : i + 3]) for i in range(0, len(previews), 3)]),
        )
    dump(
        out / "manifest.json",
        {
            "experiment_id": eid,
            "sample_hz": hz,
            "samples": len(rows),
            "intrinsics": prov,
            "optical_candidates": sum(r["optical_candidate"] for r in rows),
            "camera_frame_positions": sum(
                np.isfinite(r["z_forward_candidate_m"]) for r in rows
            ).item(),
            "source_clock_manifest_sha256": sha256(RESULT / "synchronization/manifest.json"),
            "code_sha256": sha256(Path(__file__)),
        },
    )
    print(eid, "optical", len(rows), flush=True)


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--experiment", required=True, choices=[ACQ])
    parser.add_argument(
        "--phase", choices=["gnss", "evaluate", "optical", "positions", "all"], default="all"
    )
    args = parser.parse_args()
    if args.phase in ["gnss", "all"]:
        gnss()
    if args.phase in ["evaluate", "all"]:
        evaluate()
    if args.phase in ["optical", "all"]:
        for seg in segments():
            optical(seg["experiment_id"])
    if args.phase in ["positions", "all"]:
        ctx = Day2Context()
        for seg in segments():
            estimate(ctx, seg["experiment_id"])


if __name__ == "__main__":
    main()
