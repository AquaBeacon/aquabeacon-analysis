"""Evaluate explicit range/IMU/depth and ray/IMU/depth fallback position runs."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import subprocess
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

os.environ.setdefault("OMP_NUM_THREADS", "2")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "2")
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from position_fallback_nanyo import load_nanyo
from position_fallback_ngt import load_ngt

from aquabeacon_analysis.editable_plots import PlotDocument, save_editable_figure
from aquabeacon_analysis.ngt_binary import sha256
from aquabeacon_analysis.position_fallback import run_fallback

ROOT = Path(__file__).resolve().parents[1]
REVIEW = "20261103_field_review_000"
METHODS = ("joint", "acoustic_imu_depth", "optical_imu_depth", "selected")
LABELS = {"joint": "従来の音響＋光学＋姿勢", "acoustic_imu_depth": "音響L＋IMU＋深度（方向履歴）",
          "optical_imu_depth": "光学＋IMU＋深度", "selected": "フォールバック込み"}
COLORS = {"joint": "#677680", "acoustic_imu_depth": "#c06a26",
          "optical_imu_depth": "#007fa3", "selected": "#19845a"}
ARGUMENTS = ("times", "joint_positions", "rays", "cameras", "transmitters", "light_offsets",
             "acoustic_offsets", "pressure_offsets", "ranges", "depths", "surface_reference_z")


def clean(value):
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, np.ndarray)):
        return [clean(v) for v in value]
    if isinstance(value, (float, np.floating)):
        return float(value) if np.isfinite(value) else None
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, np.integer):
        return int(value)
    return value


def dump(path, value):
    path.write_text(json.dumps(clean(value), ensure_ascii=False, indent=2, allow_nan=False),
                    encoding="utf-8")


def write_csv(path, rows):
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = list(dict.fromkeys(k for row in rows for k in row))
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fields)
        writer.writeheader()
        writer.writerows(rows)


def finite(x):
    return np.isfinite(x).all(axis=1)


def scatter(x):
    """Population RMS about each method's own mean; missing is never zero."""
    x = np.asarray(x, dtype=float).reshape(-1, 3)
    x = x[finite(x)]
    if len(x) < 2:
        return {"samples": len(x), "rms_3d_m": None, "horizontal_rms_m": None,
                **{f"rms_{a}_m": None for a in ("east", "north", "up")}}
    variance = np.mean((x - x.mean(axis=0)) ** 2, axis=0)
    return {"samples": len(x), "rms_3d_m": float(np.sqrt(variance.sum())),
            "horizontal_rms_m": float(np.sqrt(variance[:2].sum())),
            **{f"rms_{a}_m": float(np.sqrt(v)) for a, v in zip(("east", "north", "up"), variance)}}


def angular_rms(rays):
    rays = rays[finite(rays)]
    rays = rays[np.linalg.norm(rays, axis=1) > 1e-12]
    if len(rays) < 2:
        return None
    rays = rays / np.linalg.norm(rays, axis=1)[:, None]
    center = rays.mean(axis=0)
    if np.linalg.norm(center) < 1e-10:
        return None
    center /= np.linalg.norm(center)
    angles = np.arctan2(np.linalg.norm(np.cross(rays, center), axis=1), rays @ center)
    return float(np.degrees(np.sqrt(np.mean(angles ** 2))))


def longest_grid_gap(times, valid, lo, hi, hz):
    # Include empty leading/trailing periods, including the pre-ranging 14 seconds.
    count = round((hi - lo) * hz)
    occupied = np.zeros(count, dtype=bool)
    # Anchor cells to the first actual grid sample, avoiding accumulated epoch
    # np.arange roundoff without shifting a nonzero native grid phase.
    first = np.floor((times[0] - lo) * hz + 1e-4) if len(times) else 0
    slots = np.rint((times[valid] - times[0]) * hz).astype(int) + int(first) if len(times) else []
    slots = np.asarray(slots, dtype=int)
    slots = slots[(slots >= 0) & (slots < count)]
    occupied[slots] = True
    edges = np.diff(np.r_[False, ~occupied, False].astype(int))
    starts, ends = np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)
    return float(np.max(ends - starts) / hz) if len(starts) else 0.0


