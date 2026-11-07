"""Report image-reviewed pool recovery, paired positions and bounded mapping attempts."""
from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from evaluate_position_fallback import dump, f, table, write_csv
from improve_nanyo_light import ROOT, read, relative
from improve_ngt_light import CONFIG, REVIEW, folder
from position_fallback_ngt import numbers
from write_field_review import html_page
from write_nanyo_optical_followup import paired_table, render

from aquabeacon_analysis.ngt_binary import sha256

FRAME_REVIEW = ROOT/"config/20261103_field_review_000/pool_optical_frame_review.json"
DENSE_CONFIG = ROOT/"config/20261103_field_review_000/pool_mapping_dense.json"


def save(path, text):
    path.write_text(text, encoding="utf-8")
    page = html_page("プール：光源検出・位置評価・マッピング", text)
    page = page.replace("</style>", "img{max-height:650px;width:auto;object-fit:contain}</style>")
    path.with_suffix(".html").write_text(page, encoding="utf-8")


def percentage(count, total):
    return f"{100*int(count)/int(total):.1f}"


def availability_table(rows):
    output = []
    for eid in dict.fromkeys(r["experiment_id"] for r in rows):
        old, new = [next(r for r in rows if r["experiment_id"] == eid and r["version"] == v) for v in ("old", "new")]
        n = old["grid_samples"]
        output.append([eid, n, percentage(old["optical_samples"], n), percentage(new["optical_samples"], n),
                       percentage(old["joint_samples"], n), percentage(old["selected_samples"], n), percentage(new["selected_samples"], n)])
    return table(["区間", "5 Hz点数", "旧光源 %", "確認・追加後 %", "旧同時成立位置 %",
                  "旧光源＋フォールバック %", "新光源＋フォールバック %"], output)


def consistency(out):
    series = {v: read(out/f"{v}_positions.csv") for v in ("old", "new")}
    residual, masks = {}, {}
    for v, rows in series.items():
        xyz = numbers(rows, [f"optical_imu_depth_{a}_m" for a in ("east", "north", "up")])
        delta = np.linalg.norm(xyz, axis=1)-numbers(rows, ["range_m"])[:, 0]
        event = np.array([r["event_sample"] == "True" for r in rows])
        residual[v], masks[v] = delta, event & np.isfinite(delta)
    common = masks["old"] & masks["new"]
    result = []
    for v in residual:
        for sampling, mask in (("all_events", masks[v]), ("paired_events", common)):
            a = residual[v][mask]
            result.append({"version": v, "sampling": sampling, "samples": int(mask.sum()),
                           "median_abs_m": float(np.median(abs(a))) if len(a) else None,
                           "p95_abs_m": float(np.percentile(abs(a), 95)) if len(a) else None})
    write_csv(out/"range_consistency.csv", result)
    return result


def map_section(mapping, info, explanation):
    text = f"対象は親機動画 {info['parent_interval_s'][0]:.2f}〜{info['parent_interval_s'][1]:.2f}秒、{info['selected_images']}画像。{explanation}\n\n"
    text += table(["位置入力", "画像", "位置prior", "登録画像（重複除去）", "モデル", "暫定実寸モデル"],
                  [[m["version"], m["images"], m["position_priors"], m["registered_unique"], len(m["models"]),
                    sum(x["placement"]["accepted"] for x in m["models"])] for m in mapping["modes"]])
    details = []
    for mode in mapping["modes"]:
        for m in mode["models"]:
            p = m["placement"]
            details.append([mode["version"], m["model"], m["registered_images"], m["points"],
                            f(m["mean_reprojection_error_px"]), p["correspondences"], f(p.get("scale")),
                            f(p.get("fit_median_m")), "暫定m" if p["accepted"] else "局所単位", p["reason"]])
    if details:
        text += "\n\n"+table(["入力", "モデル", "登録画像", "点", "再投影 px", "位置対応", "尺度", "残差中央値 m", "単位", "判定"], details)
    text += "\n\n![子機の再構成対象画像](child_mapping_review.jpg)\n\n"
    for mode in mapping["modes"]:
        if mode["models"]:
            text += f"![{mode['version']} の最大モデル]({mode['version']}_largest_model.png)\n\n"
            for m in mode["models"]:
                text += f"- [{mode['version']} model {m['model']} / {m['unit']} 点群]({m['ply']})\n"
    text += ("\n[画像時刻と位置prior](mapping_frame_priors.csv) / [入力・校正](mapping_input.json) / "
             "[全モデルの指標](mapping.json) / [実行ログ](mapping.log)\n\n"
             "再投影誤差は画像上の整合性。位置対応に合わせた尺度も独立した絶対精度ではない。"
             "磁北ENUとGNSS ENUを偏角0°で合わせ、カメラ・測定中心を一致とするプール旧設定を保持した。"
             "子機姿勢による独立の向き検証はしていない。\n")
    return text


