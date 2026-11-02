"""Generate a linked Japanese day2 report and day1/day2 static comparison."""

import argparse
import html
import json
import subprocess
from pathlib import Path

import cv2
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pycolmap

from aquabeacon_analysis.editable_plots import save_editable_figure
from aquabeacon_analysis.ngt_binary import sha256

ACQ = "20261101_nanyo_000"


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def render_maps(eid):
    folder = Path(f"results/{eid}/visual_mapping")
    manifest = read_json(folder / "manifest.json")
    work = Path(f"data/interim/{eid}/visual_mapping")
    all_views = []
    for info in manifest["models"]:
        model = pycolmap.Reconstruction(work / "models" / str(info["id"]))
        xyz = np.array([p.xyz for p in model.points3D.values()])
        rgb = np.array([p.color for p in model.points3D.values()]) / 255
        images = sorted(model.images.values(), key=lambda im: im.name)
        views = []
        for index in [0, len(images) // 2, len(images) - 1]:
            image = images[index]
            frame = cv2.imread(str(work / "images" / image.name))
            frame = cv2.resize(frame, (480, 270))
            cv2.putText(
                frame,
                f"model {info['id']} / {image.name}",
                (10, 24),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.6,
                (0, 0, 255),
                2,
            )
            views.append(frame)
        all_views.append(np.hstack(views))
        center = np.median(xyz, axis=0)
        _, _, vt = np.linalg.svd(xyz - center, full_matrices=False)
        projected = (xyz - center) @ vt[:2].T
        ids = np.linspace(0, len(xyz) - 1, min(6000, len(xyz))).astype(int)
        fig, ax = plt.subplots(figsize=(8, 6))
        ax.scatter(projected[ids, 0], projected[ids, 1], c=rgb[ids], s=2)
        camera = (np.array([im.projection_center() for im in images]) - center) @ vt[:2].T
        ax.plot(
            camera[:, 0],
            camera[:, 1],
            ".",
            ms=3,
            color="crimson",
            label="Registered camera centers",
        )
        ax.set(
            xlabel="Principal map coordinate 1 [arbitrary]",
            ylabel="Principal map coordinate 2 [arbitrary]",
            title=f"{eid} / model {info['id']} (monocular, independent scale)",
            aspect="equal",
        )
        ax.legend(fontsize=8)
        ax.grid(alpha=0.2)
        fig.tight_layout()
        save_editable_figure(fig, folder / f"model_{info['id']}_projection.png", dpi=150)
        plt.close(fig)
    if all_views:
        cv2.imwrite(str(folder / "model_views.jpg"), np.vstack(all_views))
    return manifest


def render_html(markdown, target):
    """Small local renderer for this generated report's headings/tables/links."""
    import re

    def inline(s):
        s = html.escape(s)
        s = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", r'<img alt="\1" src="\2">', s)
        return re.sub(r"\[([^\]]*)\]\(([^)]+)\)", r'<a href="\2">\1</a>', s)

    body, table = [], False
    for line in markdown.splitlines():
        if line.startswith("|"):
            if re.match(r"^[| :\-]+$", line):
                continue
            if not table:
                body.append("<table>")
                table = True
            body.append(
                "<tr>"
                + "".join("<td>" + inline(v.strip()) + "</td>" for v in line.strip("|").split("|"))
                + "</tr>"
            )
            continue
        if table:
            body.append("</table>")
            table = False
        if line.startswith("#"):
            level = len(line) - len(line.lstrip("#"))
            body.append(f"<h{level}>" + inline(line[level:].strip()) + f"</h{level}>")
        elif line:
            body.append("<p>" + inline(line) + "</p>")
    if table:
        body.append("</table>")
    target.write_text(
        '<!doctype html><html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
        "<title>Nanyo day2 実験解析</title><style>body{font:16px/1.8 system-ui,sans-serif;max-width:1120px;margin:40px auto;padding:0 24px;color:#182b38;background:#f9fbfc}"
        "h1,h2,h3{color:#00576b}table{border-collapse:collapse;width:100%;font-size:14px;background:white}td{border:1px solid #ced9df;padding:7px 10px}tr:first-child{font-weight:600;background:#e5f1f4}"
        "img{max-width:100%;height:auto;border:1px solid #e3e8eb}a{color:#006d96}p{margin:12px 0}@media print{body{background:white;margin:0}h2{break-before:page}img,table{break-inside:avoid}}</style>"
        + "\n".join(body)
        + "</html>",
        encoding="utf-8",
    )


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--experiment", required=True, choices=[ACQ])
    parser.add_argument("--skip-map-render", action="store_true")
    args = parser.parse_args()
    root = Path(f"results/{ACQ}")
    out = root / "analysis"
    out.mkdir(exist_ok=True)
    evaluation = read_json(root / "evaluation/manifest.json")
    config = read_json(Path(f"config/{ACQ}/stationary_spectra.json"))
    static = [
        read_json(Path(f"results/{w['experiment_id']}/stationary_spectra/{w['id']}/manifest.json"))
        for w in config["windows"]
    ]
    tracks = [
        read_json(Path(f"results/{w['experiment_id']}/light_tracking/{w['id']}/manifest.json"))
        for w in config["windows"]
    ]
    by_id = {r["window"]["id"]: r for r in static}
    first = by_id["day2_095600"]["metrics"]
    second = by_id["day2_101500"]["metrics"]
    last = by_id["day2_103200"]["metrics"]
    last_parts = last["orthogonal_decomposition"]["corrected"]["rms_m"]
    hand_roll = by_id["day2_102000"]["spectra"]["parent_roll"]
    fig, axes = plt.subplots(1, 2, figsize=(12, 5))
    x = np.arange(len(static))
    labels = ["D" + r["window"]["id"][3] + " " + r["window"]["start"][11:16] for r in static]
    for shift, name, label, color in [
        (-0.18, "fixed_parent", "Parent attitude fixed", "#d99536"),
        (0.18, "corrected", "Attitude corrected", "#007b94"),
    ]:
        axes[0].bar(
            x + shift,
            [r["metrics"][name]["rmse_3d_m"] for r in static],
            0.36,
            label=label,
            color=color,
        )
    axes[0].set(ylabel="3D RMSE about own mean [m]", title="Same accepted acoustic observations")
    axes[0].legend(fontsize=9)
    axes[1].bar(x, [t["accepted"] / t["rows"] * 100 for t in tracks], color="#007b94")
    axes[1].set(
        ylabel="Accepted tracked images [%]", ylim=(0, 110), title="Missing images remain missing"
    )
    for i, r in enumerate(static):
        axes[1].text(i, 103, f"n={r['metrics']['matched_shots']}", ha="center", fontsize=8)
    for ax in axes:
        ax.set_xticks(x, labels, rotation=30, ha="right")
        ax.grid(axis="y", alpha=0.2)
        ax.axvspan(3.6, 4.4, color="orange", alpha=0.1)
    fig.suptitle("Day1 / Day2 stationary comparison; shaded = deliberate hand motion")
    fig.tight_layout()
    save_editable_figure(fig, out / "stationary_comparison.png", dpi=160)
    plt.close(fig)
    gnss_comparison_path = root / "stationary_gnss_comparison/manifest.json"
    gnss_comparison_lines = []
    if gnss_comparison_path.exists():
        comparison = read_json(gnss_comparison_path)
        gnss_comparison_lines = [
            "## PPK Floatを加えた子機の静止絶対位置比較",
            "",
            "GNSSの方式差を評価するため、上記の相対位置に加えて子機CAD原点の絶対位置を比較した。光源・測距・姿勢・採用shotは同じで、通常PPKとFloat設定PPKのみを変更している。相対位置のRMSEはGNSSによらず不変。以下は絶対位置の各区間平均周り3D RMSEで、真値誤差ではない。",
            "",
            "| 日・開始 JST | 深度 m | 共通shot | 通常PPK 補正あり m | Float 補正あり m | 通常PPK 姿勢固定 m | Float 姿勢固定 m |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
        for item in comparison["windows"]:
            w, m = item["window"], item["metrics"]
            label = w["start"][5:19].replace("T", " ")
            if "hand" in w["condition"]:
                label += "（手動）"
            gnss_comparison_lines.append(
                f"| {label} | {item['pressure_depth']['mean_m']:.2f} | {item['common_shots']} | "
                + " | ".join(
                    f"{m[k]['rmse_3d_m']:.4f}"
                    for k in [
                        "ppk_ar__corrected",
                        "ppk_float__corrected",
                        "ppk_ar__fixed_parent",
                        "ppk_float__fixed_parent",
                    ]
                )
                + " |"
            )
        gnss_comparison_lines += [
            "",
            "![GNSS方式別の子機絶対位置RMSE](../stationary_gnss_comparison/absolute_comparison.png)",
            "GNSSは最大1.5秒の隣接区間内で従来と同じ線形時刻合わせを行い、両方式の共通shotで比較した。最近傍GNSSの感度比較も実施。GNSSと相対幾何の共分散を含むので、GNSS単独RMSEの減少がそのまま子機RMSEの減少にはならない。",
            "[全6窓の俯瞰図・時系列・軸別RMSE・計算方法](../stationary_gnss_comparison/report.html) / [比較CSV](../stationary_gnss_comparison/comparison.csv)",
            "",
        ]
    lines = [
        "# Nanyo day2 実験解析・day1との静止比較",
        "",
        "対象：2026年11月1日（day2）、比較：2026年10月31日（day1）。解析日：2026年11月2日。時刻はJST。",
        "",
        "day2全5区間のGNSS・音響・光学位置候補・子機画像マッピングを解析し、day1の2区間とday2の4区間で静止RMSE、光源追跡、FFT、Lomb–Scargle、コヒーレンスを比較した。",
        "位置の真値はなく、以下のRMSEは各系列の区間平均周りのばらつき。真値に対する測位精度ではない。",
        "静止RMSEの位置は、親機の機体原点から子機の機体原点への相対ベクトルを真ENU（東・北・上）で表したもの。GNSSによる両機共通の平行移動は差し引かれる。",
        "",
        "## 静止評価と姿勢補正",
        "",
        "各窓60秒。10:14はユーザー確認で10:15へ訂正。10:15の測距開始前約14秒は欠測のまま扱う。10:20は手動揺動であり、自然波だけの条件に混ぜない。",
        "",
        "| 日・開始 | 条件 | 平均深度 m | 対応測距点 | 補正あり3D RMSE m | 親機姿勢固定 m | RMSE減少 % | 測距RMSE m |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for r in static:
        w, m = r["window"], r["metrics"]
        label = (
            "手動揺動"
            if "hand" in w["condition"]
            else ("探索選定" if "exploratory" in w["condition"] else "静止申告")
        )
        lines.append(
            f"| {w['start'][5:19].replace('T', ' ')} | {label} | {m['pressure_depth']['mean_m']:.2f} | {m['matched_shots']} | {m['corrected']['rmse_3d_m']:.4f} | {m['fixed_parent']['rmse_3d_m']:.4f} | {m['rmse_reduction_percent']:.1f} | {m['range_rmse_m']:.4f} |"
        )
    lines += [
        "",
        "平均深度は各60秒区間の子機水圧データの算術平均。収録初期180秒の水面圧代理値、仮定密度1025 kg/m³による未校正の相対深度で、海底深度や音響斜距離ではない。測距・追跡の成否によらず未平滑化の有限値を集計し、欠測は補間しない。10:15の測距開始前も圧力記録は含める。標本数・最小最大値は個別レポートに記載。",
        "",
        "![静止RMSEと追跡の採用率](stationary_comparison.png)",
        "",
        f"09:56と10:15では姿勢補正後の3D RMSEが約{first['corrected']['rmse_3d_m']:.2f} m・{second['corrected']['rmse_3d_m']:.2f} mとなり、親機姿勢固定に対して約{first['rmse_reduction_percent']:.0f}%・{second['rmse_reduction_percent']:.0f}%減少した。10:32は約{last['corrected']['rmse_3d_m']:.2f} m、減少約{last['rmse_reduction_percent']:.0f}%に留まり、補正後にもゆっくりした変動が残る。各条件で同じ採用点を使った比較であり、別窓・別日の絶対精度の優劣ではない。",
        "親機姿勢固定は前回の比較を踏襲し、同じ測距・光点・子機姿勢を使って親機回転だけを区間平均へ固定した。親子両方を固定した追加感度、各軸・水平RMSE、固定部分窓の感度も個別レポートに保存。",
        "今回は追跡した10 Hz画像を測距イベントへ最大75 msで対応付け、各shotを一度だけ数えた。前回の2 Hz画像中心の集計とは観測点集合が異なる。旧結果は上書きせず残している。",
        "",
        *gnss_comparison_lines,
        "## 全静止区間の俯瞰位置グラフ",
        "",
        "GNSS静止評価と同様に各系列の平均を原点に置き、東・北の位置分布と水平RMSEの円を表示した。各窓の3条件は同じ採用点・同じ縮尺、東西と南北も等縮尺。窓ごとの表示範囲は異なるため、日・時刻間の比較には軸の数値を使う。円は水平RMSEの半径であり、3D RMSEや信頼区間ではない。平均位置の差・絶対精度はこの図では評価しない。",
        "",
    ]
    for r in static:
        w = r["window"]
        rel = f"../../{w['experiment_id']}/stationary_spectra/{w['id']}"
        condition = "・手動揺動" if "hand" in w["condition"] else ""
        lines += [
            f"### {w['start'][:10]} {w['start'][11:19]}{condition}",
            "",
            f"![{w['id']}：静止位置の俯瞰分布]({rel}/position_overhead.png)",
            "",
            f"[PNG]({rel}/position_overhead.png) / [SVG]({rel}/position_overhead.svg) / [個別評価]({rel}/report.html)",
            "",
        ]
    lines += [
        "## 光源検出とトラッキング",
        "",
        "従来の最大白飛び領域は、遠方ライトより手前の黄・白テザーや浮遊物を選ぶ場面があった。画像で初期光源を確認し、ピラミッドLucas–Kanadeの前後整合、初期81×81画素画像とのNCC照合、局所光源重心による追跡を6つの静止窓へ追加した。",
        "追跡の予測は探索範囲にだけ使い、出力座標は各画像の局所光源重心。時系列の平滑化は行わない。喪失後は画像一致による再捕捉、または記録済み目視点で再初期化し、初期化が失敗した場合は次の目視点まで欠測とする。目視点を補間した軌跡は出力しない。",
        "明るい光源は画像全体で白飛び核を抽出してから探索範囲内の中心を選ぶ。明るいハローを探索範囲で切り取った重心が予測点へ固定される問題も修正した。",
        "",
        "| 窓 | 有効画像 / 取得 | 有効率 | 手動再初期化回数 | 確認映像・画像 |",
        "|---|---:|---:|---:|---|",
    ]
    for t in tracks:
        w = t["window"]
        rel = f"../../{w['experiment_id']}/light_tracking/{w['id']}"
        lines.append(
            f"| {w['id']} | {t['accepted']}/{t['rows']} | {t['accepted'] / t['rows']:.1%} | {len(t.get('reseed_events', []))} | [動画]({rel}/tracking_review.mp4)・[拡大]({rel}/all_crops.jpg)・[検出との比較]({rel}/detection_vs_tracking.jpg)・[全採用shot]({rel}/matched_shot_crops.jpg) |"
        )
    lines += [
        "",
        "検出のみの原CSV・旧位置候補を保持した。画素真値の独立注釈がないため、追跡率を画素精度とは呼ばない。追跡の改善は映像上の光源同定と飛び移り抑制で確認し、RMSEが小さくなることだけを成功条件にしない。",
        "比較画像の黄色×は同じ画像に適用した従来検出、赤丸は追跡。両方が出た画像に限定した差は以下の通り。40 px超の不一致は精度誤差率ではない。",
        "",
        "| 窓 | 従来検出数 | 両方採用 | 座標差中央値 px | 40 px超 |",
        "|---|---:|---:|---:|---:|",
    ]
    for t in tracks:
        m = t["detection_only_comparison"]
        median = m["coordinate_difference_median_px"]
        lines.append(
            f"| {t['window']['id']} | {m['candidate_count']} | {m['paired_count']} | {median:.1f} | {m['disagreement_over_40_px']} |"
        )
    lines += [
        "",
        "## 周波数と波の影響の切り分け",
        "",
        "day1の測距は主に4 Hz、day2は主に1 Hz。測距・位置・姿勢・光点を比較する帯域は0.45 Hz以下に制限した。姿勢と光点については10 Hzデータから5 HzまでのFFTも別図にした。",
        "FFTは欠測を跨がない最長連続部分へHann窓を適用する。全窓の実観測時刻はLomb–Scargleで確認し、両者を併記。スペクトルの線形トレンド除去と、未処理のRMSEを区別する。",
        "8秒以上の連続観測がない系列のFFTは空欄とし、Lomb–Scargleだけを保存した。day1の測距・位置、day2の10:20の光点・位置が該当する。",
        "",
        "| 窓 | 親機rollのFFT主ピーク Hz | FFT連続長 s | 0.45 Hz超の分散比 | 算出可能コヒーレンス組 / 42 |",
        "|---|---:|---:|---:|---:|",
    ]
    for r in static:
        p = r["spectra"]["parent_roll"]
        n = sum(c["status"] == "ok" for c in r["coherence"].values())
        lines.append(
            f"| {r['window']['id']} | {p['fft_native_peak_hz']:.3f} | {p['fft_duration_s']:.1f} | {p['fft_run_fraction_above_0_45_hz']:.1%} | {n}/42 |"
        )
    lines += [
        "",
        f"10:20の親機rollでは約{hand_roll['fft_native_peak_hz']:.2f} Hz（周期約{1 / hand_roll['fft_native_peak_hz']:.2f}秒）のピークがあり、トレンド除去後の分散の約{hand_roll['fft_run_fraction_above_0_45_hz'] * 100:.0f}%が0.45 Hz超にある。この主成分は1 Hz測距のナイキスト周波数0.5 Hzを超えるため、測距・位置の低周波ピークと直接対応付けできない。",
        "今回の欠測を保持する条件では、姿勢と位置のコヒーレンスは全6窓で完全窓不足となった。上表の成立組は姿勢と光点、または姿勢と測距であり、位置への波寄与率を直接示すものではない。",
        "![10:20の姿勢・光点の高周波診断](../../20261101_nanyo_003/stationary_spectra/day2_102000/native_spectra.png)",
        "",
        "| 窓 | 姿勢で選んだ周期 s | 姿勢成分 | 固定姿勢の同周期RMS m | 補正後の同周期RMS m | 補正後の残りRMS m |",
        "|---|---:|---|---:|---:|---:|",
    ]
    for r in static:
        m = r["metrics"]
        f = m["attitude_selected_frequency"]
        dec = m["orthogonal_decomposition"]
        lines.append(
            f"| {r['window']['id']} | {f['period_s']:.2f} | {f['series']} | {dec['fixed_parent']['rms_m']['attitude_frequency']:.4f} | {dec['corrected']['rms_m']['attitude_frequency']:.4f} | {dec['corrected']['rms_m']['remainder']:.4f} |"
        )
    lines += [
        "",
        "上表は姿勢だけから選んだ主周波数を使い、位置を『線形トレンド・その周波数のsin/cos・残り』へ直交分解した探索結果。3項の分散和を元の分散と照合した。残りには測定雑音、他の周期、実並進、光点同定・磁気・同期・校正誤差が混在する。",
        "約30秒のピークは60秒窓で2周期という探索下限に近い。安定した波周期を特定した値ではなく、より遅い変動・非定常なドリフトが混ざる可能性がある。欠測は動きの大きい場面へ偏り得るため、手動揺動の採用部分だけのRMSEを60秒全体の性能に一般化しない。",
        "コヒーレンスは共通1 Hz、16秒Hann窓、50%重複で、欠測のない窓が3本以上ある組だけ算出。高レート系列は間引き前に低域化した。少数・重複窓からの探索値であり、有意差判定や独立な波寄与率ではない。",
        "姿勢と光点・位置の共通周期、補正による同周期成分の減少が揃えば、姿勢変動に結び付いた影響を絞り込める。一方、波高計や独立位置真値がないため、自然波とセンサ雑音を一意に分離したとは結論できない。10:20の強制揺動も別要因として残す。",
        f"10:32の補正後3D RMSE約{last['corrected']['rmse_3d_m']:.3f} mは、この直交分解ではトレンドRMS約{last_parts['trend']:.3f} m、選択周期約{last_parts['attitude_frequency']:.3f} m、残り約{last_parts['remainder']:.3f} m。二乗和が元の分散に一致する。姿勢補正だけでは取り除けない変化があり、残り全てを測距ノイズや波に割り当てることはできない。",
        "",
        "## 区間別の結果",
        "",
        "| 区間 | 測距候補 / 全記録 | 候補率 | PPK固定 / Float / DGNSS | 初回位置候補 | 子機SfM登録 / 画像 |",
        "|---|---:|---:|---|---:|---|",
    ]
    maps = []
    for seg in evaluation["segments"][1:]:
        eid = seg["experiment_id"]
        pos = read_json(Path(f"results/{eid}/absolute_position/manifest.json"))
        mapping = (
            read_json(Path(f"results/{eid}/visual_mapping/manifest.json"))
            if args.skip_map_render
            else render_maps(eid)
        )
        maps.append(mapping)
        q = seg["ppk_quality_counts"]
        lines.append(
            f"| {eid[-3:]} | {seg['candidate_count']}/{seg['shot_count']} | {seg['candidate_fraction']:.1%} | {q.get('1', 0)} / {q.get('2', 0)} / {q.get('4', 0)} | {pos['available']}/{pos['rows']} | {mapping['registered_frames']}/{mapping['sampled_frames']} |"
        )
        Path(f"results/{eid}/report.md").write_text(
            f"# {eid}\n\n[day2全体レポート](../{ACQ}/analysis/report.md)\n\n[GNSS・音響](evaluation/) / [初回の位置候補](absolute_position/) / [子機SfM](visual_mapping/)"
            + (
                " / [追跡・静止周波数評価](stationary_spectra/)"
                if Path(f"results/{eid}/stationary_spectra").exists()
                else ""
            )
            + "\n",
            encoding="utf-8",
        )
    lines += [
        "",
        "測距候補はstate=3かつ正の距離。音速1500 m/s・offset 0 µsを保持。候補率は正しい直接波や位置真値の割合ではない。音響距離と圧力深度の差も、親機深度や基準面が未確定のため誤差とは扱わない。",
        "初回位置候補は全区間に対する従来検出の条件付き結果で、誤検出が残る。定量的な静止比較には上記の追跡後の位置を使う。未確認の候補を確定軌跡・真値地図へ昇格しない。",
        "",
        "## GNSS",
        "",
        f"全収録のPPK品質数：{evaluation['segments'][0]['ppk_quality_counts']}。RTKは未使用。主局950437・比較局041133、2026-11-01 UTCの観測から再計算。",
        f"基準局変更による水平差：中央値{evaluation['base_difference_horizontal_median_p95_m'][0]:.3f} m、95%点{evaluation['base_difference_horizontal_median_p95_m'][1]:.3f} m。基準局感度であり真値誤差ではない。",
        "[PPK/SPP・基準局比較CSVと来歴](../evaluation/) / [GNSS再処理の入力来歴](../../../data/interim/20261101_nanyo_000/gnss/manifest.json)",
        "",
        "![GNSS](../evaluation/gnss_overhead.png)",
        "",
        "## 子機画像マッピング",
        "",
        "子機動画は実ファイルで29.97 fps。共通水中内部校正を明示し、1 Hz・960×540・CLAHE・逐次5近傍・固定内部パラメータのCOLMAPで実行した。独立断片の単眼尺度は任意で、位置拘束や確定メートル尺度は付けていない。登録されなかった画像・失敗断片も保持。",
        "代表画像の確認では、001は網と魚、002は海底・近接岩面・船体、003は船体と付着物、004は船体周辺と多数の気泡、005は海底付近。動く網・魚・気泡が含まれ、低い再投影残差だけで静的地形の正しさを保証できない。特に005は2画像だけで連続地図とは扱わない。",
    ]
    for m in maps:
        eid = m["experiment_id"]
        rel = f"../../{eid}/visual_mapping"
        lines += [
            "",
            f"### 区間{eid[-3:]}",
            "",
            f"登録{m['registered_frames']}/{m['sampled_frames']}枚、{len(m['models'])}断片。",
            f"![各モデルの先頭・中間・末尾]({rel}/model_views.jpg)",
        ]
        for model in m["models"]:
            mid = model["id"]
            lines.append(
                f"モデル{mid}：{model['registered_images']}画像、{model['points']}点、平均再投影残差{model['mean_reprojection_error_px']:.3f} px。[投影図]({rel}/model_{mid}_projection.png) / [全点PLY]({rel}/model_{mid}.ply)。"
            )
    lines += ["", "## 個別静止レポート・再現性", ""]
    for r in static:
        w = r["window"]
        rel = f"../../{w['experiment_id']}/stationary_spectra/{w['id']}"
        local = Path(f"results/{w['experiment_id']}/stationary_spectra/{w['id']}")
        render_html((local / "report.md").read_text(encoding="utf-8"), local / "report.html")
        lines.append(
            f"[{w['start']}：RMSE、時系列、FFT、コヒーレンス、成分分解]({rel}/report.html) / [MD]({rel}/report.md)"
        )
    lines += [
        "",
        "各図はPNG・SVG・編集用figure.pickle・plot.jsonを同じ場所へ保存。入力CSV、設定、校正、同期、Git SHA、入力・コードSHA-256をmanifestから追跡できる。原本は変更していない。",
        "全体pytestに残る既存の図保存チェックの失敗と、今回の成果物検証・関連テストの結果は検証記録で区別する。",
        "[検証記録](verification.json) / [全窓比較CSV](../stationary_spectra/comparison.csv)",
        "",
        "手法資料：[SciPy Welch](https://docs.scipy.org/doc/scipy/reference/generated/scipy.signal.welch.html)、[コヒーレンス](https://docs.scipy.org/doc/scipy/reference/generated/scipy.signal.coherence.html)、[Lomb–Scargle](https://docs.scipy.org/doc/scipy/reference/generated/scipy.signal.lombscargle.html)、[OpenCV optical flow](https://docs.opencv.org/4.x/d4/dee/tutorial_optical_flow.html)、[テンプレート照合](https://docs.opencv.org/4.x/d4/dc6/tutorial_py_template_matching.html)。",
    ]
    text = "\n".join(lines) + "\n"
    (out / "report.md").write_text(text, encoding="utf-8")
    render_html(text, out / "report.html")
    (out / "report_generation.json").write_text(
        json.dumps(
            {
                "experiment_id": ACQ,
                "script_sha256": sha256(Path(__file__)),
                "git_commit": subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], text=True
                ).strip(),
                "upstream_manifests_sha256": {
                    str(p): sha256(p)
                    for p in [
                        Path(
                            f"results/{w['experiment_id']}/stationary_spectra/{w['id']}/manifest.json"
                        )
                        for w in config["windows"]
                    ]
                    + [
                        Path(f"results/{m['experiment_id']}/visual_mapping/manifest.json")
                        for m in maps
                    ]
                    + ([gnss_comparison_path] if gnss_comparison_path.exists() else [])
                },
                "static_windows": [r["window"]["id"] for r in static],
                "maps": [m["experiment_id"] for m in maps],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(out / "report.html", flush=True)


if __name__ == "__main__":
    main()