def evaluate_window(data, result, window):
    lo, hi = window["start_s"], window["end_s"]
    t = data["times"]
    inside = (t >= lo) & (t < hi)
    events = inside & np.array([r["event_sample"] for r in data["records"]])
    grid = inside & np.array([r["grid_sample"] for r in data["records"]])
    common = events & np.logical_and.reduce([finite(result[m]) for m in METHODS[:3]])
    rows = []
    for sampling, subset in (("all_events", events), ("common_events", common), ("all_grid", grid)):
        for method in METHODS:
            rows.append({"window": window["id"], "experiment_id": data["metadata"]["experiment_id"],
                         "condition": window["condition"], "sampling": sampling, "method": method,
                         "recorded_samples": int(subset.sum()), **scatter(result[method][subset])})
    sensor_rows = []
    ranges = data["ranges"][events]
    ranges = ranges[np.isfinite(ranges)]
    sensor_rows.append({"window": window["id"], "experiment_id": data["metadata"]["experiment_id"],
                        "condition": window["condition"], "sensor": "acoustic_range",
                        "sampling": "all_events", "samples": len(ranges), "unit": "m",
                        "centered_rms": float(ranges.std()) if len(ranges) >= 2 else None})
    for key, label in (("camera_rays", "optical_camera_bearing"), ("rays", "optical_world_bearing")):
        rays = data.get(key, np.full_like(data["rays"], np.nan))[grid]
        sensor_rows.append({"window": window["id"], "experiment_id": data["metadata"]["experiment_id"],
                            "condition": window["condition"], "sensor": label, "sampling": "all_grid",
                            "samples": int(finite(rays).sum()), "unit": "deg",
                            "centered_rms": angular_rms(rays)})
    expected = round((hi - lo) * data["metadata"]["grid_hz"])
    coverage = {"window": window["id"], "experiment_id": data["metadata"]["experiment_id"],
                "condition": window["condition"], "expected_grid_samples": expected}
    for method in METHODS:
        valid = grid & finite(result[method])
        coverage[f"{method}_samples"] = int(valid.sum())
        coverage[f"{method}_percent"] = 100 * valid.sum() / expected
        coverage[f"{method}_longest_gap_s"] = longest_grid_gap(
            t[grid], finite(result[method])[grid], lo, hi, data["metadata"]["grid_hz"])
    return rows, sensor_rows, coverage, inside, common


def output_rows(data, result):
    rows = []
    for i, original in enumerate(data["records"]):
        row = {**original, "range_m": data["ranges"][i], "pressure_depth_m": data["depths"][i],
               "position_available": bool(finite(result["selected"][i:i+1])[0]),
               "position_method": result["method"][i], "accuracy_validated": False}
        for method in METHODS:
            row[f"{method}_available"] = bool(np.isfinite(result[method][i]).all())
            for axis, value in zip(("east", "north", "up"), result[method][i]):
                row[f"{method}_{axis}_m"] = value
        for key, value in result.items():
            if key not in METHODS and key != "method":
                row[key] = value[i]
        for axis, value in zip(("east", "north", "up"), result["selected"][i]):
            row[f"relative_{axis}_m"] = value
        parent = data.get("parent_origin")
        absolute = result["selected"][i] + parent[i] if parent is not None else np.full(3, np.nan)
        row["absolute_position_available"] = bool(np.isfinite(absolute).all())
        for axis, value in zip(("east", "north", "up"), absolute):
            row[f"child_origin_{axis}_m"] = value
        for prefix, values in (("world_ray", data["rays"]), ("camera_ray", data.get("camera_rays"))):
            if values is not None:
                for j, axis in enumerate("xyz"):
                    row[f"{prefix}_{axis}"] = values[i, j]
        rows.append(row)
    return rows


def table(headers, rows):
    return "\n".join(["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |",
                       *("| " + " | ".join(str(v) for v in row) + " |" for row in rows)])


def f(value, digits=3):
    return "—" if value is None or not np.isfinite(float(value)) else f"{float(value):.{digits}f}"


