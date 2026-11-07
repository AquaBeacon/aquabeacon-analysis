"""Anchor visual fragments to true ENU with estimated positions, attitude and depth."""

import argparse
import json
import subprocess
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pycolmap as pc
from analyze_nanyo import read, write
from estimate_nanyo_absolute import Context, n
from scipy.optimize import least_squares
from scipy.spatial.transform import Rotation

from aquabeacon_analysis.editable_plots import save_editable_figure
from aquabeacon_analysis.nanyo_position import robust_rotation_mean
from aquabeacon_analysis.ngt_binary import sha256
from aquabeacon_analysis.ngt_position import nearest_indices


def fit_fragment(ctx, eid, label, source, time_rows, out, fixed_alignment=None):
    model = pc.Reconstruction(source)
    images = sorted(model.images.values(), key=lambda x: float(time_rows[x.name]["sync_utc_s"]))
    times = np.array([float(time_rows[x.name]["sync_utc_s"]) for x in images])
    st = ctx.sample(times)
    local = np.array([im.projection_center() for im in images])
    orientation = (
        st["child"]
        @ ctx.child_from_optical
        @ np.array([im.cam_from_world().rotation.matrix() for im in images])
    )
    ok = np.isfinite(orientation).all((1, 2))
    mean, rotation_error, _kept = robust_rotation_mean(orientation[ok], 15)
    if fixed_alignment is not None:
        mean = np.array(fixed_alignment["R_enu_from_model"])
        rotation_error = np.degrees(Rotation.from_matrix(orientation[ok] @ mean.T).magnitude())
    rotated = local @ mean.T
    p = read(Path(f"results/{eid}/absolute_position/positions.csv"))
    pi, position_dt = nearest_indices(n(p, ["utc_s"])[:, 0], times, 0.3)
    pos = n(p, ["child_camera_east_m", "child_camera_north_m", "child_camera_up_m"])
    anchors = (pi >= 0) & np.isfinite(pos[np.maximum(pi, 0)]).all(1)
    matched_anchor_frames = int(anchors.sum())
    # Added 5 Hz images must not give one 2 Hz observation repeated votes.
    for observation in np.unique(pi[anchors]):
        frames = np.flatnonzero(anchors & (pi == observation))
        anchors[frames] = False
        anchors[frames[np.argmin(np.abs(position_dt[frames]))]] = True
    observed = pos[np.maximum(pi, 0)]
    # The pressure sensor's mounting center is approximated by the child IMU reference.
    offset = np.einsum(
        "nij,j->ni", st["child"], ctx.lever["child_imu"] - ctx.lever["child_camera"]
    )[:, 2]
    pressure_target = -st["depth"] - offset
    dep = np.isfinite(pressure_target) & np.isfinite(rotated[:, 2])
    x = rotated[dep, 2]
    y = pressure_target[dep]
    linear = np.polyfit(x, y, 1) if np.ptp(x) > 1e-8 else [np.nan, np.nan]
    pressure_fit = (
        least_squares(lambda ab: ab[0] * x + ab[1] - y, linear, loss="huber", f_scale=0.05)
        if np.isfinite(linear).all()
        else None
    )
    scale = float(pressure_fit.x[0]) if pressure_fit is not None else np.nan
    corr = float(np.corrcoef(x, y)[0, 1]) if np.std(x) > 1e-8 and np.std(y) > 1e-8 else 0
    result = {
        "label": label,
        "source_model": str(source),
        "registered_images": len(images),
        "points": model.num_points3D(),
        "orientation_residual_median_deg": float(np.median(rotation_error)),
        "orientation_residual_p95_deg": float(np.percentile(rotation_error, 95)),
        "direct_position_anchors": int(anchors.sum()),
        "position_matched_frames_before_deduplication": matched_anchor_frames,
        "anchor_policy": "One closest image per observed position or acoustic shot, at most 0.3 s apart",
        "depth_range_m": float(np.ptp(y)),
        "depth_scale_candidate": scale,
        "depth_correlation": corr,
        "scale_method": "pressure variation with rotated camera-to-pressure lever arm",
        "input_model_sha256": {p.name: sha256(p) for p in Path(source).glob("*.bin")},
        "accuracy_validated": False,
        "accepted": False,
    }
    depth_supported = np.ptp(y) > 0.15 and corr > 0.5 and scale > 0
    position_scale = np.nan
    # A positive position-only estimate can be used provisionally even when
    # pressure does not constrain scale; record that disagreement explicitly.
    if anchors.sum() >= 4:
        xx, yy = rotated[anchors], observed[anchors]
        ss = np.sum((xx - xx.mean(0)) * (yy - yy.mean(0))) / np.sum((xx - xx.mean(0)) ** 2)
        result["position_only_unconstrained_scale"] = float(ss)
        position_fit = least_squares(
            lambda v: ((v[0] * xx + v[1:] - yy) / [1.0, 1.0, 5.0]).ravel(),
            np.r_[0.1, np.median(yy, axis=0)],
            loss="soft_l1",
            f_scale=1,
        )
        position_scale = float(position_fit.x[0])
        result["weighted_position_scale"] = position_scale
    position_supported = np.isfinite(position_scale) and position_scale > 0
    if fixed_alignment is None and not depth_supported and not position_supported:
        result["reason"] = (
            "Metric scale not supported: pressure/visual motion inconsistent or too small; no forced positive scale."
        )
        return result, [], None
    if fixed_alignment is not None:
        scale = fixed_alignment["scale_m_per_unit"]
        result["scale_method"] = "Inherited fixed-camera ENU similarity from bridge_0; no re-fit"
        result["alignment_parent"] = fixed_alignment["label"]
    elif not depth_supported:
        scale = position_scale
        result["scale_method"] = (
            "Positive robust position-only similarity; depth consistency not established"
        )
    result["depth_scale_supported"] = bool(depth_supported)
    result["static_scene_validated"] = False
    if anchors.sum() < 3:
        result["reason"] = (
            "No absolute translation anchor during these images; range/depth alone do not determine horizontal origin."
        )
        return result, [], None
    rr = read(Path(f"results/{eid}/evaluation/acoustic_depth.csv"))
    ri, range_dt = nearest_indices(n(rr, ["sync_utc_s"])[:, 0], times, 0.3)
    ranges = n(rr, ["corrected_range_m"])[:, 0]
    range_valid = np.array([r["acoustic_candidate"] == "True" for r in rr])
    rangemask = (
        (ri >= 0)
        & range_valid[np.maximum(ri, 0)]
        & np.isfinite(st["parent"]).all((1, 2))
        & np.isfinite(st["gnss"]).all(1)
    )
    for observation in np.unique(ri[rangemask]):
        frames = np.flatnonzero(rangemask & (ri == observation))
        rangemask[frames] = False
        rangemask[frames[np.argmin(np.abs(range_dt[frames]))]] = True
    tx = st["gnss"] + np.einsum(
        "nij,j->ni", st["parent"], ctx.lever["parent_acoustic"] - ctx.lever["parent_antenna"]
    )
    receiver_offset = np.einsum(
        "nij,j->ni", st["child"], ctx.lever["child_acoustic"] - ctx.lever["child_camera"]
    )
    translation = np.median(observed[anchors] - scale * rotated[anchors], axis=0)

    def residual(shift):
        world = scale * rotated + shift
        position = (world[anchors] - observed[anchors]) / np.array([1.0, 1.0, 5.0])
        acoustic = (
            np.linalg.norm(world[rangemask] + receiver_offset[rangemask] - tx[rangemask], axis=1)
            - ranges[ri[rangemask]]
        ) / 5
        return np.r_[position.ravel(), acoustic]

    if fixed_alignment is not None:
        translation = np.array(fixed_alignment["translation_enu_m"])
    else:
        fit = least_squares(residual, translation, loss="soft_l1", f_scale=1)
        translation = fit.x
    world = scale * rotated + translation
    prior_residual = np.linalg.norm(world[anchors] - observed[anchors], axis=1)
    position_difference = world - observed
    horizontal_residual = np.linalg.norm(position_difference[anchors, :2], axis=1)
    vertical_residual = np.abs(position_difference[anchors, 2])
    range_residual = (
        np.linalg.norm(world[rangemask] + receiver_offset[rangemask] - tx[rangemask], axis=1)
        - ranges[ri[rangemask]]
    )
    surface = world[:, 2] + st["depth"] + offset
    result.update(
        accepted=True,
        reason="Provisional metric fragment anchored to observed child positions",
        scale_m_per_unit=scale,
        translation_enu_m=translation.tolist(),
        R_enu_from_model=mean.tolist(),
        pressure_fit_rms_m=float(np.std(scale * x - y)),
        position_prior_residual_median_m=float(np.median(prior_residual)),
        position_prior_residual_p95_m=float(np.percentile(prior_residual, 95)),
        position_prior_horizontal_median_m=float(np.median(horizontal_residual)),
        position_prior_horizontal_p95_m=float(np.percentile(horizontal_residual, 95)),
        position_prior_vertical_abs_median_m=float(np.median(vertical_residual)),
        position_prior_vertical_abs_p95_m=float(np.percentile(vertical_residual, 95)),
        acoustic_residual_median_m=float(np.median(range_residual)),
        acoustic_residual_p95_abs_m=float(np.percentile(abs(range_residual), 95)),
        acoustic_constraints=int(rangemask.sum()),
        estimated_water_surface_up_m=float(np.nanmedian(surface)),
        vertical_position_sigma_assumed_m=5,
        horizontal_position_sigma_assumed_m=1,
        range_sigma_assumed_m=5,
        pressure_sensor_reference="Child IMU CAD reference used provisionally; actual pressure center unmeasured",
        mean_reprojection_error_px=float(model.compute_mean_reprojection_error()),
    )
    pc_trans = pc.Sim3d(scale, pc.Rotation3d(mean), translation)
    original_reprojection = model.compute_mean_reprojection_error()
    model.transform(pc_trans)
    assert abs(model.compute_mean_reprojection_error() - original_reprojection) < 1e-7
    result["similarity_preserves_reprojection"] = True
    dest = Path(f"data/processed/{eid}/position_mapping/{label}")
    dest.mkdir(parents=True, exist_ok=True)
    model.write(dest)
    model.export_PLY(out / f"{label}.ply")
    track = []
    for i, im in enumerate(images):
        row = {
            "image": im.name,
            "utc_s": times[i],
            "east_m": world[i, 0],
            "north_m": world[i, 1],
            "up_m": world[i, 2],
            "direct_anchor": bool(anchors[i]),
            "source": "visual_reconstruction_constrained_by_position_attitude_depth",
            "accuracy_validated": False,
            "depth_m": st["depth"][i],
            "orientation_available": bool(ok[i]),
            "matched_observation_utc_s": float(p[pi[i]]["utc_s"]) if pi[i] >= 0 else np.nan,
            "position_match_dt_s": float(position_dt[i]),
            "anchor_residual_east_m": position_difference[i, 0] if anchors[i] else np.nan,
            "anchor_residual_north_m": position_difference[i, 1] if anchors[i] else np.nan,
            "anchor_residual_up_m": position_difference[i, 2] if anchors[i] else np.nan,
        }
        track.append(row)
    write(out / f"{label}_camera_positions.csv", track)
    xyz = np.array([p.xyz for p in model.points3D.values()])
    rgb = np.array([p.color for p in model.points3D.values()]) / 255
    fig = plt.figure(figsize=(10, 8))
    ax = fig.add_subplot(111, projection="3d")
    ax.scatter(*xyz.T, c=rgb, s=1, alpha=0.5)
    ax.plot(*world.T, c="#db5b2b", lw=1, label="Estimated camera path")
    ax.scatter(*observed[anchors].T, c="#156a9f", s=15, label="Position observations")
    ax.set(
        xlabel="True east [m]",
        ylabel="True north [m]",
        zlabel="Up [m]",
        title=f"{eid} {label}: provisional true ENU",
    )
    ax.legend()
    ax.set_box_aspect(np.maximum(np.ptp(xyz, axis=0), 0.1))
    fig.subplots_adjust(left=0.05, right=0.94, bottom=0.08, top=0.90)
    save_editable_figure(fig, out / f"{label}.png", dpi=170, bbox_inches="tight", pad_inches=0.25)
    plt.close(fig)
    fig, ax = plt.subplots(figsize=(8, 7))
    ax.scatter(xyz[:, 0], xyz[:, 1], c=rgb, s=1, alpha=0.35)
    ax.plot(world[:, 0], world[:, 1], c="#db5b2b", label="Map camera path")
    ax.scatter(
        observed[anchors, 0], observed[anchors, 1], s=15, c="#156a9f", label="Position observations"
    )
    ax.set(xlabel="True east [m]", ylabel="True north [m]")
    ax.axis("equal")
    ax.grid(alpha=0.2)
    ax.legend()
    fig.tight_layout()
    save_editable_figure(fig, out / f"{label}_overhead.png", dpi=170)
    plt.close(fig)
    fig, axes = plt.subplots(3, 1, figsize=(10, 7), sharex=True)
    for j, (ax, component) in enumerate(zip(axes, ["East", "North", "Up"], strict=True)):
        ax.scatter(times[anchors] - times[0], position_difference[anchors, j], s=12)
        ax.axhline(0, c="gray", lw=0.7)
        ax.set_ylabel(f"{component} residual [m]")
        ax.grid(alpha=0.2)
    axes[0].set_title("Map camera minus observed camera position (fit residual, not accuracy)")
    axes[-1].set_xlabel("Time from first map image [s]")
    fig.tight_layout()
    save_editable_figure(fig, out / f"{label}_anchor_residuals.png", dpi=170)
    plt.close(fig)
    return result, track, model


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--experiment",
        action="append",
        required=True,
        choices=["20261031_nanyo_001", "20261031_nanyo_002"],
    )
    parser.add_argument("--use-bridge", action="store_true")
    args = parser.parse_args()
    ctx = Context()
    for eid in args.experiment:
        out = Path(f"results/{eid}/position_mapping")
        out.mkdir(parents=True, exist_ok=True)
        old = Path(f"data/interim/{eid}/visual_mapping/models")
        times = {r["image"]: r for r in read(Path(f"results/{eid}/visual_mapping/frame_times.csv"))}
        sources = []
        if args.use_bridge:
            bridge = Path(f"data/interim/{eid}/position_mapping/models")
            if (bridge / "cameras.bin").exists():
                sources.append(("bridge_0", bridge))
            else:
                sources.extend((f"bridge_{p.name}", p) for p in bridge.iterdir() if p.is_dir())
            times.update({r["image"]: r for r in read(out / "frame_times.csv")})
            extension = bridge.parent / "extended_models"
            if (extension / "cameras.bin").exists():
                sources.append(("extended_0", extension))
        sources.extend(
            (f"original_{p.name}", p) for p in old.iterdir() if p.is_dir() and p.name != "0"
        )
        results = []
        for label, path in sources:
            inherited = next(
                (r for r in results if r["label"] == "bridge_0" and r["accepted"]), None
            )
            result, _, _ = fit_fragment(
                ctx,
                eid,
                label,
                path,
                times,
                out,
                fixed_alignment=inherited if label.startswith("extended") else None,
            )
            results.append(result)
            print(eid, label, result, flush=True)
        manifest = {
            "experiment_id": eid,
            "origin_llh": ctx.origin,
            "frame": "True ENU in meters, shared with absolute_position",
            "models": results,
            "code_sha256": sha256(Path(__file__)),
            "git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
            "config_sha256": sha256(ctx.cfg_path),
            "inputs_sha256": {
                str(p): sha256(p)
                for p in [
                    *ctx.inputs,
                    Path(f"results/{eid}/absolute_position/positions.csv"),
                    Path(f"results/{eid}/evaluation/acoustic_depth.csv"),
                    Path(f"results/{eid}/visual_mapping/frame_times.csv"),
                    *(
                        [out / "frame_times.csv", out / "bridge_manifest.json"]
                        if args.use_bridge
                        else []
                    ),
                    *(
                        [out / "extension_manifest.json"]
                        if (out / "extension_manifest.json").exists()
                        else []
                    ),
                ]
            },
            "limitations": [
                "Registration residuals are not independent accuracy.",
                "All fragments kept; non-observable scale or translation is explicitly unplaced.",
                "Pressure scale assumes the saved child IMU point approximates pressure location.",
                "Dynamic bubbles/boat scenes are not validated static seafloor maps.",
            ],
        }
        (out / "manifest.json").write_text(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
