"""Create a focused presentation version of an existing 2D master; keep its data exact."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from aquabeacon_analysis.editable_plots import PlotDocument, save_editable_figure

ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", required=True, choices=["20261028_ngt_001"])
    args = parser.parse_args()
    source = ROOT / f"results/{args.experiment}/gnss_overhead"
    master = source / "static_scatter.figure.pickle"
    document = PlotDocument(master)
    # Saved source Artist arrays preserve the exact original mean-centred plotted data.
    values = [np.column_stack(ax.lines[0].get_data(orig=True)) for ax in document.figure.axes]
    metrics = json.loads((source / "metrics.json").read_text(encoding="utf-8"))["static_scatter"]
    out = ROOT / f"results/{args.experiment}/presentation_figures"
    out.mkdir(parents=True, exist_ok=True)
    with matplotlib.rc_context({"font.family": "Yu Gothic", "font.size": 12,
                                "axes.labelcolor": "#1a2023", "text.color": "#1a2023",
                                "axes.spines.top": False, "axes.spines.right": False,
                                "axes.edgecolor": "#9caeb5", "xtick.color": "#45545a",
                                "ytick.color": "#45545a", "svg.fonttype": "none"}):
        fig = plt.figure(figsize=(10, 5.4), facecolor="white")
        ax = fig.add_axes((.07, .18, .51, .70))
        zoom = fig.add_axes((.68, .27, .27, .50))
        colors = ["#00739c", "#15855e"]
        for i, name in enumerate(("RTK", "PPK")):
            xy = values[i]
            rms = metrics[name]["horizontal_rms_m"]
            assert len(xy) == metrics[name]["count"]
            assert np.isclose(np.sqrt(np.mean(np.sum(xy**2, axis=1))), rms)
            ax.plot(*xy.T, "o", ms=3.5, mew=0, alpha=.7, color=colors[i],
                    label=f"{name}  RMS {rms:.3f} m")
            theta = np.linspace(0, 2*np.pi, 361)
            ax.plot(rms*np.cos(theta), rms*np.sin(theta), color=colors[i], lw=1.7, ls="--")
        bound = float(np.ceil(max(np.abs(xy).max() for xy in values)))
        ax.set(xlim=(-bound, bound), ylim=(-bound, bound), xlabel="東方向 [m]", ylabel="北方向 [m]")
        ax.set_aspect("equal", adjustable="box")
        ticks = 2*np.arange(-int(bound//2), int(bound//2)+1)
        ax.set_xticks(ticks)
        ax.set_yticks(ticks)
        ax.grid(color="#dce5e9", lw=.65, zorder=0)
        ax.axhline(0, color="#b1bfc5", lw=.85, zorder=0)
        ax.axvline(0, color="#b1bfc5", lw=.85, zorder=0)
        ax.legend(loc="upper left", frameon=False, fontsize=10)
        ax.set_title("全体（各方式の平均を原点）", fontsize=12, pad=12)
        ppk = values[1]
        assert np.all(np.abs(ppk) <= 1), "Do not silently crop observations from the labelled full-data inset"
        zoom.plot(*ppk.T, "o", ms=3.8, mew=0, alpha=.75, color=colors[1])
        radius = metrics["PPK"]["horizontal_rms_m"]
        zoom.plot(radius*np.cos(theta), radius*np.sin(theta), ls="--", lw=1.5, color=colors[1])
        zoom.set(xlim=(-1, 1), ylim=(-1, 1), xticks=[-1, 0, 1], yticks=[-1, 0, 1],
                 xlabel="東方向 [m]", ylabel="北方向 [m]")
        zoom.set_aspect("equal", adjustable="box")
        zoom.grid(color="#dce5e9", lw=.65)
        zoom.set_title("PPK の拡大（全240点）", fontsize=12, pad=12)
        fig.suptitle("静止区間の水平ばらつき", y=.98, fontsize=15)
        # Keep all notes as selectable Text Artists in the shared 2D editor.
        def note(x, y, text, **style):
            return ax.text(x, y, text, transform=fig.transFigure, clip_on=False, **style)
        note(.07, .058, "各240点・親動画240–480秒。破線は平均まわりの水平RMS半径（信頼区間ではない）。", fontsize=9)
        note(.07, .015, "独立した真値なし。平均位置の差・絶対精度は評価していません。", fontsize=9, color="#52636c")
        target = out / "static_scatter_presentation.png"
        save_editable_figure(fig, target, dpi=220)
        # Check the saved artifact, not merely the arrays passed to Matplotlib.
        saved = PlotDocument(out / "static_scatter_presentation.figure.pickle")
        target.with_suffix(".pdf").write_bytes(saved.render("pdf"))
        assert np.array_equal(np.column_stack(saved.figure.axes[0].lines[0].get_data()), values[0])
        assert np.array_equal(np.column_stack(saved.figure.axes[0].lines[2].get_data()), values[1])
        assert np.array_equal(np.column_stack(saved.figure.axes[1].lines[0].get_data()), values[1])
        plt.close(fig)
    record = {"experiment_id": args.experiment, "generated_utc": datetime.now(UTC).isoformat(),
              "git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
              "inputs_sha256": {str(p.relative_to(ROOT)): sha(p) for p in
                                 (master, source / "metrics.json", source / "manifest.json", Path(__file__))},
              "data_source": "Exact mean-centred arrays in saved research master",
              "all_240_points_per_series_retained": True, "saved_arrays_exactly_match_source": True,
              "new_filtering_or_interpolation": False, "means_independent_not_absolute_alignment": True,
              "ppk_zoom_limits_m": [-1, 1], "format": "Standalone 2D editable master + PNG/SVG/PDF; no slide framing"}
    (out / "manifest.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "report.md").write_text(
        "# 静止GNSSの発表用2D図\n\n![発表用図](static_scatter_presentation.png)\n\n"
        "元の研究用Figureから各方式240点の描画値をそのまま転記し、保存後の配列一致を検証。"
        "それぞれ自身の平均を原点に表示し、同じ軸の全体図と範囲を明示したPPK拡大図を配置。"
        "水平RMSは既存metricsを検算。除外・補間・元データ変更はない。\n\n"
        "[解析根拠](../gnss_overhead/report.md)と[来歴](manifest.json)。同じフォルダの"
        "SVG・figure.pickle・plot.jsonからブラウザ編集を継続できる。\n", encoding="utf-8")
    print(target)


if __name__ == "__main__":
    main()
