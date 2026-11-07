"""Render and verify the pool wall ablation without modifying numerical runs."""
from __future__ import annotations

import html
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from diagnose_pool_mapping import CONFIG, paths, readonly_database
from evaluate_position_fallback import dump, table, write_csv
from improve_nanyo_light import ROOT, read, relative
from PIL import Image, ImageDraw
from write_field_review import html_page

from aquabeacon_analysis.editable_plots import PlotDocument, save_editable_figure
from aquabeacon_analysis.mapping_diagnostics import (
    compare_priors,
    temporal_connectivity,
    zero_cut_intervals,
)
from aquabeacon_analysis.ngt_binary import sha256

LABELS = {"visual_only": "位置拘束なし", "old": "旧位置", "new": "新位置", "old_repeat": "旧位置・再実行"}


def figures(out, frames, pairs, manifest):
    plt.rcParams.update({"font.family": "Yu Gothic", "font.size": 10})
    times = np.array([float(r["parent_video_s"]) for r in frames])
    fig, axes = plt.subplots(4, 1, figsize=(10, 10), sharex=True, layout="constrained")
    axes[0].plot(times, [int(r["keypoints"]) for r in frames], color="#275d86", label="壁領域の特徴点")
    axes[0].set_ylabel("特徴点数"); axes[0].legend(loc="upper left")
    axes[1].plot(times, [int(r["best_pair_inliers"]) for r in frames], color="#19876b", label="各画像の最大対応点数")
    axes[1].axhline(30, color="#bd6133", linestyle="--", label="初期化の点数目安")
    axes[1].set_ylabel("幾何検証後の対応点数"); axes[1].legend(loc="upper left")
    axes[2].plot(times[:-1], [int(r["verified_crossing_after"]) for r in frames[:-1]], color="#6f5291",
                 label="時刻の境界をまたぐ対応ペア数")
    axes[2].axhline(0, color="black", linewidth=.6); axes[2].set_ylabel("連結する画像ペア数")
    axes[2].legend(loc="upper left")
    for i, mode in enumerate(manifest["modes"]):
        p = out/(mode["mode"]+"_registered_images.csv")
        rows = read(p) if p.exists() else []
        axes[3].scatter([float(r["parent_video_s"]) for r in rows], [i]*len(rows), s=24, label=LABELS[mode["mode"]])
    axes[3].set_yticks(range(4), [LABELS[m["mode"]] for m in manifest["modes"]]); axes[3].set_ylabel("登録された画像")
    axes[3].set_xlabel("親機動画時刻 [s]")
    for ax in axes:
        ax.grid(alpha=.22); ax.set_xlim(times[0], times[-1])
    save_editable_figure(fig, out/"matching_timeline.png", dpi=160)
    (out/"matching_timeline.pdf").write_bytes(PlotDocument(out/"matching_timeline.figure.pickle").render("pdf"))
    plt.close(fig)
    valid = [p for p in pairs if p["tri_angle_deg"]]
    fig, ax = plt.subplots(figsize=(9, 3.6), layout="constrained")
    ax.scatter([(float(p["time_a_s"])+float(p["time_b_s"]))/2 for p in valid],
               [float(p["tri_angle_deg"]) for p in valid], s=14, alpha=.65, label="校正付き2視点からの推定値")
    ax.axhline(1.5, color="#bd6133", linestyle="--", label="初期化の視差目安 1.5°")
    ax.set_yscale("log"); ax.set_xlabel("画像ペアの中央時刻・親機動画 [s]"); ax.set_ylabel("推定三角測量角 [deg]")
    ax.set_xlim(times[0], times[-1]); ax.grid(alpha=.22); ax.legend(loc="upper left")
    save_editable_figure(fig, out/"pair_parallax.png", dpi=160)
    (out/"pair_parallax.pdf").write_bytes(PlotDocument(out/"pair_parallax.figure.pickle").render("pdf"))
    plt.close(fig)