def save_report(path, text):
    from write_field_review import html_page

    path.write_text(text, encoding="utf-8")
    path.with_suffix(".html").write_text(html_page("位置推定フォールバックの評価", text), encoding="utf-8")


def figures(out, availability, static):
    plt.rcParams["font.family"] = ["Yu Gothic", "Meiryo", "DejaVu Sans"]
    x = np.arange(len(availability))
    fig, ax = plt.subplots(figsize=(12, 4.5), layout="constrained")
    for offset, method in ((-.2, "joint"), (.2, "selected")):
        ax.bar(x + offset, [r[f"{method}_percent"] for r in availability], .38,
               label=LABELS[method], color=COLORS[method])
    ax.set(xticks=x, xticklabels=[r["experiment_id"].replace("2026", "", 1).replace("_nanyo_", "\n南予 ").replace("_ngt_", "\nプール ")
                                for r in availability], ylabel="位置成立率 [%]", ylim=(0, 105))
    ax.legend(fontsize=9)
    ax.grid(axis="y", alpha=.2)
    save_editable_figure(fig, out / "availability.png", dpi=160)
    document = PlotDocument(out / "availability.figure.pickle")
    (out / "availability.pdf").write_bytes(document.render("pdf"))
    plt.close(document.figure)
    plt.close(fig)
    windows = list(dict.fromkeys(r["window"] for r in static))
    labels = []
    for window in windows:
        if window.startswith("day"):
            day, stamp = window.split("_")
            label = f"D{day[-1]}\n{stamp[:2]}:{stamp[2:4]}:{stamp[4:]}"
            if window == "day2_102000":
                label += "\n手動揺動"
        else:
            label = "プール\n002"
        labels.append(label)
    fig, axes = plt.subplots(2, 1, figsize=(11, 8), layout="constrained")
    for ax, sampling, title in zip(axes, ("all_events", "common_events"),
                                   ("各方式の全成立イベント（採用点が異なる）", "3方式が共通して成立した同一イベント")):
        for j, method in enumerate(METHODS[:3]):
            rows = {r["window"]: r for r in static if r["sampling"] == sampling and r["method"] == method}
            values = [rows[w]["rms_3d_m"] for w in windows]
            ax.bar(np.arange(len(windows)) + (j - 1) * .25,
                   [np.nan if v is None else v for v in values], .24,
                   label=LABELS[method], color=COLORS[method])
        ax.set(xticks=np.arange(len(windows)), xticklabels=labels,
               ylabel="平均周り3D RMS [m]", title=title)
        ax.grid(axis="y", alpha=.2)
        ax.legend(fontsize=8)
    save_editable_figure(fig, out / "stationary_rms.png", dpi=160)
    document = PlotDocument(out / "stationary_rms.figure.pickle")
    (out / "stationary_rms.pdf").write_bytes(document.render("pdf"))
    plt.close(document.figure)
    plt.close(fig)


