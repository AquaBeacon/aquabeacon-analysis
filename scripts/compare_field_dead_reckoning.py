"""Compare independent child BNO085/depth displacements with current field positions."""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

os.environ.setdefault("OPENBLAS_NUM_THREADS", "2")
os.environ.setdefault("OMP_NUM_THREADS", "2")
import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from dr_sources_nanyo import load_nanyo
from dr_sources_pool import load_pool

from aquabeacon_analysis.dead_reckoning_comparison import (
    integrate_segments,
    window_comparisons,
)
from aquabeacon_analysis.editable_plots import save_editable_figure

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config/20261103_field_review_000/child_dead_reckoning.json"
AXES = ("east", "north", "up")
CAVEATS = (
    "比較対象は光学・音響・姿勢・深度と親機GNSSから求めた現在の子機位置であり、真値ではない。",
    ("水平はBNO085の加速度を姿勢で世界座標へ回し、重力を除去して二重積分する。"
    "上下は圧力深度の変化を直接使う。BNO085の直接の線形加速度出力は記録されていない。"),
    ("開始点の並進だけを合わせ、初速をゼロと仮定する。開始後の位置補正、"
    "速度拘束、軌跡の回転・尺度合わせは行わない。移動中の初速差も位置差に含まれる。"),
    ("IMU欠測後は別区間として積分を再開始し、最初の共通点で再び原点を置く。"
    "再開始をまたぐ連続航法の性能を表すものではない。"),
    ("プールは暫定磁北、海は既存の偏角補正済みENUを使う。方位と取付けの精度は未検証。"
    "水平移動量の差は座標軸の向きに依存しにくいが、時間変化する姿勢誤差の影響は受ける。"),
    ("両方式は姿勢・深度の一部を共有する。上下差は独立した深度精度の検証ではない。"
    "深度は水面に対する変化、現在位置の上下は親機GNSSの高さ変動も含む。"),
    ("IMU中心と機体原点を近似的に同一視し、回転によるレバーアーム変位は補正していない。"
    "プールの既存静止バイアスは002内の5秒から得た値、海には追加バイアス補正を適用しない。"),
    ("5・10・30・60秒窓は開始を5秒ずつずらすため重複する。独立試行数や信頼区間として扱わない。"
    "窓の始点・終点の現在位置が有効な場合だけ比較し、途中の現在位置欠測は補間しない。"),
)


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def write_csv(path, rows):
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                    encoding="utf-8")


def sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def outdir(eid, cfg):
    return ROOT / "results" / eid / "child_dead_reckoning" / cfg["run"]


def stats(values):
    values = np.asarray(values, dtype=float)
    values = values[np.isfinite(values)]
    if not len(values):
        return {"count": 0, "rms_m": None, "median_m": None, "p95_m": None, "max_m": None}
    return {"count": len(values), "rms_m": float(np.sqrt(np.mean(values**2))),
            "median_m": float(np.median(values)), "p95_m": float(np.percentile(values, 95)),
            "max_m": float(np.max(values))}


def number(value, digits=2):
    return "—" if value is None else f"{value:,.{digits}f}"


def render_html(path, text):
    import markdown

    body = markdown.markdown(text, extensions=["tables", "fenced_code"])
    path.write_text('<!doctype html><html lang="ja"><meta charset="utf-8">'
                    '<meta name="viewport" content="width=device-width,initial-scale=1">'
                    '<title>子機BNO085・深度と現在位置の比較</title><style>'
                    'body{font-family:system-ui,sans-serif;max-width:1200px;margin:32px auto;'
                    'padding:0 24px;line-height:1.75;color:#203038}table{border-collapse:collapse;'
                    'display:block;overflow:auto}td,th{border:1px solid #bcc6ca;padding:6px 10px;'
                    'white-space:nowrap}th{background:#f0f4f5}img{max-width:100%}'
                    'code{overflow-wrap:anywhere}a{color:#006d91}</style>' + body + '</html>',
                    encoding="utf-8")


