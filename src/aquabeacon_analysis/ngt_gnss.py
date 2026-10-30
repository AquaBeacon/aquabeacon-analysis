"""Offline UBX/RTCM audit and GNSS repeatability utilities for explicit experiments."""
from __future__ import annotations

import struct
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import numpy as np


def ubx_messages(data: bytes):
    """Yield checksum-valid UBX frames; reject corrupt or truncated source data."""
    offset = 0
    while offset < len(data):
        if data[offset:offset + 2] != b"\xb5\x62" or offset + 8 > len(data):
            raise ValueError(f"Invalid UBX frame at byte {offset}")
        length = int.from_bytes(data[offset + 4:offset + 6], "little")
        end = offset + length + 8
        if end > len(data):
            raise ValueError(f"Truncated UBX frame at byte {offset}")
        a = b = 0
        for value in data[offset + 2:end - 2]:
            a = (a + value) & 255
            b = (b + a) & 255
        if data[end - 2:end] != bytes([a, b]):
            raise ValueError(f"UBX checksum mismatch at byte {offset}")
        yield data[offset + 2], data[offset + 3], data[offset + 6:end - 2]
        offset = end


def decode_nav_pvt(payload: bytes) -> dict:
    """Decode NAV-PVT, retaining validity and receiver uncertainty separately."""
    if len(payload) < 92:
        raise ValueError("NAV-PVT requires 92 bytes")
    year, month, day, hour, minute, second = struct.unpack_from("<H5B", payload, 4)
    try:
        utc_s = datetime(year, month, day, hour, minute, second, tzinfo=UTC).timestamp()
        utc_s += struct.unpack_from("<i", payload, 16)[0] * 1e-9
    except ValueError:
        utc_s = float("nan")
    lon, lat, height, msl = struct.unpack_from("<4i", payload, 24)
    ha, va = struct.unpack_from("<2I", payload, 40)
    flags = payload[21]
    return {"utc_s": utc_s, "itow_ms": struct.unpack_from("<I", payload)[0],
            "latitude_deg": lat * 1e-7, "longitude_deg": lon * 1e-7,
            "height_ellipsoid_m": height * .001, "height_msl_m": msl * .001,
            "h_accuracy_m": ha * .001, "v_accuracy_m": va * .001,
            "fix_type": payload[20], "fix_valid": bool(flags & 1),
            "carrier_solution": (flags >> 6) & 3, "differential": bool(flags & 2),
            "satellites": payload[23], "utc_valid": (payload[11] & 3) == 3}


def audit_ubx(path: Path) -> tuple[dict, list[dict]]:
    counts: Counter = Counter()
    positions = []
    raw = []
    for cls, msg, payload in ubx_messages(path.read_bytes()):
        counts[f"{cls:02x}-{msg:02x}"] += 1
        if (cls, msg) == (1, 7):
            positions.append(decode_nav_pvt(payload))
        elif (cls, msg) == (2, 21):
            tow, week, leap, number = struct.unpack_from("<dHbB", payload)
            if len(payload) != 16 + number * 32:
                raise ValueError("Malformed RAWX")
            raw.append({"gps_week": week, "gps_tow_s": tow, "leap_seconds": leap,
                        "signals": number, "leap_seconds_valid": bool(payload[12] & 1)})
    return {"messages": dict(counts), "rawx_first": raw[0] if raw else None,
            "rawx_last": raw[-1] if raw else None, "checksum_errors": 0}, positions


def crc24q(data: bytes) -> int:
    crc = 0
    for value in data:
        crc ^= value << 16
        for _ in range(8):
            crc <<= 1
            if crc & 0x1000000:
                crc ^= 0x1864CFB
    return crc & 0xFFFFFF


def audit_rtcm(path: Path) -> dict:
    data = path.read_bytes()
    counts: Counter = Counter()
    offset = bad = 0
    while offset < len(data):
        if offset + 6 > len(data) or data[offset] != 211:
            bad += 1
            offset += 1
            continue
        length = ((data[offset + 1] & 3) << 8) | data[offset + 2]
        end = offset + length + 6
        if end > len(data):
            bad += 1
            break
        if crc24q(data[offset:end - 3]) != int.from_bytes(data[end - 3:end], "big"):
            bad += 1
        else:
            counts[str(int.from_bytes(data[offset + 3:offset + 5], "big") >> 4)] += 1
        offset = end
    return {"bytes": len(data), "valid_message_counts": dict(counts), "invalid_frames": bad}


def centered_scatter(enu_m: np.ndarray) -> dict:
    """Population RMS about the window mean; no ground-truth accuracy claim."""
    points = np.asarray(enu_m, dtype=float)
    if points.ndim != 2 or points.shape[1] != 3 or not len(points):
        raise ValueError("Need nonempty N x 3 ENU points")
    if not np.isfinite(points).all():
        raise ValueError("Nonfinite ENU points must be handled explicitly")
    center = points.mean(axis=0)
    residuals = points - center
    return {"count": len(points), "mean_enu_m": center.tolist(),
            "rms_e_m": float(np.sqrt(np.mean(residuals[:, 0] ** 2))),
            "rms_n_m": float(np.sqrt(np.mean(residuals[:, 1] ** 2))),
            "rms_u_m": float(np.sqrt(np.mean(residuals[:, 2] ** 2))),
            "rms_horizontal_m": float(np.sqrt(np.mean(np.sum(residuals[:, :2] ** 2, axis=1)))),
            "rms_3d_m": float(np.sqrt(np.mean(np.sum(residuals ** 2, axis=1))))}
