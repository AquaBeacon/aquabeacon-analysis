"""Fuse explicitly observable NGT channels without inventing child world position."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from aquabeacon_analysis.editable_plots import save_editable_figure
from aquabeacon_analysis.imu_localization import (
    align_body_attitude_to_camera,
    quaternion_angle_deg,
    quaternion_conjugate,
    quaternion_multiply,
)
from aquabeacon_analysis.ngt_fusion import (
    cv_filter,
    fuse_local_attitude,
    gap_interpolate,
    pressure_depth,
    stationarity,
    unobservable_child_position,
)
from aquabeacon_analysis.ngt_sync import numeric_csv


def write_table(path, table):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(table)
        writer.writerows(zip(*table.values(), strict=True))


def read_rows(path):
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def numbers(rows, keys):
    return np.array([[float(row[key]) if row[key] else np.nan for key in keys]
                     for row in rows], dtype=float)


def euler(q):
    w, x, y, z = q.T
    return np.degrees(np.column_stack([
        np.arctan2(2 * (w*x+y*z), 1-2*(x*x+y*y)),
        np.arcsin(np.clip(2*(w*y-z*x), -1, 1)),
        np.arctan2(2*(w*z+x*y), 1-2*(y*y+z*z)),
    ]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", required=True, choices=["20261028_ngt_000"])
    args = parser.parse_args()
    print(json.dumps({"pid": os.getpid(), "started_utc": datetime.now(UTC).isoformat(),
                      "purpose": "bounded NGT fusion; no servers"}), flush=True)
    root = Path.cwd()
    interim = root / "data/interim" / args.experiment
    processed = root / "data/processed" / args.experiment
    result = root / "results" / args.experiment / "fusion"
    dest = processed / "fusion"
    dest.mkdir(parents=True, exist_ok=True)
    result.mkdir(parents=True, exist_ok=True)
    sync_path = root / "results" / args.experiment / "synchronization/manifest.json"
    sync = json.loads(sync_path.read_text())
    parent_sync = sync["cameras"]["parent"]
    utc0, scale = parent_sync["utc_at_video_zero_s"], parent_sync["time_scale"]
    config = {"attitude_camera_correction_time_s": .5, "imu_max_gap_s": .1,
              "position_max_prediction_s": 3, "gnss_innovation_gate_chi2": 25,
              "gnss_sigma_floor_horizontal_m": .3, "gnss_sigma_floor_vertical_m": .5,
              "pressure_baseline_parent_video_s": [840, 900], "density_kg_m3": 997,
              "gravity_m_s2": 9.80665, "pressure_sigma_m": .04,
              "known_static_parent_video_s": [240, 480],
              "range_profile": 7, "sound_speed_m_s": 1500, "tof_offset_us": -55,
              "stationarity_gyro_max_rad_s": .035, "stationarity_acc_norm_tolerance_m_s2": .18,
              "static_depth_zero_rate_pressure_slope_max_m_s": .015}
    inputs = [sync_path, root / "config/20261028_ngt/analysis.json"]
    tables, bodies = {}, {}
    attitude_stats = {}
    for role in ["parent", "child"]:
        cache = interim / f"cameras/{role}_sync_input.npz"
        inputs.append(cache)
        with np.load(cache) as data:
            cam, body = data["camera"].copy(), data["body"].copy()
        ct = sync["cameras"][role]["utc_at_video_zero_s"] + sync["cameras"][role]["time_scale"] * cam[:, 0]
        valid = np.isfinite(cam[:, 1:5]).all(axis=1) & (np.linalg.norm(cam[:, 1:5], axis=1) > .5)
        cam, ct = cam[valid], ct[valid]
        cq = cam[:, 1:5] / np.linalg.norm(cam[:, 1:5], axis=1)[:, None]
        bt, unique = np.unique(body[:, 0] / 1e6, return_index=True)
        body = body[unique]
        rotation = np.asarray(sync["cameras"][role]["R_camera_imu"])
        fq, used, reset = fuse_local_attitude(ct, cq, bt, body[:, 1:4], rotation)
        quiet = stationarity(body[:, 1:4], body[:, 4:7])
        bodies[role] = (bt, body, quiet)
        table = {"utc_s": ct, "parent_video_s": (ct-utc0)/scale,
                 "source_video_s": cam[:, 0], "gyro_used": used, "camera_reset": reset}
        for prefix, q in [("camera", cq), ("fused", fq)]:
            for k, name in enumerate(["qw", "qx", "qy", "qz"]):
                table[f"{prefix}_{name}"] = q[:, k]
        table["camera_fused_angle_deg"] = quaternion_angle_deg(cq, fq)
        relative = quaternion_multiply(quaternion_conjugate(fq[0]), fq)
        for k, name in enumerate(["roll", "pitch", "yaw"]):
            table[f"relative_{name}_deg"] = euler(relative)[:, k]
        if role == "child":
            path = interim / "decoded/child/C_MAIN_IMU_100Hz_83304.imu.csv"
            inputs.append(path)
            bno = numeric_csv(path, ["utc_us", "qw", "qx", "qy", "qz"])
            valid = (bno[:, 0] > 1e15) & (np.linalg.norm(bno[:, 1:], axis=1) > .5)
            bno = bno[valid]
            bno_q = bno[:, 1:] / np.linalg.norm(bno[:, 1:], axis=1)[:, None]
            signs = np.ones(len(bno_q))
            signs[1:] = np.cumprod(np.where(np.sum(bno_q[1:] * bno_q[:-1], axis=1) < 0, -1, 1))
            bno_q *= signs[:, None]
            uq, indices = np.unique(bno[:, 0] / 1e6, return_index=True)
            sampled = gap_interpolate(uq, bno_q[indices], ct)
            available = np.isfinite(sampled).all(axis=1)
            sampled[available] /= np.linalg.norm(sampled[available], axis=1)[:, None]
            aligned = np.full_like(sampled, np.nan)
            if available.any():
                aligned[available] = align_body_attitude_to_camera(sampled[available], fq[available], rotation)
            table["bno_camera_disagreement_deg"] = quaternion_angle_deg(aligned, fq)
            for k, name in enumerate(["qw", "qx", "qy", "qz"]):
                table[f"bno_aligned_{name}"] = aligned[:, k]
        attitude_stats[role] = {"rows": len(ct), "gyro_fused_rows": int(used.sum()),
                                "resets_or_camera_only_rows": int(reset.sum()),
                                "camera_fused_disagreement_p95_deg": float(np.percentile(table["camera_fused_angle_deg"], 95))}
        if role == "child":
            attitude_stats[role]["bno_aligned_disagreement_median_deg"] = float(np.nanmedian(table["bno_camera_disagreement_deg"]))
            attitude_stats[role]["bno_aligned_disagreement_p95_deg"] = float(np.nanpercentile(table["bno_camera_disagreement_deg"], 95))
        tables[f"{role}_attitude"] = table
        print(f"{role} attitude complete", flush=True)
    position_stats = {}
    for source in ["rtk", "ppk"]:
        path = processed / "gnss" / ("rtk_nav_pvt.csv" if source == "rtk" else "ppk_positions.csv")
        inputs.append(path)
        rows = read_rows(path)
        a = numbers(rows, ["utc_s", "parent_video_s", "east_m", "north_m", "up_m"])
        if source == "rtk":
            valid = np.array([r["utc_valid"] == "True" and r["fix_valid"] == "True" and int(r["fix_type"]) >= 3 for r in rows])
            sigma = numbers(rows, ["h_accuracy_m", "h_accuracy_m", "v_accuracy_m"])
            quality = np.array([r["carrier_solution"] for r in rows])
        else:
            valid = np.array([int(r["quality"]) in [1, 2, 4, 5] for r in rows])
            sigma = numbers(rows, ["sd_e_m", "sd_n_m", "sd_u_m"])
            quality = np.array([r["quality"] for r in rows])
        sigma = np.maximum(sigma, [.3, .3, .5])
        bt, body, quiet = bodies["parent"]
        inertial = gap_interpolate(bt, quiet.astype(float), a[:, 0], .1) >= .999
        static = inertial & (a[:, 1] >= 240) & (a[:, 1] < 480)
        fused, covariance, accepted, predicted, reset = cv_filter(a[:, 0], a[:, 2:5], sigma, valid, inertial, static)
        table = {"utc_s": a[:, 0], "parent_video_s": a[:, 1], "quality": quality,
                 "input_valid": valid, "measurement_accepted": accepted,
                 "measurement_rejected": valid & ~accepted, "predicted_only": predicted,
                 "state_reinitialized": reset, "imu_quiet": inertial, "zero_velocity_constraint": static,
                 "outage": ~np.isfinite(fused).all(axis=1)}
        for k, axis in enumerate(["east", "north", "up"]):
            table[f"raw_{axis}_m"] = a[:, 2+k]
            table[f"fused_{axis}_m"] = fused[:, k]
            table[f"model_var_{axis}_m2"] = covariance[:, k]
            table[f"input_model_sigma_{axis}_m"] = sigma[:, k]
        position_stats[source] = {"rows": len(a), "accepted": int(accepted.sum()),
                                  "rejected": int((valid & ~accepted).sum()),
                                  "zero_velocity_rows": int(static.sum()),
                                  "outage_rows": int(table["outage"].sum())}
        tables["parent_position" if source == "rtk" else "parent_position_ppk_alternative"] = table
    health_path = interim / "decoded/child/C_MAIN_HEALTH_RANGE_83304.health.csv"
    inputs.append(health_path)
    rows = read_rows(health_path)
    h = numbers(rows, ["mono_us", "utc_anchor_mono_us", "utc_anchor_epoch_us", "utc_valid_until_us", "pressure_mbar"])
    valid = (h[:, 1] <= h[:, 0]) & (h[:, 0] < h[:, 3]) & (h[:, 2] > 1e15)
    sources = np.array([r["utc_source"] for r in rows])[valid]
    h = h[valid]
    ht = (h[:, 2]+h[:, 0]-h[:, 1]) / 1e6
    ht, indices = np.unique(ht, return_index=True)
    h = h[indices]
    sources = sources[indices]
    video_t = (ht-utc0)/scale
    baseline = np.median(h[(video_t >= 840) & (video_t < 900), 4])
    if not np.isfinite(baseline):
        raise ValueError("Declared preimmersion baseline unavailable")
    config["pressure_baseline_mbar"] = float(baseline)
    depth = pressure_depth(h[:, 4], baseline)
    bt, body, quiet = bodies["child"]
    inertial = gap_interpolate(bt, quiet.astype(float), ht, .1) >= .999
    slope = np.gradient(depth, ht)
    zero_rate = inertial & (np.abs(slope) < .015)
    fused, cov, accepted, predicted, reset = cv_filter(ht, depth, np.full(len(ht), .04), np.isfinite(depth), inertial, zero_rate, innovation_gate=100)
    table = {"utc_s": ht, "parent_video_s": video_t, "pressure_mbar": h[:, 4],
             "utc_source": sources, "anchor_mono_us": h[:, 1], "anchor_epoch_us": h[:, 2],
             "raw_depth_m": depth, "fused_depth_m": fused[:, 0], "model_var_depth_m2": cov[:, 0],
             "imu_quiet": inertial, "zero_vertical_rate_constraint": zero_rate,
             "measurement_accepted": accepted, "predicted_only": predicted, "state_reinitialized": reset}
    for k, axis in enumerate(["east", "north", "up"]):
        table[f"absolute_child_{axis}_m"] = unobservable_child_position(len(ht))[:, k]
    table["position_status"] = np.full(len(ht), "unobservable_bearing_and_lever_arms")
    tables["child_depth"] = table
    ranges_path = interim / "decoded/parent/P_MAIN_HEALTH_RANGE_11071.range_measurement.csv"
    shot_path = root / "results" / args.experiment / "ingest/child_shot_utc_mapping.csv"
    inputs.extend([ranges_path, shot_path])
    ranges = read_rows(ranges_path)
    shots = {int(r["shot_seq"]): r for r in read_rows(shot_path)}
    times, states, measured, recomputed, shot_numbers, profile = [], [], [], [], [], []
    shot_sources, tof_values = [], []
    for row in ranges:
        shot = shots.get(int(row["shot_seq"]), {})
        t = float(shot.get("child_tx_utc_us") or "nan") / 1e6
        times.append(t)
        states.append(int(row["range_state"]))
        measured.append(float(row["corrected_range_m"]))
        recomputed.append(1500 * (float(row["tof_us"])+55) / 1e6 if int(row["range_state"]) in [2, 3] else np.nan)
        shot_numbers.append(int(row["shot_seq"]))
        profile.append(int(row["range_profile"]))
        shot_sources.append(shot.get("child_tx_utc_source") or "unmapped")
        tof_values.append(float(row["tof_us"]))
    times, states, measured = map(np.asarray, [times, states, measured])
    valid = np.isin(states, [2, 3]) & np.isfinite(measured) & (measured > 0) & (np.asarray(profile) == 7)
    near_depth = gap_interpolate(ht, fused[:, 0], times, 2)
    tables["range_constraints"] = {"shot_seq": shot_numbers, "utc_s": times,
        "parent_video_s": (times-utc0)/scale, "range_state": states, "range_profile": profile,
        "child_tx_utc_source": shot_sources, "child_clock_tof_us": tof_values,
        "corrected_range_m": measured, "recomputed_profile7_range_m": recomputed,
        "range_valid": valid, "pressure_sensor_depth_m": near_depth,
        "range_minus_pressure_depth_m": measured-near_depth,
        "common_origin_geometry_valid": np.zeros(len(times), bool),
        "constraint_status": np.where(valid, "formula_candidate_direct_arrival_unverified", "invalid_range_state_or_value")}
    for name, table in tables.items():
        write_table(dest / f"{name}.csv", table)
    manifest = {"experiment_id": args.experiment, "created_utc": datetime.now(UTC).isoformat(),
        "git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "config": config, "attitude": attitude_stats, "parent_position": position_stats,
        "pressure_rows": len(ht), "range_rows": len(times), "range_valid_rows": int(valid.sum()),
        "range_state_counts": {str(s): int((states == s).sum()) for s in np.unique(states)},
        "column_semantics": {"range_valid": "Passes firmware state2/3, profile7 and finite-positive arithmetic checks only; usable-formula candidate, not physically validated direct arrival or verified successful ranging.",
                             "range_valid_rows": "Number of usable-formula candidates, including retained multipath/early-return possibilities."},
        "frames": {"attitude": "camera quaternion world gauge; yaw arbitrary; local camera IMU axes",
                   "parent_position": "GNSS shared ENU origin from gnss manifest; no attitude-to-ENU mapping",
                   "depth": "positive down from preimmersion pressure median; pressure sensor center"},
        "assumptions_and_limits": [
            "Camera and gyro complementary filter; no statistically independent attitude covariance claimed.",
            "BNO game quaternion is compared after first-valid-frame gauge and gyro-fitted rotation; not independently weighted into the attitude.",
            "Parent geometric body calibration rejected 5 degree criterion, so bodyFRD is not claimed.",
            "Previous-day child21812 has gravity only, no identifiable body forward/yaw. Not applied.",
            "GNSS CV filter uses IMU only for adaptive process noise and known-static velocity constraint. No geographic accelerometer mechanization.",
            "Quiet IMU cannot detect constant velocity. Zero velocity is confined to known static240–480s with quiet IMU.",
            "PPK is an alternative filter input; shares observations with RTK and is never fused as independent.",
            "Covariances are heuristic model covariances and do not establish absolute accuracy.",
            "Depth CV uses quiet IMU plus small local pressure slope for zero vertical rate; no accelerometer double integration.",
            "Pressure baseline840–900s is selected preimmersion; density997kg/m3 and g9.80665 explicit; baseline/offset error not in model covariance.",
            "ToF uses two child-clock edges; no inter-device clock correction changes the elapsed ToF.",
            "Acoustic range candidates and pressure depth have unverified sensor-center lever arms; no common-origin sphere-plane intersection.",
            "Range firmware state3 and the 200–22000us candidate interval do not validate direct arrival. All402 usable-formula candidates retain possible multipath, early returns and excursions up to8.6m; no physical-success claim is made.",
            "No validated optical bearing/intrinsics. Child geographic xyz remains NaN; pressure depth and range are supported channels.",
            "No ground truth; apparent smoothing/scatter does not establish true trajectory or absolute accuracy."],
        "inputs": [{"path": str(p.relative_to(root)), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in inputs],
        "generator": "scripts/fuse_ngt_sensors.py", "module": "src/aquabeacon_analysis/ngt_fusion.py"}
    (result / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    shutil.copy2(result / "manifest.json", dest / "manifest.json")
    shutil.copy2(__file__, result / "generate.py")
    shutil.copy2(root / "src/aquabeacon_analysis/ngt_fusion.py", result / "ngt_fusion.py")
    config_path = root / "config/20261028_ngt/analysis.json"
    segments = json.loads(config_path.read_text())["segments"]
    for segment in [{"experiment_id": args.experiment, "parent_video_interval_s": [-np.inf, np.inf], "label": "Complete acquisition"}, *segments]:
        out = root / "results" / segment["experiment_id"] / "fusion"
        out.mkdir(parents=True, exist_ok=True)
        interval = segment["parent_video_interval_s"]
        subset = {}
        for name, table in tables.items():
            select = (np.asarray(table["parent_video_s"]) >= interval[0]) & (np.asarray(table["parent_video_s"]) < interval[1])
            subset[name] = {key: np.asarray(value)[select] for key, value in table.items()}
            if segment["experiment_id"] != args.experiment:
                write_table(out / f"{name}.csv", subset[name])
        fig, axes = plt.subplots(3, 1, figsize=(10, 9), layout="constrained")
        pos = subset["parent_position"]
        for axis in ["east", "north", "up"]:
            axes[0].plot(pos["parent_video_s"], pos[f"raw_{axis}_m"], alpha=.25, label=f"RTK {axis}")
            axes[0].plot(pos["parent_video_s"], pos[f"fused_{axis}_m"], label=f"CV {axis}")
        axes[0].set(ylabel="Parent GNSS ENU [m]", title=segment["label"])
        dep, ran = subset["child_depth"], subset["range_constraints"]
        axes[1].plot(dep["parent_video_s"], dep["raw_depth_m"], alpha=.4, label="Pressure depth")
        axes[1].plot(dep["parent_video_s"], dep["fused_depth_m"], label="Pressure + IMU gating")
        ok = ran["range_valid"]
        axes[1].plot(ran["parent_video_s"][ok], ran["corrected_range_m"][ok], ".", ms=2, label="Acoustic range candidate; different centers")
        axes[1].set(ylabel="Depth / range [m]")
        if not len(dep["utc_s"]) and not len(ran["utc_s"]):
            axes[1].text(.5, .5, "No child pressure/range data in this interval",
                         ha="center", va="center", transform=axes[1].transAxes)
            axes[1].set_xlim(interval)
        for role in ["parent", "child"]:
            att = subset[f"{role}_attitude"]
            axes[2].plot(att["parent_video_s"], att["camera_fused_angle_deg"], label=f"{role}: camera versus fused")
        axes[2].set(xlabel="Parent video time [s]", ylabel="Attitude difference [deg]")
        for ax in axes:
            ax.grid(alpha=.25)
            ax.legend(fontsize=7, ncol=3)
        save_editable_figure(fig, out / "fusion_overview.png", dpi=150)
        plt.close(fig)
        fig, axes = plt.subplots(2, 1, figsize=(10, 6), layout="constrained")
        for ax, role in zip(axes, ["parent", "child"], strict=True):
            att = subset[f"{role}_attitude"]
            for angle in ["roll", "pitch", "yaw"]:
                ax.plot(att["parent_video_s"], att[f"relative_{angle}_deg"], label=angle)
            ax.set(ylabel="Initial-local Euler [deg]", title=f"{role}: fused local camera attitude")
            ax.legend()
            ax.grid(alpha=.25)
        axes[1].set_xlabel("Parent video time [s]")
        save_editable_figure(fig, out / "local_attitudes.png", dpi=150)
        plt.close(fig)
        counts = {name: len(table["utc_s"]) for name, table in subset.items()}
        segment_manifest = {"experiment_id": segment["experiment_id"],
                            "parent_video_interval_s": interval if np.isfinite(interval).all() else None,
                            "acquisition_manifest": "../../20261028_ngt_000/fusion/manifest.json",
                            "row_counts": counts, "config": config,
                            "child_world_xyz": "unobservable; NaN, no invented horizontal coordinate"}
        if segment["experiment_id"] != args.experiment:
            (out / "manifest.json").write_text(json.dumps(segment_manifest, indent=2), encoding="utf-8")
        report = f"# {segment['label']} — sensor fusion\n\n"
        report += "親機位置はRTKのENU座標を等速モデルで平滑化し、IMU静穏度でプロセス雑音を変更した。既知の静止区間240–480秒だけ、IMUが静穏な時刻にゼロ速度を与えた。PPKは相関した別解として別CSVに保存し、RTKと独立観測として合成していない。3秒超の欠測後は状態を再初期化する。\n\n"
        report += "姿勢は同じ実験のジャイロをカメラIMU軸へ変換して積分し、カメラ四元数で0.5秒時定数の補正を行った。IMUギャップを跨がずカメラ値へ戻す。ローカルなカメラ姿勢であり、ENU方位や校正済み機体FRDではない。子機BNOゲーム四元数は初期姿勢を合わせた比較値で、共有ジャイロとの独立性を仮定して再合成していない。\n\n"
        report += f"全体のBNO対カメラ・ジャイロ融合姿勢の角度差は中央値{attitude_stats['child']['bno_aligned_disagreement_median_deg']:.2f}°、95パーセンタイル{attitude_stats['child']['bno_aligned_disagreement_p95_deg']:.2f}°。初期姿勢を合わせた後も約35°の差が残る。フレーム定義・取付回転・センサ内部推定の対応が未解決であり、Ground Truthがないため一方の真値誤差とは解釈しない。\n\n"
        report += f"子機深度は親動画840–900秒の圧力中央値{baseline:.3f} mbarを基準に、密度997 kg/m³、g=9.80665 m/s²で求めた。圧力の等速フィルタにIMU静穏度を使い、圧力勾配も小さい場合のみ鉛直速度ゼロを与える。深度は圧力センサ位置、測距は音響中心間距離であり、測定中心の校正がないため球面と水深の交線や水平距離を算出しない。\n\n"
        report += "光源は曖昧で内部標定も不足するため方位観測を採用していない。子機の絶対XYZはNaNで明示した。前日ログの重力方向だけでは前方軸・yawを観測できず、本体軸変換に使用していない。Ground Truthはなく、平滑化後の分散やモデル共分散は絶対精度を表さない。\n\n"
        report += f"全測距状態を保持した。全体の状態別件数: {manifest['range_state_counts']}。`range_valid`の402行はファームウェア状態・profile7・有限正値の算術条件を満たす式適用候補で、検証済みの測距成功や直接波ではない。状態3や200–22000µsの候補範囲だけでは物理的妥当性を確定できず、マルチパス・早期検出の可能性と最大約8.6mの変動も保持した。この区間の行数: {counts}。\n\n"
        report += "生成コード・入力ハッシュ・閾値・座標系は[全体manifest](../../20261028_ngt_000/fusion/manifest.json)。全体CSVは`data/processed/20261028_ngt_000/fusion/`、各区間CSVはこのフォルダ。図はPNG/SVG/pickle/plot.jsonを保存した。ブラウザ編集は `python -m aquabeacon_analysis.editable_plots <figure.figure.pickle>` で起動でき、設定保存とPNG/SVG/PDF出力に対応する。\n"
        (out / "report.md").write_text(report, encoding="utf-8")
    print(json.dumps({"attitude": attitude_stats, "position": position_stats, "pressure_baseline_mbar": baseline, "range_valid": int(valid.sum())}), flush=True)


if __name__ == "__main__":
    main()
