"""Report all wall reconstruction attempts, including degenerate and unregistered frames."""
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

os.environ.setdefault("OPENBLAS_NUM_THREADS", "2")
os.environ.setdefault("OMP_NUM_THREADS", "2")
import cv2
import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from aquabeacon_analysis.editable_plots import save_editable_figure
from aquabeacon_analysis.ngt_binary import sha256

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "data/interim/20261028_ngt_004/wall_mapping"
OUT = ROOT / "results/20261028_ngt_004/wall_mapping"
sys.path.insert(0, str(WORK / "runtime"))
import pycolmap as colmap


def read_csv(path):
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path, rows):
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", required=True, choices=["20261028_ngt_004"])
    parser.parse_args()
    cv2.setNumThreads(2)
    comparisons, details, inputs = [], [], []
    for hz, suffix in [(2, ""), (5, "five_hz")]:
        work, out = WORK / suffix, OUT / suffix
        frames = read_csv(work / "frames.csv")
        for mode in ["visual_only", "aquabeacon_prior"]:
            path = out / f"{mode}_metrics.json"
            if not path.exists():
                continue
            inputs.append({"path": str(path.relative_to(ROOT)), "sha256": sha256(path)})
            raw = json.loads(path.read_text())
            names, all_names = set(), set()
            valid_models, degenerate = [], 0
            for item in raw["models"]:
                folder = work / mode / str(item["model"])
                model = colmap.Reconstruction(folder)
                registered = {im.name for im in model.images.values() if im.has_pose}
                all_names.update(registered)
                valid = model.num_points3D() >= 50
                if valid:
                    names.update(registered)
                    valid_models.append((item, model, folder))
                else:
                    degenerate += 1
                row = {"sample_hz": hz, "mode": mode, **item, "nondegenerate_structure": valid,
                       "pointcloud": str((folder / "points.ply").relative_to(ROOT))}
                for k, v in row.items():
                    if isinstance(v, float) and not np.isfinite(v):
                        row[k] = None
                details.append(row)
            comparisons.append({"sample_hz": hz, "mode": mode, "input_images": len(frames),
                                "registered_any_unique": len(all_names),
                                "registered_nondegenerate_unique": len(names),
                                "registered_fraction": len(names)/len(frames),
                                "nondegenerate_components": len(valid_models), "degenerate_components": degenerate,
                                "points_in_nondegenerate_models": sum(m.num_points3D() for _, m, _ in valid_models),
                                "mapper_elapsed_s": raw["elapsed_s"],
                                "known_dimension_error_m": None})
            write_csv(out / f"{mode}_registration.csv", [{**r,
                "registered_nondegenerate": r["image_name"] in names,
                "registered_any_including_degenerate": r["image_name"] in all_names} for r in frames])
            if hz != 5 or not valid_models:
                continue
            fig, axes = plt.subplots(2, 2, figsize=(11, 8), layout="constrained")
            review = []
            for ax, (item, model, folder) in zip(axes.flat, valid_models[:4]):
                points = np.array([p.xyz for p in model.points3D.values()])
                colors = np.array([p.color for p in model.points3D.values()])/255.
                center = points.mean(axis=0)
                _, _, vh = np.linalg.svd(points-center, full_matrices=False)
                projected = (points-center) @ vh.T
                span = np.percentile(projected[:, 0], 95)-np.percentile(projected[:, 0], 5)
                projected /= span
                ax.scatter(projected[:, 0], projected[:, 1], c=colors, s=2, alpha=.8)
                ax.set(title=f"Model {item['model']}: {item['registered_images']} frames, {item['point_count']} points",
                       xlabel="Plane axis 1 / 90% span", ylabel="Plane axis 2 / 90% span")
                ax.axis("equal")
                ax.grid(alpha=.2)
                images = sorted([im for im in model.images.values() if im.has_pose], key=lambda im: im.name)
                im = images[len(images)//2]
                frame = cv2.imread(str(work / "source_frames" / im.name))
                for p in im.points2D:
                    if p.point3D_id in model.points3D:
                        cv2.circle(frame, tuple(np.round(p.xy).astype(int)), 3, (0, 0, 255), -1)
                cv2.rectangle(frame, (832, 36), (1260, 684), (255, 255, 255), 2)
                cv2.putText(frame, f"Model {item['model']} {im.name}", (15, 35),
                            cv2.FONT_HERSHEY_SIMPLEX, .8, (255, 255, 255), 2)
                review.append(cv2.resize(frame, (640, 360)))
                write_csv(folder / "plane_coordinates.csv", [{"x_model": p[0], "y_model": p[1], "z_model": p[2],
                          "plane1_normalized": v[0], "plane2_normalized": v[1], "normal_normalized": v[2]}
                          for p, v in zip(points, projected, strict=True)])
            for ax in list(axes.flat)[len(valid_models[:4]):]:
                ax.set_visible(False)
            fig.suptitle(f"Pool-wall fragments: {mode}; each independently normalized")
            save_editable_figure(fig, OUT / f"{mode}_wall_fragments.png", dpi=150)
            plt.close(fig)
            while len(review) % 2:
                review.append(np.zeros_like(review[0]))
            cv2.imwrite(str(OUT / f"{mode}_image_review.jpg"),
                        np.vstack([np.hstack(review[i:i+2]) for i in range(0, len(review), 2)]))
    write_csv(OUT / "mapping_metrics.csv", comparisons)
    (OUT / "models.json").write_text(json.dumps(details, indent=2, allow_nan=False))
    (OUT / "summary.json").write_text(json.dumps(comparisons, indent=2))
    vslam_path = ROOT / "results/20261028_ngt_004/child_only_vslam/summary.json"
    vslam = json.loads(vslam_path.read_text(encoding="utf-8"))
    inputs.append({"path": str(vslam_path.relative_to(ROOT)), "sha256": sha256(vslam_path)})
    (OUT / "report_manifest.json").write_text(json.dumps({"generator": str(Path(__file__).relative_to(ROOT)),
        "generator_sha256": sha256(Path(__file__)), "inputs": inputs,
        "created_utc": datetime.now(UTC).isoformat(), "git_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "degenerate_diagnostic": "fewer than 50 reconstructed points; this threshold does not certify other models",
        "scale_policy": "No measured wall dimension or nominal25m used to scale/score the reconstruction"}, indent=2))
    text = """# 長田高校プール 同時移動区間の壁マッピング評価

親機・子機を同時に約25 m移動させた区間004から、子機映像に右側の壁が現れる親動画1415〜1540秒を使用した。**壁の部分的な点群は復元できたが、区間全体をつなぐ25 m規模の連続モデルは得られなかった。** 実測した壁・タイル寸法がないため、寸法誤差は評価できない。

2 Hzの250枚と5 Hzの625枚をそれぞれ、画像だけのCOLMAPとAquaBeacon位置priorを使うCOLMAPで処理した。全同時移動は1260〜1540秒であり、壁の評価対象125秒はその44.6%。これより前の映像に壁が十分見えないことを代表画像で確認した。

## 登録率と地図の分断

| 抽出 | 位置prior | 形状を持つモデルに登録した画像 | 登録率 | 地図断片 | 点数 | 退化モデル | mapper時間 |
|---|---|---:|---:|---:|---:|---:|---:|
"""
    for r in comparisons:
        text += (f"| {r['sample_hz']} Hz | {'あり' if r['mode']=='aquabeacon_prior' else 'なし'} | "
                 f"{r['registered_nondegenerate_unique']} / {r['input_images']} | {100*r['registered_fraction']:.1f}% | "
                 f"{r['nondegenerate_components']} | {r['points_in_nondegenerate_models']:,} | {r['degenerate_components']} | {r['mapper_elapsed_s']:.1f} s |\n")
    text += """

登録画像は地図間の重複を除いて数えた。復元点が50個未満のモデルを退化の診断対象とし、今回の1点・11点のモデルを別に保持した。この閾値以上でも形状の正確さを保証するものではない。未登録画像もregistration.csvに全行を残した。5 Hzで重なりを増やしたが、本試行では位置priorを加えても地図の分断を解消できなかった。

## 復元した壁の形状

![画像のみの壁断片](visual_only_wall_fragments.png)

![位置priorありの壁断片](aquabeacon_prior_wall_fragments.png)

各断片を独立に平面へ投影し、その断片の主軸5〜95%幅で正規化して表示した。異なる断片の相対位置・尺度を合わせた地図ではない。壁を平面へ押し付ける拘束は再構成に使っていない。

| 抽出 | prior | 断片 | 親動画時刻 | 再投影平均 | 平面RMS ÷ 幅 | 尺度 |
|---|---|---|---|---:|---:|---|
"""
    for r in details:
        if not r["nondegenerate_structure"]:
            continue
        scale = "位置prior依存" if r["mode"] == "aquabeacon_prior" else "単眼任意尺度"
        text += (f"| {r['sample_hz']} Hz | {'あり' if r['mode']=='aquabeacon_prior' else 'なし'} | "
                 f"{r['model']} | {r['first_parent_video_s']:.1f}〜{r['last_parent_video_s']:.1f} s | "
                 f"{r['mean_reprojection_px']:.3f} px | {100*r['wall_plane_rms_over_span']:.2f}% | {scale} |\n")
    text += """

平面からの残差は形状の内部整合性で、実壁の測量面からの誤差ではない。位置priorありのメートル尺度も同じ測位を入力しているので、測位精度を独立に検証する真値には使えない。壁ROIには浮遊物やロープも入り得るため、全点が壁だと保証していない。

![位置priorありで使った特徴点の画像確認](aquabeacon_prior_image_review.jpg)

白枠が特徴抽出に使った右側領域、赤点が復元点に結び付いた画像特徴。実物に戻って対応を確認できるよう、CLAHE前の画像も保存した。

"""
    text += ("## 子機のみの単眼VSLAMとの関係\n\n"
             f"別チャットで完了したORB-SLAM3は同時移動004の全{vslam['input_frames']:,}フレームを処理し、"
             f"追跡状態OKは{100*vslam['tracked_fraction']:.2f}%、最大地図に保存された姿勢は"
             f"{100*vslam['saved_pose_fraction']:.2f}%。地図は{vslam['map_count']}個、点は合計{vslam['map_points']:,}個だった。"
             "これは全280秒・約120 fps・全画面の評価で、本表の壁区間125秒・ROI・2/5 Hzの登録率と分母が異なる。"
             "数値の大小だけで手法の優劣を決めない。\n\n"
             "VSLAMの局所地図は画像対応や部分形状の補助に使えるが、異なる地図の座標と単眼尺度はつながっていない。"
             "今回の位置推定の独立基準や、連続した25 m地図としては扱えない。"
             f"保存区間の1秒回転角変化量と子機磁北姿勢の差はRMSE "
             f"{vslam['rotation_angle_magnitude_consistency']['magnetic_1s']['rmse_deg']:.2f}°で、"
             "全3軸の姿勢誤差・絶対方位誤差ではない。"
             "[単眼VSLAMの評価・地図](../child_only_vslam/report.md)へ保存済み。\n\n")
    text += """

## 条件と保存物

- 子機LRF 1280×720、沖縄水中校正のproxy直接値。焦点距離・歪みは固定。
- 特徴抽出は右側壁ROI x=832〜1259、y=36〜683。LABのL成分CLAHE clip=2、8×8。
- 同じ画像・特徴対応でprior有無を比較。位置priorは光学音響+親機GNSSの水平座標と圧力深度、経験的sigma 1/1/0.3 m。中心一致・磁気偏角0°を仮定。欠測を埋めない。
- CPU2スレッド、特徴抽出と照合600秒、mapper240秒、最大5モデル。上限内で得た結果であり、別設定での改善可能性を否定しない。
- 単眼VSLAMは別チャットの完了結果を読み取り、入力JSONのハッシュをreport_manifest.jsonへ記録した。

[共通指標CSV](mapping_metrics.csv)、[断片詳細](models.json)、[2 Hzの設定](manifest.json)、[5 Hzの設定](five_hz/manifest.json)。各phaseのlogとprocess.jsonに開始時刻・PID・終了を残した。5 Hzの特徴抽出初回は終了状態を読むPython APIの型違いで終了記録に失敗したが、計算済みDBを再利用して検証・完了した。初回ログも保持した。

点群・COLMAPモデル・原画像と処理画像は `data/interim/20261028_ngt_004/wall_mapping/` に保存。`five_hz/visual_only/<model>/points.ply` と `five_hz/aquabeacon_prior/<model>/points.ply` が5 Hz比較で、画像・姿勢・点のTXTも同じ場所にある。実際の原本PTSと利用した位置はframes.csv・frame_priors.csvで追跡できる。

図はPNG/SVG/figure.pickle/plot.jsonを併置。`python -m aquabeacon_analysis.editable_plots results/20261028_ngt_004/wall_mapping/aquabeacon_prior_wall_fragments.figure.pickle` でブラウザ編集し、設定とPNG/SVG/PDFへ保存できる。

再実行はREADMEの4phaseコマンド。5 Hz診断は `--dense-sampling` を追加する。使用した位置prior方式は[COLMAP公式説明](https://github.com/colmap/colmap/blob/main/doc/faq.rst)に従い、事後的なスケール合わせと区別した。
"""
    (OUT / "report.md").write_text(text, encoding="utf-8")
    shutil.copy2(__file__, OUT / "generate_report.py")
    print(json.dumps(comparisons, ensure_ascii=False))


if __name__ == "__main__":
    main()
