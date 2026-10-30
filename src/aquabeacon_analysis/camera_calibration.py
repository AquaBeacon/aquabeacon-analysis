"""Resolve the user-selected shared underwater intrinsics for either camera."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np


def load_camera_intrinsics(root: Path, device_id: str, image_size: tuple[int, int],
                           source_kind: str = 'main', override: Path | None = None):
    """Return K, distortion, provenance for a full-frame resize, never a crop.

    This selects intrinsics only. It does not certify the capture medium, camera
    mode, optical/IMU rotation, or the field accuracy of a provisional calibration.
    """
    if device_id not in ('C_MAIN_CAM', 'P_MAIN_CAM'):
        raise ValueError('Unknown camera device')
    defaults_path = root/'calibration/camera_defaults.json'
    defaults = json.loads(defaults_path.read_text(encoding='utf-8'))
    path = override or Path(defaults['underwater_intrinsics'][device_id])
    path = path if path.is_absolute() else root/path
    calibration = json.loads(path.read_text(encoding='utf-8'))
    if source_kind == 'main':
        size = calibration['image_size_px']
        k = np.asarray(calibration['camera_matrix_px'], dtype=float)
        distortion = calibration['distortion_coefficients']
    elif source_kind == 'proxy':
        size = calibration['proxy_image_size_px']
        k = np.asarray(calibration['proxy_direct_camera_matrix_px'], dtype=float)
        distortion = calibration['proxy_direct_distortion_coefficients']
    else:
        raise ValueError('source_kind must be main or proxy')
    scales = np.asarray(image_size)/size
    if (min(image_size) <= 0 or not np.isclose(scales[0], scales[1], rtol=1e-6)
            or k.shape != (3, 3)):
        raise ValueError('Only an aspect-preserving full-frame resize is supported')
    k[:2] *= scales[:, None]
    return k, np.asarray(distortion), {
        'device_id': device_id, 'source': str(path),
        'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
        'defaults_sha256': hashlib.sha256(defaults_path.read_bytes()).hexdigest(),
        'source_kind': source_kind, 'calibrated_size': size, 'output_size': list(image_size),
        'camera_matrix_px': k.tolist(), 'distortion': list(distortion),
        'status': calibration['status'], 'required_conditions': defaults['conditions'],
        'capture_conditions_independently_verified': False,
    }
