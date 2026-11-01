"""Texture existing point geometry, register to sensor navigation and build a local replay."""

import argparse
import base64
import io
import json
import subprocess
from pathlib import Path

import cv2
import numpy as np
import pycolmap as pc
import trimesh
from analyze_nanyo import read
from estimate_nanyo_absolute import n
from PIL import Image
from scipy.optimize import least_squares

from aquabeacon_analysis.camera_calibration import load_camera_intrinsics
from aquabeacon_analysis.ngt_binary import sha256
from aquabeacon_analysis.ngt_sync import sample_with_gaps
from aquabeacon_analysis.ngt_textured_map import projected_surface

ACQ = "20261031_nanyo_000"


def clean(value):
    if isinstance(value, np.ndarray):
        return clean(value.tolist())
    if isinstance(value, np.generic):
        return clean(value.item())
    if isinstance(value, float) and not np.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    return value


def dump(path, value):
    path.write_text(
        json.dumps(clean(value), ensure_ascii=False, separators=(",", ":"), allow_nan=False),
        encoding="utf-8",
    )


def segment(eid, parent):
    out = Path(f"results/{eid}/navigation_mapping")
    out.mkdir(parents=True, exist_ok=True)
    (out / "textures").mkdir(exist_ok=True)
    label = "extended_0" if eid.endswith("001") else "original_1"
    source = Path(f"data/processed/{eid}/position_mapping/{label}")
    model = pc.Reconstruction(source)
    intervals = json.loads(Path(f"experiments/{eid}/experiment.json").read_text())["utc_interval_s"]
    times = {
        r["image"]: float(r["sync_utc_s"])
        for r in read(Path(f"results/{eid}/visual_mapping/frame_times.csv"))
    }
    if eid.endswith("001"):
        times.update(
            {
                r["image"]: float(r["sync_utc_s"])
                for r in read(Path(f"results/{eid}/position_mapping/frame_times.csv"))
            }
        )
    images = sorted(model.images.values(), key=lambda im: times[im.name])
    t = np.array([times[im.name] for im in images])
    nav_path = Path(f"results/{eid}/navigation_fusion/child_navigation.csv")
    nav = read(nav_path)
    nt = n(nav, ["utc_s"])[:, 0]
    camera = n(nav, ["camera_east", "camera_north", "camera_up"])
    age = n(nav, ["anchor_age_s"])[:, 0]
    camera[age > 1] = np.nan
    target = sample_with_gaps(nt, camera, t, 0.15)
    centers = np.array([im.projection_center() for im in images])
    ok = np.isfinite(target).all(1)
    if ok.sum() < 3:
        raise ValueError(f"{eid}: no sensor anchors for registration")
    fit = least_squares(
        lambda shift: ((centers[ok] + shift - target[ok]) / [1, 1, 2]).ravel(),
        np.median(target[ok] - centers[ok], axis=0),
        loss="soft_l1",
    )
    shift = fit.x
    residual = np.linalg.norm(centers[ok] + shift - target[ok], axis=1)
    reprojection = model.compute_mean_reprojection_error()
    model.transform(pc.Sim3d(1, pc.Rotation3d(), shift))
    assert abs(model.compute_mean_reprojection_error() - reprojection) < 1e-7
    dest = Path(f"data/processed/{eid}/navigation_mapping/model")
    dest.mkdir(parents=True, exist_ok=True)
    model.write(dest)
    model.export_PLY(out / "points.ply")
    xyz = np.array([p.xyz for p in model.points3D.values()])
    rgb = np.array([p.color for p in model.points3D.values()])
    point_ids = list(model.points3D.keys())
    point_index = {id: i for i, id in enumerate(point_ids)}
    k, d, _ = load_camera_intrinsics(Path.cwd(), "C_MAIN_CAM", (960, 540), "proxy")
    image_root = Path(
        f"data/interim/{eid}/"
        + ("position_mapping" if eid.endswith("001") else "visual_mapping")
        + "/images"
    )
    selected = np.unique(np.linspace(0, len(images) - 1, min(72, len(images))).astype(int))
    surfaces = []
    seen = set()
    scene = trimesh.Scene()
    inputs = [nav_path]
    for number, index in enumerate(selected):
        im = images[index]
        path = image_root / im.name
        image = cv2.imread(str(path))
        image = cv2.undistort(image, k, d)
        texture = Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))
        observed = sorted(
            {
                p.point3D_id
                for p in im.points2D
                if p.has_point3D() and model.point3D(p.point3D_id).error < 2
            }
        )
        local_ids = np.array([point_index[p] for p in observed], int)
        if len(local_ids) < 4:
            continue
        position = im.projection_center()
        quat = im.cam_from_world().rotation.inverse().quat
        ids, uv, faces = projected_surface(
            xyz[local_ids],
            position,
            quat,
            k,
            [960, 540],
            max_edge_px=90,
            max_depth_ratio=1.18,
            max_world_edge_fraction=0.12,
            visibility_cell_px=6,
        )
        unique = []
        for f in faces:
            key = tuple(sorted(local_ids[ids[f]].tolist()))
            if key not in seen:
                seen.add(key)
                unique.append(f)
        if not unique:
            continue
        faces = np.array(unique)
        compact, inverse = np.unique(faces.ravel(), return_inverse=True)
        vertices = xyz[local_ids[ids[compact]]]
        mesh_uv = uv[compact]
        faces = inverse.reshape(-1, 3)
        material = trimesh.visual.material.PBRMaterial(
            baseColorTexture=texture, metallicFactor=0, roughnessFactor=1, doubleSided=True
        )
        mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
        mesh.visual = trimesh.visual.texture.TextureVisuals(uv=mesh_uv, material=material)
        scene.add_geometry(mesh, node_name=f"photo_{number:03d}")
        filename = f"textures/photo_{number:03d}.jpg"
        texture.save(out / filename, quality=90)
        buffer = io.BytesIO()
        texture.save(buffer, format="JPEG", quality=90)
        surfaces.append(
            {
                "time": times[im.name] - intervals[0],
                "source_image": im.name,
                "vertices": vertices,
                "faces": faces,
                "uv": mesh_uv,
                "point_indices": local_ids[ids[compact]],
                "image": base64.b64encode(buffer.getvalue()).decode("ascii"),
                "camera": position,
                "quaternion_xyzw": quat,
            }
        )
        inputs.append(path)
    if not surfaces:
        raise ValueError("No photo patches")
    (out / "textured_map.glb").write_bytes(scene.export(file_type="glb"))
    rows = nav[::5]
    parent_times = n(parent, ["utc_s"])[:, 0]
    parent_values = n(
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
    p = sample_with_gaps(parent_times, parent_values, n(rows, ["utc_s"])[:, 0], 0.15)
    navigation = []
    for i, r in enumerate(rows):
        available = r["position_available"] == "True"
        navigation.append(
            {
                "time": float(r["utc_s"]) - intervals[0],
                "child": [float(r[k]) for k in ["origin_east", "origin_north", "origin_up"]]
                if available
                else None,
                "child_camera": [float(r[k]) for k in ["camera_east", "camera_north", "camera_up"]]
                if available
                else None,
                "parent": p[i, :3],
                "parent_camera": p[i, 3:6],
                "child_q": [float(r[k]) for k in ["qw", "qx", "qy", "qz"]],
                "parent_q": p[i, 6:10],
                "age": float(r["anchor_age_s"]),
                "sigma": [
                    float(r[k]) for k in ["sigma_east", "sigma_north", "absolute_up_sigma_model_m"]
                ],
            }
        )
    manifest = {
        "experiment_id": eid,
        "frame": "true ENU meters; same origin as navigation",
        "source_model": str(source),
        "translation_from_previous_map_m": shift,
        "sensor_navigation_anchors": int(ok.sum()),
        "registration_residual_median_m": float(np.median(residual)),
        "registration_residual_p95_m": float(np.percentile(residual, 95)),
        "points": len(xyz),
        "registered_images": len(images),
        "photo_patches": len(surfaces),
        "triangles": len(seen),
        "mean_reprojection_error_px": reprojection,
        "accuracy_validated": False,
        "direction_of_dependency": "sensor-only navigation -> map registration; no SfM camera position is input to navigation",
        "geometry": "Only existing tracked points; short local Delaunay triangles with visibility/depth/edge gates. Sparse approximation, not dense reconstruction.",
        "inputs_sha256": {
            str(p): sha256(p) for p in [*inputs, *source.glob("*.bin"), Path(__file__)]
        },
    }
    dump(out / "manifest.json", manifest)
    data = {
        "id": eid,
        "label": "区間① 海底" if eid.endswith("001") else "区間② 気泡・船体付近（診断用）",
        "start_utc": intervals[0],
        "duration": intervals[1] - intervals[0],
        "points": xyz,
        "rgb": rgb,
        "surfaces": surfaces,
        "navigation": navigation,
        "center": np.median(xyz, axis=0),
        "radius": float(np.percentile(np.linalg.norm(xyz - np.median(xyz, axis=0), axis=1), 95)),
        "default_time": min(
            (
                r["time"]
                for r in navigation
                if r["child"] is not None
                and surfaces[0]["time"] <= r["time"] <= surfaces[-1]["time"]
            ),
            key=lambda t: abs(t - surfaces[len(surfaces) // 2]["time"]),
            default=surfaces[len(surfaces) // 2]["time"],
        ),
        "manifest": manifest,
        "downloads": f"../../{eid}/navigation_mapping/",
    }
    dump(out / "scene.json", data)
    print(
        eid,
        len(surfaces),
        len(seen),
        "registration median/p95",
        np.median(residual),
        np.percentile(residual, 95),
        flush=True,
    )
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", required=True, choices=[ACQ])
    parser.add_argument("--reuse-geometry", action="store_true")
    args = parser.parse_args()
    out = Path(f"results/{ACQ}/navigation_replay")
    out.mkdir(parents=True, exist_ok=True)
    parent = read(Path(f"results/{ACQ}/navigation_fusion/parent_navigation.csv"))
    segments = []
    for suffix in ["001", "002"]:
        eid = f"20261031_nanyo_{suffix}"
        segments.append(
            json.loads(
                Path(f"results/{eid}/navigation_mapping/scene.json").read_text(encoding="utf-8")
            )
            if args.reuse_geometry
            else segment(eid, parent)
        )
    media = json.loads((out / "media_manifest.json").read_text())
    data = {"segments": segments, "media": media["videos"]}
    template = Path("scripts/templates/nanyo_navigation_replay.html")
    payload = json.dumps(clean(data), ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    html = template.read_text(encoding="utf-8").replace("__DATA__", payload.replace("</", "<\\/"))
    (out / "viewer.html").write_text(html, encoding="utf-8")
    dump(
        out / "manifest.json",
        {
            "experiment_id": ACQ,
            "segments": [s["manifest"] for s in segments],
            "code_sha256": sha256(Path(__file__)),
            "template_sha256": sha256(template),
            "media_manifest_sha256": sha256(out / "media_manifest.json"),
            "git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        },
    )


if __name__ == "__main__":
    main()
