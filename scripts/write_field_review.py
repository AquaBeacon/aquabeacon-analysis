"""Build the structured local field report from templates and audited tables."""
from __future__ import annotations

import argparse
import csv
import html
import json
import os
import re
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import markdown

from aquabeacon_analysis.experiments import load_experiment
from aquabeacon_analysis.ngt_binary import sha256

ROOT = Path(__file__).resolve().parents[1]
SOURCES = {}
CONDITIONS = {"exploratory_static_selection": "探索選定", "user_static": "静止申告",
              "user_static_delayed_ranging": "静止申告・開始遅延", "intentional_hand_shaking": "手動揺動"}


def record(path):
    SOURCES[os.path.relpath(path, ROOT).replace("\\", "/")] = sha256(path)
    return path


def load(path):
    return json.loads(record(path).read_text(encoding="utf-8"))


def read(path):
    with record(path).open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def table(headers, rows):
    def cell(value):
        return str(value).replace("|", "\\|").replace("\n", " ")
    return "\n".join(["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"]*len(headers)) + " |",
                      *("| " + " | ".join(map(cell, row)) + " |" for row in rows)])


def short(eid):
    return eid.replace("20261031_nanyo_", "Day1 ").replace("20261101_nanyo_", "Day2 ")


def f(value, digits=4):
    return f"{float(value):.{digits}f}"


def jst(value):
    return datetime.fromtimestamp(float(value), timezone(timedelta(hours=9), "JST")).strftime("%m-%d %H:%M:%S")


STYLE = """
*{box-sizing:border-box}body{margin:0;color:#17212b;background:#f5f6f7;font-family:'Yu Gothic','Meiryo',sans-serif;font-size:15px;line-height:1.9}
main{max-width:1080px;margin:32px auto;padding:44px 56px;background:white}h1{font-size:27px;line-height:1.55}h2{font-size:24px;margin-top:64px;border-bottom:1px solid #ccd4d9;padding-bottom:8px}h3{font-size:19px;margin-top:32px}p{margin:16px 0}a{color:#005e83;text-decoration-thickness:1px;text-underline-offset:3px}
table{border-collapse:collapse;width:100%;font-size:13px;line-height:1.65;margin:24px 0}th,td{border:1px solid #cdd6dc;padding:9px 10px;text-align:left;vertical-align:top;overflow-wrap:anywhere}th{background:#edf3f6}tr:nth-child(even){background:#fafbfc}img{display:block;max-width:100%;height:auto;margin:22px auto}pre{background:#f1f3f5;padding:16px;overflow:auto;font-size:12px;line-height:1.6}code{overflow-wrap:anywhere}nav{padding:15px 0;border-bottom:1px solid #cdd6dc}.toc{font-size:14px}.toc ul{list-style:none;padding-left:20px}.toc>ul{padding:0}footer{color:#52616b;font-size:12px}blockquote{border-left:3px solid #b1c9d5;margin:16px 0;padding-left:20px}
@media(max-width:800px){main{margin:0;padding:22px 15px}table{font-size:11px}td,th{padding:6px}h1{font-size:24px}}@media print{body{background:white}main{max-width:none;margin:0;padding:0}h2{break-before:page}h2,h3{break-after:avoid}img,table{break-inside:avoid}a{color:inherit}.toc{font-size:11px}}
"""


