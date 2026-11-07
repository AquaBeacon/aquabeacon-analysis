"""Inspect new local models and test provisional placement using saved navigation."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from PIL import Image, ImageDraw
from refine_nanyo_mapping import dump, read_csv, write_csv
from scipy.optimize import least_squares
from scipy.spatial.transform import Rotation

from aquabeacon_analysis.editable_plots import save_editable_figure
from aquabeacon_analysis.experiments import load_experiment
from aquabeacon_analysis.field_review import valid_bracket_support
from aquabeacon_analysis.nanyo_position import robust_rotation_mean
from aquabeacon_analysis.ngt_binary import sha256
from aquabeacon_analysis.ngt_sync import sample_with_gaps
from aquabeacon_analysis.parent_magnetic_attitude import sample_rotations


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--experiment", required=True,
                   choices=["20261101_nanyo_004", "20261101_nanyo_005"])
    p.add_argument("--run", required=True)
    p.add_argument("--runtime", required=True, type=Path)
    a = p.parse_args()
    if not a.run.replace("_", "").isalnum():
        p.error("run must be alphanumeric with underscores")
    root = Path(__file__).resolve().parents[1]
    exp = load_experiment(root, a.experiment)
    sys.path.insert(0, str(a.runtime.resolve()))
    import pycolmap as pc
    out = exp.stage_path("results") / "mapping_refinement" / a.run
    work = exp.stage_path("interim") / "mapping_refinement" / a.run
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    timing = {r["image"]: r for r in read_csv(out / "frames.csv")}
    nav_path = root / f"results/{a.experiment}/mapping_navigation/child_navigation.csv"
    nav = read_csv(nav_path)
    times = np.array([float(r["utc_s"]) for r in nav])
    pos = np.array([[float(r[k]) for k in ("camera_east", "camera_north", "camera_up")] for r in nav])
    valid = np.array([r["position_available"] == "True" and float(r["anchor_age_s"]) <= 1 for r in nav])
    pos[~valid] = np.nan
    quats = np.array([[float(r[k]) for k in ("qw", "qx", "qy", "qz")] for r in nav])
    cfg_path = root / "config/20261101_nanyo_000/absolute_position.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    axes_path = root / cfg["child_axes"]
    axes = np.array(json.loads(axes_path.read_text(encoding="utf-8"))["R_body_from_sensor"])
    clock_path = root / f"data/processed/{a.experiment}/videos/child.clock.json"
    clock = json.loads(clock_path.read_text(encoding="utf-8"))
    session = clock["pieces"][0]["camera_key"]
    mount = axes @ np.array(cfg["mounts"][session]["R_dji_from_sensor"]).T @ np.array(cfg["R_dji_from_child_optical"])
    checks, viewer_models = [], []
    for source in manifest["models"]:
        mid = source["model"]
        model = pc.Reconstruction(work / f"models/{mid}")
        images = sorted(model.images.values(), key=lambda im: float(timing[im.name]["utc_s"]))
        t = np.array([float(timing[im.name]["utc_s"]) for im in images])
        q = sample_rotations(times, quats, t, 0.15)
        qok = np.isfinite(q).all(1)
        orientations = Rotation.from_quat(q[qok][:, [1, 2, 3, 0]]).as_matrix()
        candidates = orientations @ mount @ np.array([im.cam_from_world().rotation.matrix() for im in images])[qok]
        rotation, errors, _ = robust_rotation_mean(candidates, 15)
        centers = np.array([im.projection_center() for im in images])
        x = centers @ rotation.T
        target = sample_with_gaps(times, pos, t, 0.15)
        supported = valid_bracket_support(times, valid, t) & np.isfinite(target).all(1)
        result = {"model": mid, "images": len(images), "points": model.num_points3D(),
                  "orientation_median_deg": float(np.median(errors)),
                  "orientation_p95_deg": float(np.percentile(errors, 95)),
                  "navigation_brackets": int(supported.sum()),
                  "metric_accepted": False, "accuracy_validated": False,
                  "static_scene_validated": False}
        reason = "insufficient recent navigation anchors"
        if supported.sum() >= 6:
            xx, yy = x[supported], target[supported]
            span = float(np.linalg.norm(np.ptp(yy, axis=0)))
            result["navigation_span_m"] = span
            fit = least_squares(lambda v, xx=xx, yy=yy: ((v[0]*xx+v[1:]-yy)/[1, 1, 3]).ravel(),
                                np.r_[1., np.median(yy-xx, axis=0)], loss="soft_l1")
            result["diagnostic_scale"] = float(fit.x[0])
            result["diagnostic_fit_rms_m"] = float(np.sqrt(np.mean(np.sum((fit.x[0]*xx+fit.x[1:]-yy)**2, axis=1))))
            # A short, previously-selected fragment cannot independently validate a metric map.
            reason = "local geometry improved; independent scale support and static-scene validation remain open"
            if span < .25:
                reason = "navigation span below existing 0.25 m scale-excitation gate"
            if fit.x[0] <= 0:
                reason = "non-positive sensor-position scale; no reflection or forced positive scale"
        if result["orientation_median_deg"] > 25 or result["orientation_p95_deg"] > 60:
            reason = "visual and inertial orientations inconsistent with existing gates"
        result["reason"] = reason
        points = np.array([p.xyz for p in model.points3D.values()])
        colors = np.array([p.color for p in model.points3D.values()]) / 255
        if not np.isfinite(points).all():
            raise ValueError("Nonfinite reconstructed points")
        angles = []
        camera_centers = {im.image_id: im.projection_center() for im in images}
        for point in model.points3D.values():
            ids = [e.image_id for e in point.track.elements if e.image_id in camera_centers]
            if len(ids) < 2:
                continue
            # Bound pair count for long tracks; retain an explicit sampled-support label.
            ids = [ids[i] for i in np.linspace(0, len(ids)-1, min(16, len(ids)), dtype=int)]
            rays = np.array([point.xyz-camera_centers[i] for i in ids])
            rays /= np.linalg.norm(rays, axis=1)[:, None]
            angles.append(float(np.rad2deg(np.arccos(np.clip((rays @ rays.T).min(), -1, 1)))))
        result["sampled_track_max_parallax_median_deg"] = float(np.median(angles)) if angles else None
        result["sampled_track_max_parallax_p90_deg"] = float(np.percentile(angles, 90)) if angles else None
        result["points_sampled_parallax_below_1deg_percent"] = 100*float(np.mean(np.array(angles) < 1)) if angles else None
        checks.append(result)
        # Keep independently reconstructed models in separate local frames.
        fig = plt.figure(figsize=(7, 5))
        ax = fig.add_subplot(projection="3d")
        ax.scatter(*points.T, c=colors, s=3, alpha=.8)
        ax.plot(*centers.T, color="#d65f00", linewidth=1, label="SfM cameras")
        ax.set(xlabel="X [local units]", ylabel="Y [local units]", zlabel="Z [local units]")
        limits = np.ptp(points, axis=0)
        ax.set_box_aspect(np.maximum(limits, limits.max()*.1))
        ax.legend()
        save_editable_figure(fig, out / f"model_{mid}_local.png")
        plt.close(fig)
        viewer_models.append({"label": f"{a.experiment} model {mid}", "points": points.tolist(),
                              "trajectory": centers.tolist(), "unit": "任意の局所単位（mではない）",
                              "source": f"model_{mid}_LOCAL_UNITS.ply", "note": reason})
        chosen = np.linspace(0, len(images)-1, min(6, len(images)), dtype=int)
        sheet = Image.new("RGB", (960, 410), "white")
        draw = ImageDraw.Draw(sheet)
        for j, idx in enumerate(chosen):
            im = images[idx]
            with Image.open(work / "images" / im.name) as source_image:
                sheet.paste(source_image.resize((320, 180)), (j % 3 * 320, j // 3 * 205 + 22))
            draw.text((j % 3 * 320+4, j // 3 * 205+4),
                      f'{im.name} clip {float(timing[im.name]["clip_pts_s"]):.3f} s', fill="black")
        sheet.save(out / f"model_{mid}_frames.jpg")
    template = root / "src/aquabeacon_analysis/child_map_viewer.html"
    html = template.read_text(encoding="utf-8").replace("__MAP_DATA__", json.dumps(viewer_models))
    html = html.replace("長田高校・子機のみの地図", "南予 追加再構成の局所地図")
    html = html.replace("長田高校のプール実験 — 子機の疎な3D地図", "南予実験 追加再構成の局所地図")
    html = html.replace("親機の観測データは使用していません。", "点群再構成は子機画像のみ。配置診断には保存済みのセンサー航法を参照しました。")
    (out / "viewer.html").write_text(html, encoding="utf-8")
    write_csv(out / "placement_diagnostics.csv", checks)
    execution = (out / "execution.log").read_text(encoding="utf-8")
    dump(out / "review_manifest.json", {"models": checks,
        "mapper_time_limit_reached": "Reached maximum runtime" in execution,
        "linear_solver_failure_messages": execution.count("Linear solver failure"),
        "execution_log_sha256": sha256(out / "execution.log"),
        "source_manifest_sha256": sha256(out / "manifest.json"),
        "inputs_sha256": {str(p.relative_to(root)): sha256(p) for p in (nav_path, cfg_path, axes_path, clock_path, template, Path(__file__),
            root / "scripts/refine_nanyo_mapping.py", root / "src/aquabeacon_analysis/field_review.py",
            root / "src/aquabeacon_analysis/nanyo_position.py", root / "src/aquabeacon_analysis/parent_magnetic_attitude.py")},
        "note": "No old map, navigation, scale or calibration is overwritten."})
    print(json.dumps(checks, indent=2))


if __name__ == "__main__":
    main()
