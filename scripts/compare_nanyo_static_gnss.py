"""Compare ordinary and Float-configured PPK in matched child absolute static positions."""

import argparse
import csv
import json
import subprocess
from collections import Counter
from datetime import datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from report_nanyo_day2 import render_html
from scipy.spatial.transform import Rotation

from aquabeacon_analysis.editable_plots import save_editable_figure
from aquabeacon_analysis.ngt_binary import sha256
from aquabeacon_analysis.static_evaluation import static_metrics
from aquabeacon_analysis.stationary_gnss import sample_gnss, variance_budget

ACQ = "20261101_nanyo_000"
CONDITIONS = {
    "corrected": "姿勢補正あり",
    "fixed_parent": "親機姿勢固定（補正なし比較）",
    "fixed_both": "親子姿勢固定（追加感度）",
}
COLORS = {"ppk_ar": "#b36d1e", "ppk_float": "#087b99"}
AXES = ["east", "north", "up"]


def read(path):
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def js(path):
    return json.loads(path.read_text(encoding="utf-8"))


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write(path, rows):
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def array(rows, keys):
    return np.array([[float(r[k]) for k in keys] for r in rows])


def metric_row(spec, solution, condition, value, depth, sampling="linear"):
    return {
        "window": spec["id"],
        "start": spec["start"],
        "condition": spec["condition"],
        "solution": solution,
        "attitude": condition,
        "sampling": sampling,
        "depth_mean_m": depth,
        "samples": value["n"],
        **{f"{a}_rmse_m": v for a, v in zip(AXES, value["axis_rmse_m"], strict=True)},
        "horizontal_rmse_m": value["horizontal_rmse_m"],
        "rmse_3d_m": value["rmse_3d_m"],
    }


def plot_window(out, spec, t, positions, metrics, depth, config):
    centered = {k: v - v.mean(0) for k, v in positions.items()}
    extent = 1.18 * max(
        0.05,
        *(float(abs(v[:, :2]).max()) for v in centered.values()),
        *(m["horizontal_rmse_m"] for m in metrics.values()),
    )
    circle = np.linspace(0, 2 * np.pi, 361)
    stamp = spec["start"][:19].replace("T", " ") + " JST"
    hand = "（手動揺動）" if "hand" in spec["condition"] else ""
    with plt.rc_context({"font.family": "Yu Gothic", "font.size": 10}):
        fig, axes = plt.subplots(2, 3, figsize=(14.8, 10.0), sharex=True, sharey=True)
        fig.subplots_adjust(left=0.07, right=0.98, bottom=0.10, top=0.86, hspace=0.39, wspace=0.26)
        for row, solution in enumerate(config["solutions"]):
            for col, (condition, title) in enumerate(CONDITIONS.items()):
                key = f"{solution}__{condition}"
                ax, xyz, m = axes[row, col], centered[key], metrics[key]
                ax.axhline(0, color="#b0b7bd", lw=0.7)
                ax.axvline(0, color="#b0b7bd", lw=0.7)
                ax.plot(*xyz[:, :2].T, ".", color=COLORS[solution], ms=5.2, alpha=0.75)
                radius = m["horizontal_rmse_m"]
                ax.plot(
                    radius * np.cos(circle),
                    radius * np.sin(circle),
                    "--",
                    color=COLORS[solution],
                    lw=1.3,
                )
                ax.plot(0, 0, "+", color="black", ms=12, mew=1.5)
                ax.set(
                    xlim=(-extent, extent),
                    ylim=(-extent, extent),
                    aspect="equal",
                    xlabel="平均からの東方向偏差 [m]",
                    ylabel="平均からの北方向偏差 [m]",
                    title=f"{config['solutions'][solution]['label']}｜{title}\n"
                    f"水平 {radius:.3f} m ／ 3D {m['rmse_3d_m']:.3f} m",
                )
                ax.tick_params(labelbottom=True, labelleft=True)
                ax.grid(alpha=0.2)
        fig.suptitle(
            f"{stamp} から60秒{hand}\n子機絶対位置の静止分布｜圧力深度 {depth:.2f} m｜同じ {len(t)} 測距点",
            fontsize=15,
        )
        fig.supxlabel(
            "各系列の平均を原点に表示。全6条件で同じ観測点・同じ縮尺。破線円は水平RMSE、＋は平均（信頼区間ではない）。",
            fontsize=10,
            y=0.025,
        )
        save_editable_figure(fig, out / "absolute_overhead.png", dpi=170)
        plt.close(fig)
        fig, axes = plt.subplots(3, 2, figsize=(13.5, 8.8), sharex=True, sharey="row")
        for col, condition in enumerate(["corrected", "fixed_parent"]):
            for solution, info in config["solutions"].items():
                xyz = centered[f"{solution}__{condition}"]
                for j in range(3):
                    axes[j, col].plot(
                        t,
                        xyz[:, j],
                        ".",
                        color=COLORS[solution],
                        label=info["label"],
                        ms=4.5,
                        alpha=0.8,
                    )
            axes[0, col].set_title(CONDITIONS[condition])
            axes[0, col].legend(fontsize=9)
            axes[-1, col].set_xlabel("静止区間開始からの秒数 [s]")
            for j in range(3):
                axes[j, col].set_ylabel(["東", "北", "上"][j] + "方向の平均からの偏差 [m]")
                axes[j, col].set_xlim(0, 60)
                axes[j, col].grid(alpha=0.2)
        fig.suptitle(
            f"{stamp} から60秒{hand}｜子機絶対位置の時系列\n測距の欠測は補間せず、採用した実観測時刻だけを表示",
            fontsize=14,
        )
        fig.tight_layout(rect=(0, 0.01, 1, 0.93))
        save_editable_figure(fig, out / "absolute_timeseries.png", dpi=170)
        plt.close(fig)


