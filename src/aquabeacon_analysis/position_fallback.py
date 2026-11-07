"""Causal ENU position fallbacks using depth and current rotated lever arms.

Range and depth do not observe azimuth. The acoustic fallback explicitly holds
the last independent optical acoustic-point bearing fixed in world coordinates;
IMU yaw is never interpreted as a target bearing and acceleration is not integrated.
Surface seeds and bearing anchors are updated only *after* evaluating each row.
"""

from collections import deque

import numpy as np

DEFAULT_CONFIG = {
    "min_depth_m": 0.3,
    "min_vertical_ray": 0.15,
    "max_optical_distance_m": 50.0,
    "max_bearing_age_s": 3.0,
    "max_surface_age_s": 60.0,
    "surface_window_s": 30.0,
    "min_surface_samples": 3,
}


def run_fallback(
    times, joint_positions, rays, cameras, transmitters, light_offsets,
    acoustic_offsets, pressure_offsets, ranges, depths, surface_reference_z, *, config=None,
    surface_seed_mask=None,
):
    """Return positions and prior-only diagnostics for strictly increasing times.

    All vectors are N x 3 ENU metres relative to the parent body origin, except
    rays (directions). Lever arms must already use each row's IMU orientation.
    Scalars are length N; NaN/nonfinite geometry invalidates only relevant methods.
    Depth is positive down. ``surface_reference_z`` must share the same reference
    as joint Z. Surface offset seeds are joint_z + pressure_offset_z + depth -
    reference_z. Their median window is relative to the latest *prior seed*, so
    a short seed burst remains usable until max_surface_age_s, even during gaps.
    The returned anchor/age arrays describe priors before the current row update.
    Valid original joints always win, even when depth or geometry is missing.
    ``surface_seed_mask`` optionally selects independent acoustic event rows for
    surface learning, avoiding duplicated seeds when an event is resampled.
    """
    cfg = dict(DEFAULT_CONFIG)
    if config is not None:
        unknown = set(config) - set(cfg)
        if unknown:
            raise ValueError(f"Unknown fallback config: {sorted(unknown)}")
        cfg.update(config)
    if any(not np.isfinite(value) or value < 0 for value in cfg.values()):
        raise ValueError("Fallback configuration must contain finite nonnegative values")
    samples = cfg["min_surface_samples"]
    if samples < 1 or int(samples) != samples:
        raise ValueError("min_surface_samples must be a positive integer")
    if cfg["min_vertical_ray"] > 1 or cfg["max_optical_distance_m"] <= 0:
        raise ValueError("Invalid ray threshold or maximum optical distance")

    times = np.asarray(times, dtype=float)
    if times.ndim != 1 or not np.isfinite(times).all() or np.any(np.diff(times) <= 0):
        raise ValueError("times must be a finite, strictly increasing one-dimensional array")
    n = len(times)
    if surface_seed_mask is None:
        surface_seed_mask = np.ones(n, dtype=bool)
    else:
        surface_seed_mask = np.asarray(surface_seed_mask)
        if surface_seed_mask.shape != (n,) or surface_seed_mask.dtype.kind != "b":
            raise ValueError("surface_seed_mask must be a length N boolean array")

    def array(value, shape, name):
        result = np.asarray(value, dtype=float)
        if result.shape != shape:
            raise ValueError(f"{name} must have shape {shape}, got {result.shape}")
        return result

    vectors = [
        array(value, (n, 3), name) for value, name in (
            (joint_positions, "joint_positions"), (rays, "rays"), (cameras, "cameras"),
            (transmitters, "transmitters"), (light_offsets, "light_offsets"),
            (acoustic_offsets, "acoustic_offsets"), (pressure_offsets, "pressure_offsets"),
        )
    ]
    joint, rays, cameras, transmitters, light_offsets, acoustic_offsets, pressure_offsets = vectors
    ranges = array(ranges, (n,), "ranges")
    depths = array(depths, (n,), "depths")
    reference = array(surface_reference_z, (n,), "surface_reference_z")
    valid_joint = np.isfinite(joint).all(axis=1)
    out = {key: np.full((n, 3), np.nan) for key in (
        "joint", "optical_imu_depth", "acoustic_imu_depth", "selected",
    )}
    out["joint"][valid_joint] = joint[valid_joint]
    for key in ("bearing_anchor_time_s", "bearing_age_s", "surface_anchor_time_s",
                "surface_age_s", "surface_offset_m"):
        out[key] = np.full(n, np.nan)
    out["surface_samples"] = np.zeros(n, dtype=int)
    out["method"] = np.full(n, "none", dtype="U24")
    out["optical_rejection_reason"] = np.full(n, "", dtype="U40")
    out["acoustic_rejection_reason"] = np.full(n, "", dtype="U40")
    out["acoustic_bearing_model"] = np.full(n, "held_world_optical_bearing", dtype="U32")
    seeds = deque()
    bearing = None
    bearing_time = np.nan

    for i, time in enumerate(times):
        surface = np.nan
        if seeds:
            latest = seeds[-1][0]
            out["surface_anchor_time_s"][i] = latest
            out["surface_age_s"][i] = time - latest
            out["surface_samples"][i] = len(seeds)
            if len(seeds) >= samples and time - latest <= cfg["max_surface_age_s"]:
                surface = float(np.median([seed[1] for seed in seeds]))
                out["surface_offset_m"][i] = surface
        if bearing is not None:
            out["bearing_anchor_time_s"][i] = bearing_time
            out["bearing_age_s"][i] = time - bearing_time

        common_reason = ""
        if not np.isfinite(depths[i]) or depths[i] <= cfg["min_depth_m"]:
            common_reason = "invalid_depth"
        elif not np.isfinite(reference[i]) or not np.isfinite(pressure_offsets[i]).all():
            common_reason = "invalid_depth_geometry"
        elif not np.isfinite(surface):
            common_reason = "surface_unavailable"

        optical_reason = common_reason
        acoustic_reason = common_reason
        if not common_reason:
            pressure_z = reference[i] + surface - depths[i]
            if not all(np.isfinite(v).all() for v in (rays[i], cameras[i], light_offsets[i])):
                optical_reason = "invalid_optical_geometry"
            else:
                norm = np.linalg.norm(rays[i])
                ray = rays[i] / norm if norm > 0 and np.isfinite(norm) else np.zeros(3)
                if norm == 0 or not np.isfinite(norm):
                    optical_reason = "invalid_ray"
                elif abs(ray[2]) < cfg["min_vertical_ray"] or ray[2] == 0:
                    optical_reason = "near_horizontal_ray"
                else:
                    light_z = pressure_z - pressure_offsets[i, 2] + light_offsets[i, 2]
                    distance = (light_z - cameras[i, 2]) / ray[2]
                    if distance <= 0:
                        optical_reason = "backward_ray"
                    elif distance > cfg["max_optical_distance_m"]:
                        optical_reason = "optical_distance_exceeded"
                    else:
                        out["optical_imu_depth"][i] = (
                            cameras[i] + distance * ray - light_offsets[i]
                        )

            if not all(np.isfinite(v).all() for v in (transmitters[i], acoustic_offsets[i])):
                acoustic_reason = "invalid_acoustic_geometry"
            elif not np.isfinite(ranges[i]) or ranges[i] <= 0:
                acoustic_reason = "invalid_range"
            else:
                acoustic_z = pressure_z - pressure_offsets[i, 2] + acoustic_offsets[i, 2]
                dz = acoustic_z - transmitters[i, 2]
                horizontal_squared = ranges[i] ** 2 - dz ** 2
                if horizontal_squared < 0:
                    acoustic_reason = "impossible_sphere_depth"
                elif bearing is None:
                    acoustic_reason = "bearing_unavailable"
                elif time - bearing_time > cfg["max_bearing_age_s"]:
                    acoustic_reason = "bearing_expired"
                else:
                    delta = np.r_[np.sqrt(horizontal_squared) * bearing, dz]
                    out["acoustic_imu_depth"][i] = transmitters[i] + delta - acoustic_offsets[i]

        out["optical_rejection_reason"][i] = optical_reason
        out["acoustic_rejection_reason"][i] = acoustic_reason
        for method in ("joint", "optical_imu_depth", "acoustic_imu_depth"):
            if np.isfinite(out[method][i]).all():
                out["selected"][i] = out[method][i]
                out["method"][i] = method
                break

        # No acoustic self-refresh: only current independent optical information.
        anchor = out["joint"][i] if valid_joint[i] else out["optical_imu_depth"][i]
        if all(np.isfinite(v).all() for v in (anchor, acoustic_offsets[i], transmitters[i])):
            horizontal = (anchor + acoustic_offsets[i] - transmitters[i])[:2]
            norm = np.linalg.norm(horizontal)
            if norm > 0 and np.isfinite(norm):
                bearing = horizontal / norm
                bearing_time = time
        if (surface_seed_mask[i] and valid_joint[i] and np.isfinite(pressure_offsets[i]).all()
                and np.isfinite(depths[i]) and depths[i] > cfg["min_depth_m"]
                and np.isfinite(reference[i])):
            seed = joint[i, 2] + pressure_offsets[i, 2] + depths[i] - reference[i]
            seeds.append((time, seed))
            while seeds and time - seeds[0][0] > cfg["surface_window_s"]:
                seeds.popleft()
    return out