def run(experiments, cfg_path):
    config = json.loads(cfg_path.read_text(encoding="utf-8"))
    out = ROOT / f"results/{REVIEW}/position_fallback"
    if set(experiments) != set(config["experiments"]):
        scope = hashlib.sha256("\n".join(sorted(experiments)).encode()).hexdigest()[:12]
        out = ROOT / f"results/{REVIEW}/position_fallback_subset_{scope}"
    out.mkdir(parents=True, exist_ok=True)
    process = {"pid": os.getpid(), "started_utc": datetime.now(UTC).isoformat(),
               "purpose": "position fallback evaluation", "task_owned": True,
               "command": ["evaluate_position_fallback.py", *experiments], "status": "running"}
    dump(out / "process.json", process)
    try:
        availability, static, sensors, coverage, sensitivity = [], [], [], [], []
        trial_manifests = []
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        for eid in experiments:
            data = load_ngt(eid) if "_ngt_" in eid else load_nanyo(eid)
            metadata = data["metadata"]
            metadata["experiment_id"] = eid
            events = np.array([bool(r["event_sample"]) for r in data["records"]])
            grid = np.array([bool(r["grid_sample"]) for r in data["records"]])
            args = [data[name] for name in ARGUMENTS]
            result = run_fallback(*args, config=config["geometry"], surface_seed_mask=events)
            baseline = finite(result["joint"])
            assert np.all(finite(result["selected"])[baseline])
            np.testing.assert_array_equal(result["selected"][baseline], result["joint"][baseline])
            trial = ROOT / f"results/{eid}/position_fallback"
            trial.mkdir(parents=True, exist_ok=True)
            rows = output_rows(data, result)
            write_csv(trial / "positions.csv", rows)
            processed = ROOT / f"data/processed/{eid}/position_fallback"
            processed.mkdir(parents=True, exist_ok=True)
            write_csv(processed / "positions.csv", [row for row in rows if row["grid_sample"]])
            write_csv(trial / "positions_at_acoustic_events.csv", [r for r in rows if r["event_sample"]])
            count = int(grid.sum())
            summary = {"experiment_id": eid, "grid_samples": count, "recorded_events": int(events.sum())}
            for method in METHODS:
                number = int((grid & finite(result[method])).sum())
                summary[f"{method}_samples"] = number
                summary[f"{method}_percent"] = 100 * number / count
            summary["added_samples"] = summary["selected_samples"] - summary["joint_samples"]
            for method in METHODS[1:3]:
                summary[f"selected_{method}_samples"] = int((grid & (result["method"] == method)).sum())
            availability.append(summary)
            trial_static = []
            for window in metadata["static_windows"]:
                metrics, sm, cov, inside, common = evaluate_window(data, result, window)
                static.extend(metrics)
                trial_static.extend(metrics)
                sensors.extend(sm)
                coverage.append(cov)
                folder = trial / "static" / window["id"]
                folder.mkdir(parents=True, exist_ok=True)
                write_csv(folder / "samples.csv", [{**r, "common_event": bool(common[i])}
                                                   for i, r in enumerate(rows) if inside[i]])
                dump(folder / "metrics.json", {"window": window, "positions": metrics,
                                               "sensors": sm, "coverage": cov})
            # Predeclared prior age sensitivity; no parameter selected from RMS.
            for age in config["bearing_age_sensitivity_s"]:
                alternate = run_fallback(*args, config={**config["geometry"], "max_bearing_age_s": age},
                                         surface_seed_mask=events)
                sensitivity.append({"experiment_id": eid, "max_bearing_age_s": age,
                                    "selected_samples": int((grid & finite(alternate["selected"])).sum()),
                                    "acoustic_fallback_samples": int((grid & (alternate["method"] == "acoustic_imu_depth")).sum())})
            inputs = [*data["input_paths"], cfg_path, Path(__file__),
                      ROOT / "src/aquabeacon_analysis/position_fallback.py",
                      ROOT / "src/aquabeacon_analysis/nanyo_position.py",
                      ROOT / "src/aquabeacon_analysis/ngt_position.py",
                      ROOT / "src/aquabeacon_analysis/ngt_sync.py",
                      ROOT / "src/aquabeacon_analysis/parent_magnetic_attitude.py",
                      ROOT / "src/aquabeacon_analysis/camera_calibration.py"]
            manifest = {"experiment_id": eid, "created_utc": datetime.now(UTC).isoformat(),
                        "git_commit": commit, "configuration": config, "metadata": metadata,
                        "availability": summary, "static_metrics": trial_static,
                        "method_counts_grid": dict(Counter(result["method"][grid])),
                        "rejection_counts_grid": {name: dict(Counter(result[name][grid])) for name in
                                                  ("optical_rejection_reason", "acoustic_rejection_reason")},
                        "accuracy_validated": False, "ground_truth": None,
                        "baseline_preserved": True,
                        "inputs_sha256": {str(p.resolve().relative_to(ROOT)).replace("\\", "/"): sha256(p) for p in inputs}}
            dump(trial / "manifest.json", manifest)
            trial_manifests.append(trial / "manifest.json")
            text = (f"# {eid} 位置推定フォールバック\n\n"
                    f"同じ観測格子での成立率は従来 {f(summary['joint_percent'], 1)}% → "
                    f"追加後 {f(summary['selected_percent'], 1)}%（{summary['added_samples']}点追加）。\n\n"
                    f"[統合評価・方式と制約]({os.path.relpath(out / 'report.html', trial).replace(chr(92), '/')})\n\n"
                    "[全行・方式・棄却理由](positions.csv) / [一意な音響イベント](positions_at_acoustic_events.csv) / "
                    "[設定・入力ハッシュ](manifest.json)\n\n"
                    f"座標：{metadata['frame']}。成立は精度検証の合格を意味しない。\n")
            if trial_static:
                text += "\n" + table(["窓", "方式", "N", "3D RMS m（全成立イベント）"],
                                     [[r["window"], LABELS[r["method"]], r["samples"], f(r["rms_3d_m"])]
                                      for r in trial_static if r["sampling"] == "all_events"]) + "\n"
            save_report(trial / "report.md", text)
            print(f"{eid}: {summary['joint_samples']}/{count} -> {summary['selected_samples']}/{count}", flush=True)
        write_csv(out / "availability.csv", availability)
        write_csv(out / "stationary_rms.csv", static)
        write_csv(out / "sensor_rms.csv", sensors)
        write_csv(out / "stationary_coverage.csv", coverage)
        write_csv(out / "bearing_age_sensitivity.csv", sensitivity)
        figures(out, availability, static)
        report = "# 位置推定のフォールバックと静止RMS\n\n"
        report += ("音響と光学が同時に成立した従来位置を保持し、欠測時は光学＋IMU＋深度、次に音響L＋IMU＋深度を使用する。"
                   "新しい出力は各実験の `position_fallback`。旧位置・地図は従来runとして保持する。\n\n"
                   "IMUは現在の光線・測定点の姿勢変換に使う。単一音響距離と深度では水平方位は決まらないため、"
                   "音響側は最後に光学から得た世界座標の方位を最大3秒保持する条件付き推定である。"
                   "加速度の積分や、機体yawを対象方位と見なす処理は使わない。"
                   "一般的な単一ビーコンの可観測性については[一次論文](https://pmc.ncbi.nlm.nih.gov/articles/PMC3892862/)を参照。\n\n"
                   "深度基準の有効オフセットは過去の独立した音響・光学同時観測3イベント以上から求める。"
                   "直近の種観測を基準とする30秒窓の中央値、最終更新から60秒が期限。"
                   "同一時刻自身や未来の位置で初期化しない。光学方式も初期化前は成立しない。"
                   "音響側の方位を自身の推定で更新しない。閾値は事前設定でありRMS最小化で選ばない。\n\n"
                   "同期済みデータのオフライン解析であり、最近傍照合や姿勢補間は両側の観測を含み得る。"
                   "過去だけを使うという条件は、位置からの方位履歴と深度基準の更新に対するもので、オンライン実装を意味しない。\n\n"
                   "光線と深度面の前方交点を使用し、ほぼ水平（|鉛直成分|<0.15）、50 m超、"
                   "負の距離、測距球と深度面が交わらない場合は欠測のまま残す。深度0.3 m以下も追加法の対象外。"
                   "南予では現在のIMU姿勢でCADレバーアームを変換する。プールは回転修正版の中心一致・レバーアーム0の規約を保持する。\n\n")
        report += "## 位置成立率\n\n" + table(
            ["実験", "分母", "従来%", "追加後%", "追加点", "光学側追加", "音響側追加"],
            [[r["experiment_id"], r["grid_samples"], f(r["joint_percent"], 1), f(r["selected_percent"], 1),
              r["added_samples"], r["selected_optical_imu_depth_samples"], r["selected_acoustic_imu_depth_samples"]]
             for r in availability])
        report += ("\n\n分母は同一の5 Hz（プール）／10 Hz（南予）の全時間格子。南予は目視確認済み追跡を両方式で共有し、"
                   "初期2 Hz検出の過去表と分母を混ぜない。相対位置の成立とGNSSを加えた絶対位置の成立はCSVで区別する。"
                   "光学・音響の最近傍照合は保存された許容時間差内だけで、時間格子への照合を新規観測と数えない。\n\n"
                   "![成立率](availability.png)\n\n## 静止区間の位置RMS\n\n"
                   "各方式自身の平均からの母RMS（分母N）で、真値誤差ではない。平滑化・トレンド除去・追加外れ値除去なし。"
                   "音響イベントは無効も含め一度だけ数える。全成立点と3方式共通点を分け、"
                   "10:15の測距前約14秒は可用率の分母・最長欠測に含める。10:20は意図的手動揺動で、静止成績として集約しない。\n\n")
        for sampling, title in (("all_events", "各方式の全成立イベント"), ("common_events", "同一イベントでの比較")):
            report += f"### {title}\n\n" + table(["窓", "方式", "N", "水平RMS m", "3D RMS m"],
                [[r["window"], LABELS[r["method"]], r["samples"], f(r["horizontal_rms_m"]), f(r["rms_3d_m"])]
                 for r in static if r["sampling"] == sampling]) + "\n\n"
        report += "![静止位置RMS](stationary_rms.png)\n\n## 音響・光学単独の観測RMS\n\n"
        report += ("音響単独は距離[m]、光学単独はカメラ内の単位光線と平均方向の角度[deg]を評価する。"
                   "両者だけを3D位置RMSへ読み替えない。姿勢補正後の光線方向も別行で記録した。\n\n")
        report += table(["窓", "観測量", "N", "RMS", "単位"],
                        [[r["window"], r["sensor"], r["samples"], f(r["centered_rms"]), r["unit"]] for r in sensors])
        report += ("\n\n## 再現と利用範囲\n\n"
                   "[位置RMS CSV](stationary_rms.csv) / [観測RMS CSV](sensor_rms.csv) / "
                   "[静止窓の成立率・最長欠測](stationary_coverage.csv) / [期限1・3・5秒の感度](bearing_age_sensitivity.csv) / "
                   "[manifest](manifest.json)\n\n"
                   "グラフはPNG・SVG・PDFと `.figure.pickle`・`.plot.json` を同梱し、既存のブラウザエディタで調整できる。"
                   "各実験の `data/processed/<id>/position_fallback/positions.csv` が追加法の利用入口。"
                   "`position_method`・`position_available`・`absolute_position_available` と事前情報の時刻・期限を併読する。"
                   "近傍の方向保持は旋回・並進時に誤差を生む。深度基準は音響・光学と独立に校正した水面高さではない。"
                   "RMSが下がっても精度改善の証明にはならず、既存地図を自動的に再配置してはいない。\n\n")
        for eid in experiments:
            report += f"- [{eid} の位置CSV・評価](../../{eid}/position_fallback/report.html)\n"
        save_report(out / "report.md", report)
        dump(out / "manifest.json", {"experiment_id": REVIEW, "experiments": experiments,
                                      "configuration": config, "git_commit": commit,
                                      "created_utc": datetime.now(UTC).isoformat(),
                                      "inputs_sha256": {str(p.relative_to(ROOT)).replace("\\", "/"): sha256(p)
                                                        for p in [cfg_path, *trial_manifests]},
                                      "static_windows": len(coverage), "baseline_preserved": True})
        process["status"] = "completed"
    except BaseException as exc:
        process.update(status="failed", error=str(exc))
        raise
    finally:
        process["ended_utc"] = datetime.now(UTC).isoformat()
        dump(out / "process.json", process)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", action="append", required=True)
    parser.add_argument("--config", type=Path,
                        default=ROOT / f"config/{REVIEW}/position_fallback.json")
    args = parser.parse_args()
    requested = list(dict.fromkeys(args.experiment))
    cfg = json.loads(args.config.read_text(encoding="utf-8"))
    if requested == [REVIEW]:
        requested = cfg["experiments"]
    if set(requested) - set(cfg["experiments"]):
        parser.error("Explicit registered pool/Nanyo trials or the registered field review are required")
    run(requested, args.config)


if __name__ == "__main__":
    main()