def run_window(spec, config, config_path, windows_path, git_sha):
    eid = spec["experiment_id"]
    source_dir = Path(f"results/{eid}/stationary_spectra/{spec['id']}")
    out = Path(f"results/{eid}/stationary_gnss_comparison/{spec['id']}")
    out.mkdir(parents=True, exist_ok=True)
    source, upstream_path = source_dir / "matched_positions.csv", source_dir / "manifest.json"
    upstream, rows = js(upstream_path), read(source)
    t = array(rows, ["utc_s"])[:, 0]
    lo = datetime.fromisoformat(spec["start"]).timestamp()
    duration = upstream["duration_s"]
    assert ((t >= lo) & (t < lo + duration)).all() and np.all(np.diff(t) > 0)
    assert len({r["shot_seq"] for r in rows}) == len(rows)
    ep = Path(f"experiments/{eid}/experiment.json")
    acq = js(ep)["parent_acquisition"]
    attitudes_path = Path(f"results/{eid}/light_tracking/{spec['id']}/attitudes_10hz.csv")
    ats = [
        r
        for r in read(attitudes_path)
        if lo <= float(r["utc_s"]) < lo + duration and r["parent_attitude_available"] == "True"
    ]
    keys = [f"parent_true_enu_q{k}" for k in "xyzw"]
    parent_mean = Rotation.from_quat(array(ats, keys)).mean()
    rp = Rotation.from_quat(array(rows, keys))
    lever = np.array(upstream["geometry_configuration"]["lever_arms_frd_m"]["parent_antenna"])
    relative = {c: array(rows, [f"{c}_{a}_m" for a in AXES]) for c in CONDITIONS}
    parent_lever = {
        c: rp.apply(lever)
        if c == "corrected"
        else np.repeat(parent_mean.apply(lever)[None], len(t), axis=0)
        for c in CONDITIONS
    }
    offsets = {c: relative[c] - parent_lever[c] for c in CONDITIONS}
    inputs = [
        source,
        upstream_path,
        attitudes_path,
        ep,
        config_path,
        windows_path,
        Path(__file__),
        Path("src/aquabeacon_analysis/stationary_gnss.py"),
        Path("src/aquabeacon_analysis/static_evaluation.py"),
        Path("src/aquabeacon_analysis/editable_plots.py"),
        Path("scripts/report_nanyo_day2.py"),
    ]
    sampled, native, gnss_files = {}, [], {}
    for solution, info in config["solutions"].items():
        p = Path(f"results/{acq}/evaluation/{info['filename']}")
        gr = read(p)
        gt = array(gr, ["utc_s"])[:, 0]
        gp = array(gr, [f"{a}_m" for a in AXES])
        quality = np.array([int(r["quality"]) for r in gr])
        sampled[solution] = sample_gnss(
            gt,
            gp,
            np.isin(quality, config["valid_quality"]),
            t,
            config["linear_maximum_bracket_s"],
            config["nearest_sensitivity_maximum_delta_s"],
        )
        sampled[solution].update(times=gt, quality=quality)
        inputs.append(p)
        gnss_files[solution] = str(p)
        for i in np.flatnonzero((gt >= lo - 1.5) & (gt <= lo + duration + 1.5)):
            native.append({"solution": solution, **gr[i]})
    common = np.logical_and.reduce([s["available"] for s in sampled.values()])
    sensitivity_mask = common & np.logical_and.reduce(
        [s["nearest_available"] for s in sampled.values()]
    )
    if common.sum() < 4 or sensitivity_mask.sum() < 4:
        raise ValueError(f"{spec['id']}: too few common GNSS-supported shots")
    original = array(rows, [f"child_origin_{a}_m" for a in AXES])
    baseline_error = float(
        abs(original[common] - (sampled["ppk_ar"]["linear"] + offsets["corrected"])[common]).max()
    )
    assert baseline_error < 1e-8, "The ordinary-PPK baseline must reproduce prior child positions"
    depth = upstream["metrics"]["pressure_depth"]
    positions, metrics, budgets, nearest, paired_linear, table, exported = (
        {},
        {},
        {},
        {},
        {},
        [],
        [],
    )
    invariant_error = 0.0
    for solution, s in sampled.items():
        for condition in CONDITIONS:
            key = f"{solution}__{condition}"
            all_xyz = s["linear"] + offsets[condition]
            positions[key] = all_xyz[common]
            metrics[key] = static_metrics(positions[key])
            budgets[key] = variance_budget(s["linear"][common], offsets[condition][common])
            nearest[key] = static_metrics((s["nearest"] + offsets[condition])[sensitivity_mask])
            paired_linear[key] = static_metrics(all_xyz[sensitivity_mask])
            back_relative = all_xyz - (s["linear"] - parent_lever[condition])
            invariant_error = max(
                invariant_error,
                float(abs(back_relative[common] - relative[condition][common]).max()),
            )
            for sampling, m in [
                ("linear", metrics[key]),
                ("nearest", nearest[key]),
                ("linear_on_nearest_common", paired_linear[key]),
            ]:
                table.append(metric_row(spec, solution, condition, m, depth["mean_m"], sampling))
            for i, r in enumerate(rows):
                li, ri, ni = s["left_index"][i], s["right_index"][i], s["nearest_index"][i]
                exported.append(
                    {
                        "window": spec["id"],
                        "shot_seq": r["shot_seq"],
                        "utc_s": t[i],
                        "offset_s": t[i] - lo,
                        "solution": solution,
                        "attitude": condition,
                        "gnss_available": bool(s["available"][i]),
                        "common_available": bool(common[i]),
                        "nearest_common_available": bool(sensitivity_mask[i]),
                        "left_utc_s": s["times"][li],
                        "right_utc_s": s["times"][ri],
                        "left_quality": int(s["quality"][li]),
                        "right_quality": int(s["quality"][ri]),
                        "interpolation_fraction": s["fraction"][i],
                        "nearest_utc_s": s["times"][ni],
                        "nearest_quality": int(s["quality"][ni]),
                        **{
                            f"{prefix}_{a}_m": float(value[j])
                            for prefix, value in [
                                ("gnss", s["linear"][i]),
                                ("sensor_offset", offsets[condition][i]),
                                ("relative", relative[condition][i]),
                                ("parent_lever", parent_lever[condition][i]),
                                ("child_absolute", all_xyz[i]),
                                ("child_nearest", s["nearest"][i] + offsets[condition][i]),
                            ]
                            for j, a in enumerate(AXES)
                        },
                    }
                )
    write(out / "position_samples.csv", exported)
    write(out / "gnss_native.csv", native)
    write(out / "comparison.csv", table)
    plot_window(out, spec, t[common] - lo, positions, metrics, depth["mean_m"], config)
    evaluation_manifest = Path(f"results/{acq}/evaluation/manifest.json")
    inputs.append(evaluation_manifest)
    result = {
        "window": spec,
        "acquisition": acq,
        "duration_s": duration,
        "configuration": config,
        "geometry_configuration": upstream["geometry_configuration"],
        "origin_llh": js(evaluation_manifest)["origin_llh"],
        "pressure_depth": depth,
        "original_matched_shots": len(rows),
        "common_shots": int(common.sum()),
        "excluded_for_gnss": int((~common).sum()),
        "first_offset_s": float(t[common][0] - lo),
        "last_offset_s": float(t[common][-1] - lo),
        "maximum_gap_s": float(np.diff(t[common]).max()),
        "gnss_files": gnss_files,
        "original_missing_samples": str(source_dir / "all_position_samples.csv"),
        "parent_mean_quat_xyzw": parent_mean.as_quat().tolist(),
        "metrics": metrics,
        "variance_budgets": budgets,
        "relative_metrics_same_samples": {
            c: static_metrics(relative[c][common]) for c in CONDITIONS
        },
        "nearest_sensitivity": {
            "common_shots": int(sensitivity_mask.sum()),
            "metrics": nearest,
            "linear_same_shots": paired_linear,
        },
        "quality_counts_at_used_native_epochs": {
            k: dict(
                Counter(
                    map(
                        int,
                        s["quality"][
                            np.unique(np.r_[s["left_index"][common], s["right_index"][common]])
                        ],
                    )
                )
            )
            for k, s in sampled.items()
        },
        "baseline_max_difference_m": baseline_error,
        "relative_gnss_invariance_max_difference_m": invariant_error,
        "git_commit": git_sha,
        "inputs_sha256": {str(p): sha256(p) for p in inputs},
        "accuracy_validated": False,
        "independent_validation": False,
        "sample_selection": "Existing accepted unique shots, followed by common GNSS validity; no RMSE-driven trimming",
    }
    dump(out / "manifest.json", result)
    return result, table, out