def analyze(eid, cfg, config_path):
    out = outdir(eid, cfg)
    if (out / "manifest.json").exists():
        raise FileExistsError(f"Completed run exists; select a new config run: {out}")
    out.mkdir(parents=True, exist_ok=True)
    pool = "_ngt_" in eid
    source_run = cfg["pool_current_run" if pool else "sea_current_run"]
    source = ROOT / f"results/{eid}/optical_followup/{source_run}/new_positions.csv"
    rows = [r for r in read_csv(source) if r["grid_sample"].lower() == "true"]
    source_times = np.array([float(r["utc_s"]) for r in rows])
    expected_step = .2 if pool else .1
    if len(rows) < 2 or not np.all(np.abs(np.diff(source_times)-expected_step) < .001):
        raise ValueError("Expected explicit 5 Hz pool or 10 Hz sea current-position grid")
    substeps = cfg["substeps_per_position_grid"]["pool" if pool else "sea"]
    # Retain the exact comparator timestamps, including the pool affine clock scale.
    fraction = np.arange(substeps) / substeps
    times = np.r_[(source_times[:-1, None]
                   + np.diff(source_times)[:, None] * fraction).ravel(), source_times[-1]]
    grid = np.arange(0, len(times), substeps)
    current = np.full((len(times), 3), np.nan)
    available = np.zeros(len(times), bool)
    methods = np.full(len(times), "", dtype=object)
    for idx, row in zip(grid, rows, strict=True):
        if row["absolute_position_available"].lower() == "true":
            current[idx] = [float(row[f"child_origin_{axis}_m"]) for axis in AXES]
            available[idx] = np.isfinite(current[idx]).all()
        methods[idx] = row["position_method"]
    sensor = (load_pool(ROOT, eid, times,
                        use_static_bias=cfg["pool_use_existing_static_bias"])
              if pool else load_nanyo(ROOT, eid, times))
    acceleration, depth = sensor["acceleration_enu"], sensor["depth_m"]
    integrated = integrate_segments(times, acceleration, cfg["integration_max_gap_s"])
    raw, velocity, segments = (integrated[key] for key in
                               ("position_m", "velocity_m_s", "segment"))
    anchored = np.full_like(raw, np.nan)
    dr_delta, current_delta = np.full_like(raw, np.nan), np.full_like(raw, np.nan)
    anchor_utc = np.full(len(times), np.nan)
    segment_rows = []
    for segment in np.unique(segments[segments >= 0]):
        indices = np.flatnonzero(segments == segment)
        anchors = indices[available[indices] & np.isfinite(depth[indices])]
        info = {"segment": int(segment), "start_utc_s": float(times[indices[0]]),
                "end_utc_s": float(times[indices[-1]]),
                "duration_s": float(times[indices[-1]]-times[indices[0]]),
                "anchor_utc_s": None, "paired_samples": 0}
        if len(anchors):
            anchor = anchors[0]
            chosen = indices[indices >= anchor]
            elapsed = times[chosen]-times[anchor]
            dr_delta[chosen] = raw[chosen]-raw[anchor]-velocity[anchor]*elapsed[:, None]
            dr_delta[chosen, 2] = -(depth[chosen]-depth[anchor])
            current_delta[chosen] = current[chosen]-current[anchor]
            anchored[chosen] = dr_delta[chosen]+current[anchor]
            anchor_utc[chosen] = times[anchor]
            paired = chosen[available[chosen] & np.isfinite(anchored[chosen]).all(1)]
            info.update(anchor_utc_s=float(times[anchor]), paired_samples=len(paired))
            if len(paired):
                last = paired[-1]
                info.update(last_paired_elapsed_s=float(times[last]-times[anchor]),
                            dr_horizontal_endpoint_m=float(np.linalg.norm(dr_delta[last, :2])),
                            current_horizontal_endpoint_m=float(
                                np.linalg.norm(current_delta[last, :2])),
                            endpoint_horizontal_gap_m=float(
                                np.linalg.norm(anchored[last, :2]-current[last, :2])))
        segment_rows.append(info)
    windows = window_comparisons(times, raw, velocity, segments, depth, current, available,
                                 cfg["window_horizons_s"], cfg["window_stride_s"])
    for row in windows["rows"]:
        row["horizontal_magnitude_gap_m"] = abs(
            row["dr_horizontal_displacement_m"]-row["current_horizontal_displacement_m"])
    summaries = {}
    for horizon in cfg["window_horizons_s"]:
        selected = [r for r in windows["rows"] if r["horizon_s"] == horizon]
        summaries[str(horizon)] = {key: stats([r[key] for r in selected]) for key in
                                  ("horizontal_gap_m", "horizontal_magnitude_gap_m",
                                   "vertical_gap_m", "position_gap_m",
                                   "dr_horizontal_displacement_m",
                                   "current_horizontal_displacement_m")}
    difference = anchored-current
    common = available & np.isfinite(anchored).all(1)
    summary = {
        "experiment_id": eid, "environment": "pool" if pool else "sea",
        "duration_s": float(times[-1]-times[0]), "grid_samples": len(grid),
        "current_available_samples": int(available.sum()),
        "current_available_fraction": float(available[grid].mean()),
        "dr_depth_available_samples": int((np.isfinite(acceleration[grid]).all(1)
                                           & np.isfinite(depth[grid])).sum()),
        "common_samples": int(common.sum()), "segments": segment_rows,
        "window_metrics": summaries, "window_attempts": windows["attempts"],
        "window_rejections": windows["rejections"],
        "full_segment_horizontal_gap": stats(np.linalg.norm(difference[common, :2], axis=1)),
        "full_segment_vertical_gap": stats(difference[common, 2]),
        "sensor_details": sensor["details"], "caveats": list(CAVEATS),
    }
    output_rows = []
    for i in range(len(times)):
        row = {"utc_s": times[i], "elapsed_s": times[i]-times[0],
               "grid_sample": i % substeps == 0, "segment": segments[i],
               "current_available": available[i], "position_method": methods[i],
               "depth_m": depth[i], "anchor_utc_s": anchor_utc[i], "paired": common[i]}
        for prefix, values in (("acceleration", acceleration), ("inertial_position", raw),
                               ("inertial_velocity", velocity), ("dr_depth", anchored),
                               ("current", current), ("difference", difference),
                               ("dr_displacement", dr_delta),
                               ("current_displacement", current_delta)):
            unit = "m_s2" if prefix == "acceleration" else (
                "m_s" if prefix == "inertial_velocity" else "m")
            row.update({f"{prefix}_{axis}_{unit}": values[i, j]
                        for j, axis in enumerate(AXES)})
        output_rows.append(row)
    write_csv(out / "timeseries.csv", output_rows)
    write_csv(out / "windows.csv", windows["rows"])
    dump(out / "summary.json", summary)
    plot_trial(out, times, dr_delta, current_delta, segments, difference)
    report_trial(out, summary)
    paths = list(sensor["provenance_paths"]) + [source, config_path, Path(__file__),
        ROOT / "scripts/dr_sources_pool.py", ROOT / "scripts/dr_sources_nanyo.py",
        ROOT / "src/aquabeacon_analysis/dead_reckoning_comparison.py",
        ROOT / f"experiments/{eid}/experiment.json"]
    paths += list(source.parent.glob("*manifest.json"))
    unique = sorted(set(paths))
    snapshots = {}
    for p in unique:
        relative = p.relative_to(ROOT).as_posix()
        if p.suffix in {".py", ".json"}:
            archived = out / "source_snapshot" / relative
            archived.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, archived)
            snapshots[relative] = archived.relative_to(out).as_posix()
    manifest = {
        "experiment_id": eid, "run": cfg["run"], "created_utc": datetime.now(UTC).isoformat(),
        "git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT,
                                             text=True, timeout=5).strip(),
        "working_tree_dirty": bool(subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=ROOT, text=True, timeout=10).strip()),
        "method": cfg["method"], "config": cfg,
        "inputs_sha256": {p.relative_to(ROOT).as_posix(): sha(p) for p in unique},
        "input_snapshots": snapshots,
        "outputs_sha256": {p.name: sha(p) for p in out.iterdir() if p.is_file()},
    }
    dump(out / "manifest.json", manifest)
    return summary


