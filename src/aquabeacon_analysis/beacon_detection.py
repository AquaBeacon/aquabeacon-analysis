"""Color-independent, multiscale luminous-core detection in a reviewed image ROI.

These are image measurements, not beacon identity or position ground truth.
All coordinates use the input image basis, including pixel-center resampling.
"""

from dataclasses import dataclass

import cv2
import numpy as np


@dataclass(frozen=True)
class BeaconDetectionConfig:
    work_width: int = 640
    scales: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0)
    min_contrast: float = 4.0
    min_response: float = 2.0
    min_curvature_ratio: float = 0.12
    max_aspect: float = 3.0
    edge_margin: int = 6
    max_candidates: int = 24


def missing_detection(reason="no_compact_luminous_core"):
    return {"candidate": False, "x_px": np.nan, "y_px": np.nan,
            "core_contrast": np.nan, "halo_score": np.nan,
            "method": "multiscale_luminous_core", "reason": reason}


def luminous_candidates(frame, roi_center=None, roi_radius=100, config=None):
    """Find local intensity peaks with curvature in both image directions.

    Background subtraction handles exposure and colored light. Curvature and
    connected-core shape reject slender ropes. ROI gates completed components;
    it never clips a component to bias its centroid toward a prediction.
    """
    cfg = config or BeaconDetectionConfig()
    if frame.ndim != 3 or frame.shape[2] != 3 or frame.dtype != np.uint8:
        raise ValueError("Expected uint8 BGR image")
    if not np.isfinite(roi_radius) or roi_radius <= 0:
        raise ValueError("ROI radius must be finite and positive")
    h, w = frame.shape[:2]
    if min(h, w) < 16:
        return []
    scale = min(1.0, cfg.work_width / max(h, w))
    sw, sh = round(w * scale), round(h * scale)
    sx, sy = w / sw, h / sh
    small = cv2.resize(frame, (sw, sh), interpolation=cv2.INTER_AREA)
    gray = small.astype(np.float32).mean(axis=2)
    allowed = np.ones((sh, sw), bool)
    margin = cfg.edge_margin
    allowed[:margin] = allowed[-margin:] = False
    allowed[:, :margin] = allowed[:, -margin:] = False
    yy, xx = np.mgrid[:sh, :sw]
    if roi_center is not None:
        center = np.asarray(roi_center, float)
        if center.shape != (2,) or not np.isfinite(center).all():
            raise ValueError("ROI center must contain two finite coordinates")
        allowed &= ((xx + 0.5) * sx - 0.5 - center[0]) ** 2 + (
            (yy + 0.5) * sy - 0.5 - center[1]) ** 2 <= roi_radius**2
    if not allowed.any():
        return []
    proposals = []
    for sigma in cfg.scales:
        smooth = cv2.GaussianBlur(gray, (0, 0), sigma)
        background = cv2.GaussianBlur(gray, (0, 0), sigma * 4)
        response = smooth - background
        peaks = allowed & (response >= cfg.min_response)
        peaks &= response >= cv2.dilate(response, np.ones((3, 3), np.uint8))
        ys, xs = np.where(peaks)
        order = np.argsort(response[ys, xs])[::-1][:cfg.max_candidates]
        for j in order:
            x, y = int(xs[j]), int(ys[j])
            # A line has almost no curvature along its tangent.
            step = max(1, round(sigma))
            if not step <= x < sw-step or not step <= y < sh-step:
                continue
            dxx = smooth[y, x+step] + smooth[y, x-step] - 2*smooth[y, x]
            dyy = smooth[y+step, x] + smooth[y-step, x] - 2*smooth[y, x]
            dxy = (smooth[y+step, x+step] - smooth[y+step, x-step]
                   - smooth[y-step, x+step] + smooth[y-step, x-step]) / 4
            eig = np.linalg.eigvalsh([[dxx, dxy], [dxy, dyy]])
            ratio = float(eig[1] / eig[0]) if eig[0] < 0 and eig[1] < 0 else 0
            if ratio < cfg.min_curvature_ratio:
                continue
            radius = max(5, int(np.ceil(3*sigma)))
            x0, x1 = max(0, x-radius), min(sw, x+radius+1)
            y0, y1 = max(0, y-radius), min(sh, y+radius+1)
            patch = gray[y0:y1, x0:x1]
            bg = float(background[y, x])
            peak = float(patch.max())
            contrast = peak - bg
            if contrast < cfg.min_contrast:
                continue
            core = (patch >= bg + 0.55 * contrast).astype(np.uint8)
            count, labels, stats, centers = cv2.connectedComponentsWithStats(core, 8)
            choices = [i for i in range(1, count)
                       if np.linalg.norm(centers[i] - [x-x0, y-y0]) <= max(3, sigma*1.5)]
            if not choices:
                continue
            index = min(choices, key=lambda i: np.linalg.norm(centers[i]-[x-x0, y-y0]))
            left, top, bw, bh, area = stats[index]
            if (max(bw, bh)/min(bw, bh) > cfg.max_aspect or area < 2
                    or left == 0 or top == 0 or left+bw >= patch.shape[1]
                    or top+bh >= patch.shape[0]):
                continue
            weights = np.maximum(patch - bg, 0) * (labels == index)
            cy, cx = np.mgrid[:patch.shape[0], :patch.shape[1]]
            px = (x0 + float((cx*weights).sum()/weights.sum()) + 0.5)*sx-0.5
            py = (y0 + float((cy*weights).sum()/weights.sum()) + 0.5)*sy-0.5
            if roi_center is not None and np.linalg.norm(np.array([px, py])-center) > roi_radius:
                continue
            strength = float(response[y, x]) * np.sqrt(ratio)
            proposals.append({"candidate": True, "x_px": px, "y_px": py,
                              "core_contrast": contrast, "halo_score": strength,
                              "curvature_ratio": ratio, "scale_px": sigma*sx,
                              "area_px": int(area)*sx*sy,
                              "method": "multiscale_luminous_core",
                              "reason": "unverified_image_component"})
    output = []
    for item in sorted(proposals, key=lambda p: p["halo_score"], reverse=True):
        if all(np.hypot(item["x_px"]-p["x_px"], item["y_px"]-p["y_px"]) > 6
               for p in output):
            output.append(item)
    return output[:cfg.max_candidates]