def save_report(out, lines, title):
    text = "\n".join(lines) + "\n"
    (out / "report.md").write_text(text, encoding="utf-8")
    render_html(text, out / "report.html")
    p = out / "report.html"
    p.write_text(
        p.read_text(encoding="utf-8").replace(
            "<title>Nanyo day2 実験解析</title>", f"<title>{title}</title>"
        ),
        encoding="utf-8",
    )


def methods():
    return [
        (
            "既存の静止位置RMSEは、親機CAD原点に対する子機CAD原点の相対ベクトルの評価だった。"
            "GNSS並進は親子で共通なので相対位置から消える。今回追加するのは、GNSSを含めた子機CAD原点の絶対位置の平均周りRMSE。真値に対する絶対精度ではない。"
        ),
        (
            "絶対位置 = 親機GNSSアンテナ位置 + 相対位置 − 親機アンテナの回転済みレバーアーム。"
            "姿勢補正なし比較では、親機姿勢と親機レバーアームをともに区間平均回転へ固定する。親子姿勢固定は追加感度である。"
        ),
        (
            "光源追跡、画素座標、測距、同期、校正、姿勢、取り付け寸法、既存の採用shotを固定し、GNSS方式だけを変更。"
            "各shotにつき採用済みの1画像を使用し、両GNSSで有効な同じ時刻を比較する。元の光源・測距欠測は補間せず、GNSS方式変更での欠測回復は評価対象外。"
        ),
        (
            "主比較は従来の位置推定と同じ時刻合わせで、ネイティブGNSSの隣接観測間だけ線形補間する（最大間隔1.5秒、外挿なし）。"
            "無効品質をまたぐ補間はしない。通常PPK・Float設定PPKの両方に同じ処理を適用し、最近傍GNSS（最大0.51秒）による無補間感度も保存した。"
            "補間値は独立したGNSS観測ではなく、同じGNSS観測が複数shotを支える。"
        ),
        (
            "各系列の平均を基準に、軸別RMSE = sqrt(mean((x − mean(x))²))、3D RMSEは3軸RMSEの二乗和平方根。"
            "分母N、位置平滑化・トレンド除去・RMSEによる外れ値除外なし。姿勢固定条件もそれぞれの平均を基準にする。"
        ),
        (
            "Float設定PPKは整数アンビギュイティ固定を行わない処理設定の名称。使用品質1・2・4を保持しており、すべての時刻でFloat解が成立したという意味ではない。"
            "方式は前回の同じ静止窓によるGNSS評価後に選んでいるため、今回も独立した未知データでの性能検証ではない。"
        ),
        (
            "絶対位置の分散は、GNSS分散 + センサー相対幾何の分散 + 2倍の共分散となる。"
            "両者が逆向きに変動すれば絶対位置RMSEは相対位置RMSEより小さくなりうる。"
            "結果をGNSSだけの誤差低減率や波の除去率に読み替えない。一定位置バイアスもこの評価では測れない。"
        ),
    ]