def plot_trial(out, times, dr, current, segment, difference):
    plt.rcParams.update({"font.family": "Yu Gothic", "font.size": 10})
    fig, axes = plt.subplots(3, 1, figsize=(10, 8), sharex=True, layout="constrained")
    t = times-times[0]
    breaks = np.r_[False, np.diff(segment) != 0]
    d = np.linalg.norm(dr[:, :2], axis=1)
    c = np.linalg.norm(current[:, :2], axis=1)
    d[breaks] = np.nan
    axes[0].plot(t, d, color="#c35635", label="BNO085＋深度")
    axes[0].plot(t, c, ".", color="#176b91", ms=2, label="現在の位置推定")
    axes[0].set(ylabel="開始点からの水平移動量 [m]", yscale="symlog")
    axes[0].legend()
    z = dr[:, 2].copy()
    z[breaks] = np.nan
    axes[1].plot(t, z, color="#c35635")
    axes[1].plot(t, current[:, 2], ".", color="#176b91", ms=2)
    axes[1].set(ylabel="開始点からの上下変位 [m]")
    axes[2].plot(t, np.linalg.norm(difference[:, :2], axis=1), ".", ms=2, color="#6b4979")
    axes[2].set(ylabel="両方式の水平位置差 [m]", xlabel="実験開始からの時間 [s]",
                yscale="symlog")
    for ax in axes:
        ax.grid(alpha=.25)
    save_editable_figure(fig, out / "comparison.png", dpi=160)
    save_editable_figure(fig, out / "comparison.pdf")
    plt.close(fig)