def detect_luminous_core(frame, roi_center=None, roi_radius=100, config=None):
    candidates = luminous_candidates(frame, roi_center, roi_radius, config)
    return candidates[0] if candidates else missing_detection()


def detect_beacon(frame, roi_center=None, roi_radius=100, allow_white_core=True):
    """Prefer the luminous core, corroborate dim cores with cyan evidence.

    Used with an image-reviewed seed and fixed appearance tracking. It does not
    authorize automatic global beacon identification or return predictions.
    """
    from aquabeacon_analysis.beacon_halo import detect_halo

    if frame.ndim != 3 or frame.shape[2] != 3 or frame.dtype != np.uint8:
        raise ValueError("Expected uint8 BGR image")
    if not np.isfinite(roi_radius) or roi_radius <= 0:
        raise ValueError("ROI radius must be finite and positive")
    if roi_center is not None:
        point = np.asarray(roi_center, float)
        if point.shape != (2,) or not np.isfinite(point).all():
            raise ValueError("ROI center must contain two finite coordinates")
        h, w = frame.shape[:2]
        if not (24 <= point[0] < w-24 and 24 <= point[1] < h-24):
            # The legacy detector assumes a nonempty 9x9 interior patch.
            return detect_luminous_core(frame, roi_center, roi_radius)
    core = detect_luminous_core(frame, roi_center, roi_radius)
    if core["candidate"]:
        return core
    # Cyan alone can select an illuminated rope beside a magenta source. It
    # therefore corroborates a dim compact intensity peak, never overrides one.
    if frame.shape[:2] == (720, 1280):
        old = detect_halo(frame, roi_center, roi_radius, allow_white_core)
        point = np.array([old["x_px"], old["y_px"]])
        if old["candidate"] and (roi_center is None
                or np.linalg.norm(point - roi_center) <= roi_radius):
            weak = detect_luminous_core(
                frame, point, min(roi_radius, 8),
                BeaconDetectionConfig(scales=(1.0, 2.0, 4.0), min_contrast=2, min_response=.8))
            measured = np.array([weak["x_px"], weak["y_px"]])
            if weak["candidate"] and (roi_center is None
                    or np.linalg.norm(measured-roi_center) <= roi_radius):
                weak["method"] = "cyan_confirmed_dim_luminous_core"
                return weak
    return missing_detection()
