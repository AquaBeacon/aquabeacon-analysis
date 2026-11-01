"""Summarize sensor-only navigation and textured synchronized replay."""

import argparse
import importlib.metadata
import json
from pathlib import Path

from analyze_nanyo import read
from estimate_nanyo_absolute import n

from aquabeacon_analysis.aerial_tracks import enu_to_geodetic
from aquabeacon_analysis.ngt_binary import sha256


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", required=True, choices=["20261031_nanyo_000"])
    acq = parser.parse_args().experiment
    out = Path(f"results/{acq}/navigation_replay")
    fusion = json.loads(
        Path(f"results/{acq}/navigation_fusion/manifest.json").read_text(encoding="utf-8")
    )
    parent = fusion["parent"]
    lines = [
        "# Nanyo：SfM位置を使わない航法と、写真・親子映像付き3D表示",
        "",
        "[操作できる3D表示](viewer.html) / [以前の位置・地図結果](../absolute_position/report.md)",
        "",
        "親機はPPK・GNSS受信機の速度・Spresense IMU、子機は親機の光点方向・音響・深度・BNO085で再推定した。子機航法にSfMの位置・速度を入力していない。RTKは未使用。地図の内部形状と写真投影には従来のSfM再構成を使い、計算済みの航法結果へ地図を配置した。位置推定と地図の依存方向を分離している。",
        "",
        "## 表示",
        "",
        "写真面／点群／航跡を切替可能。右下の親機・子機カメラは同一のUTC時刻スライダーで再生・シークする。親機2本の動画は固有の時計対応で切り替える。地図写真の最近傍時刻と、連続カメラ映像の時刻は画面で区別する。",
        "",
        "疎点群の既存頂点間だけに、短い辺・近い奥行き・画像内の見え方を条件に三角形を張り、歪み補正した画像を投影した。穴・継ぎ目・重なりを含む近似表面で、密な再構成や測量済み形状ではない。GLBへテクスチャも埋め込んだ。動画は640×360・15 fps・音声なしの確認用。時計倍率を反映し、丸めは最大1表示フレーム程度。原動画は変更していない。",
        "",
        "## 親機GNSS＋Spresense",
        "",
        "Spresenseの補正済み加速度を真ENUへ回し、重力を除去。CADのIMUとアンテナの位置差を使い、GNSS位置とDoppler速度をIMU基準へ移した。GNSS速度の観測時刻はiTOWからUTCへ変換。全3437件で3D fix・fix-validを確認した。短い姿勢欠測だけ0.5秒以内で補間した。",
        "",
        f"11点の局所中央値からの孤立した跳びを東・北・上で判定し、各軸 {parent['hampel_rejected_axes']} 件を候補除外した。さらに位置・速度のinnovationを5σで判定する。元行、除外軸、使用可否、残差は[parent_gnss_decisions.csv](../navigation_fusion/parent_gnss_decisions.csv)に残した。",
        "",
        "状態は各軸の位置・速度・加速度bias。PPKの報告誤差をそのまま確定精度にせず、Fix/Float/DGNSS別のσ下限、加速度雑音、bias random walkを明示した。観測は各イベント1回だけ使う。IMU欠測は伝播を切り、水平位置観測が5秒を超えて途切れた状態は有効位置として出力しない。再観測後は新しい推定区間として初期化する。",
        "",
        "![親機比較](../navigation_fusion/parent_comparison.png)",
        "",
        "GNSS位置と速度を同時に隠す欠測試行：",
        "",
        "| 欠測時間 | 通常のフィルターとの差（終端） | 有効な補完として出力 |",
        "|---:|---:|---|",
    ]
    for trial in parent["dropout_trials"]:
        lines.append(
            f"| {trial['duration_s']} s | {trial['endpoint_disagreement_m']:.2f} m | {'はい' if trial['available_at_end'] else 'いいえ'} |"
        )
    lines += [
        "",
        "これは独立真値誤差ではなく、欠測に対する内部感度である。20秒で約17 m離れるため長い慣性補完は採用しない。局所外れ値は抑えられるが、GNSSのゆっくりしたbias・磁気方位・IMU誤差は残る。",
        "",
        "## 子機：光点・音響・深度・BNO085",
        "",
        "BNO085の記録済み絶対四元数を機体FRD→真ENUへ変換し、加速度はfresh_maskのbit1が立った新規サンプルだけを使う。姿勢・画素・光学軸・CAD補正は前段の監査済み変換に従い、絶対方位の精度未検証も保持する。",
        "",
        "光線、音響球、深度面の残差を一緒に最小化して観測位置を作る。不整合な音響候補はフラグを残し、光線＋深度だけの候補は直前5秒以内に音響付き位置がある場合に限ってフィルターへ渡す。音響のみで水平位置を新規生成しない。",
        "",
        "水深の基準は、親機アンテナに対する有効な水面オフセットを光点・音響・深度から区間別に推定したもの。圧力位置は子機IMUのCAD参照点で近似する。水面の絶対高さを一定の測量値として強制せず、融合した親機高さに追随させるため、親機GNSSの低周波の鉛直誤差は子機にも残る。オフセットと最低0.5 mの不確かさ指標を保存した。この指標は独立検証済みの誤差範囲ではない。",
        "",
        "BNOの加速度で観測間を伝播し、深度で鉛直方向を更新。水平観測から3秒超、仮定速度上限3 m/s超、またはIMU欠測の状態は有効位置から外す。長い欠測は埋めない。初期速度ゼロは仮定で、静止検出ではない。",
        "",
        "| 区間 | 20 Hz出力行 | 有効時間 | 水平観測更新 | 慣性補完時間（観測後1秒超） |",
        "|---|---:|---:|---:|---:|",
    ]
    for m in fusion["segments"]:
        lines.append(
            f"| {m['experiment_id'][-3:]} | {m['rows']} | {m['available'] * 0.05:.2f} s | {m['horizontal_position_updates']} | {m['dr_samples'] * 0.05:.2f} s |"
        )
    for m in fusion["segments"]:
        eid = m["experiment_id"]
        rel = f"../../{eid}"
        map_path = Path(f"results/{eid}/navigation_mapping/manifest.json")
        mapping = json.loads(map_path.read_text(encoding="utf-8"))
        rows = read(Path(f"results/{eid}/navigation_fusion/child_navigation.csv"))
        valid = [r for r in rows if r["position_available"] == "True"]
        xyz = n(valid, ["origin_east", "origin_north", "origin_up"])
        llh = enu_to_geodetic(xyz, m["origin_llh"])
        features = [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [p[1], p[0], p[2]]},
                "properties": {
                    "utc_s": float(r["utc_s"]),
                    "sfm_position_used": False,
                    "accuracy_validated": False,
                    "anchor_age_s": float(r["anchor_age_s"]),
                },
            }
            for r, p in zip(valid, llh, strict=True)
        ]
        Path(f"results/{eid}/navigation_fusion/positions.geojson").write_text(
            json.dumps({"type": "FeatureCollection", "features": features}), encoding="utf-8"
        )
        lines += [
            "",
            f"### {eid}",
            "",
            f"[位置CSV]({rel}/navigation_fusion/child_navigation.csv) / [GeoJSON]({rel}/navigation_fusion/positions.geojson) / [元候補と採否]({rel}/navigation_fusion/child_observations.csv)",
            "",
            f"![子機比較]({rel}/navigation_fusion/child_comparison.png)",
            "",
            f"地図は{mapping['registered_images']}画像・{mapping['points']:,}点、{mapping['photo_patches']}写真面・{mapping['triangles']:,}三角形。従来の尺度と回転を保持し、センサー航法のカメラ位置へ平行移動で配置した。位置合わせ残差は中央値{mapping['registration_residual_median_m']:.2f} m、95%点{mapping['registration_residual_p95_m']:.2f} m。これは適合残差で、精度向上の証明ではない。",
            "",
            f"[写真付きGLB]({rel}/navigation_mapping/textured_map.glb) / [点群PLY]({rel}/navigation_mapping/points.ply) / [地図manifest]({rel}/navigation_mapping/manifest.json)",
            "",
        ]
        if eid.endswith("002"):
            lines += ["区間②は気泡・船体付近の小断片で、静的海底地図としては未検証。", ""]
    lines += [
        "## 検証と再現",
        "",
        "既知加速度・人工外れ値・短い欠測・長い欠測の無効化・再捕捉を含む関連12テストに合格。画像UVがカメラ投影と一致し、面の全頂点が既存点群と一致することを検証。ブラウザでは2区間、親機動画の継ぎ目、両映像シーク、再生停止、欠測表示、表示切替を確認した。",
        "",
        "[検証記録](verification.json) / [生成manifest](manifest.json) / [親機航法CSV](../navigation_fusion/parent_navigation.csv) / [パラメータと処理記録](../navigation_fusion/manifest.json)",
        "",
        "出力は推定結果であり、絶対精度は未検証。旧出力・不成立候補・原データを保持。2D比較図はPNG/SVG/pickle/plot.jsonで編集可能。閲覧時はviewer.htmlとmediaフォルダを同じ配置で使用する。再現コマンドはREADME/AGENTS.mdを参照。",
        "",
    ]
    (out / "report.md").write_text("\n".join(lines), encoding="utf-8")
    (Path(f"results/{acq}") / "report.md").write_text(
        "# Nanyo解析\n\n[最新：センサー航法・写真地図・同期映像](navigation_replay/report.md)\n\n[初回絶対位置推定](absolute_position/report.md)\n",
        encoding="utf-8",
    )
    (out / "delivery.json").write_text(
        json.dumps(
            {
                "report_sha256": sha256(out / "report.md"),
                "packages": {
                    p: importlib.metadata.version(p)
                    for p in [
                        "numpy",
                        "scipy",
                        "pycolmap",
                        "opencv-python-headless",
                        "trimesh",
                        "playwright",
                    ]
                },
                "code_sha256": sha256(Path(__file__)),
                "experiment_and_configuration_sha256": {
                    str(p): sha256(p)
                    for p in [
                        *[
                            Path(f"experiments/20261031_nanyo_{s}/experiment.json")
                            for s in ["000", "001", "002"]
                        ],
                        Path(f"config/{acq}/navigation_fusion.json"),
                        Path(f"results/{acq}/navigation_fusion/manifest.json"),
                        out / "verification.json",
                    ]
                },
                "accuracy_validated": False,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print("Navigation report and GeoJSON written", flush=True)


if __name__ == "__main__":
    main()
