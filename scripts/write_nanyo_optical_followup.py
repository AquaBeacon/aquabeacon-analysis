"""Write parent-frame causes, paired position dispersion and bounded mapping results."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from analyze_nanyo_optical_followup import CONFIG
from evaluate_position_fallback import dump, f, table, write_csv
from improve_nanyo_light import ROOT, read, relative
from write_field_review import html_page

from aquabeacon_analysis.ngt_binary import sha256


def save(path, text):
    path.write_text(text, encoding="utf-8")
    path.with_suffix(".html").write_text(html_page("南予：光源検出の原因・位置評価・マッピング", text), encoding="utf-8")


def render(eid, cfg, mapping, out):
    """Only redraw saved reconstructions; no change to estimated geometry."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    from PIL import Image, ImageDraw

    from aquabeacon_analysis.editable_plots import save_editable_figure
    sys.path.insert(0, str(ROOT/cfg["runtime"]))
    import pycolmap as pc
    plt.rcParams["font.family"] = ["Yu Gothic", "Meiryo", "DejaVu Sans"]
    work = ROOT/f"data/interim/{eid}/optical_followup/{cfg['run']}"
    for mode in mapping["modes"]:
        if not mode["models"]:
            continue
        item = max(mode["models"], key=lambda model: model["registered_images"])
        metric = item["placement"]["accepted"]
        label = mode["version"]
        source = work/(label+"_metric" if metric else label)/str(item["model"])
        model = pc.Reconstruction(source)
        points = list(model.points3D.values())
        displayed = points[::max(1, len(points)//15000)]
        xyz = np.array([p.xyz for p in displayed])
        colors = np.array([p.color for p in displayed])/255
        centers = np.array([im.projection_center() for im in sorted(model.images.values(), key=lambda im: im.name)])
        fig = plt.figure(figsize=(7, 5.6))
        ax = fig.add_subplot(projection="3d")
        ax.scatter(*xyz.T, c=colors, s=2, alpha=.7)
        ax.plot(*centers.T, color="#d65f00", lw=1, label="Camera path")
        labels = ["East [m]", "North [m]", "Up [m]"] if metric else [f"{a} [local units]" for a in "XYZ"]
        ax.set(xlabel=labels[0], ylabel=labels[1], zlabel=labels[2],
               title="暫定位置拘束マップ" if metric else "尺度未確定の局所点群")
        span = np.ptp(xyz, axis=0)
        ax.set_box_aspect(np.maximum(span, max(span.max()*.1, .001)))
        ax.legend(loc="upper left")
        fig.subplots_adjust(left=.08, right=.84, bottom=.14, top=.91)
        save_editable_figure(fig, out/f"{label}_largest_model.png")
        plt.close(fig)
    new_models = mapping["modes"][1]["models"]
    if new_models:
        item = max(new_models, key=lambda m: m["registered_images"])
        frames = read(out/f"new_model_{item['model']}_cameras.csv")
    else:
        frames = read(out/"mapping_frame_priors.csv")
    source = ROOT/json.loads((out/"mapping_input.json").read_text())["source_work"]
    chosen = sorted({0, len(frames)//2, len(frames)-1})
    sheet = Image.new("RGB", (960, 205), "white")
    draw = ImageDraw.Draw(sheet)
    evidence = []
    for j, index in enumerate(chosen):
        frame = frames[index]
        path = source/"images"/frame["image"]
        with Image.open(path) as im:
            sheet.paste(im.resize((320, 180)), (j*320, 0))
        draw.text((j*320+5, 183), f"{eid} {frame['image']}", fill="black")
        evidence.append({"image": relative(path), "sha256": sha256(path), "utc_s": float(frame["utc_s"])})
    sheet.save(out/"child_mapping_review.jpg", quality=95)
    dump(out/"presentation_manifest.json", {"geometry_changed": False, "child_images": evidence,
                                            "code_sha256": sha256(Path(__file__)),
                                            "source_mapping_sha256": sha256(out/"mapping.json")})


def paired_table(rows, method):
    lines = []
    for window in dict.fromkeys(r["window"] for r in rows):
        by = {(r["sampling"], r["version"]): r for r in rows if r["window"] == window and r["method"] == method}
        old, new = by["all_available", "old"], by["all_available", "new"]
        po, pn = by["paired_same_times", "old"], by["paired_same_times", "new"]
        lines.append([window, old["condition"], f"{old['samples']} → {new['samples']}",
                      f(old["rms_3d_m"]), f(new["rms_3d_m"]), po["samples"],
                      f(po["rms_3d_m"]), f(pn["rms_3d_m"])])
    return table(["60秒窓", "条件", "有効点数 旧→新", "旧 RMS m", "新 RMS m",
                  "同時刻の点数", "同時刻・旧 RMS m", "同時刻・新 RMS m"], lines)


def cause_section(cases, reviews, prefix=""):
    text = ""
    by_index = {r["row_index"]: r for r in reviews}
    for case in cases:
        review = by_index[case["row_index"]]
        text += (f"\n### {case['segment_time_s']:.1f}秒：{case['case']}\n\n"
                 f"{review['observed']} {review['interpretation']}\n\n"
                 f"![親機の全画面：{case['segment_time_s']:.1f}秒]({prefix}{case['image']})\n\n"
                 f"元動画 `{case['source_video']}` の{case['source_s']:.3f}秒。"
                 f"状態：`{case['recovery_track_status']}`。"
                 f"[採用点の表示付き画像]({prefix}{case['marked_image']})。"
                 "緑は最終採用系列の点で、保存済み旧点を保持した場合も含む。\n")
    return text


def map_table(mapping, detail=False):
    if detail:
        rows = []
        for mode in mapping["modes"]:
            for model in mode["models"]:
                p = model["placement"]
                rows.append([mode["version"], model["model"], model["registered_images"], model["points"],
                             f(model["mean_reprojection_error_px"]), p.get("anchors", 0),
                             f(p.get("scale_m_per_unit")), f(p.get("fit_median_m")),
                             "暫定m" if p["accepted"] else "局所単位", p["reason"]])
        return table(["位置入力", "モデル", "登録画像", "点", "再投影 px", "位置対応", "診断尺度",
                      "位置対応残差中央値 m", "単位", "判定根拠"], rows)
    return table(["位置入力", "入力画像", "位置prior", "登録画像（重複除去）", "モデル数", "暫定実寸モデル"],
                 [[m["version"], m["images"], m["position_priors"], m["registered_unique"], len(m["models"]),
                   sum(p["placement"]["accepted"] for p in m["models"])] for m in mapping["modes"]])


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--experiment", action="append", required=True)
    parser.add_argument("--render", action="store_true")
    args = parser.parse_args()
    cfg = json.loads(CONFIG.read_text())
    if set(args.experiment)-set(cfg["experiments"]):
        parser.error("Explicit configured trials required")
    eids = list(dict.fromkeys(args.experiment))
    run = cfg["run"]
    review_path = ROOT/"config/20261103_field_review_000/optical_followup_frame_review.json"
    review_config = json.loads(review_path.read_text(encoding="utf-8"))
    review = review_config["experiments"]
    availability_path = ROOT/f"results/20261103_field_review_000/optical_detection/{cfg['optical_run']}/availability.csv"
    availability = {r["experiment_id"]: r for r in read(availability_path)}
    all_static, maps, inputs = [], {}, [CONFIG, review_path, availability_path, Path(__file__)]
    print("followup report PID", os.getpid(), datetime.now(UTC).isoformat(), flush=True)
    for eid in eids:
        out = ROOT/f"results/{eid}/optical_followup/{run}"
        cases = json.loads((out/"parent_frame_cases.json").read_text())
        static = read(out/"paired_static_rms.csv")
        all_static.extend(static)
        mapping = json.loads((out/"mapping.json").read_text())
        maps[eid] = mapping
        if args.render:
            render(eid, cfg, mapping, out)
        info = json.loads((out/"mapping_input.json").read_text())
        for r in review[eid]:
            if sha256(out/r["image"]) != r["image_sha256"]:
                raise ValueError("Reviewed parent evidence changed")
        a = availability[eid]
        text = (f"# {eid}：光源検出の原因・位置評価・マッピング\n\n"
                f"光源候補は{float(a['old_detected_percent']):.1f}% → {float(a['new_detected_percent']):.1f}%。"
                f"位置フォールバック全体は{float(a['old_selected_percent']):.1f}% → {float(a['new_selected_percent']):.1f}%。"
                "既存有効点を保持し、新規候補の同定除外を適用した結果。\n\n"
                f"[全7区間の比較](../../../20261103_field_review_000/optical_followup/{run}/report.html) / "
                "[原因確認フレームの時刻](parent_frame_cases.json) / [旧位置](old_positions.csv) / "
                "[新位置](new_positions.csv) / [静止比較CSV](paired_static_rms.csv) / [入力来歴](manifest.json)\n\n"
                "## 親機フレームと欠測原因\n\n"
                "画像は検出に用いた親機MP4の全画面。長い欠測の中点、回復点、旧方式のみ成功した点、"
                "同定除外点を選んだ事例であり、全フレームを原因別に分類した統計ではない。"
                "撮影時刻を照合し、原画像を保持している。\n")
        text += cause_section(cases, review[eid])
        text += "\n## 位置評価\n\n"
        if static:
            text += "親機基準の相対位置・フォールバック選択結果の平均周り3D RMS。\n\n"+paired_table(static, "selected")
            text += "\n\n光学＋姿勢＋深度だけの同じ窓での結果。\n\n"+paired_table(static, "optical_imu_depth")
        else:
            text += "この区間には事前指定された60秒の静止評価窓がないため、静止RMSを追加選定しない。\n"
        text += ("\n\n平均周りRMSは絶対位置の真値誤差ではない。欠測が埋まると評価時間も変わるので、"
                 "全有効時刻と新旧共通時刻を併記した。`intentional_hand_shaking`は意図的に揺らした区間。"
                 "[光学・深度位置と音響距離の整合性](range_consistency.csv)も保存したが、"
                 "水面基準の履歴を共有するため完全に独立した精度検証ではない。\n\n"
                 "## 同じ子機画像によるマッピング\n\n"
                 f"区間内{info['segment_interval_s'][0]:.1f}〜{info['segment_interval_s'][1]:.1f}秒、"
                 f"{info['selected_images']}画像。保存済み子機画像・特徴点・内部校正を共有し、"
                 "旧・新の位置フォールバックをカメラ中心へ変換して位置priorとして再構成した。"
                 "全実験時間の再構成率ではなく、明示した範囲での比較。\n\n")
        text += map_table(mapping)+"\n\n"+map_table(mapping, True)
        text += ("\n\n子機の再構成対象画像："+review_config.get("mapping_views", {}).get(eid, "")+
                 "\n\n![再構成に使った子機画像](child_mapping_review.jpg)\n")
        text += ("\n\n内部校正を固定し、同じ乱数seedと各100秒のmapper上限を使用。"
                 "11月1日004は両方式とも時間上限に到達した途中結果で、登録枚数の微差を精度向上と断定しない。"
                 "尺度は姿勢との対応後に符号を自由にして推定し、負の尺度や位置対応不足は実寸化しない。"
                 "暫定mのモデルも位置priorへ合わせた条件付きの地図で、静的な環境かどうかと絶対精度は未検証。"
                 "再投影誤差は画像上の整合性であり、m単位の位置精度ではない。\n\n"
                 "[マッピング入力・座標基準](mapping_input.json) / [フレームごとの旧・新prior](mapping_frame_priors.csv) / "
                 "[モデル指標](mapping.json) / [実行ログ](mapping.log)\n\n")
        for label in ("old", "new"):
            if (out/f"{label}_largest_model.png").exists():
                text += f"![{label}入力による最大モデル]({label}_largest_model.png)\n\n"
        for mode in mapping["modes"]:
            for m in mode["models"]:
                text += f"- [{mode['version']} model {m['model']}：{m['unit']} の点群]({m['ply']})\n"
        save(out/"report.md", text)
        old_report = ROOT/f"results/{eid}/optical_detection/{cfg['optical_run']}/report.md"
        old_text = old_report.read_text(encoding="utf-8").split("\n## 光源欠測の原因と追加マッピング")[0]
        old_text += (f"\n## 光源欠測の原因と追加マッピング\n\n親機の全画面・原因説明と、旧／新の位置評価・マッピングを"
                     f"[追加レポート](../../optical_followup/{run}/report.html)に保存した。\n")
        save(old_report, old_text)
        inputs += [out/name for name in ("manifest.json", "parent_frame_cases.json", "paired_static_rms.csv",
                                         "range_consistency.csv", "mapping.json", "mapping_input.json")]
    out = ROOT/f"results/20261103_field_review_000/optical_followup/{run}"
    out.mkdir(parents=True, exist_ok=True)
    text = ("# 南予：光源検出が低い原因、位置評価、マッピング\n\n"
            f"11月1日の{sum(e.startswith('20261101') for e in eids)}区間と、"
            f"10月31日の{sum(e.startswith('20261031') for e in eids)}区間を評価した。"
            "光源検出はレビュー済みmultiscale_v2、位置は同じ光学・音響・姿勢・深度フォールバックを使う。\n\n")
    text += table(["取得日・区間", "旧候補%", "追加・除外後%", "旧位置全体%", "新位置全体%"],
                  [[eid, *[f(availability[eid][key], 1) for key in ("old_detected_percent", "new_detected_percent",
                     "old_selected_percent", "new_selected_percent")]] for eid in eids])
    text += ("\n\n## 低い検出率の原因\n\n"
             f"親機の全画面{sum(len(review[e]) for e in eids)}例を確認した。主な問題は、画角外・画像端、微小で弱い光点、"
             "網やロープ・浮遊物との競合、追跡停止後の再初期化待ち、既存初期化設定の空白である。"
             "明るい光源が写っているのに欠測になる例もあり、暗さだけでは説明できない。"
             "約1秒の失敗後に次の既存確認点を待つ規則は誤追跡を抑える一方、長い取りこぼしを作る。"
             "11月1日002では新方式単体の候補率が旧16.1%から12.9%へ低下するが、"
             "旧有効点を保持する最終系列は19.0%。この違いを隠さず扱う。"
             "10月31日の全区間分母には、船内が写る投入前・引き上げ後の時間も含まれる。\n\n")
    examples = [("20261101_nanyo_001", 890), ("20261101_nanyo_002", 3166),
                ("20261101_nanyo_002", 8343), ("20261101_nanyo_004", 281),
                ("20261101_nanyo_005", 324)]
    for eid, index in examples:
        if eid not in eids:
            continue
        trial = ROOT/f"results/{eid}/optical_followup/{run}"
        case = next(c for c in json.loads((trial/"parent_frame_cases.json").read_text()) if c["row_index"] == index)
        text += f"\n### {eid}\n"+cause_section([case], review[eid], f"../../../{eid}/optical_followup/{run}/")
    text += ("\n## 精度に関する評価\n\n"
             "事前指定の60秒窓で親機基準相対位置の平均周り3D RMSを比較した。")
    for window, label in (("day2_095600", "静止窓09:56"), ("day2_101500", "静止窓10:15"),
                          ("day2_102000", "意図的に揺らした10:20")):
        subset = {(r["sampling"], r["version"]): r for r in all_static
                  if r["window"] == window and r["method"] == "selected"}
        if subset:
            text += (f"{label}は同時刻比較で{f(subset['paired_same_times', 'old']['rms_3d_m'])}→"
                     f"{f(subset['paired_same_times', 'new']['rms_3d_m'])} m。")
            if window == "day2_102000":
                text += (f"追加時刻も含めると{f(subset['all_available', 'old']['rms_3d_m'])}→"
                         f"{f(subset['all_available', 'new']['rms_3d_m'])} m。")
    text += ("成立率の増加が全条件でばらつきの改善につながるわけではない。"
             "子機位置の独立したGround Truthがないため、絶対位置精度の改善とは呼ばない。\n\n")
    text += paired_table(all_static, "selected")
    text += "\n\n## マッピングの試行結果\n\n"
    map_rows = []
    for eid, mapping in maps.items():
        old, new = mapping["modes"]
        map_rows.append([eid, new["images"], f"{old['position_priors']} → {new['position_priors']}",
                         f"{old['registered_unique']} → {new['registered_unique']}",
                         sum(m["placement"]["accepted"] for m in new["models"])])
    text += table(["区間", "試行画像", "位置prior 旧→新", "登録画像 旧→新", "新入力の暫定実寸モデル"], map_rows)
    supported = [eid for eid, m in maps.items() if any(x["placement"]["accepted"] for x in m["modes"][1]["models"])]
    negative = [eid for eid, m in maps.items() if any(x["placement"].get("scale_m_per_unit", 1) <= 0 for x in m["modes"][1]["models"])]
    empty = [eid for eid, m in maps.items() if not m["modes"][1]["models"]]
    limited = [eid for eid, m in maps.items() if any(v["elapsed_s"] >= cfg["mapper_seconds"] for v in m["modes"])]
    text += f"\n\n新入力で暫定実寸断片が成立：{'、'.join(supported) or 'なし'}。"
    text += f"負の診断尺度を含む：{'、'.join(negative) or 'なし'}。初期化不成立：{'、'.join(empty) or 'なし'}。"
    text += f"時間上限に達した試行：{'、'.join(limited) or 'なし'}。"
    text += ("正の尺度や十分な位置対応が得られないモデルは局所単位の点群として保持した。"
             "位置priorは欠けた画像特徴や視差を作れず、今回の光源回復だけで大きな地図の連続性は改善していない。"
             "各実験の座標基準と暫定性を保持し、日を跨ぐ点群統合や完成した実寸地図とは扱わない。\n\n")
    for eid in eids:
        text += f"- [{eid}：全画面・位置CSV・点群・再構成図](../../../{eid}/optical_followup/{run}/report.html)\n"
    text += ("\n[集約した静止評価CSV](paired_static_rms.csv) / [画像の確認記録](../../../../config/20261103_field_review_000/optical_followup_frame_review.json)\n\n"
             "再現：`analyze_nanyo_optical_followup.py`、`map_nanyo_optical_followup.py`、"
             "`write_nanyo_optical_followup.py`へ対象IDを`--experiment`で明示する。"
             "マッピングは別run・最大300画像・CPU2スレッド・一度に1プロセス。既存特徴点を再利用し、"
             "新たな大量動画変換は行っていない。\n")
    save(out/"report.md", text)
    write_csv(out/"paired_static_rms.csv", all_static)
    write_csv(out/"mapping_summary.csv", [dict(zip(["experiment_id", "images", "priors_old_to_new", "registered_old_to_new", "metric_models_new"], r)) for r in map_rows])
    old_report = ROOT/f"results/20261103_field_review_000/optical_detection/{cfg['optical_run']}/report.md"
    old_text = old_report.read_text(encoding="utf-8").split("\n## 光源欠測の原因と追加マッピング")[0]
    old_text += ("\n## 光源欠測の原因と追加マッピング\n\n11月1日の5区間も評価対象。"
                 f"[親機フレーム付きの原因分析・位置評価・マッピング](../../optical_followup/{run}/report.html)を追加した。\n")
    save(old_report, old_text)
    dump(out/"report_manifest.json", {"experiments": eids, "run": run, "created_utc": datetime.now(UTC).isoformat(),
                                     "inputs_sha256": {relative(p): sha256(p) for p in inputs},
                                     "accuracy_validated": False})
    print("followup report written", out/"report.html")


if __name__ == "__main__":
    main()
