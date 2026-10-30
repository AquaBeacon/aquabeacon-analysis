from __future__ import annotations

import cv2
import numpy as np


def detect_candidate(frame: np.ndarray) -> dict:
    """Find a saturated compact image region; confidence is heuristic, not probability."""
    mask = (np.min(frame, axis=2) >= 245).astype(np.uint8)
    n, _, stats, centers = cv2.connectedComponentsWithStats(mask, 8)
    h, w = mask.shape
    candidates = []
    for i in range(1, n):
        x, y, bw, bh, area = map(int, stats[i])
        if area < 20:
            continue
        edge = x == 0 or y == 0 or x + bw >= w or y + bh >= h
        fill = area / (bw * bh)
        confidence = min(1.0, area / 500.0) * min(1.0, fill / 0.5)
        candidates.append((area, centers[i], edge, confidence, (x, y, bw, bh)))
    if not candidates:
        return {
            "x_px": None,
            "y_px": None,
            "confidence": 0.0,
            "status": "missing",
            "reason": "no_saturated_component",
            "bbox": None,
        }
    area, center, edge, confidence, bbox = max(candidates, key=lambda a: a[0])
    return {
        "x_px": float(center[0]),
        "y_px": float(center[1]),
        "confidence": float(confidence),
        "status": "excluded" if edge else "candidate",
        "reason": "touches_image_edge" if edge else "unverified_bright_component",
        "bbox": bbox,
    }
