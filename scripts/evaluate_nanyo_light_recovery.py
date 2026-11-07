"""Verify recovered image observations and compare position availability on the same grid."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

os.environ.setdefault("OMP_NUM_THREADS", "2")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "2")
import numpy as np
from evaluate_position_fallback import (
    ARGUMENTS,
    evaluate_window,
    finite,
    output_rows,
    save_report,
    table,
)
from improve_nanyo_light import CONFIG, ROOT, dump, read, relative, write
from position_fallback_nanyo import load_nanyo

from aquabeacon_analysis.ngt_binary import sha256
from aquabeacon_analysis.position_fallback import run_fallback


def verify_observations(original, improved):
    if len(original) != len(improved):
        raise ValueError("Frame denominators differ")
    recovered = 0
    for old, new in zip(original, improved, strict=True):
        if old["optical_candidate"] == "True":
            for key in old:
                if old[key] != new[key]:
                    raise ValueError(f"Existing accepted measurement changed: {key}")
        if old["track_status"] == "rejected_identity_review" and new["optical_candidate"] == "True":
            raise ValueError("Image-review veto overridden")
        if new["recovered_candidate"] == "True":
            recovered += 1
            xy = [float(new[c]) for c in ("x_px", "y_px")]
            if not np.isfinite(xy).all() or not (0 <= xy[0] < 1280 and 0 <= xy[1] < 720):
                raise ValueError("Recovered measurement outside image")
            if abs(float(new["utc_s"])-float(new["decoded_utc_s"])) > 1e-6:
                raise ValueError("Recovered measurement does not use its actual frame time")
        elif old["optical_candidate"] != "True":
            if np.isfinite(float(new["x_px"])) or np.isfinite(float(new["y_px"])):
                raise ValueError("Missing observation emits a coordinate")
        if abs(float(new["decoded_source_s"])-float(old["actual_source_s"])) > .02:
            raise ValueError("Processed frame time differs by more than 20 ms")
    times = np.array([float(r["utc_s"]) for r in improved])
    if not np.all(np.diff(times) > 0):
        raise ValueError("Observation clock must remain strictly increasing")
    return recovered


def write_input_audit_reports(experiments, run):
    """Describe saved input comparisons without decoding or changing the measurements."""
    all_rows = []
    for eid in experiments:
        out = ROOT/f"results/{eid}/optical_detection/input_audit"
        rows = read(out/"image_clock_audit.csv")
        all_rows.extend(rows)
        report = (f"# {eid} 光源検出の入力監査\n\n"
                  "保存済みLRF確認画像とMP4の対応画像を局所的に照合した。"
                  "同時刻の独立した画素真値や新たな幾何校正ではない。"
                  "時間差による移動と入力形式差を含むため、照合シフトを観測へ補正適用しない。\n\n"
                  "[時刻・画素差CSV](image_clock_audit.csv) / [入力ハッシュ](manifest.json) / "
                  f"[位置評価](../{run}/report.html)\n\n")
        for image_path in sorted(out.glob("comparison_*.jpg")):
            report += f"![左：確認画像、右：再処理MP4]({image_path.name})\n\n"
        save_report(out/"report.md", report)
    old = sum(r["old_accepted_on_review"] == "True" for r in all_rows)
    new = sum(r["new_accepted_on_review"] == "True" for r in all_rows)
    maximum = max(abs(float(r["clock_difference_s"])) for r in all_rows)
    report = ("# 南予の初期化画像・時計監査\n\n"
              f"既存の正例レビュー画像{len(all_rows)}枚のROI内で、旧初期検出は{old}枚、"
              f"新初期検出は{new}枚で候補が成立した。新方式が見逃す旧正例もあり、"
              "評価は旧有効点を保持した補完方式とする。全動画の検出recallではない。\n\n"
              f"比較に使うMP4との最大時刻差は{maximum*1000:.2f} ms。"
              "高速移動で画素がずれる場合があり、初期化は確認JPEGそのもので行う。\n\n"
              "[全照合CSV](../input_audit.csv)\n\n")
    for eid in experiments:
        report += f"- [{eid}](../../../{eid}/optical_detection/input_audit/report.html)\n"
    out = ROOT/"results/20261103_field_review_000/optical_detection/input_audit"
    out.mkdir(parents=True, exist_ok=True)
    save_report(out/"report.md", report)


def evaluate(eid, run, config):
    out = ROOT / f"results/{eid}/optical_detection/{run}"
    meta_path = out / "manifest.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if not meta["complete_trial"]:
        raise ValueError("Pilot subset cannot be reported as a complete trial")
    baseline_path = ROOT / f"results/{eid}/mapping_light_tracking/camera_relative_candidates.csv"
    optical_path = out / "reviewed_camera_relative_candidates.csv"
    review_path = out / "identity_review_manifest.json"
    review = json.loads(review_path.read_text(encoding="utf-8"))
    if review["experiment_id"] != eid or review["output_sha256"] != sha256(optical_path):
        raise ValueError("Reviewed optical output identity/hash mismatch")
    for path, digest in review["inputs_sha256"].items():
        if sha256(ROOT/path) != digest:
            raise ValueError(f"Identity review evidence changed: {path}")
    notes = json.loads((out/"manual_review.json").read_text(encoding="utf-8"))
    old, new = read(baseline_path), read(optical_path)
    recovered = verify_observations(old, new)
    for path, digest in meta["inputs_sha256"].items():
        if sha256(ROOT / path) != digest:
            raise ValueError(f"Input provenance changed: {path}")
    summary = {"experiment_id": eid, "frames": len(old), "recovered": recovered,
               "automatic_additions": review["automatic_additions"],
               "review_rejections": review["review_rejections"],
               "visually_reviewed_samples": notes["samples"],
               "old_detected": sum(r["optical_candidate"] == "True" for r in old),
               "new_detected": sum(r["optical_candidate"] == "True" for r in new),
               "control_same_video": sum(r["control_candidate"] == "True" for r in new),
               "new_same_video": sum(r["new_algorithm_candidate"] == "True" for r in new)}
    for key in ("old_detected", "new_detected", "control_same_video", "new_same_video"):
        summary[key+"_percent"] = 100*summary[key]/len(old)
    provenance, static = [], []
    old_times = None
    for label, source in (("old", None), ("new", optical_path)):
        data = load_nanyo(eid, optical_path=source)
        provenance.extend(data["input_paths"])
        events = np.array([r["event_sample"] for r in data["records"]])
        grid = np.array([r["grid_sample"] for r in data["records"]])
        if old_times is None:
            old_times = data["times"].copy()
        else:
            np.testing.assert_array_equal(old_times, data["times"])
        result = run_fallback(*(data[name] for name in ARGUMENTS),
                              config=config["geometry"], surface_seed_mask=events)
        summary["position_grid_samples"] = int(grid.sum())
        for method in ("joint", "optical_imu_depth", "selected"):
            summary[f"{label}_{method}"] = int((grid & finite(result[method])).sum())
            summary[f"{label}_{method}_percent"] = 100*summary[f"{label}_{method}"]/grid.sum()
        for window in data["metadata"]["static_windows"]:
            metrics, *_ = evaluate_window(data, result, window)
            static.extend({**r, "detector_run": label} for r in metrics)
        if label == "new":
            position_rows = output_rows(data, result)
            write(out / "positions.csv", position_rows)
            write(out / "positions_10hz.csv", [r for r in position_rows if r["grid_sample"]])
            write(out / "positions_at_acoustic_events.csv",
                  [r for r in position_rows if r["event_sample"]])
            metadata = data["metadata"]
    write(out / "stationary_rms.csv", static)
    dependencies = [*provenance, meta_path, CONFIG, Path(__file__), review_path,
                    out/"manual_review.json", out.parent/"input_audit/manifest.json",
                    ROOT / "scripts/position_fallback_nanyo.py",
                    ROOT / "scripts/review_nanyo_light_recovery.py",
                    ROOT / "scripts/audit_nanyo_detection_inputs.py",
                    ROOT / "scripts/evaluate_position_fallback.py",
                    ROOT / "config/20261103_field_review_000/position_fallback.json",
                    ROOT / "src/aquabeacon_analysis/position_fallback.py",
                    ROOT / "src/aquabeacon_analysis/nanyo_position.py",
                    ROOT / "src/aquabeacon_analysis/camera_calibration.py",
                    ROOT / "src/aquabeacon_analysis/parent_magnetic_attitude.py",
                    ROOT / "src/aquabeacon_analysis/ngt_position.py",
                    ROOT / "src/aquabeacon_analysis/ngt_sync.py"]
    dump(out / "evaluation.json", {"summary": summary, "geometry_metadata": metadata,
                                   "same_position_grid": True, "baseline_measurements_preserved": True,
                                   "review_vetoes_preserved": True,
                                   "accuracy_validated": False,
                                   "created_utc": datetime.now(UTC).isoformat(),
                                   "inputs_sha256": {relative(p): sha256(p) for p in dependencies}})
    text = (f"# {eid} 光源検出の改善\n\n"
            f"画像候補の成立率：{summary['old_detected_percent']:.1f}% → "
            f"{summary['new_detected_percent']:.1f}%（{recovered}フレーム追加）。\n\n"
            f"[全7区間の方法・比較](../../../20261103_field_review_000/optical_detection/{run}/report.html)\n\n"
            "[画像同定の除外を適用した光点CSV](reviewed_camera_relative_candidates.csv) / "
            "[除外前の自動候補](camera_relative_candidates.csv) / [10 Hz位置CSV](positions_10hz.csv) / "
            "[同一音響イベントの位置](positions_at_acoustic_events.csv) / "
            "[静止窓の平均周りRMS](stationary_rms.csv) / [入力・設定](manifest.json) / "
            "[検証](evaluation.json) / [画像確認と除外範囲](manual_review.json) / "
            "[時刻と画素の入力監査](../input_audit/report.md)\n\n"
            f"追加候補{review['automatic_additions']}点のうち、画像同定の除外で"
            f"{review['review_rejections']}点を不採用とした。確認画像は{notes['samples']}点。\n\n"
            "成立率は全フレームの光源同定正解率ではなく、絶対位置精度も未検証。"
            "旧有効座標を保持し、追加点だけが再処理画像の実時刻を使う。\n")
    for path in sorted([*out.glob("recovered_review_*.jpg"), *out.glob("boundary_review_*.jpg")]):
        text += f"\n![追加点の10秒間隔サンプル]({path.name})\n"
    save_report(out / "report.md", text)
    print(eid, summary["old_detected"], "->", summary["new_detected"],
          "positions", summary["old_selected"], "->", summary["new_selected"], flush=True)
    return summary


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--experiment", action="append", required=True)
    parser.add_argument("--run", default="multiscale_v2")
    args = parser.parse_args()
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    if set(args.experiment)-set(cfg["experiments"]):
        parser.error("Explicit Nanyo trials required")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]*", args.run):
        parser.error("Invalid run name")
    position_config = json.loads((ROOT/cfg["position_config"]).read_text(encoding="utf-8"))
    print("position comparison PID", os.getpid(), datetime.now(UTC).isoformat(), flush=True)
    write_input_audit_reports(list(dict.fromkeys(args.experiment)), args.run)
    rows = [evaluate(eid, args.run, position_config) for eid in dict.fromkeys(args.experiment)]
    out = ROOT / f"results/20261103_field_review_000/optical_detection/{args.run}"
    out.mkdir(parents=True, exist_ok=True)
    write(out / "availability.csv", rows)
    count_keys = [k for k, v in rows[0].items() if isinstance(v, int)]
    totals = {k: sum(r[k] for r in rows) for k in count_keys}
    for key in ("old_detected", "new_detected", "control_same_video", "new_same_video"):
        totals[key+"_percent"] = 100*totals[key]/totals["frames"]
    for method in ("joint", "optical_imu_depth", "selected"):
        for label in ("old", "new"):
            key = f"{label}_{method}"
            totals[key+"_percent"] = 100*totals[key]/totals["position_grid_samples"]
    dump(out/"summary.json", totals)
    report = "# 南予の光源検出改善\n\n"
    report += (f"{len(rows)}区間・{totals['frames']:,}画像で、光源候補の成立率は"
               f"{totals['old_detected_percent']:.1f}% → {totals['new_detected_percent']:.1f}%"
               f"（{totals['recovered']:,}点追加）。同じ10 Hz格子の位置全体の成立率は"
               f"{totals['old_selected_percent']:.1f}% → {totals['new_selected_percent']:.1f}%。\n\n"
               "この値は保存済み旧有効点に新しい画像観測を補い、画像同定の除外を適用した系列。"
               "新方式単体の全自動検出率や真値に対する正解率ではない。\n\n")
    report += ("色に依存しない複数サイズの局所輝度ピークを優先し、弱い光源は青緑色の局所ピークでも確認する。"
               "線状のロープを形状と曲率で除外し、41 pxの固定テンプレートと前後光学フローで追跡する。"
               "強い光源核を別に測定して固定核とも照合し、短い欠測だけは最後の実測点の120 px以内で競合のない核を再検出する。"
               "既存の目視初期化点を共有し、未同定区間・同定除外を保持。"
               "連続10画像の検出失敗後は次の確認点まで待つ。予測座標・補間点・平滑化点を観測として出力しない。\n\n"
               "目視座標を最大約75 ms前の別画像へ渡していた初期化を修正し、ハッシュ照合した確認JPEGそのもので初期化して後続画像へ追跡する。"
               "画像のない静止窓の座標点は実フレームとの差75 ms以内だけ使う。旧アルゴリズムの対照も同じ初期化条件を使う。\n\n"
               "このPCにLRF原本がないため、保存済み高解像度MP4からGPUで検証用動画を作成した。"
               "4フレームごとに元PTSを保持し、全画面を1280×720へ縮小、8 bit化後はlossless H264。"
               "過去の観測時刻から20 ms以内の画像を使い、追加点のUTCは実際の採用画像に対応する。"
               "同じ動画へ旧方式も適用し、入力形式変更の影響と分けて比較する。\n\n")
    report += table(["実験", "全画像数", "保存済み旧候補 %", "除外適用後 %", "採用追加数",
                     "同じ動画の旧方式 %", "同じ動画の新方式・追加除外前 %"],
                    [[r["experiment_id"], r["frames"], f"{r['old_detected_percent']:.1f}",
                      f"{r['new_detected_percent']:.1f}", r["recovered"],
                      f"{r['control_same_video_percent']:.1f}", f"{r['new_same_video_percent']:.1f}"]
                     for r in rows])
    report += "\n\n## 同じ10 Hz格子での位置成立率\n\n"
    report += table(["実験", "格子数", "共同推定 旧%", "共同推定 新%", "光学＋姿勢＋深度 旧%",
                     "光学＋姿勢＋深度 新%", "フォールバック全体 旧%", "フォールバック全体 新%"],
                    [[r["experiment_id"], r["position_grid_samples"],
                      *[f"{r[k+'_percent']:.1f}" for k in ("old_joint", "new_joint",
                         "old_optical_imu_depth", "new_optical_imu_depth", "old_selected", "new_selected")]]
                     for r in rows])
    report += ("\n\n音響・姿勢・深度・校正原典・フォールバック閾値を両方式で共有する。"
               "旧LRF点にはLRF用Kと歪みを保持し、追加MP4点は画素中心規約で元の1920×1080へ戻してMP4用Kと歪みを適用する。"
               "音響と光学の共同推定、光学＋姿勢＋深度、全方式の選択結果は異なる指標。"
               "初期化・深度基準・水平に近い光線等の条件で、光源が見えても位置が成立しない場合がある。\n\n"
               "画像の候補成立率を全画像の検出recallや正解率と呼ばない。"
               "確認点は学習済みモデルの真値ではなく初期化ROI。追加点の10秒間隔画像を同定監査に用いる。"
               "静止RMSは各系列自身の平均周りで、絶対精度改善の証明には使わない。"
               "従来の光点・位置・地図・統合報告は保持した。\n\n")
    report += (f"## 画像確認と限界\n\n追加点の10秒間隔画像に加え、追加検出が途切れる各区間の"
               f"最初と最後を確認した（重複除去後{totals['visually_reviewed_samples']}画像）。"
               f"自動追加{totals['automatic_additions']}点のうち、網・ロープ等への誤追跡または"
               f"同定を確認できない範囲の{totals['review_rejections']}点を明示的に除外した。"
               "除外前CSVと確認画像は保持し、位置評価にはreviewed_camera_relative_candidates.csvを使う。"
               "短い光源核の照合でも背景の反射へ移る場合があり、未レビューの新規動画への完全自動適用は未検証。\n\n")
    report += table(["実験", "確認画像数", "自動追加数", "同定除外数", "採用追加数"],
                    [[r["experiment_id"], r["visually_reviewed_samples"], r["automatic_additions"],
                      r["review_rejections"], r["recovered"]] for r in rows])
    report += "\n\n[初期化画像とMP4の時計・画素照合](../input_audit/report.md)\n\n"
    for r in rows:
        report += f"- [{r['experiment_id']}：CSV・入力・追加点画像](../../../{r['experiment_id']}/optical_detection/{args.run}/report.html)\n"
    save_report(out / "report.md", report)
    dump(out / "evaluation_manifest.json", {"experiments": args.experiment, "run": args.run,
                                            "git_commit": subprocess.check_output(
                                                ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                                            "same_position_grid": True, "totals": totals,
                                            "evaluation_sha256": {r["experiment_id"]: sha256(ROOT /
                                                f"results/{r['experiment_id']}/optical_detection/{args.run}/evaluation.json")
                                                for r in rows},
                                            "created_utc": datetime.now(UTC).isoformat()})


if __name__ == "__main__":
    main()