def cause_section(cases, prefix=""):
    text = ""
    for c in cases:
        text += (f"\n### 親機動画 {c['parent_video_s']:.3f}秒（区間内{c['segment_time_s']:.1f}秒）\n\n"
                 f"{c['explanation']}\n\n"
                 f"[![親機全画面]({prefix}{c['image']})]({prefix}{c['image']})\n\n"
                 f"旧状態：`{c['baseline_status']}`。データ行 {c['row_index']}（0始まり）。"
                 "原画像の表示回転90°を保持した720×1280全画面。\n")
    return text


def main():
    parser = argparse.ArgumentParser(__doc__); parser.add_argument("--experiment", action="append", required=True)
    parser.add_argument("--render", action="store_true"); args = parser.parse_args(); cfg = json.loads(CONFIG.read_text())
    if set(args.experiment) != set(cfg["experiments"]):
        parser.error("This integrated report requires all configured pool trial IDs explicitly")
    eids = list(dict.fromkeys(args.experiment)); reviews = json.loads(FRAME_REVIEW.read_text(encoding="utf-8"))
    identity = json.loads(REVIEW.read_text(encoding="utf-8"))
    inputs = [CONFIG, FRAME_REVIEW, REVIEW, Path(__file__), ROOT/"scripts/write_nanyo_optical_followup.py"]
    availability, static, map_summaries = [], [], []
    print("pool report PID", os.getpid(), datetime.now(UTC).isoformat(), flush=True)
    for eid in eids:
        out = folder(eid, cfg)
        rows = read(out/"availability.csv"); availability += rows
        trial_static = read(out/"paired_static_rms.csv") if (out/"paired_static_rms.csv").exists() else []
        static += trial_static
        for c in reviews["experiments"][eid]:
            if sha256(out/c["image"]) != c["image_sha256"]:
                raise ValueError("Parent evidence changed")
        detection = json.loads((out/"detection_manifest.json").read_text())
        accepted = len(identity["experiments"][eid]["accepted_rows"])
        text = (f"# {eid} 光源検出・位置評価・マッピング\n\n"
                f"同じ親機LRFの{detection['rows']}画像で旧検出の状態・座標を再現。"
                f"追加候補{detection['proposed_additions']}点を画像確認し、{accepted}点を採用した。"
                "旧有効点と画面端・既存同定除外は保持した。追加候補の全件レビューを含む補完結果で、"
                "未知の動画への全自動同定成績ではない。\n\n")
        text += availability_table(rows)
        text += "\n\n## 親機フレームによる原因確認\n"+cause_section(reviews["experiments"][eid])
        text += ("\n[全保存フレームの時刻とハッシュ](parent_frame_cases.json) / "
                 "[自動候補](automatic_candidates.csv) / [確認後の候補](reviewed_candidates.csv)\n\n")
        if detection["proposed_additions"]:
            text += "全追加候補の確認用拡大図：\n\n"
            for sheet in sorted(out.glob("crops_*.jpg")):
                text += f"- [{sheet.stem}]({sheet.name})\n"
        text += "\n## 位置評価\n\n"
        if trial_static:
            text += paired_table(trial_static, "selected")
            old_rms, new_rms = [next(r for r in trial_static if r["method"] == "selected"
                                and r["sampling"] == "paired_same_times" and r["version"] == v) for v in ("old", "new")]
            joint_rms = next(r for r in trial_static if r["method"] == "joint" and r["sampling"] == "all_available" and r["version"] == "old")
            text += (f"\n\n002は事前指定の静止窓。新旧光源＋フォールバックの共通{new_rms['samples']}点の"
                     f"平均周り3D RMSは{f(old_rms['rms_3d_m'])}→{f(new_rms['rms_3d_m'])} m。"
                     f"従来の同時成立位置は{joint_rms['samples']}点・{f(joint_rms['rms_3d_m'])} mで、採用時刻が異なる。\n")
        else:
            text += "この試験は動作を含み、事前指定の静止窓がない。全区間の位置変化を測位雑音や真値誤差として集計しない。\n"
        check = consistency(out)
        text += "\n光学＋姿勢＋深度位置から得た距離と、音響距離の整合性：\n\n"
        text += table(["入力", "時刻集合", "一意の音響イベント数", "絶対差中央値 m", "絶対差95%点 m"],
                      [[r["version"], r["sampling"], r["samples"], f(r["median_abs_m"]), f(r["p95_abs_m"])] for r in check])
        text += ("\n\n深度の水面基準に過去の同時成立位置を使うため、この差は独立した真値誤差ではない。"
                 "将来の真値付きデータや未使用動画での検証は別途必要。"
                 "[旧位置CSV](old_positions.csv) / [新位置CSV](new_positions.csv) / [距離整合性CSV](range_consistency.csv)\n\n")
        mapping = json.loads((out/"mapping.json").read_text()); info = json.loads((out/"mapping_input.json").read_text())
        if args.render:
            render(eid, cfg, mapping, out)
        text += "## 全区間からのマッピング試行\n\n"+map_section(mapping, info, reviews["mapping_views"][eid])
        old, new = mapping["modes"]
        map_summaries.append({"experiment_id": eid, "run": cfg["run"], "images": new["images"],
             "old_priors": old["position_priors"], "new_priors": new["position_priors"],
             "old_registered": old["registered_unique"], "new_registered": new["registered_unique"],
             "new_metric_models": sum(m["placement"]["accepted"] for m in new["models"])})
        if eid == "20261028_ngt_004":
            dense_cfg = json.loads(DENSE_CONFIG.read_text()); dense_out = folder(eid, dense_cfg)
            dm = json.loads((dense_out/"mapping.json").read_text()); di = json.loads((dense_out/"mapping_input.json").read_text())
            if args.render:
                render(eid, dense_cfg, dm, dense_out)
            dense_text = ("# プール004：後半の壁の高密度試行\n\n"
                          "全区間の疎い試行後に、既存映像で壁が見える後半30秒を選んだ探索的な追加試行。"
                          "10 Hz・300画像、右側の壁領域に特徴点を限定。元の画像座標と内部行列を保持し、画像はcropしていない。"
                          "[全区間の比較レポート](../"+cfg["run"]+"/report.html)\n\n")
            dense_text += map_section(dm, di, "新旧で同じ画像・マスク・特徴点・乱数seedを使う。時間区間の選択は独立評価ではない。")
            save(dense_out/"report.md", dense_text)
            text += f"\n## 後半の壁を使った追加試行\n\n[300画像を使った高密度試行](../{dense_cfg['run']}/report.html)。\n"
            do, dn = dm["modes"]
            map_summaries.append({"experiment_id": eid, "run": dense_cfg["run"], "images": dn["images"],
                 "old_priors": do["position_priors"], "new_priors": dn["position_priors"],
                 "old_registered": do["registered_unique"], "new_registered": dn["registered_unique"],
                 "new_metric_models": sum(m["placement"]["accepted"] for m in dn["models"])})
            inputs += [DENSE_CONFIG, dense_out/"mapping.json", dense_out/"mapping_input.json"]
        save(out/"report.md", text)
        inputs += [out/name for name in ("detection_manifest.json", "identity_review_manifest.json", "analysis_manifest.json",
                   "availability.csv", "range_consistency.csv", "mapping.json", "mapping_input.json")]
        inputs += [out/f"{v}_positions.csv" for v in ("old", "new")]
    total = {v: {k: sum(int(r[k]) for r in availability if r["version"] == v)
                    for k in ("grid_samples", "optical_samples", "joint_samples", "selected_samples")} for v in ("old", "new")}
    old, new = total["old"], total["new"]
    accepted_total = sum(len(identity["experiments"][e]["accepted_rows"]) for e in eids)
    rejected_total = sum(len(identity["experiments"][e]["rejected_rows"]) for e in eids)
    out = folder("20261028_ngt_000", cfg); out.mkdir(parents=True, exist_ok=True)
    text = ("# 長田高校プール：光源検出の改善、位置評価、マッピング\n\n"
            "2026年10月28日の002（子機静止）・003（測距限界）・004（親子同時移動）を比較した。"
            "001は親機GNSSの静止試験なので、今回の光学測位比較には含めない。\n\n"
            f"親機{new['grid_samples']}画像を同一LRF・同一時刻で再処理し、画像確認した光源{accepted_total}点を追加。"
            f"既存の同定除外を適用した光源候補成立率は{percentage(old['optical_samples'],old['grid_samples'])}→"
            f"{percentage(new['optical_samples'],new['grid_samples'])}%、位置成立率は旧光源＋フォールバック"
            f"{percentage(old['selected_samples'],old['grid_samples'])}→新光源＋フォールバック"
            f"{percentage(new['selected_samples'],new['grid_samples'])}%となった。\n\n")
    text += availability_table(availability)
    text += ("\n\n旧同時成立位置は光学・音響が同時に使える場合の位置。フォールバックによる改善と、光源追加による改善を分けて示した。"
             "新方式単体の全画面候補には機体の文字、ロープ、プール底、皮膚の反射が多数含まれ、"
             f"{accepted_total+rejected_total}追加候補のうち{rejected_total}点を不採用とした。色条件を緩めるだけでは同定を保証できず、今回の採用率は画像レビュー後の値である。\n\n"
             "## なぜ検出できないか\n\n"
             "確認した原因は、画角外・画面端への移動、前景による遮蔽、白く飽和しない色付きの発光面、既存の保守的な除外区間。"
             "003の欠測を距離限界だけでは説明できず、子機が画面から外れた時間も多い。"
             "この事例集だけから欠測原因の時間割合や水中の減衰量は求めていない。\n")
    for eid, index in [("20261028_ngt_003", 375), ("20261028_ngt_003", 279),
                       ("20261028_ngt_004", 611), ("20261028_ngt_004", 1200)]:
        if eid in eids:
            c = next(r for r in reviews["experiments"][eid] if r["row_index"] == index)
            text += cause_section([c], f"../../../{eid}/optical_followup/{cfg['run']}/")
    text += (f"\n## 精度に関する評価\n\n静止区間002の同時刻比較では平均周り3D RMSが{f(old_rms['rms_3d_m'])}→"
             f"{f(new_rms['rms_3d_m'])} m（{new_rms['samples']}点）。"
             "この区間は旧検出が100%で、新光源による位置の変化はない。"
             "003・004は動作を含むため、その移動量を位置精度として集計しない。子機位置の独立したGround Truthがなく、絶対精度の改善は主張しない。\n\n")
    text += paired_table(static, "selected")
    text += "\n\n## マッピングの結果\n\n"+table(
        ["区間・試行", "画像", "位置prior 旧→新", "登録画像 旧→新", "新入力の暫定実寸モデル"],
        [[r["experiment_id"]+" / "+r["run"], r["images"], f"{r['old_priors']} → {r['new_priors']}",
          f"{r['old_registered']} → {r['new_registered']}", r["new_metric_models"]] for r in map_summaries])
    text += ("\n\n002はぼけた水中像と静止、003の局所モデルは主に水面・水上像で位置対応がなく、"
             "004の疎い全区間画像は初期化に至らなかった。004では後半の壁を10 Hzで再試行した。"
             "登録画像数だけを完成した地図や精度の改善と呼ばず、位置対応・点数・尺度判定を各レポートに残した。"
             "すべてCPU2スレッド・最大300画像・mapper各100秒・同時1 workerで実行した。"
             "既存画像を再利用したため新しい動画変換は不要だった。\n\n")
    for eid in eids:
        text += f"- [{eid}：親機フレーム・位置CSV・点群・詳細](../../../{eid}/optical_followup/{cfg['run']}/report.html)\n"
    text += ("\n[全候補の採否記録](../../../../config/20261103_field_review_000/pool_optical_identity_review.json) / "
             "[写真の説明と確認記録](../../../../config/20261103_field_review_000/pool_optical_frame_review.json)\n")
    save(out/"report.md", text)
    write_csv(out/"availability.csv", availability); write_csv(out/"mapping_summary.csv", map_summaries)
    write_csv(out/"paired_static_rms.csv", static)
    dump(out/"report_manifest.json", {"experiments": eids, "run": cfg["run"], "created_utc": datetime.now(UTC).isoformat(),
          "inputs_sha256": {relative(p): sha256(p) for p in inputs}, "accuracy_validated": False, "totals": total})
    print("pool report written", out/"report.html")


if __name__ == "__main__":
    main()
