"""Build the local NGT analysis index from saved, auditable numerical outputs."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from aquabeacon_analysis.experiments import load_experiment


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def stats(path, column, flag=None):
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    values = np.array([float(row[column]) for row in rows
                       if flag is None or row[flag] == "True"])
    values = values[np.isfinite(values)]
    return {"source": str(path), "column": column, "total_rows": len(rows),
            "selected_rows": len(values), "mean_m": float(np.mean(values)),
            "centered_rms_m": float(np.std(values)),
            "min_m": float(np.min(values)), "max_m": float(np.max(values))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", required=True, choices=["20261028_ngt_000"])
    args = parser.parse_args()
    root = Path.cwd()
    exp = load_experiment(root, args.experiment)
    out = exp.stage_path("results")
    config_path = root / exp.metadata["config_paths"][0]
    config = read(config_path)
    sync = read(out / "synchronization/manifest.json")
    gnss = read(out / "gnss/metrics.json")
    verification = read(out / "verification.json")
    jst = timezone(timedelta(hours=9))
    segments = []
    names = ["親機GNSS静止", "子機位置推定・静止", "測位範囲限界", "親子同時移動"]
    for spec, label in zip(config["segments"], names, strict=True):
        eid = spec["experiment_id"]
        summary = {**spec, "label_ja": label}
        segment_dir = root / "results" / eid
        cut = read(segment_dir / "synchronization/manifest.json")
        summary["jst_interval"] = [datetime.fromisoformat(cut[key]).astimezone(jst).isoformat()
                                   for key in ("start_utc", "end_utc")]
        if eid != "20261028_ngt_001":
            summary["depth"] = stats(segment_dir / "fusion/child_depth.csv", "raw_depth_m")
            summary["acoustic_candidate"] = stats(segment_dir / "fusion/range_constraints.csv",
                                                    "corrected_range_m", "range_valid")
        segments.append(summary)
        videos = "\n".join(
            f'- [{role} 動画](../../data/interim/{eid}/synchronized/{role}/{role}_video.mp4)'
            for role in ("parent", "child"))
        segment_text = (
            f"# {label}\n\n親機原動画の {spec['parent_video_interval_s'][0]}–"
            f"{spec['parent_video_interval_s'][1]} 秒を保存。\n\n"
            f"JST {summary['jst_interval'][0]} から {summary['jst_interval'][1]}。\n\n"
            f"{videos}\n\n"
            "- [同期CSVと切出し根拠](synchronization/report.md)\n"
            "- [RTK・PPK経路と評価](gnss/report.md)\n"
            "- [親子の姿勢・親位置・子深度と測距候補](fusion/report.md)\n"
            "- [全体レポートと制限](../20261028_ngt_000/report.md)\n\n"
            "グラフはPNG/SVGに加え、ブラウザ編集用のfigure.pickleとplot.jsonを併置。\n"
        )
        if eid == "20261028_ngt_001":
            segment_text += ("\n子カメラは先頭48.09秒が未収録。子83304のセンサもこの区間にはない。"
                             "子映像の再生0秒は親動画の4:48.09付近に対応する。\n")
        (segment_dir / "report.md").write_text(segment_text, encoding="utf-8")
    static = gnss["static"]
    lines = ["# 2026年10月28日 学校プール実験の解析", "",
             ("カメラ内IMUと機体IMUで時刻をそろえ、指定区間の動画8本とセンサCSV88個を保存した。"
             "RTK・PPKの経路と静止ばらつき、親子のローカル姿勢、親機位置、子機深度を算出した。"
             "子機の絶対3D位置は、機体軸・光学方位・測定中心の不足により未確定。"), "",
             ("原本は変更していない。基準座標がないことをユーザーに確認済みで、"
             "RMSは平均位置からのばらつき、RTK–PPK差は方式間の一致度として扱う。"), "",
             "## 実験区間と保存先", "",
             ("時刻は親機原動画を基準にした。GNSSの保存時刻は14時台で、"
             "『15時頃』という申告に合わせた時刻の手動変更はしていない。"), "",
             "| ID | 内容 | 親動画 | GNSS基準のJST | 保存結果 |", "|---|---|---|---|---|"]
    for segment in segments:
        start, end = segment["parent_video_interval_s"]
        time = lambda seconds: f"{int(seconds) // 60}:{int(seconds) % 60:02d}"
        jstart, jend = [value[11:19] for value in segment["jst_interval"]]
        eid = segment["experiment_id"]
        lines.append(f"| {eid} | {segment['label_ja']} | {time(start)}–{time(end)} | "
                     f"{jstart}–{jend} | [動画・CSV・図](../{eid}/report.md) |")
    lines += ["", ("各区間の動画・元列を保持した同期CSVは `data/interim/<id>/synchronized/`。"
              "親子MP4は1080p・119.88fpsの無再圧縮コピー。DJIテレメトリは別CSVに保持した。"
              "001の子カメラ先頭48.09秒と子センサは未収録。"), "", "## 同期の結果", "",
              ("四元数の差分から得たカメラ角速度と機体ジャイロを、複数の動作区間で照合した。"
              "UTC = 下表の開始UTC + scale × 元動画秒。"), "",
              "| カメラ | 動画0秒のUTC | scale | drift | 窓間残差最大 |", "|---|---|---|---|---|"]
    for role, label in (("parent", "親機"), ("child", "子機")):
        m = sync["cameras"][role]
        lines.append(f"| {label} | {m['utc_at_video_zero_iso']} | {m['time_scale']:.12f} | "
                     f"{m['drift_ppm']:.2f} ppm | {m['window_fit_residual_max_s']*1000:.2f} ms |")
    lines += ["", ("窓間残差は同じログ内の整合度で、外部基準に対する時刻精度ではない。"
              "探索刻み5ms、カメラテレメトリ約30Hz、センサの配信遅延も残る。"
              "子機9:20付近の動作を使用し、後半の低相関窓は採用していない。"), "",
              ("[同期manifest](synchronization/manifest.json)・"
              "[親機の照合図](synchronization/parent_imu_sync.png)・"
              "[子機の照合図](synchronization/child_imu_sync.png)"), "", "## GNSSの静止ばらつき", "",
              "親動画4:00–8:00、各240点。各方式の同区間の平均を引いた残差。", "",
              "| 方式 | 水平RMS | 3D RMS | 解の内訳 |", "|---|---|---|---|"]
    for key, quality in (("RTK receiver", "Float 240点"), ("PPK", "Float 234点 / DGPS 6点")):
        m = static[key]
        lines.append(f"| {key} | {m['rms_horizontal_m']:.3f} m | {m['rms_3d_m']:.3f} m | {quality} |")
    lines += ["", ("最終PPKはMSM7の追尾切れ情報を保ち、整数固定を無効にして再計算した。"
              "元の混在MSM4/7変換では55衛星行・58個のLLIが失われ、静止解が大きく飛んだ。"
              "元の解と条件別の比較を残している。PPKを真値として扱わない。"), "",
              "![静止ENU残差](gnss/static_enu_residuals.png)", "",
              "[GNSS評価・全3実験の経路・処理条件](gnss/report.md)", "",
              "## センサフュージョン", "",
              ("親子の姿勢はカメラIMU軸へ合わせた機体ジャイロで予測し、カメラ四元数で補正した。"
              "校正済み機体FRDや地理的方位ではなく、ローカルなカメラ姿勢。"
              "親機の位置はGNSSとIMUの静穏判定を組み合わせ、既知静止区間だけゼロ速度拘束を与えた。"
              "PPKを用いた同じ処理も代替解として別保存した。"), "",
              ("子機は圧力とIMUの静穏度から深度を推定した。光源候補には誤検出があり、"
              "水中内部標定・センサ測定中心も未確定のため、絶対XYZはNaNとした。"
              "IMUだけを二重積分した漂流軌跡を実験の移動経路として示してはいない。"), "",
              "子機静止17:30–18:30の成分別評価は次のとおり。3D位置RMSEではない。", "",
              "| 観測 | 点数 | 平均 | 平均まわりのRMS |", "|---|---|---|---|"]
    for field, label in (("depth", "圧力換算深度"), ("acoustic_candidate", "音響距離の式適用候補")):
        value = segments[1][field]
        lines.append(f"| {label} | {value['selected_rows']} | {value['mean_m']:.3f} m | "
                     f"{value['centered_rms_m']:.3f} m |")
    lines += ["", ("圧力は1031.199951mbarを基準、密度997kg/m³。音響は保存profile7の"
              "1500m/s・offset -55µsを使用した。両者は異なる測定中心に対応するため、差をそのまま誤差にしない。"
              "全1330測距状態を保持し、402点は式適用候補。最大8.628mの飛びも残し、直接波・測位成功と認定していない。"), "",
              "[フュージョンの方法・設定・限界](fusion/report.md)", "",
              "![同時移動の親機位置・子深度・測距候補](../20261028_ngt_004/fusion/fusion_overview.png)", "",
              "## 軸合わせとCAD", "",
              ("親機の4姿勢収録は完了しているが、保存データの再評価では静止性条件と"
              "方向整合5°の条件を通らなかった。方向残差は0°、4.79°、8.42°、3.39°。"
              "候補変換は監査用に保存し、校正済みの機体軸としては適用しなかった。"), "",
              ("前日に子機の4姿勢の軸合わせとBNO085本体の校正を両方完了したことをユーザーに確認した。"
              "今回のフォルダの全6組の子機ログを調べたが、保存変換・4姿勢の区間マーカーは特定できなかった。"
              "1秒の短い静止区間も再探索したが、4姿勢の直交方向の順序は特定できなかった。"
              "前日候補21812は約25秒の同じ姿勢の記録で、これだけでは前方軸・yawを復元できない。"
              "BNO四元数とカメラ融合姿勢には初期合わせ後も中央値約35°の差があり、未解決として残した。"), "",
              ("[校正監査](ingest/calibration_audit.json)・"
              "[親機候補変換と残差](ingest/parent_axes_geometric_candidate.json)・"
              "[子機の日付・重力監査](ingest/child_date_gravity_audit.json)・"
              "[短い静止区間も含む再確認](ingest/child_short_pose_evidence.json)"), "",
              ("これは校正未実施という意味ではない。軸変換の数値を再使用するには、"
               "C_MAIN_BODY_ALIGN、区間マーカー付きC_MAIN_BODY_CAPTURE、または過去の"
               "AquaBeaconBodySolution1 JSONが必要。BNO内部の校正はセンサ内に残る場合があり、"
               "SDの保存履歴がないことだけで未校正とは断定しない。"), "",
              ("ユーザー指定の4つのassemblyを今後の可視化参照として記録。"
              "旧CADからの更新を検出したため `cad_20261028` を新規採取し、669部品・9センサー参照点と"
              "親子GLBを保存した。子機ライト構成も含む。実機測定中心の校正には使わない。"), "",
              "[CAD参照](cad_geometry/report.md)・[継続利用の記録](../../docs/cad_rig.md)", "",
              "## 編集・再現・検証", "",
              ("17組のグラフにPNG/SVG/figure.pickle/plot.jsonを保存。次のコマンドで軸、凡例、"
              "フォント、サイズ、表示範囲、系列の見た目をブラウザで編集できる。寸法のドラッグ、"
              "数値入力とスライダー、設定の保存・再読込、PNG/SVG/PDF出力に対応する。"), "",
              "```powershell",
              "python -m aquabeacon_analysis.editable_plots results/20261028_ngt_000/gnss/static_enu_residuals.figure.pickle",
              "```", "",
              ("[実行手順](../../README.md#2026-10-28-学校プール実験)・"
              "[解析manifest](manifest.json)・[成果物検証](verification.json)。"
              f"検証済み同期CSV {len(verification['csv'])}個、映像 {len(verification['videos'])}本。"
              "8本とも元動画の指定時刻の初表示フレームと画素ハッシュが一致。"
              "表示期間差は1フレーム未満。16原本ログのSHA-256も復号開始時と一致した。"), "",
              ("全体テスト・Lint結果とプロセス終了確認は [最終確認](final_checks.json)。"
              "解析・検証用の常駐サーバーは起動していない。")]
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (out / "summary.json").write_text(json.dumps({"segments": segments,
        "position_status": "parent_estimated_child_absolute_xyz_unobservable",
        "gnss_static": static}, ensure_ascii=False, indent=2), encoding="utf-8")
    code = list((root / "src/aquabeacon_analysis").glob("ngt_*.py"))
    code += list((root / "scripts").glob("*ngt*.py"))
    inputs = [config_path, root / "calibration/rigs/cad_20261028.json"]
    manifest = {"experiment_id": args.experiment, "created_utc": datetime.now(UTC).isoformat(),
                "git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
                "working_tree": "Existing user changes preserved; code hashes below identify this run",
                "config_calibration_code": [{"path": str(p.relative_to(root)),
                    "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in inputs + code],
                "upstream_manifests": ["ingest/decode_manifest.json", "ingest/calibration_audit.json",
                    "synchronization/manifest.json", "gnss/manifest.json", "fusion/manifest.json",
                    "cad_geometry/manifest.json", "verification.json"],
                "limitations": ["No surveyed ground truth", "Child saved body alignment not identified",
                    "Parent body capture failed strict fit acceptance", "Optical bearing uncalibrated",
                    "Child absolute XYZ unobservable", "CAD reference is not sensor extrinsic calibration"]}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(out / "report.md")


if __name__ == "__main__":
    main()