def html_page(title, text):
    body = markdown.markdown(text, extensions=["tables", "fenced_code", "toc", "sane_lists"],
                             extension_configs={"toc": {"toc_depth": "1-3"}})
    return f'<!doctype html><html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title><style>{STYLE}</style><main>{body}</main></html>'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", required=True, choices=["20261103_field_review_000"])
    args = parser.parse_args()
    exp = load_experiment(ROOT, args.experiment)
    cfg = load(ROOT / exp.metadata["config_paths"][0])
    out = exp.stage_path("results") / "research_review"
    stationary = read(out / "stationary_summary.csv")
    simulation = read(out / "simulation_comparison.csv")
    mapping = read(out / "mapping_trials.csv")
    anchors = read(out / "mapping_anchor_audit.csv")
    acoustic = read(out / "acoustic_capture_quality.csv")
    for eid in cfg["nanyo_trials"]:
        record(ROOT / f"results/{eid}/acoustic_audit_20261103/manifest.json")
        record(ROOT / f"results/{eid}/acoustic_audit_20261103/captures.csv")
    inventory = load(out / "inventory_summary.json")
    values = {}
    fallback_folder = ROOT / f"results/{args.experiment}/position_fallback"
    fallback_available = fallback_folder / "availability.csv"
    for key in ("pool_fallback_table", "nanyo_fallback_table", "fallback_rms_table"):
        values[key] = "位置フォールバックは未評価。`evaluate_position_fallback.py`で明示実行する。"
    if fallback_available.exists():
        fallback = read(fallback_available)
        fallback_rms = read(fallback_folder / "stationary_rms.csv")
        record(fallback_folder / "manifest.json")
        for key, tag in (("pool_fallback_table", "_ngt_"), ("nanyo_fallback_table", "_nanyo_")):
            values[key] = table(["実験", "分母（時間格子）", "従来成立率%", "追加後%", "光学側追加点", "音響側追加点"],
                [[short(r["experiment_id"]), r["grid_samples"], f(r["joint_percent"], 1),
                  f(r["selected_percent"], 1), r["selected_optical_imu_depth_samples"],
                  r["selected_acoustic_imu_depth_samples"]] for r in fallback if tag in r["experiment_id"]])
        common = [r for r in fallback_rms if r["sampling"] == "common_events"]
        comparison = []
        for window in dict.fromkeys(r["window"] for r in common):
            methods = {r["method"]: r for r in common if r["window"] == window}
            comparison.append([window, methods["joint"]["samples"],
                               *[f(methods[m]["rms_3d_m"]) if methods[m]["rms_3d_m"] else "—"
                                 for m in ("joint", "acoustic_imu_depth", "optical_imu_depth")]])
        values["fallback_rms_table"] = table(
            ["窓", "共通イベントN", "従来3D RMS m", "音響L＋IMU＋深度 m", "光学＋IMU＋深度 m"], comparison)
    values["audit_overview"] = (f'整理対象は取得・区間・結合を合わせて{inventory["experiments"]}台帳、'
                                 f'既存の詳細レポート{inventory["reports"]}本である。'
                                 '原本と過去の解析結果を保持し、今回の横断表と図を別の結果フォルダへ保存した。')
    experiment_rows = []
    for eid, meaning in [("20261028_ngt_001", "親機GNSS静止"), ("20261028_ngt_002", "子機静止"),
                         ("20261028_ngt_003", "範囲限界"), ("20261028_ngt_004", "同時移動・壁"),
                         *((eid, "海上試行") for eid in cfg["nanyo_trials"])]:
        registry = load(ROOT / f"experiments/{eid}/experiment.json")
        interval = registry.get("utc_interval_s")
        if interval:
            time_text = f'{jst(interval[0])}〜{jst(interval[1]).split(" ")[1]}'
            seconds = f(interval[1]-interval[0], 1)
        else:
            periods = {"001": (240, 480), "002": (1050, 1110), "003": (1110, 1260), "004": (1260, 1540)}
            lo, hi = periods[eid[-3:]]
            time_text, seconds = f"親動画 {lo}〜{hi} s", str(hi-lo)
        experiment_rows.append([eid, meaning, time_text, seconds])
    values["experiment_table"] = table(["実験ID", "主な目的", "JSTまたは親動画範囲", "長さs"], experiment_rows)
    pool = read(out / "pool_position_comparison.csv")
    values["pool_position_table"] = table(["区間", "一意な音響イベント", "位置成立率%", "最長欠測s", "旧位置との差中央値m", "同95%点m"],
        [[r["experiment"], r["event_count"], f(100*float(r["availability_fraction"]), 1), f(r["longest_gap_s"], 1),
          f(r["median_displacement_m"], 3), f(r["p95_displacement_m"], 3)] for r in pool])
    values["stationary_table"] = table(["JST開始", "条件", "平均深度m", "採用shot/記録", "相対3D RMS m", "親姿勢固定m", "減少%", "端を含む最長空白s"],
        [[r["start_jst"][5:19].replace("T", " "), CONDITIONS[r["condition"]], f(r["depth_mean_m"], 2),
          f'{r["samples"]}/{r["range_records"]}', f(r["relative_rms_m"]), f(r["fixed_parent_rms_m"]),
          f(r["reduction_percent"], 1), f(r["max_gap_with_edges_s"], 2)] for r in stationary])
    values["absolute_table"] = table(["窓", "共通shot", "通常PPK絶対RMS m", "Float設定PPK絶対RMS m"],
        [[r["window"], r["samples"], f(r["absolute_ppk_ar_rms_m"]), f(r["absolute_ppk_float_rms_m"])] for r in stationary])
    values["direction_table"] = table(["窓", "採用shotの測距RMS m", "視線方向RMS m", "直交方向RMS m", "直交分散比%", "換算角度°"],
        [[r["window"], f(r["range_centered_rms_m"]), f(r["radial_rms_m"]), f(r["tangential_rms_m"]),
          f(r["tangential_variance_percent"], 2), f(r["equivalent_tangent_angle_deg"], 3)] for r in stationary])
    values["acoustic_table"] = table(["区間", "記録波形数", "保存サンプル", "欠落を報告した回数", "取得側lost_samples", "区間端で部分保存"],
        [[short(r["experiment_id"]), r["recorded_captures_in_csv"], f'{int(r["envelope_rows"]):,}',
          r["captures_reporting_loss"], f'{int(r["recorded_lost_samples"]):,}', r["partial_or_boundary_captures"]] for r in acoustic])
    values["simulation_table"] = table(["窓", "平均斜距離m", "実測 平均周りRMS m", "基本雑音モデルRMS m", "比 実測/モデル"],
        [[r["window"], f(r["reference_range_m"], 3), f(r["observed_centered_rms_m"]),
          f(r["mc_centered_rms_m"]), f(r["observed_to_sim_scatter_ratio"], 2)] for r in simulation if r["case"] == "base_noise"])
    values["optical_model_table"] = table(["窓", "仮定角度σ°", "光学モデル 条件付きRMS m", "モデルの見失い確率%"],
        [[r["window"], f(r["sigma_az_deg"], 3), f(r["mc_centered_rms_m"]),
          f(100*float(r["default_optical_dropout_probability"]), 1)] for r in simulation if r["case"] == "optical_c0_3_conditional"])
    values["mapping_table"] = table(["区間", "初回SfM登録/入力", "既存配置断片/候補", "配置点数", "写真面", "光源採用率%", "航法有効率%"],
        [[short(r["experiment_id"]), f'{r["initial_registered_frames"]}/{r["initial_sampled_frames"]}',
          f'{r["accepted_fragments"]}/{r["candidate_fragments"]}', f'{int(r["map_points"]):,}', r["photo_patches"],
          f(r["light_valid_percent"], 1), f(r["navigation_valid_percent"], 1)] for r in mapping])
    values["mapping_anchor_table"] = table(["区間", "旧モデル", "登録画像", "同時刻の航法対応", "最近の有効航法との最小差s"],
        [[short(r["experiment_id"]), r["visual_model"], r["registered_images"], r["recent_navigation_brackets"],
          f(r["minimum_gap_to_recent_navigation_s"], 3)] for r in anchors if r["experiment_id"].endswith(("_004", "_005"))])
    refinement_rows, placement_rows, media = [], [], []
    for eid, run in [("20261101_nanyo_004", "field_review_20261103_2hz"), ("20261101_nanyo_005", "field_review_20261103_5hz")]:
        folder = ROOT / f"results/{eid}/mapping_refinement/{run}"
        m, review = load(folder / "manifest.json"), load(folder / "review_manifest.json")
        relative = os.path.relpath(folder, out).replace("\\", "/")
        refinement_rows.append([short(eid), f'{m["clip_interval_s"][0]:g}〜{m["clip_interval_s"][1]:g}',
                                m["sampling_hz"], f'{m["registered_frames"]}/{m["sampled_frames"]}',
                                sum(s["points"] for s in m["models"]),
                                "到達" if review["mapper_time_limit_reached"] else "未到達"])
        local_sections = []
        for model in review["models"]:
            mid = model["model"]
            reason = "尺度が非正" if "non-positive" in model["reason"] else "航法対応不足"
            placement_rows.append([short(eid), mid, model["images"], model["navigation_brackets"],
                                   f(model["orientation_median_deg"], 3),
                                   f(model["sampled_track_max_parallax_median_deg"], 2), reason])
            local_sections.append(f'## モデル{mid}\n\n{model["images"]}画像・{model["points"]}点。実寸配置は不採用（{reason}）。\n\n'
                                  f'![代表画像](model_{mid}_frames.jpg)\n\n![局所点群](model_{mid}_local.png)\n\n'
                                  f'[PLY](model_{mid}_LOCAL_UNITS.ply) / [カメラ姿勢CSV](model_{mid}_poses.csv)\n')
        local = f'# {eid} 追加の局所地図\n\n[回転・拡大できる地図](viewer.html) / [統合報告]({os.path.relpath(out / "07_mapping.html", folder).replace(chr(92), "/")})\n\n'
        local += '各モデルは独立した任意単位。尺度が未成立のため現行の実寸地図に追加していない。\n\n' + "\n".join(local_sections)
        local += '\n[実行条件](manifest.json) / [配置診断](review_manifest.json) / [実行ログ](execution.log)\n'
        (folder / "report.md").write_text(local, encoding="utf-8")
        (folder / "report.html").write_text(html_page(eid, local), encoding="utf-8")
        representative = max(review["models"], key=lambda r: r["images"])["model"]
        media.append(f'### {short(eid)}の追加モデル\n\n[局所地図・画像・PLY]({relative}/report.html) / [オフライン3D表示]({relative}/viewer.html)\n\n'
                     f'![登録された代表画像]({relative}/model_{representative}_frames.jpg)\n')
    values["refinement_table"] = table(["区間", "区間動画秒", "抽出Hz", "登録/入力画像", "延べ点数", "mapper時間上限"], refinement_rows)
    values["placement_table"] = table(["区間", "モデル", "画像", "航法対応", "姿勢整合中央値°", "最大視差の中央値°", "実寸配置の判定"], placement_rows)
    values["refinement_media"] = "\n".join(media)
    source_entries = [
        ("プールの座標・尺度再評価", "../../20261028_ngt_000/coordinate_reassessment/report.md"),
        ("プール子機単独SLAM", "../../20261028_ngt_000/child_only_comprehensive_report/report.md"),
        ("プール包絡線", "../../20261028_ngt_000/multipath/report.md"),
        ("南予Day1の同期", "../../20261031_nanyo_000/synchronization/report.md"),
        ("南予Day2の同期", "../../20261101_nanyo_000/synchronization/report.md"),
        ("南予静止・周波数・初回SfM", "../../20261101_nanyo_000/analysis/report.md"),
        ("南予GNSSと絶対位置", "../../20261101_nanyo_000/stationary_gnss_comparison/report.md"),
        ("南予の採用地図", "../../20261102_nanyo_combined_000/mapping_replay/report.md"),
        ("シミュレーション数式", "../../../../aquabeacon-sim/docs/MATH_SPEC.md"),
        ("シミュレーションの目標精度", "../../../../aquabeacon-sim/docs/reports/20_target_accuracy.md"),
    ]
    values["source_table"] = table(["論点", "原資料"], [[label, f'[{Path(path).name}]({path})'] for label, path in source_entries])
    for _, path in source_entries:
        record((out / path).resolve())
    asset_entries = [
        ("実測と基本雑音モデル", "observed_simulation_scatter", "新規・2D編集元あり"),
        ("視線方向別のばらつき", "directional_scatter", "新規・2D編集元あり"),
        ("全7区間の追跡・航法可用性", "navigation_availability", "新規・分母は系列ごとに異なる"),
        ("プール静止の修正", "../../20261028_ngt_000/coordinate_reassessment/static_relative_positions", "既存修正版・2D編集元あり"),
        ("海上静止の姿勢補正", "../../20261101_nanyo_000/analysis/stationary_comparison", "既存・2D編集元あり"),
        ("GNSS方式別の絶対位置", "../../20261101_nanyo_000/stationary_gnss_comparison/absolute_comparison", "既存・2D編集元あり"),
        ("2日分の写真地図", "../../20261102_nanyo_combined_000/mapping_replay/mapping_overhead", "既存・暫定尺度と日間未接続を表示"),
    ]
    values["asset_table"] = table(["用途", "貼付け用", "扱い"], [[label, f'[PNG]({path}.png) / [SVG]({path}.svg)', note] for label, path, note in asset_entries])
    values["verification_summary"] = '各確認の成否・件数・実行時点は、下の検証JSONに保存した。静止数値は採用shotのCSVから独立に再計算し、元manifestとの一致を確認する。'
    chapters = []
    template_folder = ROOT / "scripts/templates/field_review"
    for path in sorted(template_folder.glob("*.md")):
        content = record(path).read_text(encoding="utf-8")
        for key, value in values.items():
            content = content.replace("{{" + key + "}}", value)
        if "{{" in content:
            raise ValueError(f"Unfilled template: {path}")
        title = content.splitlines()[0].lstrip("# ")
        (out / path.name).write_text(content, encoding="utf-8")
        (out / path.with_suffix(".html").name).write_text(html_page(title, content), encoding="utf-8")
        chapters.append((path.name, title, content))
    title = "AquaBeacon 長田高校プールと南予海上実験の統合研究レポート"
    cover = f"# {title}\n\n対象：2026年10月28日・10月31日・11月1日　整理日：2026年11月3日（JST）\n\n"
    report = cover + "[TOC]\n\n" + "\n\n".join(re.sub(r"^#", "##", c, flags=re.MULTILINE) for _, _, c in chapters)
    (out / "report.md").write_text(report, encoding="utf-8")
    (out / "report.html").write_text(html_page(title, report), encoding="utf-8")
    index = cover + "[全章を読む](report.html) / [全章Markdown](report.md)\n\n" + "\n".join(f"- [{t}]({Path(n).with_suffix('.html')})" for n, t, _ in chapters)
    (out / "README.md").write_text(index+"\n", encoding="utf-8")
    for eid in [*cfg["acquisitions"], *cfg["ngt_trials"], *cfg["nanyo_trials"], "20261102_nanyo_combined_000"]:
        folder = ROOT / f"results/{eid}/research_review"
        folder.mkdir(exist_ok=True)
        link = os.path.relpath(out / "report.html", folder).replace("\\", "/")
        (folder / "report.md").write_text(f"# {eid} 統合研究報告への入口\n\n[2026年11月3日の統合レポート]({link})\n\n"
                                          "旧結果と修正版の採用関係、実験条件、追加解析、図表と再現手順を主題別に整理した。\n", encoding="utf-8")
    record(Path(__file__))
    outputs = [*out.glob("*.md"), *out.glob("*.html")]
    (out / "report_manifest.json").write_text(json.dumps({"created_utc": datetime.now(UTC).isoformat(),
        "inputs_sha256": SOURCES, "outputs_sha256": {p.name: sha256(p) for p in outputs}}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(out / "report.html"), "chapters": len(chapters), "characters": len(report)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
