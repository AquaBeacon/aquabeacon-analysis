"""Write the absolute-position and georeferenced-map report and offline 3D viewer."""

import argparse
import json
import subprocess
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import plotly.graph_objects as go
import pycolmap as pc
from analyze_nanyo import read
from estimate_nanyo_absolute import n

from aquabeacon_analysis.editable_plots import save_editable_figure
from aquabeacon_analysis.ngt_binary import sha256


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", required=True, choices=["20261031_nanyo_000"])
    args = parser.parse_args()
    acq = args.experiment
    out = Path(f"results/{acq}/absolute_position")
    cfg = json.loads(Path(f"config/{acq}/absolute_position.json").read_text())
    prep = json.loads((out / "preparation.json").read_text())
    figure = go.Figure()
    lines = [
        "# Nanyo海上実験：子機の絶対位置推定と位置拘束マッピング",
        "",
        "親機PPK・親機磁気姿勢・親機映像の光点・音響距離・子機の記録済み絶対姿勢・CAD基準点を使い、子機の暫定絶対位置を算出した。RTKは未使用。推定可能性と精度検証を分け、全推定のaccuracy_validatedはfalseとする。",
        "",
        "[回転・拡大できる3D表示](viewer.html) / [以前のGNSS・音響レポート](../analysis/report.md)",
        "",
        "## 位置推定",
        "",
        "| 区間 | 元サンプル | 位置算出 | 急変候補（3 m/s超） | CAD補正量中央値 |",
        "|---|---:|---:|---:|---:|",
    ]
    results = []
    all_input_paths = []
    for suffix in ["001", "002"]:
        eid = f"20261031_nanyo_{suffix}"
        folder = Path(f"results/{eid}")
        p = folder / "absolute_position/positions.csv"
        rows = read(p)
        manifest = json.loads((p.parent / "manifest.json").read_text())
        mapping = json.loads((folder / "position_mapping/manifest.json").read_text())
        xyz = n(rows, ["child_origin_east_m", "child_origin_north_m", "child_origin_up_m"])
        valid = np.isfinite(xyz).all(1)
        assert int(valid.sum()) == manifest["available"]
        assert all(r["accuracy_validated"] == "False" for r in rows)
        assert np.nanmax(abs(n(rows, ["acoustic_equation_residual_m"]))) < 1e-8
        lines.append(
            f"| {suffix} | {len(rows)} | {valid.sum()} | {manifest['motion_outlier_candidates']} | {manifest['cad_correction_median_m']:.3f} m |"
        )
        times = n(rows, ["segment_time_s"])[:, 0]
        figure.add_trace(
            go.Scatter3d(
                x=xyz[:, 0],
                y=xyz[:, 1],
                z=xyz[:, 2],
                mode="markers",
                marker={"size": 2, "color": times, "colorscale": "Viridis"},
                name=f"{suffix} 子機の観測位置",
                meta={"segment": suffix, "kind": "observation"},
                text=[f"{v:.1f} s" for v in times],
                hovertemplate="%{text}<br>E=%{x:.2f} N=%{y:.2f} U=%{z:.2f}<extra>%{fullData.name}</extra>",
            )
        )
        parent = n(rows, ["parent_antenna_east_m", "parent_antenna_north_m", "parent_antenna_up_m"])
        figure.add_trace(
            go.Scatter3d(
                x=parent[:, 0],
                y=parent[:, 1],
                z=parent[:, 2],
                mode="lines",
                line={"width": 2, "color": "gray"},
                name=f"{suffix} 親機PPK",
                meta={"segment": suffix, "kind": "parent"},
            )
        )
        accepted = [m for m in mapping["models"] if m["accepted"]]
        primary = max(accepted, key=lambda m: m["registered_images"]) if accepted else None
        for m in mapping["models"]:
            if not m["accepted"]:
                continue
            label = m["label"]
            model = pc.Reconstruction(f"data/processed/{eid}/position_mapping/{label}")
            assert model.num_reg_images() == m["registered_images"]
            assert m["scale_m_per_unit"] > 0
            if m.get("alignment_parent"):
                parent_model = pc.Reconstruction(
                    f"data/processed/{eid}/position_mapping/{m['alignment_parent']}"
                )
                for image_id, im in parent_model.images.items():
                    assert np.allclose(
                        im.projection_center(),
                        model.image(image_id).projection_center(),
                        atol=1e-7,
                        rtol=0,
                    )
                m["parent_world_camera_positions_preserved"] = True
            points = np.array([p.xyz for p in model.points3D.values()])
            colors = [
                "rgb(" + ",".join(str(int(v)) for v in p.color) + ")"
                for p in model.points3D.values()
            ]
            path = read(folder / f"position_mapping/{label}_camera_positions.csv")
            track = n(path, ["east_m", "north_m", "up_m"])
            for r in path:
                image = next(im for im in model.images.values() if im.name == r["image"])
                assert np.allclose(
                    image.projection_center(),
                    [float(r[k]) for k in ["east_m", "north_m", "up_m"]],
                    atol=1e-7,
                )
            if m is primary:
                figure.add_trace(
                    go.Scatter3d(
                        x=points[:, 0],
                        y=points[:, 1],
                        z=points[:, 2],
                        mode="markers",
                        marker={"size": 1, "color": colors},
                        name=f"{suffix} 位置拘束点群（暫定）",
                        meta={"segment": suffix, "kind": "map"},
                    )
                )
                figure.add_trace(
                    go.Scatter3d(
                        x=track[:, 0],
                        y=track[:, 1],
                        z=track[:, 2],
                        mode="lines",
                        line={"color": "#dd6c28", "width": 4},
                        name=f"{suffix} 地図からのカメラ位置",
                        meta={"segment": suffix, "kind": "map"},
                    )
                )
            names = [row["image"] for row in sorted(path, key=lambda row: float(row["utc_s"]))]
            chosen = [names[0], names[len(names) // 2], names[-1]]
            image_root = Path(
                f"data/interim/{eid}/"
                + (
                    "position_mapping"
                    if label.startswith(("bridge", "extended"))
                    else "visual_mapping"
                )
                + "/images"
            )
            fig, axes = plt.subplots(1, 3, figsize=(12, 3))
            for ax, name in zip(axes, chosen, strict=True):
                ax.imshow(plt.imread(image_root / name))
                ax.set_title(name)
                ax.axis("off")
            fig.tight_layout()
            save_editable_figure(fig, folder / f"position_mapping/{label}_views.png", dpi=130)
            plt.close(fig)
        primary_names = (
            {
                im.name
                for im in pc.Reconstruction(
                    f"data/processed/{eid}/position_mapping/{primary['label']}"
                ).images.values()
            }
            if primary
            else set()
        )
        for m in mapping["models"]:
            names = {im.name for im in pc.Reconstruction(m["source_model"]).images.values()}
            m["images_covered_by_primary"] = len(names & primary_names)
        results.append(
            {
                "experiment_id": eid,
                "positions": manifest,
                "mapping": mapping,
                "primary_map": primary["label"] if primary else None,
            }
        )
        all_input_paths.extend(
            [p, p.parent / "manifest.json", folder / "position_mapping/manifest.json"]
        )
    origin = results[0]["positions"]["origin_llh"]
    lines += [
        "",
        f"共通原点（緯度・経度・楕円体高）は {origin}。右手系の真東・真北・上（ENU、m）。CSVには子機機体原点、子機カメラ、親機GNSSアンテナ、緯度・経度・楕円体高を保存した。GeoJSONの順は経度・緯度・楕円体高。",
        "",
        "## 姿勢・光学軸・取付け",
        "",
        f"子機kind16をCRC検証付きで{prep['child_rows']:,}件展開。SH2 Rotation Vectorはセンサー軸→磁北ENU、wxyz。品質内訳は{prep['status_counts']}で、低品質を削除せず保存した。四元数を適用した比力中央値は{np.round(prep['gravity_world_median'], 4).tolist()} m/s²、磁場中央値は{np.round(prep['magnetic_world_median'], 3).tolist()} µT。逆四元数では重力が整合しないことも確認した。",
        "",
        f"磁気偏角はWMM2025、2026-10-31、32.9645°N・132.5055°E・高さ30 mで {cfg['declination_deg']:.6f}°（東偏正、約7.82°西偏）。磁北ENU→真北ENUはRz(-D)、磁北NED→真北NEDはRz(D)。親子両方に適用した。局所的な船体の磁気擾乱はこの補正で消えない。",
        "",
        "親機は保存済みSpresense→FRD軸校正と別磁気取付設定を使用し、今回のDJI姿勢で傾き補正。今回の磁気はcalibration_generation=0で補正値と生値が同じなので、新たなbiasを仮定していない。invalid/overrunと欠測は保持し、数値不能時のみ姿勢を欠損にする。",
        "",
        "ユーザー確認によりIMU・カメラの固定方向は校正時から不変。保存済み子機/親機の機体軸を利用した。保存済み子機カメラ校正は校正区間限定のため本番へ直接適用せず、今回の同期窓のジャイロ回転対応からDJI内部IMU→機体を求めた。",
        "",
        f"画像はネイティブ1280×720、自動回転OFF。前回の90°補正を適用しない。今回の光学右/下/前→DJI内部IMUは {cfg['R_dji_from_child_optical']}。子機169組中147組で相対回転残差35%未満、親機は3組中2組の限定的な裏付け。光学条件は共通水中内部校正の暫定性を保持する。",
        "",
        "CADはcad_20261028の4構成のスナップショットを参照。親機のカメラ・TD04A取付基準・アンテナ、子機のカメラ・ライト・TD04A・IMUの参照点を機体FRDに変換した。子機カメラとライトは実機記録に従って位置を交換。ケース原点・取付穴・ライトホルダの位置を測定中心の近似として使うため、実測位相中心の校正済み値ではない。設定JSONに全ベクトルと5 cmの感度条件を記録した。",
        "",
        "光点への光線と音響距離球を、親子の姿勢で回転したレバーアーム込みで交差させる。子機ライト位置を求め、子機姿勢で機体原点・カメラ位置へ変換する。音響は今回のprofile0・1500 m/s・補正0 µsを保持。最寄り全shotを選んでから成立候補を判定し、欠測を過去の成功値で埋めない。",
        "",
        "親機PPKの鉛直成分には大きな変動があり、位置推定にも伝わる。急変候補を保持し、磁気・光点同定・音響誤検出・CAD基準点の不確かさを精度検証済みとは扱わない。5°の方位変化、音速±50 m/s、取付点5 cmの感度をCSVに併記した。",
        "",
        "## 位置を使ったマッピング",
        "",
        "画像の元フレーム時刻を、以前と同じFFmpeg fpsフィルターの入力/破棄/出力ログから復元した。旧公称1秒ビンではなく、選択された入力PTSで姿勢・位置と対応させる。時刻同期の内部残差は別の同期manifestを参照。",
        "",
        "地図の向きは子機絶対姿勢との回転対応、尺度は水深変化とCAD基準点補正、平行移動は観測済み子機位置と音響距離で推定した。水深から尺度を決められない場合も、位置だけの重み付き相似変換が正尺度なら暫定配置し、水深との不整合を記録する。位置残差の仮定重みは水平1 m・鉛直5 m、音響残差5 m、外れ値に強い損失を使用。これらは真値に基づく標準偏差ではない。負尺度を無理に正へ変換しない。",
        "",
        "画像が途切れた箇所に5 Hzの画像を追加し、既存モデルから再構成を延長した。相似変換後も再投影誤差が変わらないことと、CSV・COLMAPモデルのカメラ位置一致を検証した。",
        "",
        "位置・音響の各観測に最も近い画像を1枚だけ対応付け、追加画像によって同じ観測が繰り返し重く扱われないようにした。区間①の拡張処理は設定した実行時間上限で停止してモデルを保存。バンドル調整中の線形解法警告も実行ログに残るため、最適化の完全収束は保証しない。",
        "",
        "続いて既存カメラ姿勢を固定して末端を延長した。extended_0はbridge_0と同一の相似変換を継承し、既存部分の世界座標を動かさず追加画像を同じ座標系へ配置する。元断片の単独配置が不成立でも、画像のつながりを通じた配置は区別して保存する。",
        "",
    ]
    for result in results:
        eid = result["experiment_id"]
        rel = f"../../{eid}"
        lines += [
            f"### {eid}",
            "",
            f"[絶対位置CSV]({rel}/absolute_position/positions.csv) / [GeoJSON]({rel}/absolute_position/positions.geojson) / [地図判定manifest]({rel}/position_mapping/manifest.json)",
            "",
            f"![子機位置]({rel}/absolute_position/overhead.png)",
            "",
            f"![時系列]({rel}/absolute_position/trajectory_components.png)",
            "",
            "| 断片 | 画像 | 直接位置拘束 | 単独配置 | 主表示地図に含まれる画像 |",
            "|---|---:|---:|---|---:|",
        ]
        for m in result["mapping"]["models"]:
            lines.append(
                f"| {m['label']} | {m['registered_images']} | {m['direct_position_anchors']} | {'暫定ENUへ配置' if m['accepted'] else '未配置：尺度または位置の拘束不足'} | {m['images_covered_by_primary']} |"
            )
        primary = next(
            (m for m in result["mapping"]["models"] if m["label"] == result["primary_map"]), None
        )
        if primary:
            name = primary["label"]
            lines += [
                "",
                f"主表示は{name}。{primary['points']:,}点、尺度 {primary['scale_m_per_unit']:.4f} m/元単位。尺度推定方法：{'水深変化' if primary['depth_scale_supported'] else '位置のみ（水深との整合未確認）'}。水深差の残差RMS {primary['pressure_fit_rms_m']:.3f} m、位置拘束残差中央値 {primary['position_prior_residual_median_m']:.2f} m、95%点 {primary['position_prior_residual_p95_m']:.2f} m。いずれも適合残差で、独立した精度評価ではない。",
                "",
                f"残差を分けると、水平は中央値 {primary['position_prior_horizontal_median_m']:.2f} m・95%点 {primary['position_prior_horizontal_p95_m']:.2f} m、鉛直絶対値は中央値 {primary['position_prior_vertical_abs_median_m']:.2f} m・95%点 {primary['position_prior_vertical_abs_p95_m']:.2f} m。位置推定の不確かさと画像地図の変形を含むため、地図全域の位置精度としては扱えない。",
                "",
                f"![共通座標の点群]({rel}/position_mapping/{name}.png)",
                "",
                f"![元画像]({rel}/position_mapping/{name}_views.png)",
                "",
                f"![位置との残差]({rel}/position_mapping/{name}_anchor_residuals.png)",
                "",
                f"[PLY点群]({rel}/position_mapping/{name}.ply) / [地図から得たカメラ位置CSV]({rel}/position_mapping/{name}_camera_positions.csv)",
                "",
            ]
        if eid.endswith("002"):
            lines += [
                "区間②の映像は船体付近・気泡を多く含む。幾何学的な配置が得られても、静的な海底地図としての成立とは区別する。",
                "",
            ]
        (Path(f"results/{eid}") / "report.md").write_text(
            f"# {eid}\n\n[子機絶対位置・位置拘束マッピング](../{acq}/absolute_position/report.md)\n\n[GNSS・音響の初回解析](../{acq}/analysis/report.md)\n",
            encoding="utf-8",
        )
    lines += [
        "## 検証・再現",
        "",
        "原本は変更しない。出力とコード・設定・校正・入力のSHA-256、Git SHA、欠測・品質値・未配置断片を各manifestに保持。`data/processed/<id>/absolute_position/attitudes_10hz.csv`に親子FRD→真北ENUのwxyz姿勢を保存。地図からの位置は観測位置CSVと分けた。再実行コマンドはREADME/AGENTS.md。",
        "",
        "[幾何・時刻の検証記録](geometry_verification.json)：NOAA公式100ケースとの偏角差は最大0.005°未満。算出した全位置で光線とレバーアーム補正後の音響距離を再確認し、各区間の最初の2枚は動画から同じ画像を再生成できた。これらは実装・内部整合の検証で、位置の実測精度を保証しない。",
        "",
        "[NOAA WMM2025係数](https://www.ncei.noaa.gov/products/world-magnetic-model/wmm-coefficients) / [CEVA BNO08Xデータシート](https://www.ceva-ip.com/wp-content/uploads/2019/10/BNO080_085-Datasheet.pdf)。",
        "",
    ]
    (out / "report.md").write_text("\n".join(lines), encoding="utf-8")
    figure.update_layout(
        title="Nanyo：子機位置と位置拘束点群（暫定・真北ENU）",
        scene={
            "xaxis_title": "East [m]",
            "yaxis_title": "North [m]",
            "zaxis_title": "Up [m]",
            "aspectmode": "data",
        },
        legend={"itemsizing": "constant"},
        margin={"l": 0, "r": 0, "b": 35, "t": 60},
    )
    buttons = []
    for label, suffix, map_only in [
        ("全体", None, False),
        ("区間①", "001", False),
        ("区間②", "002", False),
        ("区間① 地図", "001", True),
        ("区間② 地図", "002", True),
    ]:
        visible = [
            (suffix is None or t.meta["segment"] == suffix)
            and (not map_only or t.meta["kind"] == "map")
            for t in figure.data
        ]
        layout = {f"scene.{axis}axis.autorange": True for axis in "xyz"}
        buttons.append({"label": label, "method": "update", "args": [{"visible": visible}, layout]})
    figure.update_layout(updatemenus=[{"buttons": buttons, "x": 0.01, "y": 1.01}])
    figure.write_html(out / "viewer.html", include_plotlyjs=True, full_html=True)
    viewer_path = out / "viewer.html"
    viewer_path.write_text(
        viewer_path.read_text(encoding="utf-8").replace(
            "<head>", "<head><style>html,body{height:100%;margin:0}</style>", 1
        ),
        encoding="utf-8",
    )
    (out / "verification.json").write_text(
        json.dumps(
            {
                "status": "passed",
                "segments": results,
                "git_commit": subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], text=True
                ).strip(),
                "code_sha256": {str(p): sha256(p) for p in Path("scripts").glob("*nanyo*.py")},
                "reviewed_inputs_sha256": {str(p): sha256(p) for p in all_input_paths},
            },
            indent=2,
        )
    )
    print("Absolute report and standalone viewer written", flush=True)


if __name__ == "__main__":
    main()
