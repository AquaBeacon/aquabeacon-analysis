"""Report the selected-GNSS Day1/Day2 map products and limits of their union."""

import argparse
import json
from pathlib import Path

from report_nanyo_day2 import render_html

ACQS = ["20261031_nanyo_000", "20261101_nanyo_000"]
COMBINED = "20261102_nanyo_combined_000"


def report(eid):
    out = Path(f"results/{eid}/mapping_replay")
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    days = ACQS if eid == COMBINED else [eid]
    lines = [
        f"# {manifest['title']} マッピング",
        "",
        "[写真付き3Dマップ・航跡・同期映像を開く](viewer.html)",
        "",
        "[Day1](../../20261031_nanyo_000/mapping_replay/viewer.html) / [Day2](../../20261101_nanyo_000/mapping_replay/viewer.html) / [Day1 + Day2](../../20261102_nanyo_combined_000/mapping_replay/viewer.html)",
        "",
        "Day1と同じ写真面・点群・航跡・親子同期映像の形式で再生成した。最初は全区間の俯瞰を表示し、区間を選ぶと時間再生できる。GLB・PLY、PNG保存、表示設定保存も利用できる。",
        "",
        "![マップ俯瞰](mapping_overhead.png)",
        "",
        "[GLB](textured_map.glb) / [PLY](points.ply) / [俯瞰SVG](mapping_overhead.svg)",
        "",
        "## GNSSの採用結果",
        "",
        "ユーザー指定の静止60秒窓で、SPP・通常PPK・Float設定PPK・別基準局PPKを比較した。全候補で有効な同時刻を1対1対応させ、各窓の平均位置からの3D RMSEを算出。窓ごとのRMSE二乗を等重みで平均し、平方根が最小の方式を各日で一つ採用した。時間補間・平滑化・トレンド除去はしていない。",
        "",
        "| 日 | SPP [m] | 通常PPK [m] | Float設定PPK [m] | 別基準局PPK [m] | 採用 |",
        "|---|---:|---:|---:|---:|---|",
    ]
    selections = []
    for acq in days:
        m = json.loads(
            Path(f"results/{acq}/mapping_gnss_selection/manifest.json").read_text(encoding="utf-8")
        )
        selections.append((acq, m))
        v = m["scores_3d_m"]
        day = "Day1" if acq == ACQS[0] else "Day2"
        lines.append(
            f"| {day} | {v['spp']:.4f} | {v['ppk_ar']:.4f} | {v['ppk_float']:.4f} | {v['ppk_alternate']:.4f} | {m['selected_label']} |"
        )
    lines += [
        "",
        "このRMSEは静止時のばらつきであり、測量真値に対する誤差ではない。日をまたぐ系統誤差を保証する値でもない。Day1の14:41:10窓は既存の探索的選定、15:03は指定窓。Day2は09:56・10:15・10:32を選定に用い、手で揺らした10:20は診断用として別扱いにした。全窓60秒。",
        *(
            [
                "[追加比較：PPK Floatを使った子機絶対位置の静止RMSE・全窓俯瞰](../../20261101_nanyo_000/stationary_gnss_comparison/report.html)"
            ]
            if Path("results/20261101_nanyo_000/stationary_gnss_comparison/manifest.json").exists()
            else []
        ),
        "",
        "## 静止地点ごとのGNSS RMSEと深度",
        "",
        "| 静止開始 JST | 共通時刻数 | 平均深度 [m] | SPP 3D [m] | 通常PPK 3D [m] | Float設定 3D [m] | 別基準局 3D [m] | 用途 |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for acq, m in selections:
        for w in m["window_assessments"]:
            spec = w["window"]
            static = json.loads(
                Path(
                    f"results/{spec['experiment_id']}/stationary_spectra/{spec['id']}/manifest.json"
                ).read_text(encoding="utf-8")
            )
            depth = static["metrics"]["pressure_depth"]["mean_m"]
            v = w["metrics"]
            use = "手動揺動・選定除外" if "102000" in spec["id"] else "選定に使用"
            lines.append(
                f"| {spec['start'][:19].replace('T', ' ')} | {w['common_epochs']} | {depth:.2f} | {v['spp']['rmse_3d_m']:.4f} | {v['ppk_ar']['rmse_3d_m']:.4f} | {v['ppk_float']['rmse_3d_m']:.4f} | {v['ppk_alternate']['rmse_3d_m']:.4f} | {use} |"
            )
    lines += [
        "",
        "深度は子機圧力ログの60秒平均。各日の初期180秒中央値を水面基準の近似とし、海水密度1025 kg/m³・重力9.80665 m/s²で換算した。海底水深や測量済み絶対深度ではない。GNSS欠測によって深度の集計範囲を縮めていない。",
        "",
    ]
    for acq, m in selections:
        for w in m["window_assessments"]:
            spec = w["window"]
            rel = f"../../{acq}/mapping_gnss_selection/{spec['id']}_overhead"
            lines += [
                f"### {spec['start'][:19].replace('T', ' ')}",
                "",
                f"![GNSS静止俯瞰]({rel}.png)",
                f"[編集用SVG]({rel}.svg) / [GNSS集計表](../../{acq}/mapping_gnss_selection/comparison.csv) / [子機の補正あり・なし俯瞰と周波数評価](../../{spec['experiment_id']}/stationary_spectra/{spec['id']}/report.html)",
                "",
            ]
    lines += [
        "## 区間別のマップ",
        "",
        "| 区間 | 採用断片 / 全断片 | 点数 | 写真面 | 三角形 | 光源追跡の採用 / 全フレーム | 子機航法の有効率 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for s in manifest["segments"]:
        trial = s["experiment_id"]
        track = json.loads(
            Path(f"results/{trial}/mapping_light_tracking/manifest.json").read_text(
                encoding="utf-8"
            )
        )
        nav = json.loads(
            Path(f"results/{trial}/mapping_navigation/manifest.json").read_text(encoding="utf-8")
        )
        lines.append(
            f"| {trial} | {s['accepted_fragments']} / {s['total_fragments']} | {s['points']:,} | {s['photo_patches']} | {s['triangles']:,} | {track['accepted']:,} / {track['rows']:,} | {100 * nav['available'] / nav['rows']:.1f}% |"
        )
    lines += [
        "",
        "光源は目視で同定した初期位置から、画像の移動と固定テンプレートで追跡し、局所光源重心を採用した。確認済みの静止窓は前回の追跡結果を元のフレームUTCのまま再使用。位置の平滑化で静止RMSEを小さく見せる処理はしていない。追跡失敗は欠測。10 Hz追跡から音響shotごとに最近傍1枚（75 ms以内）を対応させ、測距を複数回の観測として重複利用しない。航法はSfMの位置を入力に用いない。",
        "",
        "静止窓の追跡とは異なり、全区間の追跡は初期化画像と採用フレームの抜粋で確認したもの。発光部が小さい・暗い・遮蔽される区間は採用率が低い。光源の画素真値に対する精度改善率を求めた結果ではない。",
        "",
        "親機は選定GNSS・Doppler速度・Spresense、子機は光源方位・音響・圧力・BNOを融合。親機の慣性補完は最大5秒、子機は最大3秒とし、それ以上は位置を表示しない。Day2子機映像は実測約29.97 fpsのPTSを時計変換した。表示用同期映像は15 fps。",
        "",
        "Day1は前回採用した内部形状・縮尺・向きを維持し、選定GNSSと追跡による航法へ平行移動を再推定した。Day2は各SfM断片を姿勢で回転し、圧力変化で縮尺を推定。圧力で縮尺が決まらない場合は、十分なセンサー位置観測がある正の縮尺推定を暫定使用する。全断片の受入・除外と残差は以下に残した。",
        "",
    ]
    for s in manifest["segments"]:
        lines += [
            f"### {s['experiment_id']}",
            "",
            "| 断片 | 判定 | 位置合わせ残差 中央 / 95% [m] | 理由・縮尺 |",
            "|---|---|---:|---|",
        ]
        for f in s["fragments"]:
            texture = f.get("texture_manifest", {})
            median = f.get("registration_median_m", texture.get("registration_residual_median_m"))
            p95 = f.get("registration_p95_m", texture.get("registration_residual_p95_m"))
            residual = f"{median:.3f} / {p95:.3f}" if median is not None else "—"
            lines.append(
                f"| {f['fragment']} | {'暫定採用' if f['accepted'] else '未採用'} | {residual} | {f.get('scale_method', f.get('reason', ''))} |"
            )
        lines += [
            "",
            f"[来歴・変換・残差JSON](../../{s['experiment_id']}/mapping_selected/manifest.json)",
            "",
        ]
    overlap = json.loads(
        Path(f"results/{COMBINED}/mapping_replay/crossday_overlap/manifest.json").read_text(
            encoding="utf-8"
        )
    )
    lines += [
        "## Day1・Day2の結合",
        "",
        f"両日の元のGNSS緯度・経度・楕円体高を、Day1原点 {manifest['common_origin_llh']} の共通ENUへ剛体変換した。両日の点が合うような任意の移動やICPは加えていない。",
        "",
        f"両日の全再構成モデルから代表画像を選び、Day1 {overlap['day1_images']}枚 × Day2 {overlap['day2_images']}枚 = {overlap['image_pairs']}組を照合した。相互比率検定・エピポーラ整合性・画面上の広がり・3D対応数を満たす接続候補は {overlap['candidate_pairs']}組だった。結合版はGNSSによる重ね合わせであり、写真から連続した単一面が検証されたモデルではない。この代表画像の探索だけで、未確認の重複領域が存在しないとは断定できない。",
        "",
        f"[照合した全画像組](../../{COMBINED}/mapping_replay/crossday_overlap/image_pairs.csv) / [照合条件](../../{COMBINED}/mapping_replay/crossday_overlap/manifest.json)",
        "",
        "写真面は疎な対応点の近傍だけを三角形にした近似で、密な海底形状ではない。網・魚・気泡・船体などの動き、磁気方位、暫定的な水中内部パラメータ、圧力センサーの取付中心近似、日間のGNSSバイアスが残る。位置合わせ残差は同じ航法観測への一致度であり、独立精度ではない。",
        "",
        "## 再現と関連レポート",
        "",
        "[入力ハッシュ・Git SHA・変換来歴](manifest.json) / [検証結果](../../20261102_nanyo_combined_000/mapping_replay/verification.json) / [Day2解析と両日の静止FFT・RMSE](../../20261101_nanyo_000/analysis/report.html)",
    ]
    text = "\n".join(lines) + "\n"
    (out / "report.md").write_text(text, encoding="utf-8")
    render_html(text, out / "report.html")
    page = (
        (out / "report.html")
        .read_text(encoding="utf-8")
        .replace(
            "<title>Nanyo day2 実験解析</title>", f"<title>{manifest['title']} マッピング</title>"
        )
    )
    (out / "report.html").write_text(page, encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--experiment", required=True, choices=[COMBINED])
    parser.parse_args()
    for eid in [*ACQS, COMBINED]:
        report(eid)
