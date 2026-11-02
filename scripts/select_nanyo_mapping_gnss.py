"""Choose one GNSS solution per acquisition using matched stationary observations."""

import argparse
import csv
import json
import subprocess
from datetime import datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from aquabeacon_analysis.editable_plots import save_editable_figure
from aquabeacon_analysis.ngt_binary import sha256
from aquabeacon_analysis.ngt_position import nearest_indices
from aquabeacon_analysis.static_evaluation import static_metrics

ACQUISITIONS = ["20261031_nanyo_000", "20261101_nanyo_000"]


def read(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def write(path, rows):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def run(acquisition, windows, window_path):
    out = Path(f"results/{acquisition}/mapping_gnss_selection")
    out.mkdir(parents=True, exist_ok=True)
    source = Path(f"results/{acquisition}/evaluation")
    paths = {
        "spp": source / "spp_positions.csv",
        "ppk_ar": source / "ppk_positions.csv",
        "ppk_float": source / "ppk_float_positions.csv",
        "ppk_alternate": source
        / (
            "ppk_base_041133.csv"
            if acquisition == ACQUISITIONS[0]
            else "ppk_base_041133_positions.csv"
        ),
    }
    names = {
        "spp": "SPP",
        "ppk_ar": "PPK AR・950437",
        "ppk_float": "PPK Float設定・950437",
        "ppk_alternate": "PPK AR・041133",
    }
    candidates = {k: read(p) for k, p in paths.items()}
    assessments, exported, table = [], [], []
    for spec in windows:
        start = datetime.fromisoformat(spec["start"]).timestamp()
        rows = {
            k: [r for r in rs if start <= float(r["utc_s"]) < start + 60]
            for k, rs in candidates.items()
        }
        reference = np.array([float(r["utc_s"]) for r in rows["ppk_ar"]])
        matching, valid = {}, np.ones(len(reference), bool)
        for key, rs in rows.items():
            t = np.array([float(r["utc_s"]) for r in rs])
            index, dt = nearest_indices(t, reference, 0.51)
            xy = np.array([[float(r[k]) for k in ["east_m", "north_m", "up_m"]] for r in rs])
            quality = np.array(
                [
                    r["fix_valid"] in ["True", "1"] and int(r["fix_type"]) >= 3
                    if key == "spp"
                    else int(r["quality"]) in [1, 2, 4]
                    for r in rs
                ]
            )
            okay = (
                (index >= 0)
                & np.isfinite(xy[np.maximum(index, 0)]).all(1)
                & quality[np.maximum(index, 0)]
            )
            # A source epoch must not receive repeated votes.
            for j in np.unique(index[okay]):
                duplicate = np.flatnonzero(okay & (index == j))
                if len(duplicate) > 1:
                    chosen = duplicate[np.argmin(np.abs(dt[duplicate]))]
                    okay[duplicate] = False
                    okay[chosen] = True
            valid &= okay
            matching[key] = (index, dt, xy)
        if valid.sum() < 30:
            raise ValueError(f"{spec['id']}: fewer than 30 common GNSS epochs")
        metrics = {}
        centered = {}
        for key, (idx, dt, xyz) in matching.items():
            points = xyz[idx[valid]]
            metrics[key] = static_metrics(points)
            centered[key] = points[:, :2] - points[:, :2].mean(0)
            m = metrics[key]
            table.append(
                {
                    "window": spec["id"],
                    "start": spec["start"],
                    "condition": spec["condition"],
                    "selection_window": "hand" not in spec["condition"],
                    "solution": key,
                    "matched_epochs": int(valid.sum()),
                    "horizontal_rmse_m": m["horizontal_rmse_m"],
                    "rmse_3d_m": m["rmse_3d_m"],
                    "up_rmse_m": m["axis_rmse_m"][2],
                }
            )
            for ref, ix, delta, point in zip(
                reference[valid], idx[valid], dt[valid], points, strict=True
            ):
                exported.append(
                    {
                        "window": spec["id"],
                        "solution": key,
                        "reference_utc_s": ref,
                        "source_utc_s": rows[key][ix]["utc_s"],
                        "time_delta_s": delta,
                        "east_m": point[0],
                        "north_m": point[1],
                        "up_m": point[2],
                    }
                )
        assessments.append(
            {
                "window": spec,
                "common_epochs": int(valid.sum()),
                "metrics": metrics,
                "used_for_selection": "hand" not in spec["condition"],
            }
        )
        limit = 1.18 * max(float(np.max(np.abs(v))) for v in centered.values())
        angle = np.linspace(0, 2 * np.pi, 361)
        with plt.rc_context({"font.family": "Yu Gothic", "font.size": 10}):
            fig, axes = plt.subplots(2, 2, figsize=(9, 9))
            fig.subplots_adjust(
                left=0.1, right=0.97, bottom=0.12, top=0.88, hspace=0.45, wspace=0.3
            )
            for ax, key in zip(axes.flat, paths, strict=True):
                radius = metrics[key]["horizontal_rmse_m"]
                ax.plot(*centered[key].T, ".", ms=5, color="#007b94")
                ax.plot(0, 0, "+", color="black", ms=12)
                ax.plot(
                    radius * np.cos(angle),
                    radius * np.sin(angle),
                    "--",
                    color="#d28a25",
                    label=f"水平RMSE {radius:.3f} m",
                )
                ax.set(
                    title=names[key] + f"\n3D RMSE {metrics[key]['rmse_3d_m']:.3f} m",
                    xlabel="東偏差 [m]",
                    ylabel="北偏差 [m]",
                    xlim=(-limit, limit),
                    ylim=(-limit, limit),
                    aspect="equal",
                )
                ax.legend(fontsize=8)
                ax.grid(alpha=0.2)
            fig.suptitle(
                f"GNSS静止評価：{spec['start'][:19].replace('T', ' ')} JST\n同じ時刻の観測 {int(valid.sum())}点・各系列の平均からの偏差"
            )
            fig.supxlabel(
                "円は水平RMSE半径。真値誤差・信頼区間ではない。手動揺動は採用判定から除く。",
                fontsize=9,
                y=0.025,
            )
            save_editable_figure(fig, out / f"{spec['id']}_overhead.png", dpi=160)
            plt.close(fig)
    primary = [a for a in assessments if a["used_for_selection"]]
    scores = {
        key: float(np.sqrt(np.mean([a["metrics"][key]["rmse_3d_m"] ** 2 for a in primary])))
        for key in paths
    }
    horizontal_scores = {
        key: float(np.sqrt(np.mean([a["metrics"][key]["horizontal_rmse_m"] ** 2 for a in primary])))
        for key in paths
    }
    selected = min(scores, key=scores.get)
    selected_rows = []
    for r in candidates[selected]:
        row = dict(r)
        if selected == "spp":
            row.update(
                quality=5,
                sd_e_m=r["h_accuracy_m"],
                sd_n_m=r["h_accuracy_m"],
                sd_u_m=r["v_accuracy_m"],
            )
        row["selected_solution"] = selected
        selected_rows.append(row)
    write(out / "selected_positions.csv", selected_rows)
    write(out / "matched_static_positions.csv", exported)
    write(out / "comparison.csv", table)
    manifest = {
        "experiment_id": acquisition,
        "selected": selected,
        "selected_label": names[selected],
        "selection_rule": "Minimum sqrt(mean(window 3D RMSE squared)); each non-hand-shaken 60-second window has equal weight. Own mean, denominator N, no smoothing or detrending.",
        "matching": "Shared valid epochs of all four candidates, one-to-one nearest association <=0.51s, within each half-open 60-second window, no interpolation.",
        "scores_3d_m": scores,
        "scores_horizontal_m": horizontal_scores,
        "window_assessments": assessments,
        "source": str(paths[selected]),
        "origin_llh": json.loads((source / "manifest.json").read_text(encoding="utf-8"))[
            "origin_llh"
        ],
        "accuracy_validated": False,
        "limitations": [
            "Low static scatter does not prove absolute accuracy or absence of a day-to-day bias.",
            "Parent may move with waves even when the child is grounded.",
            "Selection windows are also evaluation windows; no independent validation set.",
            "Exploratory day1 window is identified separately; deliberate hand-shaken day2 window is diagnostic only.",
        ],
        "git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "inputs_sha256": {
            str(p): sha256(p)
            for p in [*paths.values(), source / "manifest.json", window_path, Path(__file__)]
        },
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = [
        f"# {acquisition}：マッピングに使うGNSSの静止比較",
        "",
        f"採用：**{names[selected]}**。静止窓の等重み3D RMSE {scores[selected]:.4f} m、水平 {horizontal_scores[selected]:.4f} m。",
        "",
        "同一時刻の観測を各系列の平均周りで評価し、平滑化・トレンド除去を行わない。各60秒窓の3D分散を等重みで平均した値の平方根が最小の方式を、その日の全区間へ一貫して採用する。10:20の手動揺動は参考評価のみ。",
        "",
        "| 方式 | 採用指標3D RMSE m | 水平RMSE m |",
        "|---|---:|---:|",
    ]
    for key in paths:
        lines.append(f"| {names[key]} | {scores[key]:.4f} | {horizontal_scores[key]:.4f} |")
    lines += [
        "",
        "| 窓 | 条件 | 方式 | 同時刻点数 | 水平RMSE m | 3D RMSE m |",
        "|---|---|---|---:|---:|---:|",
    ]
    for r in table:
        lines.append(
            f"| {r['window']} | {r['condition']} | {names[r['solution']]} | {r['matched_epochs']} | {r['horizontal_rmse_m']:.4f} | {r['rmse_3d_m']:.4f} |"
        )
    for a in assessments:
        lines += ["", f"![{a['window']['id']}]({a['window']['id']}_overhead.png)"]
    lines += [
        "",
        "静止時のばらつきによる選択であり、絶対精度・日をまたぐオフセットの小ささを保証しない。独立真値なし。",
        "",
        "[採用位置CSV](selected_positions.csv) / [比較表CSV](comparison.csv) / [同時刻の全比較点](matched_static_positions.csv) / [設定と来歴](manifest.json)",
    ]
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    from report_nanyo_day2 import render_html

    render_html("\n".join(lines), out / "report.html")
    print(acquisition, selected, scores, flush=True)


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--experiment", required=True, choices=ACQUISITIONS, action="append")
    args = parser.parse_args()
    path = Path("config/20261101_nanyo_000/stationary_spectra.json")
    config = json.loads(path.read_text(encoding="utf-8"))
    for acquisition in args.experiment:
        windows = [
            w
            for w in config["windows"]
            if json.loads(
                Path(f"experiments/{w['experiment_id']}/experiment.json").read_text(
                    encoding="utf-8"
                )
            )["parent_acquisition"]
            == acquisition
        ]
        run(acquisition, windows, path)


if __name__ == "__main__":
    main()
