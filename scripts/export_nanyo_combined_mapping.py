"""Build Day1, Day2 and a WGS84-linked replay using selected GNSS and tracked lights."""

import argparse
import json
import os
import struct
import subprocess
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pycolmap as pc
import trimesh
from analyze_nanyo import read
from analyze_nanyo_day2 import Day2Context
from estimate_nanyo_absolute import Context, n
from export_nanyo_replay import clean, dump, segment
from scipy.optimize import least_squares
from scipy.spatial.transform import Rotation

from aquabeacon_analysis.editable_plots import save_editable_figure
from aquabeacon_analysis.mapping_frames import enu_transform
from aquabeacon_analysis.nanyo_position import robust_rotation_mean
from aquabeacon_analysis.ngt_binary import sha256
from aquabeacon_analysis.ngt_sync import sample_with_gaps

ACQUISITIONS = ["20261031_nanyo_000", "20261101_nanyo_000"]
COMBINED = "20261102_nanyo_combined_000"


def default_time(scene):
    surfaces = scene["surfaces"]
    middle = surfaces[len(surfaces) // 2]["time"] if surfaces else scene["duration"] / 2
    available = [
        r["time"]
        for r in scene["navigation"]
        if r["child"] is not None
        and np.isfinite(r["child"]).all()
        and (not surfaces or surfaces[0]["time"] <= r["time"] <= surfaces[-1]["time"])
    ]
    return min(available, key=lambda t: abs(t - middle), default=middle)


def fit_day2(ctx, eid, source, label):
    model = pc.Reconstruction(source)
    time_path = Path(f"results/{eid}/visual_mapping/frame_times.csv")
    timing = {r["image"]: float(r["sync_utc_s"]) for r in read(time_path)}
    images = sorted(model.images.values(), key=lambda im: timing[im.name])
    t = np.array([timing[im.name] for im in images])
    result = {
        "fragment": label,
        "source_model": str(source),
        "registered_images": len(images),
        "points": model.num_points3D(),
        "accepted": False,
        "accuracy_validated": False,
        "static_scene_validated": False,
    }
    if len(images) < 4:
        return {
            **result,
            "reason": "Fewer than four registered images; metric scale/translation unsupported",
        }, None
    st = ctx.sample(t)
    vm = json.loads(Path(f"results/{eid}/visual_mapping/manifest.json").read_text(encoding="utf-8"))
    camera_name = next(
        k
        for k, v in ctx.sync.items()
        if k.startswith("child") and Path(v["source_video"]["path"]).stem == Path(vm["source"]).stem
    )
    mounting = (
        ctx.child_axes
        @ np.array(ctx.cfg["mounts"][camera_name]["R_dji_from_sensor"]).T
        @ np.array(ctx.cfg["R_dji_from_child_optical"])
    )
    candidates = (
        st["child"] @ mounting @ np.array([im.cam_from_world().rotation.matrix() for im in images])
    )
    valid = np.isfinite(candidates).all((1, 2))
    if valid.sum() < 3:
        return {**result, "reason": "Insufficient synchronized attitude"}, None
    rotation, errors, _ = robust_rotation_mean(candidates[valid], 15)
    result.update(
        orientation_median_deg=float(np.median(errors)),
        orientation_p95_deg=float(np.percentile(errors, 95)),
        child_camera_session=camera_name,
    )
    if np.median(errors) > 25 or np.percentile(errors, 95) > 60:
        return {**result, "reason": "Visual and inertial orientations inconsistent"}, None
    centers = np.array([im.projection_center() for im in images]) @ rotation.T
    nav_path = Path(f"results/{eid}/mapping_navigation/child_navigation.csv")
    nav = read(nav_path)
    camera = n(nav, ["camera_east", "camera_north", "camera_up"])
    camera[n(nav, ["anchor_age_s"])[:, 0] > 1] = np.nan
    target = sample_with_gaps(n(nav, ["utc_s"])[:, 0], camera, t, 0.15)
    anchors = np.isfinite(target).all(1)
    result["sensor_anchors"] = int(anchors.sum())
    if anchors.sum() < 3:
        return {
            **result,
            "reason": "Fewer than three recent sensor-navigation anchors; no absolute placement",
        }, None
    # Camera depth includes the rotating pressure-to-camera lever arm.
    lever_up = np.einsum(
        "nij,j->ni", st["child"], ctx.lever["child_imu"] - ctx.lever["child_camera"]
    )[:, 2]
    pressure_target = -st["depth"] - lever_up
    dep = np.isfinite(pressure_target)
    x, y = centers[dep, 2], pressure_target[dep]
    depth_scale, correlation, depth_fit = np.nan, 0.0, None
    if len(x) >= 4 and np.ptp(x) > 1e-6 and np.ptp(y) > 0.15:
        depth_fit = least_squares(
            lambda v: v[0] * x + v[1] - y, np.polyfit(x, y, 1), loss="huber", f_scale=0.05
        )
        depth_scale = float(depth_fit.x[0])
        correlation = float(np.corrcoef(x, y)[0, 1])
    pressure_supported = depth_scale > 0 and correlation > 0.5
    if pressure_supported:
        scale, method = depth_scale, "pressure variation + attitude + camera/pressure lever"
    else:
        xx, yy = centers[anchors], target[anchors]
        if len(xx) < 4 or np.linalg.norm(np.ptp(yy, axis=0)) < 0.25:
            return {
                **result,
                "reason": "Neither pressure nor sensor motion constrains metric scale",
                "pressure_correlation": correlation,
            }, None
        fit = least_squares(
            lambda v: ((v[0] * xx + v[1:] - yy) / [1, 1, 3]).ravel(),
            np.r_[1.0, np.median(yy - xx, axis=0)],
            loss="soft_l1",
        )
        scale, method = (
            float(fit.x[0]),
            "positive sensor-position fit; pressure agreement not established",
        )
    if not np.isfinite(scale) or scale <= 0:
        return {
            **result,
            "reason": "Non-positive metric scale; no forced reflection or scale",
        }, None
    xx, yy = scale * centers[anchors], target[anchors]
    shift = least_squares(
        lambda v: ((xx + v - yy) / [1, 1, 2]).ravel(), np.median(yy - xx, axis=0), loss="soft_l1"
    ).x
    residual = np.linalg.norm(xx + shift - yy, axis=1)
    result.update(
        scale_m_per_unit=scale,
        scale_method=method,
        pressure_scale_supported=bool(pressure_supported),
        pressure_correlation=correlation,
        depth_range_m=float(np.ptp(y)) if len(y) else None,
        R_enu_from_model=rotation,
        translation_enu_m=shift,
        registration_median_m=float(np.median(residual)),
        registration_p95_m=float(np.percentile(residual, 95)),
    )
    if np.median(residual) > 5:
        return {**result, "reason": "Median navigation registration disagreement exceeds 5 m"}, None
    before = model.compute_mean_reprojection_error()
    model.transform(pc.Sim3d(scale, pc.Rotation3d(rotation), shift))
    after = model.compute_mean_reprojection_error()
    assert abs(after - before) < 1e-6
    dest = Path(f"data/processed/{eid}/mapping_selected/{label}/registered")
    dest.mkdir(parents=True, exist_ok=True)
    model.write(dest)
    result.update(
        accepted=True,
        reason="Provisional sensor-registered fragment",
        reprojection_px=after,
        reprojection_change_px=after - before,
        inputs_sha256={
            str(p): sha256(p)
            for p in [time_path, nav_path, *ctx.inputs, *source.glob("*.bin"), Path(__file__)]
        },
    )
    return result, dest


def navigation(eid, parent):
    lo, hi = json.loads(Path(f"experiments/{eid}/experiment.json").read_text(encoding="utf-8"))[
        "utc_interval_s"
    ]
    nav = read(Path(f"results/{eid}/mapping_navigation/child_navigation.csv"))[::5]
    pt = n(parent, ["utc_s"])[:, 0]
    pv = n(
        parent,
        [
            "antenna_east",
            "antenna_north",
            "antenna_up",
            "camera_east",
            "camera_north",
            "camera_up",
            "qw",
            "qx",
            "qy",
            "qz",
        ],
    )
    pp = sample_with_gaps(pt, pv, n(nav, ["utc_s"])[:, 0], 0.15)
    rows = []
    for i, r in enumerate(nav):
        available = r["position_available"] == "True"
        rows.append(
            {
                "time": float(r["utc_s"]) - lo,
                "child": [float(r[f"origin_{k}"]) for k in ["east", "north", "up"]]
                if available
                else None,
                "child_camera": [float(r[f"camera_{k}"]) for k in ["east", "north", "up"]]
                if available
                else None,
                "parent": pp[i, :3],
                "parent_camera": pp[i, 3:6],
                "child_q": [float(r[k]) for k in ["qw", "qx", "qy", "qz"]],
                "parent_q": pp[i, 6:],
                "age": float(r["anchor_age_s"]),
                "sigma": [
                    float(r[k]) for k in ["sigma_east", "sigma_north", "absolute_up_sigma_model_m"]
                ],
            }
        )
    return lo, hi, rows


def reframe_scene(scene, source_origin, common_origin):
    rotation, shift = enu_transform(source_origin, common_origin)

    def positions(a):
        return np.asarray(a, float) @ rotation.T + shift

    scene["points"] = positions(scene["points"]) if len(scene["points"]) else np.empty((0, 3))
    for s in scene["surfaces"]:
        s["vertices"], s["camera"] = positions(s["vertices"]), positions(s["camera"])
        s["quaternion_xyzw"] = (
            Rotation.from_matrix(rotation) * Rotation.from_quat(s["quaternion_xyzw"])
        ).as_quat()
    for row in scene["navigation"]:
        for k in ["child", "child_camera", "parent", "parent_camera"]:
            if row[k] is not None:
                row[k] = positions(row[k])
        for k in ["child_q", "parent_q"]:
            q = np.array(row[k], float)
            if np.isfinite(q).all():
                row[k] = (
                    Rotation.from_matrix(rotation) * Rotation.from_quat(q[[1, 2, 3, 0]])
                ).as_quat()[[3, 0, 1, 2]]
    scene["manifest"].update(
        origin_llh=common_origin,
        source_origin_llh=source_origin,
        R_common_from_source_enu=rotation,
        common_translation_m=shift,
    )
    return scene, rotation, shift


def write_geometry(out, scenes, meshes):
    out.mkdir(parents=True, exist_ok=True)
    xyz = np.concatenate([np.array(s["points"]).reshape(-1, 3) for s in scenes])
    rgb = np.concatenate([np.array(s["rgb"]).reshape(-1, 3) for s in scenes])
    if len(xyz):
        (out / "points.ply").write_bytes(
            trimesh.points.PointCloud(xyz, colors=rgb).export(file_type="ply")
        )
    else:
        (out / "points.ply").write_text(
            "ply\nformat ascii 1.0\nelement vertex 0\nproperty float x\nproperty float y\nproperty float z\nend_header\n"
        )
    glb = trimesh.Scene()
    for i, geometry in enumerate(meshes):
        for name, mesh in geometry.geometry.items():
            glb.add_geometry(mesh.copy(), node_name=f"{i}_{name}")
    if not len(glb.geometry) and len(xyz):
        glb.add_geometry(trimesh.points.PointCloud(xyz, colors=rgb))
    if len(glb.geometry):
        binary = glb.export(file_type="glb")
    else:
        payload = json.dumps(
            {
                "asset": {
                    "version": "2.0",
                    "generator": "AquaBeacon: no accepted georeferenced geometry",
                },
                "scene": 0,
                "scenes": [{"nodes": []}],
                "nodes": [],
            }
        ).encode()
        payload += b" " * (-len(payload) % 4)
        binary = (
            struct.pack("<4sII", b"glTF", 2, 20 + len(payload))
            + struct.pack("<I4s", len(payload), b"JSON")
            + payload
        )
    (out / "textured_map.glb").write_bytes(binary)


def build_trial(ctx, eid, common_origin, reuse=False):
    out = Path(f"results/{eid}/mapping_selected")
    out.mkdir(parents=True, exist_ok=True)
    if reuse and (out / "scene.json").exists():
        existing = json.loads((out / "scene.json").read_text(encoding="utf-8"))
        existing["default_time"] = default_time(existing)
        dump(out / "scene.json", existing)
        return existing, trimesh.load(out / "textured_map.glb", force="scene") if existing[
            "points"
        ] else trimesh.Scene()
    day1 = eid.startswith("20261031")
    acq = f"{eid[:8]}_nanyo_000"
    parent = read(Path(f"results/{acq}/mapping_navigation/parent_navigation.csv"))
    if day1:
        name = "extended_0" if eid.endswith("001") else "original_1"
        sources = [Path(f"data/processed/{eid}/position_mapping/{name}")]
    else:
        sources = sorted(Path(f"data/interim/{eid}/visual_mapping/models").iterdir())
    fragments, scenes, meshes = [], [], []
    for index, source in enumerate(sources):
        label = f"fragment_{index}"
        if day1:
            registration = {
                "fragment": label,
                "accepted": True,
                "scale_method": "Day1 previously accepted metric geometry retained; translation re-estimated from selected GNSS/tracked-light navigation",
                "source_model": str(source),
            }
            registered = source
        else:
            registration, registered = fit_day2(ctx, eid, source, label)
        fragments.append(registration)
        if not registration["accepted"]:
            print(eid, label, registration["reason"], flush=True)
            local = pc.Reconstruction(source)
            local.export_PLY(out / f"{label}_UNREGISTERED_LOCAL_UNITS.ply")
            continue
        image_root = Path(
            f"data/interim/{eid}/{'position_mapping' if eid == '20261031_nanyo_001' else 'visual_mapping'}/images"
        )
        s = segment(
            eid,
            parent,
            source_path=registered,
            output_name=f"mapping_selected/{label}",
            nav_name="mapping_navigation",
            image_root_override=image_root,
            align=day1,
        )
        registration.update(texture_manifest=s["manifest"])
        s, r, shift = reframe_scene(s, ctx.origin, common_origin)
        mesh = trimesh.load(out / label / "textured_map.glb", force="scene")
        transform = np.eye(4)
        transform[:3, :3], transform[:3, 3] = r, shift
        mesh.apply_transform(transform)
        meshes.append(mesh)
        scenes.append(s)
    lo, hi, nav = navigation(eid, parent)
    joined = {
        "id": eid,
        "label": f"Day{1 if day1 else 2} · 区間{int(eid[-3:])}",
        "start_utc": lo,
        "duration": hi - lo,
        "points": np.empty((0, 3)),
        "rgb": np.empty((0, 3)),
        "surfaces": [],
        "navigation": nav,
        "manifest": {
            "experiment_id": eid,
            "accuracy_validated": False,
            "fragments": fragments,
            "sfm_navigation": False,
        },
        "downloads": f"../../{eid}/mapping_selected/",
        "quality_note": "GNSSは静止RMSEで選定したFloat設定PPK。写真面は疎点群の近似。位置精度・静的形状は未検証。",
    }
    joined, _, _ = reframe_scene(joined, ctx.origin, common_origin)
    offset = 0
    points, rgb = [], []
    for s in scenes:
        points.extend(s["points"])
        rgb.extend(s["rgb"])
        for surf in s["surfaces"]:
            surf["point_indices"] = np.array(surf["point_indices"]) + offset
            surf["source_image"] = (
                f"{Path(s['manifest']['source_model']).name}/{surf['source_image']}"
            )
            joined["surfaces"].append(surf)
        offset += len(s["points"])
    joined["points"], joined["rgb"] = points, rgb
    joined["surfaces"].sort(key=lambda s: s["time"])
    joined["manifest"].update(
        points=len(points),
        photo_patches=len(joined["surfaces"]),
        triangles=sum(len(s["faces"]) for s in joined["surfaces"]),
        accepted_fragments=sum(f["accepted"] for f in fragments),
        total_fragments=len(fragments),
    )
    visible = (
        np.array(points)
        if points
        else np.array(
            [
                r[k]
                for r in joined["navigation"]
                for k in ["child", "parent"]
                if r[k] is not None and np.isfinite(r[k]).all()
            ]
        )
    )
    joined["center"] = np.median(visible, axis=0)
    joined["radius"] = max(
        1, float(np.percentile(np.linalg.norm(visible - joined["center"], axis=1), 98))
    )
    joined["default_time"] = default_time(joined)
    if not points:
        joined["quality_note"] = (
            "位置合わせを支持する情報が不足し、写真断片の絶対配置は未採用。センサー航跡を表示します。"
        )
    write_geometry(out, [joined], meshes)
    dump(out / "scene.json", joined)
    dump(out / "manifest.json", joined["manifest"])
    return clean(joined), trimesh.load(out / "textured_map.glb", force="scene") if joined[
        "points"
    ] else trimesh.Scene()


def overview_plot(out, scenes):
    fig, ax = plt.subplots(figsize=(10, 8))
    colors = ["#217a9b", "#ce7835"]
    for s in scenes:
        color = colors[s["id"].startswith("20261101")]
        p = np.array(s["points"])
        if len(p):
            p = p[:: max(1, len(p) // 12000)]
            ax.scatter(p[:, 0], p[:, 1], s=1, alpha=0.25, color=color)
        xy = np.array(
            [r["child"] if r["child"] is not None else [np.nan] * 3 for r in s["navigation"]], float
        )
        ax.plot(xy[:, 0], xy[:, 1], lw=0.6, color=color, label=s["label"].replace("区間", "Trial"))
    ax.set(
        xlabel="East [m]",
        ylabel="North [m]",
        title="Selected-GNSS mapping in a common WGS84 ENU frame",
        aspect="equal",
    )
    ax.grid(alpha=0.2)
    ax.legend(fontsize=8)
    fig.tight_layout()
    save_editable_figure(fig, out / "mapping_overhead.png", dpi=160)
    plt.close(fig)


def write_group(eid, title, scenes, meshes, common_origin):
    out = Path(f"results/{eid}/mapping_replay")
    out.mkdir(parents=True, exist_ok=True)
    media, inputs = [], []
    for acq in ACQUISITIONS:
        media_name = "navigation_replay" if acq == ACQUISITIONS[0] else "mapping_replay"
        media_path = Path(f"results/{acq}/{media_name}/media_manifest.json")
        if not any(s["id"].startswith(acq[:8]) for s in scenes):
            continue
        inputs.append(media_path)
        for video in json.loads(media_path.read_text(encoding="utf-8"))["videos"]:
            media.append(
                {**video, "path": os.path.relpath(Path(video["target"]), out).replace("\\", "/")}
            )
    write_geometry(out, scenes, meshes)
    overview_plot(out, scenes)
    template = Path("scripts/templates/nanyo_combined_mapping.html")
    payload = json.dumps(
        clean({"title": title, "segments": scenes, "media": media}),
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    (out / "viewer.html").write_text(
        template.read_text(encoding="utf-8").replace("__DATA__", payload.replace("</", "<\\/")),
        encoding="utf-8",
    )
    manifest = {
        "experiment_id": eid,
        "title": title,
        "common_origin_llh": common_origin,
        "join_method": "Rigid WGS84 ENU frame conversion; no ICP, arbitrary day translation or visual seam assumed",
        "sfm_navigation": False,
        "accuracy_validated": False,
        "segments": [s["manifest"] for s in scenes],
        "inputs_sha256": {
            str(p): sha256(p)
            for p in [
                *inputs,
                *[Path(f"results/{s['id']}/mapping_selected/scene.json") for s in scenes],
                template,
                Path(__file__),
                Path("scripts/export_nanyo_replay.py"),
            ]
        },
        "git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
    }
    dump(out / "manifest.json", manifest)
    print(title, sum(len(s["points"]) for s in scenes), "points", flush=True)


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--experiment", required=True, choices=[COMBINED])
    parser.add_argument("--reuse-geometry", action="store_true")
    args = parser.parse_args()
    common_origin = json.loads(
        Path(f"results/{ACQUISITIONS[0]}/mapping_gnss_selection/manifest.json").read_text(
            encoding="utf-8"
        )
    )["origin_llh"]
    scenes, meshes = [], []
    for acq in ACQUISITIONS:
        ctx = Context() if acq == ACQUISITIONS[0] else Day2Context()
        ctx.inputs.extend(
            [
                Path(f"results/{acq}/mapping_gnss_selection/manifest.json"),
                Path(f"results/{acq}/mapping_navigation/manifest.json"),
            ]
        )
        daily_scenes, daily_meshes = [], []
        for i in range(1, 3 if acq == ACQUISITIONS[0] else 6):
            s, m = build_trial(ctx, f"{acq[:8]}_nanyo_{i:03d}", common_origin, args.reuse_geometry)
            daily_scenes.append(s)
            daily_meshes.append(m)
        write_group(
            acq,
            "Day1" if acq == ACQUISITIONS[0] else "Day2",
            daily_scenes,
            daily_meshes,
            common_origin,
        )
        scenes.extend(daily_scenes)
        meshes.extend(daily_meshes)
    write_group(COMBINED, "Day1 + Day2", scenes, meshes, common_origin)


if __name__ == "__main__":
    main()
