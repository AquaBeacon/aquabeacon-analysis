"""Render reconstructed fragments and assemble the Nanyo analysis report."""

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pycolmap
from analyze_nanyo import read

from aquabeacon_analysis.editable_plots import save_editable_figure
from aquabeacon_analysis.ngt_binary import sha256


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", required=True)
    args = parser.parse_args()
    root = Path.cwd()
    review_path = root / f"config/{args.experiment}/mapping_review.json"
    review = json.loads(review_path.read_text(encoding="utf-8"))
    out = root / f"results/{args.experiment}/analysis"
    evaldir = out.parent / "evaluation"
    json.loads((evaldir / "manifest.json").read_text())
    comparison = json.loads((evaldir / "base_comparison.json").read_text())
    lines = [
        "# Nanyo海上実験：GNSS・音響・子機位置・地図",
        "",
        (
            "2026-10-31、前回同期・分割した001/002を解析した。RTKは未使用。"
            "GNSS・音響の評価と条件付き相対位置、子機画像のみのCOLMAPを分けて保存した。"
        ),
        "",
        "## GNSS：単独測位とPPK",
        "",
        (
            "UBXの3,437エポックは全てcarrier_solution=0・非差分測位。RAWX/SFRBXから"
            "RINEXを生成し、GEONET 950437（約5.25 km）を主局、041133（約8.47 km）を比較局にした。"
            "RTKLIB EX 2.5.1、kinematic/combined、GPS+Galileo+QZSS、仰角15°、"
            "30秒基準局観測・最大age15秒。AR continuousとAR offの両方を保存。"
        ),
        "",
        (
            "全収録の主局解は固定340・Float 2,291・DGNSS 806時刻。"
            "RINEXヘッダの基準局座標を用い、測地系epochやアンテナ高の追加補正は未実施。"
            "船の実移動を含むため静止誤差評価とはしない。"
        ),
        "",
        (
            f"基準局変更による水平差：中央値 {comparison['horizontal_base_solution_difference_median_m']:.3f} m、"
            f"95%点 {comparison['horizontal_base_solution_difference_p95_m']:.3f} m。測量真値との差ではない。"
        ),
        "",
        "[全SPP/PPK CSVとmanifest](../evaluation/) / [比較局結果](../evaluation/base_comparison.json)",
        "",
        "![GNSS俯瞰](../evaluation/gnss_overhead.png)",
        "",
        "![航空写真](../evaluation/gnss_aerial.png)",
        "",
        "航空写真は国土地理院。位置の真値ではない。Web Mercator軸は地表距離そのものではない。",
        "",
        "## 区間別の音響と位置候補",
        "",
        "| 区間 | 全shot | 音響候補 | 候補率 | PPK固定/Float/DGNSS | カメラ相対位置候補 |",
        "|---|---:|---:|---:|---|---:|",
    ]
    if (out.parent / "absolute_position/report.md").exists():
        lines[3:3] = [
            (
                "**追加解析あり：** [子機の絶対位置・位置拘束マッピング](../absolute_position/report.md)。"
                "このページの位置・地図に関する未確定事項は初回解析時点の記録。固定取付けの確認、"
                "絶対姿勢ログの追加解釈、真北・CAD補正を適用した最新結果はリンク先を参照。"
            ),
            "",
        ]
    summaries = []
    for suffix in ["001", "002"]:
        eid = "20261031_nanyo_" + suffix
        folder = root / f"results/{eid}"
        metrics = json.loads((folder / "evaluation/metrics.json").read_text())
        relative = json.loads((folder / "relative_position/manifest.json").read_text())
        mapping = json.loads((folder / "visual_mapping/manifest.json").read_text())
        q = metrics["ppk_quality_counts"]
        lines.append(
            f"| {suffix} | {metrics['shot_count']} | {metrics['candidate_count']} | "
            f"{metrics['candidate_fraction'] * 100:.1f}% | {q.get('1', 0)}/{q.get('2', 0)}/{q.get('4', 0)} | "
            f"{relative['candidate_positions']} |"
        )
        # Verify invariants without reclassifying or hiding unregistered observations.
        positions = read(folder / "relative_position/camera_relative_candidates.csv")
        finite = 0
        for r in positions:
            xyz = np.array(
                [
                    float(r[k])
                    for k in ["x_right_candidate_m", "y_down_candidate_m", "z_forward_candidate_m"]
                ]
            )
            assert r["global_position_valid"] == "False"
            if np.isfinite(xyz).all():
                assert abs(np.linalg.norm(xyz) - float(r["range_m"])) < 1e-8
                finite += 1
        assert finite == relative["candidate_positions"]
        status = read(folder / "visual_mapping/frame_status.csv")
        assert len(status) == mapping["sampled_frames"]
        assert sum(r["registered"] == "True" for r in status) == mapping["registered_frames"]
        for model_info in mapping["models"]:
            mid = model_info["id"]
            model = pycolmap.Reconstruction(
                root / f"data/interim/{eid}/visual_mapping/models/{mid}"
            )
            assert model.num_reg_images() == model_info["registered_images"]
            xyz = np.array([p.xyz for p in model.points3D.values()])
            rgb = np.array([p.color for p in model.points3D.values()]) / 255
            fig = plt.figure(figsize=(9, 7))
            ax = fig.add_subplot(111, projection="3d")
            ax.scatter(*xyz.T, c=rgb, s=1, alpha=0.6)
            ax.set(
                xlabel="X [arbitrary units]",
                ylabel="Y [arbitrary units]",
                zlabel="Z [arbitrary units]",
                title=f"{eid} model {mid}: separate monocular fragment",
            )
            ax.set_box_aspect(np.maximum(np.ptp(xyz, axis=0), 0.01))
            fig.tight_layout()
            fig.savefig(folder / f"visual_mapping/model_{mid}.png", dpi=160)
            plt.close(fig)
            names = sorted(image.name for image in model.images.values())
            selected = [names[0], names[len(names) // 2], names[-1]]
            fig, axes = plt.subplots(1, 3, figsize=(12, 3))
            for ax, name in zip(axes, selected, strict=True):
                ax.imshow(plt.imread(root / f"data/interim/{eid}/visual_mapping/images" / name))
                ax.set_title(name)
                ax.axis("off")
            fig.tight_layout()
            fig.savefig(folder / f"visual_mapping/model_{mid}_views.png", dpi=120)
            plt.close(fig)
        fig, ax = plt.subplots(figsize=(10, 3))
        ax.scatter(
            [float(r["approx_source_video_s"]) for r in status],
            [int(r["sift_features"]) for r in status],
            c=["#16856d" if r["registered"] == "True" else "#c5734e" for r in status],
            s=5,
        )
        ax.set(
            xlabel="Child source video [s]",
            ylabel="SIFT features",
            title="Green: registered; brown: unregistered (all sampled frames retained)",
        )
        fig.tight_layout()
        save_editable_figure(fig, folder / "visual_mapping/coverage.png", dpi=150)
        plt.close(fig)
        summaries.append(
            {"experiment_id": eid, "evaluation": metrics, "relative": relative, "mapping": mapping}
        )
        (folder / "report.md").write_text(
            f"# {eid}\n\n[統合解析レポート](../20261031_nanyo_000/analysis/report.md)\n\n"
            "[GNSS/音響](evaluation/) / [条件付き相対位置](relative_position/) / [画像地図](visual_mapping/)。\n",
            encoding="utf-8",
        )
    lines += [
        "",
        (
            "音響候補はstate3かつ正値。今回のprofile0、音速1500 m/s、補正0 µsを保持し、"
            "前回の55 µsを流用していない。検出候補率は測位成功率や正しい直接波の割合ではない。"
            "設定64組の集計CSVも各区間へ保存した。"
        ),
        "",
        (
            "圧力水深と距離の差は主に負になる。親機も水中に入り、親機深度・レバーアーム・"
            "海水音速・圧力基準が未確定のため、この差を測距誤差や失敗判定にはしない。"
        ),
        "",
        (
            "位置候補は親機光学右/下/前座標。光点候補＋最寄り全shot（0.3秒以内）と"
            "共通水中内部校正を用いた。光点同定・水中判定は未検証、音響/光学中心の一致を仮定。"
            "船上・欠測は補完しない。親機カメラが動くので固定世界の子機軌跡にはならない。"
        ),
        "",
        (
            "今回の光学/機体extrinsicと取付けが確認できないため、GNSS座標に重ねた子機の"
            "絶対3D位置や、それを拘束条件にしたメートル尺度地図は未確定。"
        ),
        "",
        "## 子機だけの画像マッピング",
        "",
        (
            "前回のORB-SLAM3用Dockerが応答せず、今回はCOLMAP 1 Hz・960×540・CLAHE・"
            "固定の共通水中内部校正・逐次5近傍照合で実行。native120fps VSLAM/VI-SLAMの"
            "再実行結果ではない。光学条件の完全一致は未確認。地図は任意尺度の独立断片である。"
        ),
        "",
        (
            "画像時刻は公称1 Hzビン（最大1秒幅の近似）であり、原フレームPTSは未保存。"
            "同期済み姿勢・圧力との精密な融合には再抽出が必要。"
        ),
    ]
    for s in summaries:
        eid = s["experiment_id"]
        m = s["mapping"]
        scene_review = review["segments"][eid]
        rel = f"../../{eid}"
        lines += [
            "",
            f"### {eid}",
            "",
            (
                f"登録 {m['registered_frames']}/{m['sampled_frames']}画像（{m['registered_fraction'] * 100:.1f}%）、"
                f"モデル {len(m['models'])}個。未登録はframe_status.csvに残した。"
            ),
            "",
            f"![音響と水深]({rel}/evaluation/acoustic_depth.png)",
            "",
            f"![相対位置候補]({rel}/relative_position/relative_candidates.png)",
            "",
            f"[実映像と光点の確認]({rel}/relative_position/light_review.jpg)",
            "",
            f"![地図登録範囲]({rel}/visual_mapping/coverage.png)",
            "",
            scene_review["summary"],
        ]
        for model in m["models"]:
            mid = model["id"]
            lines += [
                "",
                (
                    f"モデル{mid}: {model['registered_images']}画像、{model['points']}点、"
                    f"平均再投影残差 {model['mean_reprojection_error_px']:.3f} px（物理精度ではない）。"
                ),
                "",
                f"![点群]({rel}/visual_mapping/model_{mid}.png)",
                "",
                scene_review["models"][str(mid)],
                "",
                f"![登録画像の先頭・中間・末尾]({rel}/visual_mapping/model_{mid}_views.png)",
                "",
                (
                    f"[PLY点群]({rel}/visual_mapping/model_{mid}.ply) / "
                    f"[カメラ位置CSV]({rel}/visual_mapping/model_{mid}_poses.csv)"
                ),
            ]
    lines += ["", "10画像未満のモデルは低支持の小断片として保持し、完成した地図とは扱わない。"]
    lines += [
        "",
        "## 検証・再現",
        "",
        (
            "関連自動試験17件合格。UBXチェックサム、RTKゼロ、候補数・距離ノルム、"
            "全地図サンプルの登録数を確認。2D図はPNG/SVG/figure.pickle/plot.jsonを保持。"
            "設定config/20261031_nanyo_000/evaluation.json、スクリプトscripts/*nanyo*.py。"
        ),
        "",
        (
            "入力原本・旧解析は変更していない。比較局、PPK設定とコマンド、ダウンロード要求、"
            "ハッシュ、マッピング実行ログは実験ID配下に保持。再計算はREADMEのコマンドを参照。"
        ),
        "",
        (
            "[GEONETデータ提供元](https://terras.gsi.go.jp/) / "
            "[RTKLIB EX 2.5.1](https://github.com/rtklibexplorer/RTKLIB/releases/tag/v2.5.1)。"
        ),
    ]
    (out / "report.md").write_text("\n".join(lines), encoding="utf-8")
    (out / "verification.json").write_text(
        json.dumps(
            {
                "status": "passed",
                "segments": summaries,
                "scripts": {str(p): sha256(p) for p in Path("scripts").glob("*nanyo*.py")},
                "manual_mapping_review": {"path": str(review_path), "sha256": sha256(review_path)},
            },
            indent=2,
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
