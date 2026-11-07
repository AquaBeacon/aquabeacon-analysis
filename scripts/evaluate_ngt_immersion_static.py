"""Evaluate an explicitly selected post-immersion minute, without mapping."""
from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("OPENBLAS_NUM_THREADS", "2")
os.environ.setdefault("OMP_NUM_THREADS", "2")
import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from aquabeacon_analysis.editable_plots import save_editable_figure
from aquabeacon_analysis.ngt_binary import sha256
from aquabeacon_analysis.ngt_position import centered_scatter

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config/20261028_ngt/static_after_immersion.json"
REL = ["relative_magnetic_east_m", "relative_magnetic_north_m", "relative_down_m"]
ABS = ["child_east_declination0_m", "child_north_declination0_m", "child_up_approx_m"]


def read(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def write(path, rows):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def select(rows, start, duration):
    return [r for r in rows if start <= float(r["parent_video_s"]) < start + duration]


def coordinates(rows, columns, flag):
    values = np.array([[float(r[c] or "nan") for c in columns] for r in rows])
    values[[r[flag] != "True" for r in rows]] = np.nan
    return values


def metrics(rows, columns, flag):
    values = coordinates(rows, columns, flag)
    result = centered_scatter(values)
    result.update(total=len(rows), available_fraction=result["count"] / len(rows))
    valid = np.isfinite(values).all(axis=1)
    t = np.array([float(r["parent_video_s"]) for r in rows])
    first, last = values[valid & (t < t.min()+10)], values[valid & (t >= t.min()+50)]
    result["first_last_10s_mean_displacement_m"] = (
        float(np.linalg.norm(last.mean(axis=0)-first.mean(axis=0)))
        if len(first) and len(last) else None
    )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", required=True, choices=["20261028_ngt_002"])
    args = parser.parse_args()
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    out = ROOT / "results" / args.experiment / cfg["run"]
    out.mkdir(parents=True, exist_ok=True)
    if (out / "manifest.json").exists():
        raise FileExistsError("Preserve previous run; use a new configured run name")
    start = cfg["immersion_parent_video_s"] + cfg["delay_s"]
    duration = cfg["duration_s"]
    inputs = [CONFIG, Path(__file__), ROOT / "src/aquabeacon_analysis/ngt_position.py"]
    datasets = {}
    for kind, filename in [("grid", "positions_5hz.csv"),
                           ("events", "positions_at_acoustic_events.csv")]:
        rows = []
        for eid in cfg["source_experiments"]:
            base = ROOT / f"results/{eid}/position_evaluation/rotation_corrected_20261031"
            path = base / filename
            inputs += [path, base / "manifest.json"]
            rows += [{**r, "source_experiment": eid} for r in read(path)]
        rows.sort(key=lambda r: float(r["parent_video_s"]))
        if len({r["parent_video_s"] for r in rows}) != len(rows):
            raise ValueError("Duplicate source timestamps")
        datasets[kind] = rows
    summary = {}
    for kind, rows in datasets.items():
        chosen = select(rows, start, duration)
        if kind == "grid":
            np.testing.assert_allclose([float(r["parent_video_s"]) for r in chosen],
                                       np.arange(start, start+duration, .2), atol=1e-8)
        write(out / f"positions_{kind}.csv", chosen)
        summary[kind] = {
            "relative": metrics(chosen, REL, "magnetic_position_available"),
            "child_global_approx": metrics(chosen, ABS, "approximate_global_available"),
            "unique_acoustic_shots": len({r["shot_seq"] for r in chosen}),
        }
    sensitivity = []
    for lo in cfg["sensitivity_start_parent_video_s"]:
        m = metrics(select(datasets["grid"], lo, duration), REL, "magnetic_position_available")
        sensitivity.append({"start_parent_video_s": lo, **m})
    summary["window_sensitivity"] = sensitivity
    rows = select(datasets["grid"], start, duration)
    values = coordinates(rows, REL, "magnetic_position_available")
    residual = values-np.nanmean(values, axis=0)
    t = np.array([float(r["parent_video_s"])-start for r in rows])
    plt.rcParams.update({"font.family": "Yu Gothic", "font.size": 11})
    fig, ax = plt.subplots(figsize=(8, 3.5), layout="constrained")
    for i, label in enumerate(["東（磁北基準）", "北（磁北基準）", "鉛直下向き"]):
        ax.plot(t, residual[:, i]*100, label=label, linewidth=1.2)
    ax.set(xlabel="評価開始からの時間 [s]", ylabel="平均位置からの偏差 [cm]", xlim=(0, 60))
    ax.grid(alpha=.25)
    ax.legend(ncol=3, fontsize=9)
    save_editable_figure(fig, out / "relative_residuals.pdf")
    save_editable_figure(fig, out / "relative_residuals.png", dpi=180)
    plt.close(fig)
    syncpath = ROOT / "results/20261028_ngt_000/synchronization/manifest.json"
    sync = json.loads(syncpath.read_text())["cameras"]["parent"]
    jst = timezone(timedelta(hours=9))
    clock = [datetime.fromtimestamp(sync["utc_at_video_zero_s"]+x*sync["time_scale"], jst)
             .isoformat(timespec="milliseconds") for x in (start, start+duration)]
    summary["interval_parent_video_s"] = [start, start+duration]
    summary["interval_jst"] = clock
    (out / "metrics.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    text = ["# 長田高校プール：入水1分後からの静止位置評価", "",
            f"評価区間は親機動画 {start:.1f}〜{start+duration:.1f} 秒（17:36〜18:36）、",
            f"同期後の時刻は {clock[0]} 〜 {clock[1]}。ユーザー指定の60秒間を評価した。",
            "画像回転修正版の位置推定を再集計し、マッピングは実行していない。", "",
            "|評価|有効数 / 全数|成立率|水平RMS [cm]|鉛直RMS [cm]|3D RMS [cm]|95%半径 [cm]|",
            "|---|---:|---:|---:|---:|---:|---:|"]
    for kind, label in [("events", "音響イベント時刻"), ("grid", "5 Hz格子")]:
        for name, suffix in [("relative", "親子相対"), ("child_global_approx", "GNSS加算子機位置")]:
            m = summary[kind][name]
            text.append(f"|{label}・{suffix}|{m['count']} / {m['total']}|"
                        f"{m['available_fraction']:.1%}|{100*m['centered_rms_horizontal_m']:.2f}|"
                        f"{100*m['std_population_m'][2]:.2f}|{100*m['centered_rms_3d_m']:.2f}|"
                        f"{100*m['radius_p95_m']:.2f}|")
    text += ["", "RMSは有効位置の平均からの偏差の二乗平均平方根。真値誤差・絶対精度ではない。",
             "音響イベント時刻を主評価とし、5 Hz格子は可用性と時系列の確認に使う。",
             "イベント成立率の分母は記録された54イベントであり、記録されなかった送信の成功率を示さない。",
             "5 Hz格子では同じ測距を再利用するため、300点を独立した測距回数とは数えない。",
             "無効値を保持し、外れ値除去・欠測補間・静止拘束・平滑化の追加は行っていない。", "",
             "## 入水と区間の根拠", "",
             "子機原MP4を既存の時計同期で親機動画時刻へ換算して確認した。",
             "[995秒の画像](entry_parent_995s.jpg)は水面上、[996秒の画像](entry_parent_996s.jpg)は水中。",
             "カメラの入水を995〜996秒と区切り、最初に水中を確認した996秒を基準とした。",
             "圧力深度は997.49秒に約0.219 mとなるが、998.49秒には約0.007 mに戻る。",
             "圧力センサとカメラの取付け位置が異なるため、圧力立ち上がりを入水の唯一の基準にしない。",
             "入水1分後から1分間静止したというユーザー確認を採用した。位置RMSを最小化して区間を選んでいない。",
             "旧002は1050〜1110秒だったため、最後の6秒は旧003の位置CSVから読み込んだ。旧台帳と結果は変更していない。", "",
             "|開始 [親機動画s]|3D RMS [cm]|成立率|", "|---:|---:|---:|"]
    for m in sensitivity:
        text.append(f"|{m['start_parent_video_s']:.1f}|{100*m['centered_rms_3d_m']:.2f}|{m['available_fraction']:.1%}|")
    text += ["", "開始を前後1秒ずらした表は境界感度であり、最良窓の選定ではない。", "",
             "## 座標と解釈", "",
             "親子相対位置は磁北基準の東・北・下。GNSS加算位置は偏角0°を仮定した暫定値。",
             "光学・音響・GNSS等の測定中心の一致と未校正の取付けを仮定しており、一定の系統誤差はこの評価では分からない。",
             "子機だけの静止は親機の静止を保証しない。親子相対ばらつきには親機運動や姿勢推定の影響も含む。",
             "GNSS加算位置のばらつきは親機GNSSの変動を含む。測距の直接波同定も独立には検証されていない。", "",
             (f"5 Hz格子の最初と最後の10秒平均の差は、親子相対位置で"
             f"{100*summary['grid']['relative']['first_last_10s_mean_displacement_m']:.1f} cm、"
             f"GNSS加算子機位置で{100*summary['grid']['child_global_approx']['first_last_10s_mean_displacement_m']:.1f} cm。"),
             "時系列にはゆっくりした変動があり、RMSを独立なランダム雑音だけの大きさとは解釈しない。", "",
             "![親子相対位置の平均周り偏差](relative_residuals.png)", "",
             "[SVG](relative_residuals.svg) / [PDF](relative_residuals.pdf) / [詳細数値](metrics.json)", "",
             "編集は `python -m aquabeacon_analysis.editable_plots <このフォルダ>/relative_residuals.figure.pickle`。",
             "元CSV、設定、校正参照と入力ハッシュはmanifest.jsonに記録。"]
    (out / "report.md").write_text("\n".join(text)+"\n", encoding="utf-8")
    inputs += [syncpath, ROOT / "data/processed/20261028_ngt_000/fusion/child_depth.csv"]
    inputs += list(out.glob("entry_parent_*s.jpg"))
    manifest = {"experiment_id": args.experiment, "config": cfg,
                "git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT,
                                                       text=True, timeout=10).strip(),
                "inputs_sha256": {str(p.relative_to(ROOT)): sha256(p) for p in dict.fromkeys(inputs)},
                "calibration_provenance": "See both source manifests; no recalibration or refitting",
                "mapping_executed": False, "source_positions_modified": False,
                "selection_review": "995 s above-water and 996 s underwater frames visually inspected",
                "outputs": [p.name for p in out.iterdir() if p.is_file()]}
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