def report_trial(out, s):
    lines = [f"# {s['experiment_id']}：BNO085＋深度と現在位置の比較", "",
             (f"比較時間は{number(s['duration_s'], 1)}秒。現在位置の有効点は"
             f"{s['current_available_samples']}/{s['grid_samples']}点、"
             f"BNO085＋深度の入力がそろう点は{s['dr_depth_available_samples']}点、"
             f"開始点を合わせて比較できた点は{s['common_samples']}点。"), "",
             "| 積分時間 | 窓数 | 水平位置差 RMS [m] | 水平移動量の差 中央値 [m] | 上下差 RMS [m] |",
             "|---|---:|---:|---:|---:|"]
    for h, metrics in s["window_metrics"].items():
        lines.append(f"| {h}秒 | {metrics['horizontal_gap_m']['count']} | "
                     f"{number(metrics['horizontal_gap_m']['rms_m'])} | "
                     f"{number(metrics['horizontal_magnitude_gap_m']['median_m'])} | "
                     f"{number(metrics['vertical_gap_m']['rms_m'])} |")
    lines += ["", "![比較](comparison.png)", "",
              ("水平移動量と水平位置差は対称対数軸。各IMU連続区間の最初の共通点を原点とする。"
              "現在位置は有効な格子点だけを表示する。上下の正方向は上。"), "",
              "| IMU連続区間 | 長さ [s] | 比較点数 | 最後の共通点まで [s] | DR水平移動量 [m] | 現在位置の水平移動量 [m] |",
              "|---|---:|---:|---:|---:|---:|"]
    for r in s["segments"]:
        lines.append(f"| {r['segment']} | {number(r['duration_s'])} | {r['paired_samples']} | "
                     f"{number(r.get('last_paired_elapsed_s'))} | "
                     f"{number(r.get('dr_horizontal_endpoint_m'))} | "
                     f"{number(r.get('current_horizontal_endpoint_m'))} |")
    lines += ["", *[f"- {c}" for c in CAVEATS], "", ("入力・適用設定・除外数は"
              "[summary.json](summary.json)、来歴は[manifest.json](manifest.json)。"
              "時系列は[timeseries.csv](timeseries.csv)、全窓は[windows.csv](windows.csv)。"), "",
              ("[PNG](comparison.png) / [SVG](comparison.svg) / [PDF](comparison.pdf)。"
              "ブラウザ編集用の `.figure.pickle` と `.plot.json` を同梱する。"), "",
              "```powershell", "python -m aquabeacon_analysis.editable_plots "
              + (out / "comparison.figure.pickle").relative_to(ROOT).as_posix(), "```", ""]
    text = "\n".join(lines)
    (out / "report.md").write_text(text, encoding="utf-8")
    render_html(out / "report.html", text)


