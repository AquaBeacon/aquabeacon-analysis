"""Publish editable trajectories and offline 3D viewers from saved NGT estimates."""
from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

os.environ.setdefault("OPENBLAS_NUM_THREADS", "2")
os.environ.setdefault("OMP_NUM_THREADS", "2")
import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from aquabeacon_analysis.editable_plots import save_editable_figure
from aquabeacon_analysis.ngt_binary import sha256
from aquabeacon_analysis.ngt_visualization import contiguous_segments, surface_display

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "scripts/templates/ngt_scene_viewer.html"
COLORS = {"parent": "#147cb2", "child": "#e16b24"}
LABELS = {"002": "Child static", "003": "Range / viewing limit", "004": "Simultaneous movement"}


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def clean(value):
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, np.ndarray)):
        return [clean(v) for v in value]
    if isinstance(value, (float, np.floating)):
        return float(value) if np.isfinite(value) else None
    if isinstance(value, np.integer):
        return int(value)
    return value


def save_json(path, value):
    path.write_text(json.dumps(clean(value), ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def viewer(path, title, data):
    text = TEMPLATE.read_text(encoding="utf-8").replace("__TITLE__", title)
    path.write_text(text.replace("__DATA__", json.dumps(clean(data), ensure_ascii=False,
        separators=(",", ":")).replace("</", "<\\/")), encoding="utf-8")


def provenance(out, paths, extra):
    save_json(out / "manifest.json", {"created_utc": datetime.now(UTC).isoformat(),
        "git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "inputs": [{"path": str(p.relative_to(ROOT)), "sha256": sha256(p)} for p in paths],
        **extra})
    shutil.copy2(__file__, out / "generate.py")
    shutil.copy2(TEMPLATE, out / "viewer_template.html")


def draw_track(ax, xyz, segments, label, color):
    for j, segment in enumerate(segments):
        a = xyz[segment]
        ax.plot(a[:, 0], a[:, 1], color=color, lw=1.4, marker=".", ms=2,
                label=label if j == 0 else None)
    valid = np.flatnonzero(np.isfinite(xyz).all(axis=1))
    if len(valid):
        first, last = xyz[valid[[0, -1]]]
        ax.scatter(*first[:2], marker="o", s=65, color=color, edgecolors="white", zorder=4)
        ax.scatter(*last[:2], marker="X", s=80, color=color, edgecolors="white", zorder=4)


def trajectories(experiment):
    source = ROOT / f"results/{experiment}/position_evaluation/positions_5hz.csv"
    rows = read_csv(source)
    arr = lambda cols: np.array([[float(r[k]) for k in cols] for r in rows])
    t = arr(["parent_video_s"])[:, 0]
    parent_raw = arr(["parent_east_m", "parent_north_m", "parent_up_m"])
    child_raw = arr(["child_east_declination0_m", "child_north_declination0_m", "child_up_approx_m"])
    parent, child, origin = surface_display(parent_raw, child_raw, arr(["relative_down_m"])[:, 0])
    segments = {"parent": contiguous_segments(parent, t), "child": contiguous_segments(child, t)}
    valid = np.flatnonzero(np.isfinite(child).all(axis=1))
    preview = int(valid[np.argmin(abs(t[valid]-(t[0]+(t[-1]-t[0])*.65)))])
    out = ROOT / f"results/{experiment}/position_visualization"
    out.mkdir(parents=True, exist_ok=True)
    data = {"kind": "trajectory", "experiment": experiment, "metric": True,
        "description": "親機は選定済みGNSS、子機は親機GNSS＋カメラ光線×音響距離。青と橙で位置関係を表示します。",
        "notes": "水面基準の表示：親機の高さを0 m、子機を推定相対位置の下向き成分だけ下げています。元のGNSS高度は変更していません。水平は磁気偏角0°・測定中心一致の仮定を含みます。欠測をつなぎません。",
        "times": t, "parent": parent, "child": child, "origin_original_enu_m": origin,
        "parent_segments": segments["parent"], "child_segments": segments["child"],
        "range": arr(["range_m"])[:, 0], "preview_index": preview, "sample_hz": 5,
        "body_geometry": "Identification proxies only; no measured mounting or body orientation implied"}
    save_json(out / "scene_data.json", data)
    viewer(out / "viewer.html", f"長田高校 親機と子機の軌跡 {experiment[-3:]}", data)
    with (out / "display_positions.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["parent_video_s", "elapsed_s", "parent_display_e_m", "parent_display_n_m", "parent_display_up_m",
                         "child_display_e_m", "child_display_n_m", "child_display_up_m"])
        writer.writerows([[time, time-t[0], *p, *c] for time, p, c in zip(t, parent, child, strict=True)])
    combined = np.vstack([parent, child])
    spans_xy = np.nanmax(combined[:, :2], axis=0)-np.nanmin(combined[:, :2], axis=0)
    height = min(6.5, max(2.8, 5*spans_xy[1]/max(spans_xy[0], .1)+1.3))
    fig, axes = plt.subplots(1, 3, figsize=(15, height), layout="constrained", sharex=True, sharey=True)
    for ax, bodies, title in zip(axes, [("parent",), ("child",), ("parent", "child")],
                                 ["Parent GNSS", "Child estimate", "Parent and child"], strict=True):
        for body in bodies:
            draw_track(ax, parent if body == "parent" else child, segments[body], body.title(), COLORS[body])
        ax.set(title=title, xlabel="East from first parent [m]", ylabel="North from first parent [m]")
        ax.set_aspect("equal", adjustable="box")
        ax.grid(alpha=.25)
        ax.legend(fontsize=8)
    fig.suptitle(LABELS[experiment[-3:]]+" | circle: first / X: last | GNSS jumps retained")
    save_editable_figure(fig, out / "horizontal_tracks.png", dpi=150)
    plt.close(fig)
    fig, axes = plt.subplots(3, 1, figsize=(11, 8), layout="constrained", sharex=True)
    for dim, ax in enumerate(axes):
        for body, xyz in [("parent", parent), ("child", child)]:
            ax.plot(t-t[0], xyz[:, dim], color=COLORS[body], lw=1, label=body.title())
        ax.set_ylabel(["East [m]", "North [m]", "Surface display height [m]"][dim])
        ax.grid(alpha=.25)
        ax.legend(loc="best", fontsize=9)
    axes[0].set_title(LABELS[experiment[-3:]]+" | full observation interval")
    axes[-1].set_xlabel("Elapsed time in experiment [s]")
    save_editable_figure(fig, out / "trajectory_time_series.png", dpi=150)
    plt.close(fig)
    fig = plt.figure(figsize=(11, 6.5), layout="constrained")
    ax = fig.add_subplot(projection="3d")
    for body, xyz in [("parent", parent), ("child", child)]:
        for j, segment in enumerate(segments[body]):
            p = xyz[segment]
            ax.plot(*p.T, color=COLORS[body], lw=1.5, label=body.title() if j == 0 else None)
    for index in np.linspace(0, len(valid)-1, 10).astype(int):
        k = valid[index]
        ax.plot(*np.array([parent[k], child[k]]).T, color="#279379", alpha=.6, lw=.8)
    all_points = np.vstack([parent, child])
    spans = np.nanmax(all_points, axis=0)-np.nanmin(all_points, axis=0)
    ax.set_box_aspect(np.maximum(spans, .5))
    ax.set(xlabel="East [m]", ylabel="North [m]", zlabel="Surface display height [m]",
           title="Parent / child relationship | surface display; no vertical exaggeration")
    ax.set_xlabel("East [m]", labelpad=16)
    ax.set_ylabel("North [m]", labelpad=16)
    ax.set_zlabel("Surface display height [m]", labelpad=12)
    ax.legend()
    save_editable_figure(fig, out / "track_3d.png", dpi=150)
    plt.close(fig)
    provenance(out, [source, source.parent / "manifest.json", Path(__file__), TEMPLATE,
        ROOT / "src/aquabeacon_analysis/ngt_visualization.py"], {"experiment_id": experiment,
        "display_frame": "ENU with declination=0; common horizontal translation; parent z=0, child z=-relative_down",
        "original_origin_enu_m": origin, "missing_policy": "all rows retained, no line through invalid cells",
        "metric_changes": "none; visualization only; original position_evaluation outputs unchanged"})
    (out / "report.md").write_text(f"""# 長田高校 親機と子機の軌跡 {experiment[-3:]}

[時刻を動かせる3D表示](viewer.html)で、全体・上・横・親子拡大の表示を切り替えられる。青が親機、橙が子機、緑が同時刻の親子間の線。欠測時は子機と線を表示しない。

![親機と子機の水平軌跡](horizontal_tracks.png)

![3Dの位置関係](track_3d.png)

![全時刻の座標](trajectory_time_series.png)

水平位置は選定済み親機GNSSと、光学音響の相対位置の合成。磁気偏角0°、測定中心一致を仮定する。原点を最初の有効な親機位置へ平行移動した。円が最初、Xが最後の有効位置で、欠測のため親子の始終時刻は同じとは限らない。GNSS採用元切替えの跳びも残している。

3D表示では親機の高さを0 mとし、子機を相対下向き成分だけ下げた。水面との関係を読むための表示であり、元のGNSS高度や評価値を補正したものではない。実際のプール底・壁を推測した面は追加していない。機体モデルは識別用の簡略形状で、実機寸法・姿勢・センサ取付位置を表さない。

5 Hzの全時間表示には、近傍音響イベントと光点が組み合わさった位置候補も含む。音響イベント時刻だけの静止統計とは標本が異なる。004の親動画1283.2秒では下向き成分3.354 m、圧力深度0.875 mとなり、深さに整合しない突発値がある。こうした候補やGNSSの跳びを実際の機体運動と断定せず、元の欠測・異常候補を残した表示として読む。

[Blenderシーン](parent_child.blend)は1フレーム0.2秒。タイムラインで同じ位置を再生できる。欠測区間を補間せず、キーフレーム間はサンプル保持とする。[Blender俯瞰](blender_overview.png) / [親子拡大](blender_closeup.png)。

各グラフにSVG・figure.pickle・plot.jsonを併置した。共通エディタでラベル、凡例、軸範囲、寸法、色・線・点を編集し、設定を保存・再読込、PNG/SVG/PDF出力できる。例：

```powershell
python -m aquabeacon_analysis.editable_plots results/{experiment}/position_visualization/horizontal_tracks.figure.pickle
```

[表示座標CSV](display_positions.csv) / [時刻と全表示データ](scene_data.json) / [入力と設定](manifest.json) / [位置推定の評価](../position_evaluation/report.md)
""", encoding="utf-8")
    return out


def wall_maps():
    experiment = "20261028_ngt_004"
    work = ROOT / f"data/interim/{experiment}/wall_mapping/five_hz"
    source = ROOT / f"results/{experiment}/wall_mapping"
    out = ROOT / f"results/{experiment}/map_visualization"
    out.mkdir(parents=True, exist_ok=True)
    maps, inputs = [], [work / "frame_priors.csv", source / "five_hz/manifest.json"]
    priors = {r["image_name"]: r for r in read_csv(work / "frame_priors.csv")}
    for mode in ("aquabeacon_prior", "visual_only"):
        metric_path = source / "five_hz" / f"{mode}_metrics.json"
        inputs.append(metric_path)
        for model in json.loads(metric_path.read_text())["models"]:
            folder = work / mode / str(model["model"])
            pts_path = folder / "points3D.txt"
            points, colors = [], []
            for line in pts_path.read_text().splitlines():
                if line.startswith("#") or not line.strip():
                    continue
                words = line.split()
                points.append([float(x) for x in words[1:4]])
                colors.append("#"+"".join(f"{int(x):02x}" for x in words[4:7]))
            cams = read_csv(folder / "camera_centers.csv")
            cameras = np.array([[float(c[k]) for k in ("x", "y", "z")] for c in cams])
            times = np.array([float(c["parent_video_s"]) for c in cams])
            prior_values = np.array([[float(priors[c["image_name"]][k]) for k in
                ("prior_east_m", "prior_north_m", "prior_up_pressure_m")] for c in cams])
            use_prior = mode == "aquabeacon_prior"
            usable = np.isfinite(prior_values).all(axis=1)
            residual = np.linalg.norm(cameras[usable]-prior_values[usable], axis=1) if use_prior else []
            representative = cams[len(cams)//2]["image_name"]
            image = work / "source_frames" / representative
            inputs.extend([pts_path, folder / "camera_centers.csv", image])
            label = ("位置拘束あり" if use_prior else "画像のみ")+f" / 断片 {model['model']}"
            if not model["nondegenerate_structure"]:
                label += "（退化診断）"
            caption = (f"{label}\n登録画像 {len(cams)} 枚 / 点 {len(points):,} 個\n"
                f"親動画 {times[0]:.1f}〜{times[-1]:.1f} 秒\n"
                f"再投影平均 {model['mean_reprojection_px']:.3f} px\n"
                +(f"有効な入力位置 {int(usable.sum())} 点\n位置拘束との差 RMS {np.sqrt(np.mean(residual**2)):.3f} m（真値誤差ではない）"
                  if use_prior and len(residual) else "単眼尺度は未確定"))
            maps.append({"label": label, "mode": mode, "model": model["model"], "metric": use_prior,
                "valid_structure": model["nondegenerate_structure"], "points": points, "colors": colors,
                "cameras": cameras, "camera_segments": contiguous_segments(cameras, times, .5),
                "priors": prior_values[usable] if use_prior else [], "caption": caption,
                "image": "data:image/jpeg;base64,"+base64.b64encode(image.read_bytes()).decode(),
                "image_caption": f"登録された元画像 {representative}。右側が壁。",
                "source_pointcloud": str((folder / "points.ply").relative_to(ROOT)),
                "prior_residual_rms_m": float(np.sqrt(np.mean(residual**2))) if len(residual) else None})
    valid_maps = [m for m in maps if m["mode"] == "aquabeacon_prior" and m["valid_structure"]]
    palette = ["#1678aa", "#de713d", "#3c9a72", "#9271ac"]
    combined = {"label": "位置拘束あり / 4断片の配置", "mode": "aquabeacon_prior", "model": "combined",
        "metric": True, "valid_structure": True, "points": [], "colors": [], "cameras": [],
        "camera_segments": [], "priors": [], "caption": "位置拘束の共通座標に4断片を表示。\n色は断片の識別用。追加の位置合わせや隙間の補完はしていません。\n点群のない場所は復元できていない領域です。", "image": ""}
    for m, color in zip(valid_maps, palette, strict=True):
        offset = len(combined["cameras"])
        combined["points"].extend(m["points"])
        combined["colors"].extend([color]*len(m["points"]))
        combined["cameras"].extend(m["cameras"])
        combined["camera_segments"].extend([[i+offset for i in s] for s in m["camera_segments"]])
        combined["priors"].extend(m["priors"])
    maps.insert(0, combined)
    data = {"kind": "maps", "experiment": experiment, "maps": maps,
        "description": "子機画像から得た壁の疎点群。位置拘束を使った4断片の配置と、各断片の拡大・画像のみの比較を選べます。",
        "notes": "位置拘束は親機GNSS＋光学音響の水平位置と、子機の圧力深度。画像特徴で再構成し、この位置へ近づくよう最適化する弱い拘束です。姿勢・位置を完全固定した再構成ではありません。点のない隙間を面で埋めていません。画像のみの各地図は独立した任意尺度です。"}
    save_json(out / "map_data.json", data)
    viewer(out / "wall_viewer.html", "長田高校 プール壁の再構成地図", data)
    fig = plt.figure(figsize=(11, 7), layout="constrained")
    ax = fig.add_subplot(projection="3d")
    for m, color in zip(valid_maps, palette, strict=True):
        p = np.array(m["points"])
        ax.scatter(*p.T, s=1.4, color=color, label=f"Fragment {m['model']}", alpha=.7)
        for seg in m["camera_segments"]:
            ax.plot(*m["cameras"][seg].T, color=color, lw=1.5)
    ax.set(xlabel="East scenario [m]", ylabel="North scenario [m]", zlabel="Up, pressure prior [m]",
           title="Reconstructed wall fragments in position-prior coordinates")
    ax.set_xlabel("East scenario [m]", labelpad=16)
    ax.set_ylabel("North scenario [m]", labelpad=16)
    ax.set_zlabel("Up, pressure prior [m]", labelpad=12)
    p = np.array(combined["points"])
    ax.set_box_aspect(np.maximum(np.ptp(p, axis=0), .3))
    ax.legend()
    save_editable_figure(fig, out / "wall_fragments_3d.png", dpi=160)
    plt.close(fig)
    provenance(out, [*inputs, Path(__file__), TEMPLATE], {"experiment_id": experiment,
        "processing": "saved point clouds only; no reconstruction, alignment, filtering or invented surface",
        "prior_total_frames": 436, "wall_input_frames": 625,
        "display": "combined=unchanged prior coordinates; separate visual maps retain independent scales"})
    (out / "report.md").write_text("""# 長田高校 プール壁の再構成地図

[回転・拡大できる地図ビューア](wall_viewer.html)で、4断片の配置、断片ごとの拡大、画像のみの比較を選択できる。点群に加え、青い再構成カメラ位置と橙の入力位置拘束を表示する。元画像も併置した。

![再構成した壁断片](wall_fragments_3d.png)

今回のマッピングは、求めた位置を再構成の位置拘束として使っている。水平位置は親機GNSS＋カメラ光線×音響距離、鉛直は子機圧力深度。625画像中436画像に有限の位置拘束がある。画像特徴の対応からカメラと3D点を求め、位置拘束を加えたバンドル調整で最適化した。位置を完全に固定したり、推定軌跡だけから壁を作ったりする処理ではない。

同じ画像・対応点で位置拘束なしも処理した。現在得られた地図は4つの疎な壁断片で、連続した25 mの壁面メッシュではない。共通位置拘束の座標にそのまま置き、追加の位置合わせや面の穴埋めをしていない。実測壁寸法はなく、位置拘束との差も独立精度ではない。

[Blenderの壁地図](wall_map.blend)には4断片の全体表示と個別拡大シーンを保存。[全体プレビュー](blender_wall_overview.png) / [断片0の拡大](blender_wall_fragment0.png)。表示用の点は見える大きさを持たせた記号で、表面の厚さを意味しない。

[元のマッピング評価](../wall_mapping/report.md) / [点群とカメラと入力位置の表示データ](map_data.json) / [入力と来歴](manifest.json)。元COLMAPモデル・PLYのパスはmap_data.json内のsource_pointcloud。

静的グラフはSVG・figure.pickle・plot.jsonも保存し、共通ブラウザエディタで編集できる。HTMLの表示設定はJSON保存・再読込でき、地図の回転・拡大・点サイズを変えられる。
""", encoding="utf-8")
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", action="append", required=True,
                        choices=["20261028_ngt_"+x for x in LABELS])
    args = parser.parse_args()
    print(json.dumps({"pid": os.getpid(), "started_utc": datetime.now(UTC).isoformat(),
        "purpose": __doc__}), flush=True)
    for experiment in dict.fromkeys(args.experiment):
        print(trajectories(experiment), flush=True)
        if experiment.endswith("004"):
            print(wall_maps(), flush=True)
    index = ROOT / "results/20261028_ngt_000/position_visualization"
    index.mkdir(parents=True, exist_ok=True)
    text = """# 長田高校 親機と子機の軌跡と再構成地図

親機・子機の個別軌跡、重ね合わせ、座標の時系列、3D表示とBlender再生を追加した。全時刻の欠測と突発的な位置候補を保持している。

| 区間 | 軌跡と説明 | 時刻を動かせる3D | Blender |
|---|---|---|---|
"""
    for suffix, label in [("002", "子機静止"), ("003", "測位限界"), ("004", "親子同時移動")]:
        stem = f"../../20261028_ngt_{suffix}/position_visualization/"
        text += f"| {suffix} {label} | [軌跡]({stem}report.md) | [3D表示]({stem}viewer.html) | [シーン]({stem}parent_child.blend) |\n"
    text += """

![親機と子機の軌跡](../../20261028_ngt_004/position_visualization/horizontal_tracks.png)

![Blenderの位置関係](../../20261028_ngt_004/position_visualization/blender_closeup.png)

青が親機、橙が子機、緑が親子を結ぶ観測線。親機高さ0 m、子機高さは負の相対下向き成分という水面基準の表示である。模型は識別用で、実機形状・取付け・姿勢の再現ではない。

## 壁マッピング

[回転できる再構成地図](../../20261028_ngt_004/map_visualization/wall_viewer.html) / [説明と画像](../../20261028_ngt_004/map_visualization/report.md) / [Blender地図](../../20261028_ngt_004/map_visualization/wall_map.blend)

マッピングは、求めた水平位置と圧力深度を位置拘束に入れて画像特徴から再構成する。位置を完全固定しているわけではなく、画像の整合性と位置拘束の双方を使う。現在の地図は4つの疎な壁断片であり、隙間を補った25 mの壁面メッシュではない。

![壁断片の配置](../../20261028_ngt_004/map_visualization/wall_fragments_3d.png)

静的図はブラウザ編集用マスターを併置。HTMLは単独で開けるが、内蔵ブラウザのfileプロトコル制限により今回の実画面操作は未確認。データと操作ロジックをNodeの模擬DOMで検証し、静的図とBlenderレンダーは目視確認した。[検証記録](verification.json)
"""
    (index / "report.md").write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
