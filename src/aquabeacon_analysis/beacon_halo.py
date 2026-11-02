"""Image-reviewed ROI light detector; identity still needs image review."""

import cv2
import numpy as np


def detect_halo(frame, roi_center=None, roi_radius=100, allow_white_core=True):
    """Distinguish a cyan light halo from saturated yellow/white tether highlights.

    Detect at fixed half-resolution for reproducibility. The cyan halo proposes
    a location; local achromatic intensity locates the actual luminous center.
    Never use position RMSE or attitude to select a light.
    """
    small = cv2.resize(frame, (640, 360), interpolation=cv2.INTER_AREA).astype(np.float32)
    b, g, r = cv2.split(small)
    cyan = np.maximum(0, np.minimum(b, g) - r)
    score = cv2.GaussianBlur(cyan, (0, 0), 2) - cv2.GaussianBlur(cyan, (0, 0), 15)
    allowed = np.ones((360, 640), bool)
    score[:12] = score[-12:] = -np.inf
    score[:, :12] = score[:, -12:] = -np.inf
    if roi_center is not None:
        yy, xx = np.mgrid[:360, :640]
        cx, cy = np.asarray(roi_center) / 2
        allowed = (xx - cx) ** 2 + (yy - cy) ** 2 <= (roi_radius / 2) ** 2
        score[~allowed] = -np.inf
    # A broad white core near the reviewed beacon wins over color, which becomes
    # weak under saturation. Slender tether highlights are excluded by shape.
    # Find the luminous core in the full image, then gate its center by ROI.
    # Clipping a broad halo to the ROI would return the search-center itself.
    balanced_bright = (np.mean(small, axis=2) >= 245) & (np.ptp(small, axis=2) < 30)
    mask = balanced_bright.astype(np.uint8)
    count, _, stats, centers = cv2.connectedComponentsWithStats(mask, 8)
    compact = [
        i
        for i in range(1, count)
        if stats[i, 4] >= 8
        and max(stats[i, 2:4]) / min(stats[i, 2:4]) < 2
        and stats[i, 4] / (stats[i, 2] * stats[i, 3]) > 0.3
        and allowed[round(centers[i, 1]), round(centers[i, 0])]
    ]
    if compact and allow_white_core:
        best = max(compact, key=lambda i: stats[i, 4])
        x, y = centers[best]
        return {
            "x_px": float((x + 0.5) * 2 - 0.5),
            "y_px": float((y + 0.5) * 2 - 0.5),
            "halo_score": float(stats[best, 4]),
            "core_contrast": float(255 - np.median(small)),
            "candidate": True,
            "method": "reviewed_ROI_compact_white_core",
        }
    _, peak, _, point = cv2.minMaxLoc(score)
    x, y = point
    intensity = cyan[y - 4 : y + 5, x - 4 : x + 5]
    background = float(
        np.median(np.r_[intensity[0], intensity[-1], intensity[:, 0], intensity[:, -1]])
    )
    weights = np.maximum(intensity - background, 0) ** 2
    contrast = float(intensity.max() - background)
    if weights.sum() > 0:
        yy, xx = np.mgrid[: weights.shape[0], : weights.shape[1]]
        x = x - 4 + float(np.sum(xx * weights) / weights.sum())
        y = y - 4 + float(np.sum(yy * weights) / weights.sum())
    luminance = np.mean(small, axis=2)
    light_contrast = cv2.GaussianBlur(luminance, (0, 0), 1) - cv2.GaussianBlur(luminance, (0, 0), 8)
    luminous = float(light_contrast[round(y), round(x)])
    accepted = peak >= 1 and contrast >= 2 and luminous >= 2
    return {
        "x_px": float((x + 0.5) * 2 - 0.5),
        "y_px": float((y + 0.5) * 2 - 0.5),
        "halo_score": float(peak),
        "core_contrast": contrast,
        "candidate": bool(accepted),
        "method": "reviewed_ROI_cyan_DoG_sigma2_15; local cyan centroid",
    }