def evidence_images(out, source, source_work):
    rows = read(source/"mapping_frame_priors.csv"); images = source_work/"inputs/images"
    cases = [(38, "約4秒の分断区間。壁の模様は見えるが、今回の特徴点対応では連結を保てない。"),
             (140, "手前に細い物体が写り、壁の見え方も変化する。画面内の点をすべて静止環境とは仮定できない。"),
             (209, "別の約4秒の分断区間。壁の明暗と形の見え方が大きく変化している。"),
             (245, "後半の最大連結成分に入る。壁の模様の対応が回復するが、連続した3D形状には不足。"),
             (290, "4画像・12点の小さな再構成付近。壁の細部はぼけ、形状全体を支える点は少ない。")]
    output = []
    for i, explanation in cases:
        src = images/rows[i]["image"]
        output.append({"index": i, "parent_video_s": float(rows[i]["parent_video_s"]),
                       "image": relative(src), "sha256": sha256(src), "explanation": explanation})
    canvas = Image.new("RGB", (1280, 390*3), "white"); draw = ImageDraw.Draw(canvas)
    for j, case in enumerate(output):
        with Image.open(ROOT/case["image"]) as opened:
            im = opened.copy(); im.thumbnail((640, 360))
        x, y = (j % 2)*640, (j//2)*390
        canvas.paste(im, (x, y)); draw.text((x+10, y+365), f"index {case['index']}, parent {case['parent_video_s']:.3f} s", fill="black")
    canvas.save(out/"child_frames.jpg", quality=94)
    dump(out/"image_review.json", output)
    return output


def log_summary(out):
    text = (out/"run.log").read_text(encoding="utf-8", errors="replace")
    result = []
    for mode in LABELS:
        block = text.split("BEGIN MODE "+mode+" ", 1)[1].split("END MODE "+mode+" ", 1)[0]
        result.append({"mode": mode,
                       "bad_initial_pair": block.count("Discarding reconstruction due to bad initial pair"),
                       "insufficient_size": block.count("Discarding reconstruction due to insufficient size"),
                       "linear_solver_failure": block.count("Linear solver failure"),
                       "prior_alignment_failure": block.count("Final alignment w.r.t. prior positions failed")})
    write_csv(out/"log_summary.csv", result)
    return result


def report(cfg):
    out, _work, source, source_work = paths(cfg)
    manifest = json.loads((out/"manifest.json").read_text()); audit = json.loads((out/"image_audit.json").read_text())
    frames, pairs = read(out/"image_diagnostics.csv"), read(out/"pair_diagnostics.csv")
    figures(out, frames, pairs, manifest); cases = evidence_images(out, source, source_work); logs = log_summary(out)
    counts, tracks = [], []
    for mode in manifest["modes"]:
        points = sum(m["points"] for m in mode["models"])
        p = out/(mode["mode"]+"_points.csv")
        rows = read(p) if p.exists() else []
        longest = max((float(r["duration_s"]) for r in rows), default=0.)
        counts.append({"mode": mode["mode"], "prior_images": mode["prior_count"], "registered_images": mode["registered_unique"],
                       "models": len(mode["models"]), "points_sum": points, "longest_point_track_s": longest})
        tracks.append([LABELS[mode["mode"]], mode["prior_count"], mode["registered_unique"], points, f"{longest:.3f}"])
    write_csv(out/"ablation_summary.csv", counts)
    spacing = []
    for gap in range(1, 11):
        selected = [p for p in pairs if abs(int(p["frame_gap"])) == gap]
        spacing.append({"frame_gap": gap, "nominal_dt_s": gap/10, "tested_pairs": len(selected),
                        "verified_pairs": sum(int(p["inliers"]) >= 15 for p in selected)})
    write_csv(out/"spacing_summary.csv", spacing)
    intervals = []
    for lo, hi in ((1505, 1528), (1528, 1535)):
        selected = [p for p in pairs if min(float(p["time_a_s"]), float(p["time_b_s"])) >= lo
                    and max(float(p["time_a_s"]), float(p["time_b_s"])) < hi]
        intervals.append({"start_s": lo, "end_s": hi, "pairs": len(selected),
                          "calibrated_pose_pairs": sum(bool(p["tri_angle_deg"]) for p in selected),
                          "initial_indicators": sum(p["initial_pair_indicator"] == "True" for p in selected)})
    write_csv(out/"interval_summary.csv", intervals)
    gaps = sorted(audit["unbridged_intervals"], key=lambda r: r["duration_s"], reverse=True)
    text = "# プール004：マッピングを制限している要因の切り分け\n\n"
    text += ("親機動画1505〜1535秒の壁が見える区間を、同じ300画像・特徴点・対応点・内部校正で比較した。"
             "主な制限は、画像をまたぐ特徴の対応が途切れることと、3D形状を支える観測が短いことだった。"
             "位置拘束を外しても連続した地図は得られず、この条件では位置フォールバックだけを失敗原因とはできない。\n\n"
             "**この区間の旧位置と新位置は、252点の座標と48画像の欠測まで完全に一致した。** "
             "したがって、前回のこの区間の点数差を光源検出改善の効果と解釈しない。\n\n")
    text += "## 同じ画像による比較\n\n"+table(["位置拘束", "位置入力付き画像", "登録画像", "点数・全モデル合計", "最長の点追跡 [s]"], tracks)
    text += ("\n\nどの条件も最大2つの小断片で、実寸地図は採用していない。登録画像数には、ほとんど3D点を持たない画像も含む。"
             "旧位置の再実行でも結果がわずかに変わった。pipeline・mapper・triangulatorのseedを明示しても、"
             "CPU2スレッド等を含む今回の実行は完全決定的ではない。10対11画像、12対13点の差を方式の優劣に使わない。"
             "この対照だけでは、位置拘束による小断片の幾何への影響がゼロだとも断定できない。\n\n")
    text += "## 特徴点数より、対応の継続が制限\n\n"
    text += (f"壁領域の特徴点は1画像あたり{audit['keypoints']['min']:.0f}〜{audit['keypoints']['max']:.0f}点、"
             f"中央値{audit['keypoints']['median']:.1f}点。しかし、検査した{audit['pairs_tested']}組のうち、"
             f"幾何検証後に15点以上残ったのは{audit['verified_pairs']}組（{100*audit['verified_pairs']/audit['pairs_tested']:.1f}%）。"
             f"{audit['isolated_images']}/300画像は、検査範囲のどの画像ともこの条件でつながらない。"
             f"対応グラフは{len(audit['verified_component_sizes'])}成分に分かれ、最大でも{audit['verified_component_sizes'][0]}画像だった。\n\n"
             "特徴点を多く見つけられても、同じ物体上の同じ点として次の画像へ引き継げなければ地図にならない。"
             "しきい値を下げて検出数だけを増やす効果は、この結果からは支持できない。\n\n"
             "![対応の継続と登録時刻](matching_timeline.png)\n\n")
    text += table(["対応がつながらない範囲・親機動画 [s]", "長さ [s]"],
                  [[f"{r['start_s']:.3f}〜{r['end_s']:.3f}", f"{r['duration_s']:.3f}"] for r in gaps[:5]])
    text += ("\n\nこの範囲は、時刻の境界をまたぐ検証済みの画像ペアがない区間。隣接だけでなく、最大10画像先までの飛び越し対応も数えた。"
             "オンラインSLAMの追跡喪失時刻ではなく、この10 Hzサンプルと対応探索範囲の診断である。\n\n")
    text += "## 視差と校正付き幾何\n\n"
    text += (f"校正付き2視点幾何から姿勢を推定できたのは{audit['calibrated_angles_deg']['count']}ペア。"
             f"推定三角測量角の中央値は{audit['calibrated_angles_deg']['median']:.2f}°で、"
             f"点数30以上・角度1.5°以上・前進成分の条件を満たす初期化候補の目安は{audit['initial_pair_indicators']}ペアだけだった。"
             "これは完全な初期化判定の再現ではなく、保存済み対応からの診断指標である。\n\n")
    text += table(["時刻 [s]", "検査ペア", "校正付き姿勢を推定", "初期化候補の目安"],
                  [[f"{r['start_s']}〜{r['end_s']}", r["pairs"], r["calibrated_pose_pairs"], r["initial_indicators"]] for r in intervals])
    text += ("\n\n![2視点からの推定三角測量角](pair_parallax.png)\n\n"
             "前半では対応があっても校正付き3D幾何として残りにくく、後半に成立候補が集中する。"
             "別に192ペアが平面または回転に近い分類、173ペアが未校正の幾何分類になった。"
             "この分類だけで、実際の動きが純回転だった、または内部校正が誤っていたとは断定できない。"
             "壁の平面性、誤対応、ぼけ、動く前景、水中校正モデルの不一致は追加切り分けが必要。\n\n")
    text += "## 実画像での確認\n\n![子機画像の確認](child_frames.jpg)\n\n"
    for case in cases:
        link = Path(os.path.relpath(ROOT/case["image"], out)).as_posix()
        text += f"- 親機動画{case['parent_video_s']:.3f}秒：[子機の原抽出画像]({link})。{case['explanation']}\n"
    text += ("\n近距離の壁は写っているが、細部のぼけと似た模様、明暗の変化、前景の物体が確認できる。"
             "見た目は対応の不安定さと整合するが、これだけで各原因の寄与率を定量化しない。\n\n")
    text += "## 位置入力と数値計算の留保\n\n"
    text += ("後半の旧新位置入力は同一で、約0.1秒あたり最大0.527 mの変化を含む。これは推定位置の変化量であり、実速度の真値ではない。"
             "中心一致・磁北とGNSSの偏角0°・位置拘束に仮定した標準偏差[1,1,3] mという暫定条件も残る。"
             "それでも、位置拘束を完全に外した条件でも対応と地図が回復しなかったため、今回の主な制限を位置入力だけでは説明できない。\n\n")
    text += table(["条件", "初期画像ペア不成立のログ回数", "小規模モデル破棄", "線形計算失敗", "位置への最終整合失敗"],
                  [[LABELS[r["mode"]], r["bad_initial_pair"], r["insufficient_size"], r["linear_solver_failure"], r["prior_alignment_failure"]] for r in logs])
    text += ("\n\nこれらは試行ログの出現回数で、独立した画像数や失敗率ではない。全条件は時間上限前に終了した。"
             "特徴点と対応は既存DBを再利用しており、GPUによる同じ計算の高速化だけで不足した幾何が増えるわけではない。\n\n"
             "## 次の改善で優先すること\n\n"
             "1. 特徴点数の増加より、短い時間間隔での追跡の継続を先に検証する。今回の分断区間を30〜120 Hzの短い窓で試し、"
             "追跡した同じ点を、十分な視差のある画像まで保てるかを見る。高密度化だけで視差が増えるわけではない。\n"
             "2. 対応が比較的残る1528〜1535秒で、校正付き幾何と平面モデル、実画像の対応位置を照合する。"
             "前景や浮遊物への誤対応を除き、固定壁上の長い点追跡を確保してから地図を伸ばす。\n"
             "3. その画像側の基準ができてから、位置拘束の重み・時刻・測定中心の感度を比較する。"
             "今の断片の点数差だけでは、光源検出や位置フォールバックの改善を地図の改善として評価できない。\n\n"
             "これは選んだ30秒・300画像・既存SIFT対応についての診断であり、元動画全体や他の特徴追跡法の限界ではない。\n\n"
             "## 再現用データ\n\n"
             "[比較表CSV](ablation_summary.csv) / [画像ごとの診断](image_diagnostics.csv) / [全画像ペア](pair_diagnostics.csv) / "
             "[連結成分](components.csv) / [分断区間](unbridged_intervals.csv) / [間隔別集計](spacing_summary.csv) / "
             "[ログ](run.log) / [入力と実行条件](manifest.json) / [写真確認記録](image_review.json)\n\n"
             "2次元図にはPNG・SVG・PDF・`.figure.pickle`・`.plot.json`を保存した。既存の研究グラフエディタで軸・系列等を編集できる。"
             "数値・欠測・採否は見た目の編集で変えない。3D点群とCOLMAPモデルは診断用の局所座標で保存し、既存地図へ結合していない。\n")
    for mode in manifest["modes"]:
        text += f"\n- [{LABELS[mode['mode']]}の詳細]({mode['mode']}_summary.json)"
        for model in mode["models"]:
            text += f" / [model {model['model']} 点群]({model['ply']})"
    (out/"report.md").write_text(text+"\n", encoding="utf-8")
    (out/"report.html").write_text(html_page("プール004：マッピング不成立の切り分け", text), encoding="utf-8")
    inputs = [CONFIG, Path(__file__), out/"manifest.json", out/"image_audit.json", out/"image_diagnostics.csv",
              out/"pair_diagnostics.csv", out/"run.log", ROOT/"src/aquabeacon_analysis/editable_plots.py"]
    dump(out/"report_manifest.json", {"created_utc": datetime.now(UTC).isoformat(), "inputs_sha256": {relative(p): sha256(p) for p in inputs},
                                      "scope": cfg["scope"], "accuracy_validated": False})
    print(out/"report.html")


def verify(cfg):
    out, work, source, source_work = paths(cfg)
    manifest = json.loads((out/"manifest.json").read_text()); audit = json.loads((out/"image_audit.json").read_text())
    for name in ("manifest.json", "report_manifest.json"):
        for path, digest in json.loads((out/name).read_text())["inputs_sha256"].items():
            assert sha256(ROOT/path) == digest, path
    prior_rows = read(source/"mapping_frame_priors.csv")
    for path, digest in manifest["source_image_hashes"].items():
        assert sha256(ROOT/path) == digest, path
    for row in prior_rows:
        assert sha256(source_work/"inputs/images"/row["image"]) == row["image_sha256"]
    a, b = [np.array([[float(r[f"{v}_{x}_m"]) for x in "enu"] for r in prior_rows]) for v in ("old", "new")]
    assert compare_priors(a, b) == audit["prior_comparison"]
    assert audit["prior_comparison"]["identical"]
    pairs, frames = read(out/"pair_diagnostics.csv"), read(out/"image_diagnostics.csv")
    graph = temporal_connectivity(len(frames), [(int(p["index_a"]), int(p["index_b"])) for p in pairs if int(p["inliers"]) >= 15])
    assert graph["component_sizes"] == audit["verified_component_sizes"]
    assert zero_cut_intervals(graph["crossing"], [float(r["parent_video_s"]) for r in frames]) == audit["unbridged_intervals"]
    assert audit["verified_pairs"] == sum(int(p["inliers"]) >= 15 for p in pairs)
    with readonly_database(source_work/"features.db") as db:
        assert db.execute("SELECT count(*) FROM images").fetchone()[0] == len(frames) == 300
        stored = dict(db.execute("SELECT pair_id,rows FROM two_view_geometries"))
        assert {int(p["pair_id"]): int(p["inliers"]) for p in pairs} == stored
        tables = {name: db.execute("SELECT * FROM "+name+" ORDER BY 1").fetchall()
                  for name in ("images", "cameras", "keypoints", "descriptors", "matches", "two_view_geometries")}
    sys.path.insert(0, str(ROOT/cfg["runtime"]))
    import pycolmap as pc
    priors = {}
    for mode in manifest["modes"]:
        label = mode["mode"]
        with readonly_database(work/(label+".db")) as db:
            for name, records in tables.items():
                assert db.execute("SELECT * FROM "+name+" ORDER BY 1").fetchall() == records, (label, name)
            priors[label] = db.execute("SELECT * FROM pose_priors ORDER BY 1").fetchall()
            assert len(priors[label]) == mode["prior_count"]
        registered = set()
        for summary in mode["models"]:
            model = pc.Reconstruction(work/label/str(summary["model"]))
            assert model.num_points3D() == summary["points"]
            assert model.num_reg_images() == summary["registered"]
            registered.update(im.name for im in model.images.values())
            assert summary["unit"] == "local_units" and not summary["metric_map_adopted"]
        assert len(registered) == mode["registered_unique"]
        assert not mode["deadline_cancelled"]
    assert len(priors["visual_only"]) == 0
    assert priors["old"] == priors["new"] == priors["old_repeat"]
    process = json.loads((out/"process.json").read_text())
    assert process["status"] == "completed" and process["returncode"] == 0
    page = (out/"report.html").read_text(encoding="utf-8")
    for link in re.findall(r'(?:href|src)=[\"\x27]([^\"\x27]+)', page):
        link = unquote(html.unescape(link)).split("#")[0]
        if link and not link.startswith(("http:", "https:", "data:")):
            assert (out/link).exists(), link
    for stem in ("matching_timeline", "pair_parallax"):
        for suffix in (".png", ".svg", ".pdf", ".figure.pickle", ".plot.json"):
            assert (out/(stem+suffix)).is_file()
    for case in json.loads((out/"image_review.json").read_text(encoding="utf-8")):
        assert sha256(ROOT/case["image"]) == case["sha256"]
    result = {"status": "passed", "created_utc": datetime.now(UTC).isoformat(), "images": 300,
              "pairs": len(pairs), "modes": list(priors), "identical_old_new_prior_rows": len(priors["old"]),
              "checks": ["source hashes and all child image hashes", "same feature/match/camera tables across all modes",
                         "no priors in visual-only DB and exact old/new/repeat equality", "pair counts and graph gaps",
                         "reconstruction image and point counts", "process completed without deadline", "report links and editable figures"],
              "accuracy_validated": False}
    dump(out/"verification.json", result); print(json.dumps(result, ensure_ascii=False))
