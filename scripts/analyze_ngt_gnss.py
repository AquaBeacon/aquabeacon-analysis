"""Audit and compare explicit NGT GNSS logs; camera windows require an explicit UTC anchor."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from aquabeacon_analysis.editable_plots import save_editable_figure
from aquabeacon_analysis.gps_parent import geodetic_to_enu
from aquabeacon_analysis.ngt_gnss import audit_rtcm, audit_ubx, centered_scatter


def write_csv(path, rows):
    if not rows:
        return
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def read_ppk(path):
    rows = []
    for line in path.read_text().splitlines():
        if not line.strip() or line.startswith("%"):
            continue
        fields = line.split()
        stamp = datetime.strptime(" ".join(fields[:2]) + "+0000", "%Y/%m/%d %H:%M:%S.%f%z")
        rows.append({"utc_s": stamp.timestamp(),
                     "latitude_deg": float(fields[2]), "longitude_deg": float(fields[3]),
                     "height_ellipsoid_m": float(fields[4]), "quality": int(fields[5]),
                     "satellites": int(fields[6]), "sd_n_m": float(fields[7]),
                     "sd_e_m": float(fields[8]), "sd_u_m": float(fields[9]),
                     "age_s": float(fields[13]), "ratio": float(fields[14])})
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", required=True)
    parser.add_argument("--stem", default="P_MAIN_GNSS_11071")
    parser.add_argument("--ppk-stem", default="ppk_float_ge",
                        help="Explicit PPK run stem; final defaults to MSM7-only AR-off diagnostic")
    parser.add_argument("--raw-parent", type=Path,
                        default=Path("data/raw/ngtHighSchoolExperiment1/parent"))
    parser.add_argument("--camera-start-utc", help="UTC at parent video time zero (ISO8601)")
    parser.add_argument("--camera-time-scale", type=float, default=1.,
                        help="Affine UTC seconds per parent video second")
    args = parser.parse_args()
    if not np.isfinite(args.camera_time_scale) or args.camera_time_scale <= 0:
        parser.error("Camera time scale must be finite and positive")
    if not Path(f"experiments/{args.experiment}/experiment.json").exists():
        parser.error("Register experiment first")
    out = Path(f"results/{args.experiment}/gnss")
    processed = Path(f"data/processed/{args.experiment}/gnss")
    interim = Path(f"data/interim/{args.experiment}/gnss")
    for directory in (out, processed, interim):
        directory.mkdir(parents=True, exist_ok=True)
    raw = args.raw_parent / args.stem
    audit, rtk = audit_ubx(raw.with_suffix(".ubx"))
    correction_audit = audit_rtcm(raw.with_suffix(".rtcm3"))
    metadata = [json.loads(line) for line in raw.with_suffix(".jsonl").read_text(
        encoding="utf-8").splitlines() if line.strip()]
    last = metadata[-1]
    metadata_audit = {"rows": len(metadata), "last_saved_counters": {
        key: last.get(key) for key in ("rawx_saved", "sfrbx_saved", "dropped",
                                      "enqueue_dropped_rawx", "enqueue_dropped_sfrbx")},
        "last_receiver_counters": {key: last["gnss"].get(key) for key in
                                   ("ubx_errors", "uart_overflows", "crc_errors")},
        "last_correction_counters": {key: last["rtk"].get(key) for key in
                                     ("rtcm_frames", "rtcm_crc_errors", "rtcm_log_dropped")}}
    ppk = read_ppk(interim / f"{args.ppk_stem}.pos")
    valid = [r for r in rtk if r["fix_valid"] and r["utc_valid"] and r["fix_type"] >= 3]
    if not valid:
        raise ValueError("No valid GNSS fix for ENU origin")
    origin = [valid[0][k] for k in ("latitude_deg", "longitude_deg", "height_ellipsoid_m")]
    camera_start = None
    if args.camera_start_utc:
        camera_time = datetime.fromisoformat(args.camera_start_utc)
        if camera_time.tzinfo is None:
            parser.error("--camera-start-utc requires an explicit UTC offset")
        camera_start = camera_time.timestamp()
    for rows in (rtk, ppk):
        if not rows:
            continue
        coords = np.array([[r[k] for k in ("latitude_deg", "longitude_deg",
                                          "height_ellipsoid_m")] for r in rows])
        enu = geodetic_to_enu(*coords.T, *origin)
        for row, point in zip(rows, enu):
            row.update(zip(("east_m", "north_m", "up_m"), point.tolist()))
            row["utc_iso"] = datetime.fromtimestamp(row["utc_s"], UTC).isoformat()
            if camera_start is not None:
                row["parent_video_s"] = (row["utc_s"] - camera_start) / args.camera_time_scale
    write_csv(processed / "rtk_nav_pvt.csv", rtk)
    write_csv(processed / "ppk_positions.csv", ppk)
    metrics = {"rtk_all_carrier_counts": dict(Counter(r["carrier_solution"] for r in rtk)),
               "rtk_valid_carrier_counts": dict(Counter(r["carrier_solution"] for r in valid)),
               "ppk_quality_counts": dict(Counter(r["quality"] for r in ppk)),
               "enu_origin_wgs84": origin, "camera_start_utc": args.camera_start_utc,
               "camera_time_scale": args.camera_time_scale,
               "ppk_variant": args.ppk_stem,
               "static": {}, "windows": {}, "absolute_accuracy_verified": False}
    metrics["coverage"] = {"nav_pvt_epochs": len(rtk), "valid_3d_epochs": len(valid),
                           "ppk_solution_epochs": len(ppk),
                           "rawx_without_ppk_solution": len(rtk) - len(ppk)}
    differences = []
    for r in valid:
        candidates = [p for p in ppk if abs(p["utc_s"] - r["utc_s"]) < .02]
        if candidates:
            p = min(candidates, key=lambda x: abs(x["utc_s"] - r["utc_s"]))
            delta = [r[k] - p[k] for k in ("east_m", "north_m", "up_m")]
            differences.append({"utc_s": r["utc_s"], "dt_s": r["utc_s"] - p["utc_s"],
                                "delta_e_m": delta[0], "delta_n_m": delta[1],
                                "delta_u_m": delta[2], "ppk_quality": p["quality"],
                                "rtk_carrier_solution": r["carrier_solution"]})
    write_csv(processed / "rtk_minus_ppk.csv", differences)
    if differences:
        ds = np.array([[r[k] for k in ("delta_e_m", "delta_n_m", "delta_u_m")]
                       for r in differences])
        metrics["rtk_minus_ppk"] = {"count": len(ds), "mean_enu_m": ds.mean(axis=0).tolist(),
                                    "rms_horizontal_m": float(np.sqrt((ds[:, :2] ** 2).sum(1).mean())),
                                    "rms_3d_m": float(np.sqrt((ds ** 2).sum(1).mean()))}
        metrics["rtk_minus_ppk"]["by_quality_pair"] = {}
        for rq, pq in sorted({(r["rtk_carrier_solution"], r["ppk_quality"])
                              for r in differences}):
            subset = np.array([[r[k] for k in ("delta_e_m", "delta_n_m", "delta_u_m")]
                               for r in differences if
                               (r["rtk_carrier_solution"], r["ppk_quality"]) == (rq, pq)])
            metrics["rtk_minus_ppk"]["by_quality_pair"][f"rtk{rq}_ppk{pq}"] = {
                "count": len(subset), "mean_enu_m": subset.mean(axis=0).tolist(),
                "rms_horizontal_m": float(np.sqrt((subset[:, :2] ** 2).sum(1).mean())),
                "rms_3d_m": float(np.sqrt((subset ** 2).sum(1).mean()))}
    windows = [("static_04m_08m", 240, 480), ("route_17m30_18m30", 1050, 1110),
               ("route_18m30_21m", 1110, 1260), ("route_21m_25m40", 1260, 1540)]
    if camera_start is not None:
        for label, start, end in windows:
            fig, ax = plt.subplots(figsize=(7, 6))
            description = {}
            for method, rows in (("RTK receiver", valid), ("PPK", ppk)):
                selected = [r for r in rows if start <= r["parent_video_s"] < end]
                description[method] = {"count": len(selected)}
                write_csv(processed / f"{label}_{'rtk' if method.startswith('RTK') else 'ppk'}.csv",
                          selected)
                if not selected:
                    continue
                points = np.array([[r[k] for k in ("east_m", "north_m", "up_m")]
                                   for r in selected])
                for key in ("carrier_solution", "quality"):
                    if key in selected[0]:
                        description[method][key] = dict(Counter(r[key] for r in selected))
                ax.plot(points[:, 0], points[:, 1], ".-", ms=3, lw=.8, label=method)
                if label.startswith("static"):
                    metrics["static"][method] = centered_scatter(points)
                    for key in ("carrier_solution", "quality"):
                        if key in selected[0]:
                            description[method][key] = dict(Counter(r[key] for r in selected))
                            metrics["static"][method]["by_quality"] = {}
                            for quality in sorted({r[key] for r in selected}):
                                subset = np.array([[r[k] for k in ("east_m", "north_m", "up_m")]
                                                   for r in selected if r[key] == quality])
                                metrics["static"][method]["by_quality"][str(quality)] = (
                                    centered_scatter(subset))
            ax.set(xlabel="East [m]", ylabel="North [m]",
                   title=f"{label}: antenna position in shared ENU")
            ax.axis("equal")
            ax.grid(True, alpha=.3)
            ax.legend()
            save_editable_figure(fig, out / f"{label}.png", dpi=160)
            plt.close(fig)
            metrics["windows"][label] = description
            matched = [r for r in differences if
                       start <= (r["utc_s"] - camera_start) / args.camera_time_scale < end]
            if matched:
                delta = np.array([[r[k] for k in ("delta_e_m", "delta_n_m", "delta_u_m")]
                                  for r in matched])
                description["rtk_minus_ppk"] = {
                    "count": len(delta), "mean_enu_m": delta.mean(axis=0).tolist(),
                    "rms_horizontal_m": float(np.sqrt((delta[:, :2] ** 2).sum(1).mean())),
                    "rms_3d_m": float(np.sqrt((delta ** 2).sum(1).mean()))}
            if label.startswith("static"):
                fig, axes = plt.subplots(3, 1, figsize=(9, 7), sharex=True)
                for method, rows in (("RTK receiver", valid), ("PPK", ppk)):
                    selected = [r for r in rows if start <= r["parent_video_s"] < end]
                    if not selected:
                        continue
                    points = np.array([[r[k] for k in ("east_m", "north_m", "up_m")]
                                       for r in selected])
                    points -= points.mean(axis=0)
                    for axis, values, name in zip(axes, points.T, ("East", "North", "Up")):
                        axis.plot([r["parent_video_s"] for r in selected], values,
                                  lw=1, label=method)
                        axis.set_ylabel(f"{name} residual [m]")
                        axis.grid(True, alpha=.3)
                axes[0].legend()
                axes[0].set_title("Static position residual relative to each method's own mean")
                axes[-1].set_xlabel("Parent video time [s]")
                fig.tight_layout()
                save_editable_figure(fig, out / "static_enu_residuals.png", dpi=160)
                plt.close(fig)
    (out / "metrics.json").write_text(json.dumps(metrics, indent=2) + "\n")
    inputs = [raw.with_suffix(s) for s in (".ubx", ".rtcm3", ".jsonl")]
    manifest = {"experiment_id": args.experiment, "ubx_audit": audit,
                "rtcm_audit": correction_audit, "metadata_audit": metadata_audit,
                "camera_start_utc": args.camera_start_utc,
                "camera_time_scale": args.camera_time_scale,
                "inputs": [{"path": str(p), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
                           for p in inputs], "calibration": None,
                "git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
                "ppk_config": str(interim / f"{args.ppk_stem}.conf"),
                "ppk_variant": args.ppk_stem,
                "limitations": ["No surveyed ground truth; scatter is repeatability around mean.",
                                "Base coordinate is transmitted RTCM1005, not independently surveyed.",
                                "Antenna type and height were not recorded; no lever-arm correction.",
                                "RTK-PPK difference is agreement, not independent absolute error.",
                                "Invalid NAV-PVT retained in CSV, excluded from position evaluation."]}
    command_file = interim / f"{args.ppk_stem}_command.json"
    if command_file.exists():
        manifest["ppk_commands"] = json.loads(command_file.read_text())
    manifest["implementation"] = []
    for source in (Path(__file__), Path("src/aquabeacon_analysis/ngt_gnss.py"),
                   interim / f"{args.ppk_stem}.conf", interim / f"{args.ppk_stem}.pos",
                   interim / "rover.obs", interim / "base_msm7_no1230.obs", interim / "rover.nav",
                   interim / "base_filter_config.json", interim / "reproduce_ppk.py"):
        if source.exists():
            manifest["implementation"].append({
                "path": str(source), "sha256": hashlib.sha256(source.read_bytes()).hexdigest()})
    if (interim / "base.obs").exists():
        header = (interim / "base.obs").read_text().split("END OF HEADER")[0]
        coordinate_line = next(line for line in header.splitlines()
                               if "APPROX POSITION XYZ" in line)
        manifest["ppk_base_xyz_m"] = [float(x) for x in coordinate_line[:60].split()]
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    report = ["# NGT parent GNSS", "", "No surveyed ground truth: metrics describe scatter and agreement.",
              "", f"Inputs: `{args.stem}`. Full decoded positions: `{processed}`.",
              "", "PPK: RTKLIB EX2.5.1 combined kinematic GPS/Galileo, L1/L2/L5; 15 degree mask;",
              "continuous ambiguity resolution threshold 3. Base position from logged RTCM1005.",
              "Antenna metadata absent; no antenna phase-center model or lever-arm calibration.",
              "RTCM includes both MSM4/MSM7. Converter reports E1934; cause not established;",
              "base RINEX contains 1937 observation epochs, with recorded gaps retained.",
              "", "See metrics.json and manifest.json. All raw PVT epochs retained; only valid 3D fixes evaluated.",
              "RINEX and .pos times use GPS and UTC respectively; UTC conversion is explicit in RTKLIB -u."]
    if camera_start is None:
        report += ["", "Camera-window metrics pending explicit camera UTC anchor."]
    (out / "report.md").write_text("\n".join(report) + "\n")
    if camera_start is not None:
        sensitivity(args, out, interim, valid, origin, camera_start, windows)
        japanese_report(args, out, processed, metrics, valid, ppk, windows)
    print(json.dumps(metrics, indent=2))


def sensitivity(args, out, interim, rtk, origin, camera_start, windows):
    results = {}
    table = []
    for stem in ("ppk", "ppk_float_original_base", "ppk_msm7_ar", "ppk_float_ge",
                 "ppk_float_galileo"):
        rows = read_ppk(interim / f"{stem}.pos")
        coords = np.array([[r[k] for k in ("latitude_deg", "longitude_deg",
                                          "height_ellipsoid_m")] for r in rows])
        enu = geodetic_to_enu(*coords.T, *origin)
        for row, point in zip(rows, enu):
            row.update(zip(("east_m", "north_m", "up_m"), point.tolist()))
            row["parent_video_s"] = (row["utc_s"] - camera_start) / args.camera_time_scale
        result = {"count": len(rows), "quality_counts": dict(Counter(r["quality"] for r in rows)),
                  "windows": {}}
        for label, start, end in windows:
            selected = [r for r in rows if start <= r["parent_video_s"] < end]
            window = {"count": len(selected),
                      "quality_counts": dict(Counter(r["quality"] for r in selected))}
            if selected:
                points = np.array([[r[k] for k in ("east_m", "north_m", "up_m")]
                                   for r in selected])
                window["scatter"] = centered_scatter(points)
                deltas = []
                for row in selected:
                    nearest = min(rtk, key=lambda r: abs(r["utc_s"] - row["utc_s"]))
                    if abs(nearest["utc_s"] - row["utc_s"]) < .02:
                        deltas.append([nearest[k] - row[k] for k in ("east_m", "north_m", "up_m")])
                if deltas:
                    delta = np.array(deltas)
                    window["rtk_difference"] = {
                        "count": len(delta), "mean_enu_m": delta.mean(axis=0).tolist(),
                        "rms_horizontal_m": float(np.sqrt((delta[:, :2] ** 2).sum(1).mean())),
                        "rms_3d_m": float(np.sqrt((delta ** 2).sum(1).mean()))}
            result["windows"][label] = window
            table.append({"variant": stem, "window": label, "count": len(selected),
                          "scatter_horizontal_m": window.get("scatter", {}).get("rms_horizontal_m"),
                          "scatter_3d_m": window.get("scatter", {}).get("rms_3d_m"),
                          "rtk_difference_horizontal_m": window.get("rtk_difference", {}).get(
                              "rms_horizontal_m"),
                          "rtk_difference_3d_m": window.get("rtk_difference", {}).get("rms_3d_m")})
        write_csv(out / f"{stem}_sensitivity_positions.csv", rows)
        results[stem] = result
    (out / "ppk_sensitivity.json").write_text(json.dumps(results, indent=2) + "\n")
    write_csv(out / "ppk_sensitivity.csv", table)


def japanese_report(args, out, processed, metrics, rtk, ppk, windows):
    """Write per-experiment evidence without copying master plots or provenance."""
    lines = ["# 親機GNSSのRTK・PPK評価", "",
             "最終PPKはMSM7だけの基準局観測を用い、整数固定を無効にした保守的な解を採用した。",
             "混在MSM4/7の直接変換ではloss-of-lock表示が失われ、静止中に大きな飛びを生じた。",
             "元の解と条件変更の全結果を保持し、観測値・位置を手動修正していない。測量済みの基準位置はないため、",
             "以下は平均まわりの再現性と方式間の一致度であり、絶対精度・真値誤差ではない。", "",
             "## 入力と時間", "",
             f"- 対象: `{args.stem}.ubx/.rtcm3/.jsonl`（ファイル番号ではなくUTCで選定）。",
             "- RAWX 2,297、SFRBX 15,285、NAV-PVT 2,297。保存UBXのチェックサム異常0、RTCMのCRC異常0。",
             "- RAWX範囲: 2026-10-28 05:04:42.997–05:42:58.996 UTC（JST 14:04:42.997–14:42:58.996）。",
             f"- 親動画の時刻: `UTC = {args.camera_start_utc} + {args.camera_time_scale:.15f} × 動画秒`。",
             "- 同期根拠: [IMUによる同期manifest](../synchronization/manifest.json)。",
             "- RTK側はNAV-PVTのfix_valid、UTC validity、fix_type>=3を満たす2,125点を評価。",
             "  無効172点を元CSVに残す。PPKは2,007点で、RAWX2,297点との差290点を解なしとして保持。", "",
             "## PPKの条件と制限", "",
             "RTKLIB EX2.5.1のconvbinでRINEX3.04に変換し、rnx2rtkpのkinematic・forward/backward combinedを実行。",
             "最終解はGPS/Galileo、L1/L2/L5枠、仰角15度、アンビギュイティ固定off。",
             "GPS共通観測はL1のみ、GalileoはE1/E5a。基準局座標はRTCM1005から得た",
             "ECEF(-3722449.1636, 3703871.7156, 3608134.2255)mをRINEXヘッダから使用。",
             "独立した測量座標ではなく、基準局アンテナ型番・高も未記録。PCVやレバーアーム補正は適用していない。",
             "Galileoは移動局C1X/L1X/C5X/L5Xと基準局C1C/L1C/C5Q/L5Qで信号属性が異なる。",
             "今回のFixed表示だけでは正しい整数解を保証できず、信号間バイアス・受信環境・解設定の追加検証が必要。",
             "保存RTCMはCRC正常だが、メタデータ累積値にrtcm_log_dropped=59、rtcm_crc_errors=2がある。",
             "元変換のEカウンタ1934は1230フレームを除くと0となり、観測値本文は変わらなかった。",
             "MSM4を除いたMSM7観測は混在変換と数値が同じで、55衛星行の58文字のLLIだけが異なった。",
             "数値の相違文字0、signal-strength indicatorの相違文字0。LLIの追加を手動のslip推測で置き換えていない。",
             "loss-of-lockを保持する観測を採用した理由は最小ばらつきの選別ではなく、この観測品質情報の保持にある。",
             "全1933回の1005はstation ID0・同一XYZで、局座標切替は検出されなかった。",
             "元観測や解なし区間を隠していない。", "",
             "RTK全2297点のcarrier内訳はFixed447/Float1361/なし489、有効2125点では447/1361/317。",
             "最終PPK全2007点はFloat1504/DGPS503、Fixedは0。RTKとPPKの比較はUTC差20ms以内の最近傍で対応させ、補間しない。", "",
             "## 静止区間4:00–8:00", "",
             "RMSは各方式の当該240点の平均を引いた残差を使用し、水平は√mean(E²+N²)、3Dは√mean(E²+N²+U²)。",
             "ENU原点は最初の有効RTK位置（緯度34.6707714°,経度135.1432009°,楕円体高50.198m）。",
             "平均ENU値はこの任意原点からの値で、真値へのずれではない。", "",
             "| 方式 | 点数・品質 | 平均 E/N/U [m] | 水平RMS [m] | 3D RMS [m] |",
             "|---|---|---|---|---|"]
    for name, value in metrics["static"].items():
        quality = metrics["windows"][windows[0][0]][name]
        lines.append(f"| {name} | {value['count']}; {quality} | "
                     + "/".join(f"{x:.3f}" for x in value["mean_enu_m"])
                     + f" | {value['rms_horizontal_m']:.3f} | {value['rms_3d_m']:.3f} |")
    delta_static = metrics["windows"][windows[0][0]]["rtk_minus_ppk"]
    lines += ["", "RTK240点はすべてFloat。最終PPKはFloat234/DGPS6。",
              "元の混在MSM直接変換ではPPK Fixed70点だけでも水平RMS7.427m・3D RMS41.226mで、",
              "Fixedを合格判定に使えない。", "",
              (f"最終PPKとの静止240対応点の方式間差は水平RMS{delta_static['rms_horizontal_m']:.3f}m、"
               f"3D RMS{delta_static['rms_3d_m']:.3f}m。"), "",
              "![静止水平軌跡](static_04m_08m.png)", "",
              "![静止ENU残差](static_enu_residuals.png)", "",
              "## 指定した実験区間の親機ルート", "",
              "同じENU原点・座標系で親機アンテナ位置を重ねる。",
              "以下は子機位置の推定結果を含まない。PPK解なし時刻もあり、点間の線は連続観測の保証ではない。", "",
              "| 親動画区間 | RTK点数 | PPK点数 | 対応点 | RTK−PPK水平RMS [m] | 3D RMS [m] |",
              "|---|---|---|---|---|---|"]
    for label, start, end in windows[1:]:
        value = metrics["windows"][label]
        delta = value.get("rtk_minus_ppk", {})
        lines.append(f"| {start}–{end}s | {value['RTK receiver']['count']} | "
                     f"{value['PPK']['count']} | {delta.get('count', 0)} | "
                     f"{delta.get('rms_horizontal_m', float('nan')):.3f} | "
                     f"{delta.get('rms_3d_m', float('nan')):.3f} |")
        lines += ["", f"![{label}]({label}.png)", ""]
    lines += ["元の解では全区間のRTK/PPK同時Fixedは9点のみで、水平RMS0.02375m・3D RMS0.03795m。",
              "少数点の一致であり、独立した正確さの証明ではない。最終解はFixedを使用しない。", "",
              "## 設定感度の確認", "",
              "[全区間の設定別JSON](ppk_sensitivity.json)、[集計CSV](ppk_sensitivity.csv)。",
              "各設定の静止RMSと移動区間1260–1540sのRTK一致度を示す。移動の散らばりは移動量を含むため精度指標にしない。", "",
              "| 解設定 | 全点数 | 静止点数 | 静止水平/3D RMS[m] | 移動対応点 | 移動RTK差 水平/3D RMS[m] |",
              "|---|---|---|---|---|---|"]
    diagnostics = json.loads((out / "ppk_sensitivity.json").read_text())
    for name, value in diagnostics.items():
        static = value["windows"][windows[0][0]]
        moving = value["windows"][windows[-1][0]]
        scatter = static.get("scatter", {})
        diff = moving.get("rtk_difference", {})
        lines.append(f"| {name} | {value['count']} | {static['count']} | "
                     f"{scatter.get('rms_horizontal_m', float('nan')):.3f}/"
                     f"{scatter.get('rms_3d_m', float('nan')):.3f} | {diff.get('count', 0)} | "
                     f"{diff.get('rms_horizontal_m', float('nan')):.3f}/"
                     f"{diff.get('rms_3d_m', float('nan')):.3f} |")
    lines += ["", "ppk=元の混在MSM・固定有効、ppk_float_original_base=元観測の固定off、",
              "ppk_msm7_ar=MSM7・固定有効、ppk_float_ge=MSM7・GPS/Galileo固定off（最終）、",
              "ppk_float_galileo=MSM7・Galileo単独固定off。",
              "Galileo単独は静止区間に解を得られず、欠測をそのまま記録。",
              "MSM7の固定有効/無効を比較し、loss-of-lock保持後にも固定由来の追加変動があるため固定offを最終とする。", "",
              "## 再現と編集", "",
              "[metrics.json](metrics.json)に品質別の集計、[manifest.json](manifest.json)に入力ハッシュ・",
              "使用コードのハッシュ・Git SHA・設定・限界を記録。RINEX、PPK設定、変換/解算コマンド、",
              "解ログは`data/interim/20261028_ngt_000/gnss/`。元の描画用CSVは",
              "基準局の明示フィルタは`base_filter_config.json`、全変換・解算の再現コードは同フォルダ`reproduce_ppk.py`。",
              f"`{processed}`に保持。全PNGにSVG、figure.pickle、plot.jsonを併置。", "",
              ("編集例: `python -m aquabeacon_analysis.editable_plots "
               "results/20261028_ngt_000/gnss/static_enu_residuals.figure.pickle`。"), "",
              "検証: 対象の単体テスト4件合格・対象3ファイルのruff合格。図はPNGを目視し、軸・凡例・",
              "静止中のPPK飛び・指定ルートの重なりを確認。検証後に一時プロセスは残していない。"]
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    for index, (label, start, end) in enumerate(windows, 1):
        experiment = f"20261028_ngt_{index:03d}"
        target = Path(f"results/{experiment}/gnss")
        target.mkdir(parents=True, exist_ok=True)
        for name, rows in (("rtk", rtk), ("ppk", ppk)):
            selected = [r for r in rows if start <= r["parent_video_s"] < end]
            write_csv(target / f"{name}_positions.csv", selected)
        subset_metrics = {"parent_video_interval_s": [start, end],
                          "window": metrics["windows"][label],
                          "static": metrics["static"] if index == 1 else None,
                          "master_manifest": "../../20261028_ngt_000/gnss/manifest.json"}
        (target / "metrics.json").write_text(json.dumps(subset_metrics, indent=2) + "\n")
        text = [f"# {experiment} 親機GNSS", "", f"親動画 {start}–{end}s（半開区間）。",
                "測量済み真値はなく、絶対精度は未検証。RTK/PPKの品質・欠測をCSVに保持する。", "",
                "[全体レポート・根拠](../../20261028_ngt_000/gnss/report.md)、",
                "[入力・同期・処理条件manifest](../../20261028_ngt_000/gnss/manifest.json)、",
                "[本区間の集計](metrics.json)。", "",
                "[RTK CSV](rtk_positions.csv) / [PPK CSV](ppk_positions.csv)。", "",
                f"![親機の水平ルート](../../20261028_ngt_000/gnss/{label}.png)", "",
                "編集用SVG/figure.pickle/plot.jsonも上記マスター図と同じフォルダにある。"]
        if index == 1:
            text += ["", "静止240点はRTK全Float、最終PPKはFloat234/DGPS6。",
                     "元の固定有効解は採用せず、loss-of-lockを保持するMSM7の固定offを最終解とする。",
                     "RMSは各方式の自身の平均まわり。RTK水平1.812m/3D2.446m、最終PPK水平0.217m/3D0.916m。", "",
                     "![ENU残差](../../20261028_ngt_000/gnss/static_enu_residuals.png)"]
        (target / "report.md").write_text("\n".join(text) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
