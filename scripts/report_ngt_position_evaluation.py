"""Write pool-plan evaluation, sensitivity evidence and per-experiment reports."""
from __future__ import annotations

import argparse
import csv
import json
import shutil
from pathlib import Path

import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from aquabeacon_analysis.editable_plots import save_editable_figure
from aquabeacon_analysis.ngt_binary import sha256
from aquabeacon_analysis.ngt_position import calibrated_rays

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "results/20261028_ngt_000/position_evaluation"
LABELS = {"002": "子機静止", "003": "測位限界", "004": "親子同時移動"}


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path, rows):
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def pure_water_speed(t):
    """Marczak 1997, NPL transcription, 0–95 C at atmospheric pressure."""
    return 1402.385+5.038813*t-.05799136*t**2+.0003287156*t**3-1.398845e-6*t**4+2.78786e-9*t**5


def diagnostics():
    health_path = ROOT / "data/interim/20261028_ngt_000/decoded/child/C_MAIN_HEALTH_RANGE_83304.health.csv"
    health = read_csv(health_path)
    sync = json.loads((ROOT / "results/20261028_ngt_000/synchronization/manifest.json").read_text())["cameras"]["parent"]
    temperatures = []
    for r in health:
        mono, anchor, epoch, until = [float(r[k]) for k in
                                     ["mono_us", "utc_anchor_mono_us", "utc_anchor_epoch_us", "utc_valid_until_us"]]
        if anchor <= mono < until and epoch > 1e15:
            pt = ((epoch+mono-anchor)/1e6-sync["utc_at_video_zero_s"])/sync["time_scale"]
            temperatures.append((pt, float(r["temp_c"])))
    temperatures = np.array(temperatures)
    calibration_path = ROOT / "calibration/parent_camera_underwater_20260924_provisional.json"
    cal = json.loads(calibration_path.read_text())
    k, d = cal["proxy_direct_camera_matrix_px"], cal["proxy_direct_distortion_coefficients"]
    late = cal["late_only_diagnostic"]
    rows = []
    for suffix in LABELS:
        folder = ROOT / f"results/20261028_ngt_{suffix}/position_evaluation"
        m = json.loads((folder / "metrics.json").read_text())
        a = read_csv(folder / "positions_at_acoustic_events.csv")
        lo, hi = m["interval_s"]
        temp = temperatures[(temperatures[:, 0] >= lo) & (temperatures[:, 0] < hi), 1]
        pixels = np.array([[float(r[k]) for k in ["light_x_px", "light_y_px"]] for r in a])
        distance = np.array([float(r["range_m"]) for r in a])
        rays = calibrated_rays(pixels, k, d)
        late_rays = calibrated_rays(pixels, late["proxy_direct_camera_matrix_px"],
                                   late["proxy_direct_distortion_coefficients"])
        delta = distance*np.linalg.norm(rays-late_rays, axis=1)
        angle = np.degrees(np.arccos(np.clip(np.sum(rays*late_rays, axis=1), -1, 1)))
        c = pure_water_speed(float(np.median(temp)))
        good = [r for r in a if r["magnetic_position_available"] == "True"]
        rows.append({"experiment": suffix, "logged_temperature_median_c": float(np.median(temp)),
                     "logged_temperature_min_c": float(temp.min()), "logged_temperature_max_c": float(temp.max()),
                     "pure_water_speed_at_median_m_s": c,
                     "range_relative_change_percent": (c/1500-1)*100,
                     "range_change_at_1m_m": c/1500-1,
                     "alternative_intrinsics_position_difference_p95_m": float(np.nanpercentile(delta, 95)),
                     "alternative_intrinsics_ray_angle_p95_deg": float(np.nanpercentile(angle, 95)),
                     "coobserved_range_min_m": min(float(r["range_m"]) for r in good),
                     "coobserved_range_max_m": max(float(r["range_m"]) for r in good),
                     "coobserved_offaxis_max_deg": max(float(r["off_axis_deg"]) for r in good)})
    write_csv(OUT / "sensitivity.csv", rows)
    (OUT / "sensitivity_manifest.json").write_text(json.dumps({
        "generator": str(Path(__file__).relative_to(ROOT)), "generator_sha256": sha256(Path(__file__)),
        "health_sha256": sha256(health_path), "calibration_sha256": sha256(calibration_path),
        "temperature_policy": "logged temp_c is an unverified water-path temperature proxy; sensitivity only",
        "model": "Marczak pure water at atmospheric pressure; no seawater35ppt assumption",
        "source": "https://resource.npl.co.uk/acoustics/techguides/soundpurewater/underlying-phys.html",
        "primary_range_unchanged": True}, ensure_ascii=False, indent=2), encoding="utf-8")
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", required=True, choices=["20261028_ngt_000"])
    parser.parse_args()
    summary = json.loads((OUT / "summary.json").read_text())
    sensitivity = diagnostics()
    table_rows = []
    table = ["| 試験 | 時間 | 光学候補 | 測距候補 | 位置算出 | 最大連続欠測 | 音響イベント時の位置点数 |",
             "|---|---:|---:|---:|---:|---:|---:|"]
    for suffix, label in LABELS.items():
        experiment = "20261028_ngt_"+suffix
        m = summary[experiment]
        a = m["grid_availability_fraction"]
        table.append(f"| [{suffix} {label}](../../{experiment}/position_evaluation/report.md) | {m['duration_s']} s | "
                     f"{100*a['optical_available']:.1f}% | {100*a['range_formula_available']:.1f}% | "
                     f"{100*a['magnetic_position_available']:.1f}% | {m['longest_gap_s']:.1f} s | "
                     f"{m['shot_magnetic_positions']} / {m['recorded_shots']} |")
        table_rows.append({"experiment_id": experiment, "duration_s": m["duration_s"],
                           "optical_fraction": a["optical_available"], "acoustic_fraction": a["range_formula_available"],
                           "position_fraction": a["magnetic_position_available"], "longest_gap_s": m["longest_gap_s"],
                           "position_events": m["shot_magnetic_positions"], "recorded_events": m["recorded_shots"]})
        specific = ""
        if suffix == "002":
            scatter = m["relative_scatter"]
            specific = (f"静止区間の磁北NED相対位置は53点。平均からの3次元RMSは **{scatter['centered_rms_3d_m']:.3f} m**、"
                        f"水平RMSは{scatter['centered_rms_horizontal_m']:.3f} m、95%半径は{scatter['radius_p95_m']:.3f} m。"
                        "これは繰り返しのばらつきで、独立基準座標に対する誤差ではない。\n\n"
                        f"カメラ固定座標での3次元RMSは{m['camera_relative_scatter']['centered_rms_3d_m']:.3f} m、"
                        f"磁気を使わないカメラの局所重力座標では{m['local_gravity_relative_scatter']['centered_rms_3d_m']:.3f} m。"
                        "座標系が異なり、親機の回転・並進や光点中心の変動を含むので、その差を磁気センサ単体の誤差と解釈しない。\n\n")
        elif suffix == "003":
            longest = max(m["missing_intervals"], key=lambda x: x["duration_s"])
            specific = (f"最長欠測は親動画{longest['start_s']:.1f}〜{longest['end_s']:.1f}秒。"
                        "代表画像では光点が画面端へ移動し、その後は視野外になる。"
                        "実測距離目盛りが未対応のため、光・音響の物理的な最大距離は確定できない。"
                        "`conditional_range_angle_bins.csv` は観測できた距離・角度に条件付けた成立率で、"
                        "真の距離・角度に対する成功率ではない。\n\n")
        else:
            specific = (f"欠測をまたがない子機水平の観測折れ線長は{m['global_horizontal_observed_trajectory']['observed_polyline_length_m']:.2f} m、"
                        f"最初と最後の有効位置の水平変位は{m['global_horizontal_observed_trajectory']['first_to_last_displacement_m']:.2f} m。"
                        f"親機GNSSの始終変位は{m['parent_horizontal_trajectory']['first_to_last_displacement_m']:.2f} m。"
                        "GNSS採用元の切替えによる約5 mの跳びを含むため、折れ線長を実際の移動距離とみなさない。"
                        "ユーザー申告の約25 mは未測量なので、長さ誤差や正解スケールには使っていない。\n\n"
                        "[同区間のプール壁マッピング評価](../wall_mapping/report.md)\n\n")
        folder = ROOT / f"results/{experiment}/position_evaluation"
        text = f"# 長田高校プール {label}の位置推定評価\n\n"
        text += (f"親機カメラのライト光線と音響測距を組み合わせ、親機姿勢で磁北NEDへ変換した。"
                 f"{m['duration_s']}秒中の位置候補算出率は **{100*a['magnetic_position_available']:.1f}%**、"
                 f"最大連続欠測は **{m['longest_gap_s']:.1f}秒**。\n\n")
        text += specific
        text += "![成立状況とセンサ](availability_and_sensors.png)\n\n![相対位置と水平軌跡](positions.png)\n\n![相対位置成分](relative_components.png)\n\n"
        text += ("## データと再現\n\n[全体の方法と限界](../../20261028_ngt_000/position_evaluation/report.md)、"
                 "[音響イベントごとの位置](positions_at_acoustic_events.csv)、[5 Hzの全時刻](positions_5hz.csv)、"
                 "[欠測一覧](outages.csv)、[集計](metrics.json)、[入力と設定](manifest.json)。"
                 "範囲外・無効イベントはNaNで残し、距離の外れ値や深度差による除外は行っていない。\n\n"
                 "表示位置は光学・音響中心の一致を仮定する。磁北姿勢の精度とセンサ間レバーアームは未検証。"
                 "GNSSとの合成は磁気偏角0°を仮定した参考座標であり、校正済みの真北ENUではない。\n\n"
                 "PNG・SVG・ローカル生成のfigure.pickle・plot.jsonを併置。ブラウザでラベル、凡例、文字、線・点、"
                 "軸範囲、図の寸法を編集し、style.jsonと編集後PNG/SVG/PDFを保存できる。\n\n"
                 f"```powershell\npython -m aquabeacon_analysis.editable_plots results/{experiment}/position_evaluation/positions.figure.pickle\n```\n")
        (folder / "report.md").write_text(text, encoding="utf-8")
    write_csv(OUT / "evaluation_summary.csv", table_rows)
    fig, ax = plt.subplots(figsize=(9, 4.5), layout="constrained")
    x = np.arange(3)
    for shift, key, label in [(-.24, "optical_fraction", "Optical candidate"),
                              (0, "acoustic_fraction", "Acoustic candidate"),
                              (.24, "position_fraction", "Position available")]:
        ax.bar(x+shift, [100*r[key] for r in table_rows], width=.24, label=label)
    ax.set(xticks=x, xticklabels=["002 Static", "003 Limit", "004 Simultaneous move"],
           ylabel="Coverage of 5 Hz time cells [%]", ylim=(0, 110), title="Range/light position availability")
    ax.legend(fontsize=9)
    ax.grid(axis="y", alpha=.25)
    save_editable_figure(fig, OUT / "evaluation_summary.png", dpi=150)
    plt.close(fig)
    text = """# 長田高校プール実験の位置推定評価

親機カメラの子機ライトから求めた光線と音響測距を統合し、親機の磁北姿勢で相対位置を算出した。静止区間では平均位置からの3次元のばらつきが **0.240 m**、親子同時移動では位置候補の時間継続率が **62.4%**、最長欠測が **26.4秒**だった。独立した基準座標がないため、これらは絶対位置精度の評価ではない。

実験計画書の「静止」「測位限界」「同時移動」を区間002〜004に対応付けた。マッピングは指定どおり同時移動004のプール壁を対象にした。

## 成立率と欠測

""" + "\n".join(table) + """

分母は各区間全体の5 Hz時刻セル。無効値・画面外・遮蔽・時刻不一致も分母に含む。音響イベント列の分母は保存されたイベント数で、送信計画回数ではない。静止区間では記録54イベント中53点を算出できたが、記録自体の空白も含む時間継続率は88.7%となる。

音響の「成立」はstate2/3、profile7、有限正値を満たす式適用候補。直接波の正しさを独立に検証した成功率ではない。光学も飽和領域を使う候補で、5秒ごとの98枚と代表画像を目視した。1400秒・1500秒の遮蔽前後各2.5秒は保守的に除外し、除外前フラグもCSVに残した。

![成立率](evaluation_summary.png)

## 静止位置のばらつき

磁北NEDの53点について、北・東・下の標準偏差は **0.232 / 0.046 / 0.041 m**、水平RMSは **0.237 m**、平均位置を中心とする95%半径は **0.389 m**。カメラ固定座標の3次元RMSは0.124 m、カメラの局所重力座標では0.243 mだった。これらには実際の機体の動きと光点中心の変動が含まれる。座標系の違いだけから磁気センサの誤差や姿勢補正の良否は決められない。

GNSSも足した参考位置の3次元RMSは0.341 m。相対測位と親機GNSSを含む位置のばらつきを分けた。圧力深度に対する光学・音響の下向き成分の差は中央値 **−0.160 m**。異なる測定中心を含む内部整合性の差であり、深度の真値誤差ではない。

## 測位範囲と移動の評価

区間003は光点が画面右端へ移動し、その後は視野外となった。最長108秒の欠測を含み、単純な遠距離減衰と区別できない。メジャー目盛りとログの対応がないため、「最大測位距離○m」という結論は出せない。距離・角度別CSVは観測できた値に条件付けた集計として保存した。

区間004の子機の始終水平変位は **21.40 m**、親機GNSSは **21.60 m**。欠測をまたがない子機の観測折れ線長は42.27 mで、GNSS採用元切替えの約5 mの跳びと細かな変動を含む。約25 mという実施記録を正解距離に置き換えず、移動追従の成否は継続率・欠測・軌跡で評価した。

[004の壁マッピング評価](../../20261028_ngt_004/wall_mapping/report.md)では、5 Hzの625枚中124枚（19.8%）から4つの壁断片を復元した。位置拘束あり・なしで登録画像数は同じで、連続した25 m規模の地図は得られなかった。別チャットの子機単眼VSLAMも同時移動全体で追跡7.29%、5地図に分断している。壁COLMAPは125秒・ROI、VSLAMは280秒・全画面なので成立率の直接比較はしない。実測した壁・タイル寸法がないため、寸法誤差は未算出とする。

## 海の解析から継承したものと今回の条件

| 項目 | 今回の扱い |
|---|---|
| 水中内部校正 | ユーザー指定の沖縄水中校正を親機・子機の共通既定に使用。LRFの1280×720にはproxy直接校正を適用 |
| 光学とIMUの軸 | 旧海解析の仮回転を流用せず、今回の映像の重力方向・運動を照合した離散軸対応。精密な外部校正ではない |
| 親機姿勢 | 今回のSpresense軸校正011と本番カメラ同期、独立LIS3MDL取付設定による磁北NED。精度検証フラグを利用禁止条件にしない |
| 時刻 | 今回のカメラ別UTC affine変換とshot_seq対応。海の固定オフセットは不使用 |
| 音響 | 本番profile7の c=1500 m/s、(ToF+55 µs) を主結果とする。海水塩分35‰の仮定は不使用 |
| 圧力 | 今回の水没前840〜900秒の1031.200 mbar基準、密度997 kg/m³。海の1013.25 mbar・1025 kg/m³は不使用 |
| 測定中心 | カメラ・ライト・音響・GNSS・圧力の中心差は未測量。ゼロレバーアームの条件付き位置として明記 |
| 世界座標 | 相対位置は磁北NED。GNSSとの合成は偏角0°の参考シナリオ。真北偏角の自動推定・黙示補正はしない |

`calibration/camera_defaults.json` が両カメラを同じ校正へ向ける。共通ローダーで解像度とmain/proxyを選択し、実使用値・ハッシュをmanifestへ記録した。校正は視点多様性の限られた暫定値で、撮影条件の一致を独立には検証していない。

今回の離散軸は光学右・下・前をカメラIMUの−Z・+Y・+Xへ対応させた。155〜205秒の空中校正動作の画像重力方向と、24通りの軸置換の運動照合に基づく。運動照合は37候補中1候補だけが相対残差35%以内であり、精密な回転校正としては不足する。方位付き位置の24 cmはこの暫定軸に条件付けた結果で、校正精度の保証を含まない。

## 条件を変えたときの影響

"""
    text += "| 試験 | 記録温度中央値 | 純水モデル音速 | 1500 m/sからの距離変化 | 内部校正別候補での位置差95%値 |\n|---|---:|---:|---:|---:|\n"
    for row in sensitivity:
        text += (f"| {row['experiment']} | {row['logged_temperature_median_c']:.2f} °C | {row['pure_water_speed_at_median_m_s']:.2f} m/s | "
                 f"{row['range_relative_change_percent']:.2f}% | {1000*row['alternative_intrinsics_position_difference_p95_m']:.1f} mm |\n")
    text += """

音速の比較は記録温度を水路温度と仮定した感度分析であり、主結果は変更していない。温度センサの水温代表性・塩分は未測定。純水式は[NPLのMarczak式](https://resource.npl.co.uk/acoustics/techguides/soundpurewater/underlying-phys.html)を使う。内部校正別候補は同じ校正動画の後半のみで求めた既存解で、差が小さくても両解に共通する系統誤差を否定できない。1 mの測距では角度が5°ずれると横方向の差は約8.7 cmとなるため、光学とIMUの外部校正と中心間距離の実測が次の改善点になる。

## 計画書との対応と残る評価

| 計画書の項目 | 今回の実施 | 判断 |
|---|---|---|
| 静止の相対位置ばらつき | 53イベント、平均中心RMS・標準偏差・95%半径 | 算出済み。独立基準座標に対するRMSEは未算出 |
| 距離・角度に対する成立率 | 全時間成立率、観測値条件付きbin、視野外・遮蔽の画像確認 | 真の距離・角度別の限界は未確定 |
| 同時移動の継続率と欠測と軌跡 | 5 Hz全時刻、全イベント、欠測一覧、参考GNSS合成軌跡 | 算出済み。約25 mを正解スケールには使用せず |
| マッピングの寸法と形状 | 同時移動区間の壁でCOLMAP、位置prior有無を比較 | 詳細は壁レポート。実測寸法との差は未評価 |

## 再現と編集

`config/20261028_ngt/position_evaluation.json` に時間許容差と除外理由を保存した。音響は全イベントから最近傍を選んでからvalidを判定し、無効イベントを飛び越えて良い距離を補間しない。音響の最大時差0.6秒、光学0.12秒、磁気姿勢のSLERP括弧0.25秒。外挿・外れ値除去・欠測区間をつなぐ平滑化は行っていない。

```powershell
python scripts/evaluate_ngt_position.py --experiment 20261028_ngt_002 --experiment 20261028_ngt_003 --experiment 20261028_ngt_004
python scripts/report_ngt_position_evaluation.py --experiment 20261028_ngt_000
python -m aquabeacon_analysis.editable_plots results/20261028_ngt_000/position_evaluation/evaluation_summary.figure.pickle
```

編集UIはラベル・凡例・フォント・軸範囲・目盛り・グリッド・線と点の見た目、スライダーと数値入力、寸法ドラッグ、style.json保存・再読込、編集後PNG/SVG/PDF出力に対応する。元のPNG/SVG、figure.pickle、plot.json、CSV、生成コード、入力SHA-256とGit SHAを保持した。

計画書原本は `data/raw/ngtHighSchoolExperiment1/AquaBeacon_pool_experiment_checklist.docx`。手法の参照は[海の再推定記録](../../../20260922_cmain_023/calibrated_estimate/report.md)と[OpenCVの光線への変換](https://docs.opencv.org/3.4.1/da/d54/group__imgproc__transform.html)。検証の実施結果は[verification.json](verification.json)に記録する。
"""
    # Correct path to sibling experiment: report resides two levels under results.
    text = text.replace("../../../20260922_cmain_023/", "../../20260922_cmain_023/")
    (OUT / "report.md").write_text(text, encoding="utf-8")
    shutil.copy2(__file__, OUT / "generate_report.py")
    print("Wrote evaluation reports and sensitivity evidence")


if __name__ == "__main__":
    main()
