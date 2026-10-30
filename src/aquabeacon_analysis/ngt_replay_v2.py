"""Pure temporal/frame operations and read-only COLMAP replay decoding."""
from __future__ import annotations

import struct
from pathlib import Path

import numpy as np
from scipy.spatial.transform import Rotation, Slerp

R_ENU_FROM_NED = np.array([[0., 1., 0.], [1., 0., 0.], [0., 0., -1.]])


def utc_to_video(utc_s, utc_at_video_zero_s, time_scale):
    """Map recorded UTC to parent video seconds with its fitted clock only."""
    if not np.isfinite(time_scale) or time_scale <= 0:
        raise ValueError("Clock time_scale must be positive and finite")
    return (np.asarray(utc_s) - utc_at_video_zero_s) / time_scale


def enu_quaternions(ned_wxyz):
    """Convert body FRD→magnetic NED wxyz to FRD→ENU, without declination."""
    q = np.asarray(ned_wxyz, dtype=float)
    norm = np.linalg.norm(q, axis=-1)
    if not np.isfinite(q).all() or np.any(np.abs(norm - 1) > .05):
        raise ValueError("Nonfinite or nonunit quaternion")
    rotation = Rotation.from_matrix(R_ENU_FROM_NED) * Rotation.from_quat(
        (q / norm[..., None])[..., [1, 2, 3, 0]])
    return rotation.as_quat()[..., [3, 0, 1, 2]]


def compose_cad_quaternions(frd_to_enu_wxyz, frd_from_cad):
    """R_ENU_CAD = R_ENU_FRD @ R_FRD_CAD; visual alignment is caller metadata."""
    matrix = np.asarray(frd_from_cad)
    if (not np.allclose(matrix @ matrix.T, np.eye(3), atol=1e-8)
            or not np.isclose(np.linalg.det(matrix), 1)):
        raise ValueError("CAD alignment must be a proper rotation")
    q = np.asarray(frd_to_enu_wxyz)
    return (Rotation.from_quat(q[..., [1, 2, 3, 0]])
            * Rotation.from_matrix(matrix)).as_quat()[..., [3, 0, 1, 2]]


def sample_attitudes(times, quaternions, valid, query, *, max_gap_s=.1,
                     nearest_tolerance_s=.055):
    """Bounded SLERP or explicitly flagged nearest match; never extrapolate.

    Invalid rows remain barriers to interpolation. A nearest match is an observed
    sample within a separately declared tolerance, not gap-filling interpolation.
    """
    times, quaternions = np.asarray(times), np.asarray(quaternions)
    valid, query = np.asarray(valid, bool), np.asarray(query)
    if len(times) < 2 or np.any(np.diff(times) <= 0):
        raise ValueError("At least two strictly increasing source times required")
    result = np.full((len(query), 4), np.nan)
    residual = np.full(len(query), np.nan)
    method = ["missing"] * len(query)
    for j, target in enumerate(query):
        if target < times[0] or target > times[-1]:
            continue
        hi = int(np.searchsorted(times, target))
        candidates = [i for i in (hi - 1, hi) if 0 <= i < len(times) and valid[i]]
        if not candidates:
            continue
        nearest = min(candidates, key=lambda i: abs(times[i] - target))
        delta = float(times[nearest] - target)
        if abs(delta) < 1e-6:
            result[j], residual[j], method[j] = quaternions[nearest], delta, "exact"
        elif (0 < hi < len(times) and valid[hi - 1] and valid[hi]
              and times[hi] - times[hi - 1] <= max_gap_s + 1e-10):
            rotation = Rotation.from_quat(quaternions[hi - 1:hi + 1, [1, 2, 3, 0]])
            result[j] = Slerp(times[hi - 1:hi + 1], rotation)([target]).as_quat()[0,
                                                                                          [3, 0, 1, 2]]
            residual[j], method[j] = 0., "slerp"
        elif abs(delta) <= nearest_tolerance_s:
            result[j], residual[j], method[j] = quaternions[nearest], delta, "nearest"
    return result, residual, method


def observation_times(track_image_ids, image_names, frame_times):
    """First/second distinct observing-image times in a final point's track.

    A second observation is a defensible retrospective reveal convention; it
    does not recover optimizer insertion time or the evolving online point xyz.
    """
    values = sorted({float(frame_times[image_names[i]]) for i in track_image_ids})
    if len(values) < 2 or not np.isfinite(values).all():
        raise ValueError("Point requires two distinct finite observing image times")
    return values[0], values[1], values[-1], len(values)


def read_image_names(path: Path):
    """Read COLMAP binary image IDs/names without loading image point arrays."""
    names = {}
    with path.open("rb") as stream:
        count = struct.unpack("<Q", stream.read(8))[0]
        for _ in range(count):
            values = struct.unpack("<i7di", stream.read(64))
            name = bytearray()
            while (char := stream.read(1)) != b"\0":
                if not char:
                    raise ValueError("Truncated COLMAP image name")
                name.extend(char)
            names[values[0]] = name.decode("utf-8")
            points = struct.unpack("<Q", stream.read(8))[0]
            stream.seek(points * 24, 1)
    return names


def read_points(path: Path):
    """Yield original ID, float64 xyz, uint8 rgb and track from points3D.bin."""
    with path.open("rb") as stream:
        count = struct.unpack("<Q", stream.read(8))[0]
        for _ in range(count):
            values = struct.unpack("<Q3d3BdQ", stream.read(51))
            track = np.frombuffer(stream.read(values[-1] * 8), dtype="<i4").reshape(-1, 2)
            yield values[0], values[1:4], values[4:7], track[:, 0]
