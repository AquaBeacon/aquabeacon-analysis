import cv2
import numpy as np

from aquabeacon_analysis.beacon_halo import detect_halo


def test_cyan_beacon_beats_large_yellow_tether():
    yy, xx = np.mgrid[:720, :1280]
    base = np.empty((720, 1280, 3), np.float32)
    base[:] = [70, 45, 15]
    glow = np.exp(-((xx - 330) ** 2 + (yy - 310) ** 2) / (2 * 25**2))
    base += glow[..., None] * [180, 205, 170]
    base = np.clip(base, 0, 255).astype(np.uint8)
    cv2.line(base, (900, 700), (700, 20), (255, 255, 255), 8)
    cv2.line(base, (890, 700), (690, 20), (20, 255, 255), 8)
    result = detect_halo(base)
    assert result["candidate"]
    assert abs(result["x_px"] - 330) < 8
    assert abs(result["y_px"] - 310) < 8


def test_uniform_water_is_not_a_light():
    frame = np.full((720, 1280, 3), [70, 45, 15], dtype=np.uint8)
    assert not detect_halo(frame)["candidate"]


def test_bright_halo_is_not_clipped_to_search_center():
    yy, xx = np.mgrid[:720, :1280]
    radius = (xx - 330) ** 2 + (yy - 300) ** 2
    halo = 170 + 65 * np.exp(-radius / (2 * 40**2)) + 60 * np.exp(-radius / (2 * 4**2))
    frame = np.repeat(np.clip(halo, 0, 255).astype(np.uint8)[..., None], 3, axis=2)
    result = detect_halo(frame, [330, 314], 20)
    assert result["candidate"]
    assert np.linalg.norm(np.array([result["x_px"], result["y_px"]]) - [330, 300]) < 2