def report_window(result, out, config):
    w, metrics = result["window"], result["metrics"]
    title = f"{w['start'][:19].replace('T', ' ')}｜PPK方式と静止絶対位置"
    lines = [
        f"# {title}",
        "",
        (
            f"60秒窓・JST。子機圧力深度 {result['pressure_depth']['mean_m']:.2f} m。"
            f"元の採用測距 {result['original_matched_shots']} 点、両GNSS共通 {result['common_shots']} 点。"
        ),
        "手で揺らした診断区間。通常静止の集約値から除外。"
        if "hand" in w["condition"]
        else "指定した静止条件での平均周りのばらつき。",
        (
            f"[両日比較のまとめ](../../../{ACQ}/stationary_gnss_comparison/report.html) / "
            f"[従来の相対位置・FFT](../../stationary_spectra/{w['id']}/report.html)"
        ),
        "",
        "## 子機の絶対位置RMSE",
        "",
        "| GNSS | 姿勢条件 | 東 m | 北 m | 上 m | 水平 m | 3D m |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for s, label in config["solutions"].items():
        for c, name in CONDITIONS.items():
            m = metrics[f"{s}__{c}"]
            nums = [*m["axis_rmse_m"], m["horizontal_rmse_m"], m["rmse_3d_m"]]
            lines.append(
                f"| {label['label']} | {name} | " + " | ".join(f"{v:.4f}" for v in nums) + " |"
            )
    lines += [
        "",
        "![全条件の俯瞰位置分布](absolute_overhead.png)",
        "各系列の平均を原点にした等縮尺図。破線円は水平RMSEであり、信頼区間ではない。",
        "[編集用SVG](absolute_overhead.svg)",
        "",
        "![絶対位置の時系列](absolute_timeseries.png)",
        "[編集用SVG](absolute_timeseries.svg)",
        "",
        "## GNSSに依存しない相対位置の確認",
        "",
        "| 姿勢条件 | 水平RMSE m | 3D RMSE m |",
        "|---|---:|---:|",
    ]
    for c, name in CONDITIONS.items():
        m = result["relative_metrics_same_samples"][c]
        lines.append(f"| {name} | {m['horizontal_rmse_m']:.4f} | {m['rmse_3d_m']:.4f} |")
    lines += [
        "",
        "上表は両GNSSで同じ。単にGNSSを変更して相対位置RMSEが改善したとは扱わない。",
        "",
        "## GNSSとセンサー幾何の同時変動（姿勢補正あり）",
        "",
        "| GNSS | GNSS分散 m² | センサー幾何分散 m² | 2×共分散 m² | 絶対位置分散 m² |",
        "|---|---:|---:|---:|---:|",
    ]
    for s, info in config["solutions"].items():
        b = result["variance_budgets"][f"{s}__corrected"]
        lines.append(
            f"| {info['label']} | "
            + " | ".join(
                f"{b[k]:.6f}"
                for k in [
                    "gnss_variance_3d_m2",
                    "sensor_offset_variance_3d_m2",
                    "twice_covariance_3d_m2",
                    "absolute_variance_3d_m2",
                ]
            )
            + " |"
        )
    lines += [
        "",
        (
            "ここでのセンサー幾何は子機原点−親機GNSSアンテナのベクトルで、親子CAD原点間の相対位置とはレバーアーム分だけ異なる。"
            "分散は3軸の合計。各行の最初の3項の和が最後の項となる。"
        ),
        "",
        "## GNSS時刻合わせの感度",
        "",
        f"主比較と最近傍比較で共通の {result['nearest_sensitivity']['common_shots']} shotを使用。最近傍値を複数shotで再利用する場合があり、独立標本数とは異なる。",
        "| GNSS | 姿勢条件 | 線形時刻合わせ3D m | 最近傍3D m |",
        "|---|---|---:|---:|",
    ]
    for s, info in config["solutions"].items():
        for c, name in CONDITIONS.items():
            key = f"{s}__{c}"
            n = result["nearest_sensitivity"]
            lines.append(
                f"| {info['label']} | {name} | {n['linear_same_shots'][key]['rmse_3d_m']:.4f} | {n['metrics'][key]['rmse_3d_m']:.4f} |"
            )
    lines += [
        "",
        "## 条件・計算方法",
        "",
        *methods(),
        "",
        "深度は元の60秒全区間の水圧平均を維持。初期180秒の圧力中央値を水面基準の代理値とし、密度1025 kg/m³・重力9.80665 m/s²で換算。海底深度ではない。",
        f"有効位置の区間内時刻：{result['first_offset_s']:.3f}〜{result['last_offset_s']:.3f} s、最大間隔 {result['maximum_gap_s']:.3f} s。",
        f"採用したネイティブGNSS品質の個数：{result['quality_counts_at_used_native_epochs']}。",
        "",
        "## 保存データ・検証",
        "",
        "[位置・時刻対応CSV](position_samples.csv) / [RMSE集計CSV](comparison.csv) / [ネイティブGNSS抜粋](gnss_native.csv) / [設定・入力ハッシュ・来歴](manifest.json)",
        f"通常PPKの元の絶対位置との差の最大値 {result['baseline_max_difference_m']:.2g} m。GNSS変更前後での相対位置差 {result['relative_gnss_invariance_max_difference_m']:.2g} m。",
        f"[独立再計算の検証記録](../../../{ACQ}/stationary_gnss_comparison/verification.json)",
    ]
    save_report(out, lines, title)


def plot_summary(out, results, config):
    x = np.arange(len(results))
    days = {r["window"]["id"][3] for r in results}
    day_title = "Day1・Day2" if len(days) > 1 else "Day" + next(iter(days))
    labels = [
        "D"
        + r["window"]["id"][3]
        + " "
        + r["window"]["start"][11:19]
        + ("\n手動揺動" if "hand" in r["window"]["condition"] else "")
        for r in results
    ]
    with plt.rc_context({"font.family": "Yu Gothic", "font.size": 11}):
        fig, axes = plt.subplots(1, 2, figsize=(14.8, 6.8))
        for ax, condition in zip(axes, ["corrected", "fixed_parent"], strict=True):
            for shift, (s, info) in zip([-0.19, 0.19], config["solutions"].items(), strict=True):
                values = [r["metrics"][f"{s}__{condition}"]["rmse_3d_m"] for r in results]
                bars = ax.bar(x + shift, values, width=0.36, label=info["label"], color=COLORS[s])
                ax.bar_label(bars, fmt="%.3f", fontsize=8, padding=3)
            ax.set_xticks(x, labels, rotation=25, ha="right")
            ax.set(ylabel="子機絶対位置の平均周り3D RMSE [m]", title=CONDITIONS[condition])
            ax.set_ylim(0, ax.get_ylim()[1] * 1.15)
            ax.legend(fontsize=10)
            ax.grid(axis="y", alpha=0.2)
            for i, r in enumerate(results):
                if "hand" in r["window"]["condition"]:
                    ax.axvspan(i - 0.48, i + 0.48, color="#d99536", alpha=0.1)
        fig.suptitle(
            f"{day_title}｜GNSS方式を変更した静止絶対位置の比較\n光源・測距・姿勢・採用shotは共通、各窓60秒",
            fontsize=15,
        )
        fig.tight_layout(rect=(0, 0.01, 1, 0.91))
        save_editable_figure(fig, out / "absolute_comparison.png", dpi=180)
        plt.close(fig)


def summary_table(results):
    lines = [
        "| 日・開始 JST | 深度 m | 共通shot | 通常PPK 補正あり m | Float 補正あり m | 減少率 % | 通常PPK 姿勢固定 m | Float 姿勢固定 m |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in results:
        w, m = r["window"], r["metrics"]
        a, f = m["ppk_ar__corrected"]["rmse_3d_m"], m["ppk_float__corrected"]["rmse_3d_m"]
        label = w["start"][5:19].replace("T", " ") + (
            "（手動）" if "hand" in w["condition"] else ""
        )
        lines.append(
            f"| {label} | {r['pressure_depth']['mean_m']:.2f} | {r['common_shots']} | {a:.4f} | {f:.4f} | {100 * (1 - f / a):.1f} | {m['ppk_ar__fixed_parent']['rmse_3d_m']:.4f} | {m['ppk_float__fixed_parent']['rmse_3d_m']:.4f} |"
        )
    return lines


def report_summary(acq, results, table, config, config_path):
    out = Path(f"results/{acq}/stationary_gnss_comparison")
    out.mkdir(parents=True, exist_ok=True)
    plot_summary(out, results, config)
    write(out / "comparison.csv", table)
    aggregates = {}
    for day in sorted({r["acquisition"] for r in results}):
        selected = [
            r for r in results if r["acquisition"] == day and "hand" not in r["window"]["condition"]
        ]
        aggregates[day] = {
            k: float(np.sqrt(np.mean([r["metrics"][k]["rmse_3d_m"] ** 2 for r in selected])))
            for k in selected[0]["metrics"]
        }
    title = ("Day1・Day2" if acq == ACQ else "Day1") + "｜PPK Floatを用いた静止位置RMSE比較"
    lines = [
        f"# {title}",
        "",
        "子機の絶対位置を、通常PPKとFloat設定PPKで再計算した。従来の相対位置評価も保持し、同じ採用測距点で比較する。",
        "",
        *summary_table(results),
        "",
        "表の位置RMSEは3D、各系列の平均周り。減少率は姿勢補正ありの通常PPKに対する比率。真値誤差の改善率ではない。",
        "",
        "![絶対位置RMSE比較](absolute_comparison.png)",
        "[編集用SVG](absolute_comparison.svg)",
        "",
        "## 読み取り",
        "",
    ]
    changed = [
        r
        for r in results
        if abs(
            r["metrics"]["ppk_ar__corrected"]["rmse_3d_m"]
            - r["metrics"]["ppk_float__corrected"]["rmse_3d_m"]
        )
        > 1e-9
    ]
    unchanged = [r for r in results if r not in changed]
    lines.append(
        f"姿勢補正ありでは {len(changed)}/{len(results)} 窓でFloat設定のRMSEが変わり、{len(unchanged)} 窓は同じだった。"
    )
    for r in changed:
        m, w = r["metrics"], r["window"]
        a, f = m["ppk_ar__corrected"]["rmse_3d_m"], m["ppk_float__corrected"]["rmse_3d_m"]
        lines.append(
            f"{w['start'][5:19].replace('T', ' ')}：{a:.4f} → {f:.4f} m（{100 * (1 - f / a):.1f}%減少）"
            + ("。手動揺動の診断値。" if "hand" in w["condition"] else "。")
        )
    lines += [
        "10:15では親機姿勢固定のRMSEがFloatで増える一方、補正ありでは減る。方式の優劣はGNSS単独の分散だけでは決まらず、センサー幾何との共分散にも依存する。"
        if acq == ACQ
        else "同じ区間・同じ観測点での条件付き比較であり、日間の位置バイアスや独立測位精度は評価していない。",
        "",
        "## 日別の集約（手動揺動を除外）",
        "",
        "| 日 | 通常PPK 補正あり m | Float 補正あり m | 通常PPK 姿勢固定 m | Float 姿勢固定 m |",
        "|---|---:|---:|---:|---:|",
    ]
    for day, m in aggregates.items():
        label = "Day1" if day.startswith("20261031") else "Day2"
        lines.append(
            f"| {label} | "
            + " | ".join(
                f"{m[k]:.4f}"
                for k in [
                    "ppk_ar__corrected",
                    "ppk_float__corrected",
                    "ppk_ar__fixed_parent",
                    "ppk_float__fixed_parent",
                ]
            )
            + " |"
        )
    lines += [
        "",
        "各60秒窓のRMSE二乗を等重みで平均した平方根。全点を一つの平均位置にまとめてはいない。探索選定したDay1 14:41:10を含む。",
        "",
        "## 比較条件",
        "",
        *methods(),
        "",
        "深度は既存の水圧60秒平均を使用し、GNSS・光源の有効範囲には縮めない。海底深度ではない。10:15の測距開始前約14秒は引き続き位置欠測。",
        "",
        "## 全静止区間の俯瞰図・時系列・内訳",
        "",
    ]
    for r in results:
        w = r["window"]
        rel = f"../../{w['experiment_id']}/stationary_gnss_comparison/{w['id']}"
        lines += [
            f"### {w['start'][:19].replace('T', ' ')}",
            "",
            f"![{w['id']} 絶対位置俯瞰]({rel}/absolute_overhead.png)",
            f"[時系列・軸別RMSE・最近傍感度・共分散の詳細]({rel}/report.html) / [編集用SVG]({rel}/absolute_overhead.svg)",
            "",
        ]
    lines += [
        "## 来歴と検証",
        "",
        (
            "通常PPKの子機絶対位置を元データと1e-8 m以内で照合し、GNSSを変更しても相対位置が不変であることを確認。"
            "入力CSV・時刻合わせ・品質・設定・Git SHA・SHA-256を保存した。旧静止位置、元GNSS、マッピングは変更していない。"
        ),
        (
            "[全比較CSV](comparison.csv) / [来歴](manifest.json) / "
            f"[独立再計算検証](../../{ACQ}/stationary_gnss_comparison/verification.json)"
        ),
        (
            f"[従来のDay1・Day2静止解析・FFT](../../{ACQ}/analysis/report.html) / "
            f"[GNSS単独の選定](../../{acq}/mapping_gnss_selection/report.html)"
        ),
    ]
    save_report(out, lines, title)
    manifests = [
        Path(
            f"results/{r['window']['experiment_id']}/stationary_gnss_comparison/{r['window']['id']}/manifest.json"
        )
        for r in results
    ]
    dump(
        out / "manifest.json",
        {
            "experiment_id": acq,
            "configuration": config,
            "windows": results,
            "aggregates": aggregates,
            "input_sha256": {str(p): sha256(p) for p in [config_path, *manifests]},
            "git_commit": results[0]["git_commit"],
            "code_sha256": sha256(Path(__file__)),
            "accuracy_validated": False,
        },
    )
    print(out / "report.html", flush=True)


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--experiment", required=True, choices=[ACQ])
    args = parser.parse_args()
    config_path = Path(f"config/{args.experiment}/stationary_gnss_comparison.json")
    config = js(config_path)
    windows_path = Path(config["windows_config"])
    windows = js(windows_path)["windows"]
    git_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    results, table = [], []
    for spec in windows:
        result, rows, out = run_window(spec, config, config_path, windows_path, git_sha)
        report_window(result, out, config)
        results.append(result)
        table.extend(rows)
        print(spec["id"], result["common_shots"], "shots", flush=True)
    day1 = [r for r in results if r["acquisition"] == "20261031_nanyo_000"]
    report_summary(
        "20261031_nanyo_000",
        day1,
        [r for r in table if r["window"].startswith("day1")],
        config,
        config_path,
    )
    report_summary(ACQ, results, table, config, config_path)


if __name__ == "__main__":
    main()