def aggregate(cfg, ids, config_path=CONFIG):
    summaries = [json.loads((outdir(eid, cfg)/"summary.json").read_text(encoding="utf-8"))
                 for eid in ids]
    out = outdir(cfg["aggregate_experiment"], cfg)
    out.mkdir(parents=True, exist_ok=True)
    all_rows = []
    pooled = []
    for environment in ("pool", "sea"):
        for horizon in cfg["window_horizons_s"]:
            included = [s for s in summaries if s["environment"] == environment
                        and s["window_metrics"][str(horizon)]["horizontal_gap_m"]["count"]]
            count = sum(s["window_metrics"][str(horizon)]["horizontal_gap_m"]["count"]
                        for s in included)
            record = {"environment": environment, "horizon_s": horizon,
                      "windows": count, "experiments": len(included)}
            for key in ("horizontal_gap_m", "vertical_gap_m", "horizontal_magnitude_gap_m",
                        "dr_horizontal_displacement_m", "current_horizontal_displacement_m"):
                record[key + "_rms_m"] = (float(np.sqrt(sum(
                    s["window_metrics"][str(horizon)][key]["rms_m"]**2
                    * s["window_metrics"][str(horizon)][key]["count"] for s in included) / count))
                    if count else None)
            pooled.append(record)
    write_csv(out / "pooled_summary.csv", pooled)
    overview = []
    if set(ids) == set(cfg["experiments"]):
        static = next(s for s in summaries if s["experiment_id"] == "20261028_ngt_002")
        longest = max(static["segments"], key=lambda r: r.get("last_paired_elapsed_s", 0))
        overview = [
            ("深度センサで上下位置を拘束しても、子機単独の水平位置は時間とともに現在位置から離れた。"
             f"静止プール002では約{number(longest['last_paired_elapsed_s'], 1)}秒後の水平移動量が"
             f"DRで{number(longest['dr_horizontal_endpoint_m'])} m、現在位置で"
             f"{number(longest['current_horizontal_endpoint_m'])} mとなった。"
             "今回の条件では、数十秒以上の水平位置を現在の方式から置き換える根拠は得られない。"),
            "", "| 積分時間 | プール 水平位置差RMS [m]（窓数） | 海 水平位置差RMS [m]（窓数） |",
            "|---|---:|---:|",
        ]
        for h in cfg["window_horizons_s"]:
            p, s = [next(v for v in pooled if v["environment"] == e and v["horizon_s"] == h)
                    for e in ("pool", "sea")]
            overview.append(f"| {h}秒 | {number(p['horizontal_gap_m_rms_m'])}（{p['windows']}） | "
                            f"{number(s['horizontal_gap_m_rms_m'])}（{s['windows']}） |")
        overview += ["", ("全採用窓をまとめたRMSで、窓数の多い実験の寄与が大きい。"
                     "窓は重複し、積分時間によって採用する開始時刻も異なる。"
                     "プール003は水上・手動姿勢の条件を含み、水中の004と条件が異なる。"), "",
                     ("プールの60秒は004の3窓のみ。海005の30・60秒は比較両端の現在位置がそろわず、"
                      "比較不能として残している。"), "",
                     ("短い窓には現在位置の跳びや実際の運動も含まれる。"
                      "これらの差をBNO085だけの誤差や、現在位置の精度として割り当てることはできない。"), ""]
    lines = ["# 海・プールの子機BNO085＋深度と現在の位置推定", "", *overview,
             ("現状の位置推定に対して、外部の水平位置更新を使わない子機単独の推定が"
             "どれだけ離れるかを、同じ開始点・同じ時刻で比較した。"
             "各窓の初速はゼロで、現在位置への追従補正は加えていない。"), "",
             ("水平位置差は向きを含む差、水平移動量の差は各開始点からの移動量の大きさの差。"
             "いずれも真値誤差ではない。30秒を共通の比較時間として以下に示す。"), "",
             "| 実験 | 現在位置の成立率 | DR＋深度入力率 | 30秒窓数 | 水平位置差 RMS [m] | 水平移動量の差 中央値 [m] | 上下差 RMS [m] |",
             "|---|---:|---:|---:|---:|---:|---:|"]
    for s in summaries:
        eid, m = s["experiment_id"], s["window_metrics"]["30"]
        relative = os.path.relpath(outdir(eid, cfg)/"report.html", out).replace("\\", "/")
        lines.append(f"| [{eid}]({relative}) | {100*s['current_available_fraction']:.1f}% | "
                     f"{100*s['dr_depth_available_samples']/s['grid_samples']:.1f}% | "
                     f"{m['horizontal_gap_m']['count']} | {number(m['horizontal_gap_m']['rms_m'])} | "
                     f"{number(m['horizontal_magnitude_gap_m']['median_m'])} | "
                     f"{number(m['vertical_gap_m']['rms_m'])} |")
        for h, metrics in s["window_metrics"].items():
            all_rows.append({"experiment_id": eid, "environment": s["environment"],
                             "horizon_s": int(h), "windows": metrics["horizontal_gap_m"]["count"],
                             **{f"{k}_{stat}": v[stat] for k, v in metrics.items()
                                for stat in ("rms_m", "median_m", "p95_m")}})
    lines += ["", ("推定入力率はデータがそろう割合であり、位置の信頼性を保証する値ではない。"
              "現在位置が得られない時刻は方式間の差を評価できない。"), "",
              "![積分時間ごとの水平位置差](horizon_comparison.png)", "",
              ("各点は同じ長さの窓の水平位置差RMS。窓数・中央値・95百分位は"
              "[summary.csv](summary.csv)に保存した。"), "",
              *[f"- {c}" for c in CAVEATS], "",
              ("プール001は子機BNO085・深度の重なる記録がないため比較対象外。"
              "対象はプール002〜004と南予7区間。既存の解析結果は変更していない。"), "",
              ("[検証記録](verification.json) / [全数値](summary.csv) / [環境別集計](pooled_summary.csv) / "
              "[設定](../../../../config/20261103_field_review_000/child_dead_reckoning.json)"), ""]
    text = "\n".join(lines)
    (out / "report.md").write_text(text, encoding="utf-8")
    render_html(out / "report.html", text)
    write_csv(out / "summary.csv", all_rows)
    dump(out / "summary.json", summaries)
    plt.rcParams.update({"font.family": "Yu Gothic", "font.size": 10})
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.5), sharey=True, layout="constrained")
    for ax, environment, label in zip(axes, ("pool", "sea"), ("プール", "海"), strict=True):
        for s in summaries:
            if s["environment"] != environment:
                continue
            values = s["window_metrics"]
            ax.plot([int(h) for h in values],
                    [v["horizontal_gap_m"]["rms_m"] for v in values.values()],
                    "o-", label=s["experiment_id"].replace("2026", "").replace("_nanyo", ""))
        ax.set(title=label, xlabel="積分時間 [s]", yscale="log", xticks=cfg["window_horizons_s"])
        ax.grid(alpha=.25)
        ax.legend(fontsize=8)
    axes[0].set_ylabel("現在位置との差：水平RMS [m]")
    save_editable_figure(fig, out / "horizon_comparison.png", dpi=160)
    save_editable_figure(fig, out / "horizon_comparison.pdf")
    plt.close(fig)
    inputs = [outdir(eid, cfg)/"summary.json" for eid in ids]
    inputs += [outdir(eid, cfg)/"windows.csv" for eid in ids]
    inputs += [config_path, Path(__file__)]
    snapshots = {}
    for path in (config_path, Path(__file__)):
        relative = path.relative_to(ROOT).as_posix()
        archived = out / "source_snapshot" / relative
        archived.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, archived)
        snapshots[relative] = archived.relative_to(out).as_posix()
    dump(out / "manifest.json", {
        "experiment_id": cfg["aggregate_experiment"], "run": cfg["run"],
        "created_utc": datetime.now(UTC).isoformat(), "config": cfg,
        "git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT,
                                             text=True, timeout=5).strip(),
        "inputs_sha256": {p.relative_to(ROOT).as_posix(): sha(p) for p in inputs},
        "input_snapshots": snapshots,
        "outputs_sha256": {p.name: sha(p) for p in out.iterdir()
                           if p.is_file() and p.name not in
                           {"manifest.json", "verification.json", "validation_notes.json"}},
    })


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", action="append", required=True)
    parser.add_argument("--config", type=Path, default=CONFIG)
    parser.add_argument("--phase", choices=["run", "report"], default="run")
    args = parser.parse_args()
    cfg = json.loads(args.config.read_text(encoding="utf-8"))
    ids = args.experiment
    if len(ids) != len(set(ids)) or not set(ids) <= set(cfg["experiments"]):
        parser.error("Explicit unique supported experiment IDs required")
    if args.phase == "run":
        for eid in ids:
            result = analyze(eid, cfg, args.config.resolve())
            print(eid, html.escape(str(result["window_metrics"]["30"]["horizontal_gap_m"])),
                  flush=True)
    aggregate(cfg, ids, args.config.resolve())


if __name__ == "__main__":
    main()
