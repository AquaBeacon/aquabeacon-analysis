"""Image-only beacon tracking with flow and fixed appearance matching."""

import cv2
import numpy as np

from aquabeacon_analysis.beacon_halo import detect_halo


class BeaconTracker:
    """Flow predicts a search region; image-match coordinates are never smoothed."""

    def __init__(self, seed=None):
        self.point = np.asarray(seed, float) if seed is not None else None
        self.gray = None
        self.template = None
        self.allow_white_core = True
        self.missing = 0

    def update(self, frame, seed=None):
        cv2.setNumThreads(1)
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        predicted = self.point.copy() if self.point is not None else None
        flow_ok, fb_error, flow_points = False, np.nan, 0
        if self.gray is not None and self.point is not None and self.missing < 10:
            delta = np.array([[x, y] for y in [-5, 0, 5] for x in [-5, 0, 5]])
            p0 = (self.point + delta).astype(np.float32).reshape(-1, 1, 2)
            options = {
                "winSize": (31, 31),
                "maxLevel": 3,
                "criteria": (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, 0.01),
                "minEigThreshold": 1e-6,
            }
            p1, status, _ = cv2.calcOpticalFlowPyrLK(self.gray, gray, p0, None, **options)
            pback, back_status, _ = cv2.calcOpticalFlowPyrLK(gray, self.gray, p1, None, **options)
            error = np.linalg.norm((pback - p0).reshape(-1, 2), axis=1)
            good = status[:, 0].astype(bool) & back_status[:, 0].astype(bool) & (error < 1.5)
            flow_points = int(good.sum())
            if flow_points >= 5:
                shift = np.median((p1 - p0).reshape(-1, 2)[good], axis=0)
                flow_ok = bool(np.linalg.norm(shift) < 120)
                if flow_ok:
                    predicted = self.point + shift
                    fb_error = float(np.median(error[good]))
        if seed is not None:
            predicted = np.asarray(seed, float)
            self.missing = 0
            self.template = None
        match_score = np.nan
        if self.template is None and self.gray is not None and seed is None:
            det = {
                "candidate": False,
                "x_px": np.nan,
                "y_px": np.nan,
                "halo_score": np.nan,
                "core_contrast": np.nan,
                "method": "awaiting_reviewed_reinitialization",
            }
            measured = np.full(2, np.nan)
            accepted = False
            status = "initial_detection"
        elif self.template is None:
            det = detect_halo(frame, predicted, 20)
            status = "initialized" if det["candidate"] else "missing_initial_detection"
            measured = np.array([det["x_px"], det["y_px"]])
            accepted = bool(
                det["candidate"]
                and (predicted is None or np.linalg.norm(measured - predicted) < 20)
            )
            if accepted:
                self.template = cv2.getRectSubPix(gray, (81, 81), tuple(measured.astype(float)))
                self.allow_white_core = det["method"] == "reviewed_ROI_compact_white_core"
        else:
            radius = 25 if flow_ok and seed is None else (45 if self.missing < 10 else 90)
            cx, cy = predicted
            x0 = max(0, int(cx) - radius - 40)
            y0 = max(0, int(cy) - radius - 40)
            x1 = min(gray.shape[1], int(cx) + radius + 41)
            y1 = min(gray.shape[0], int(cy) + radius + 41)
            search = gray[y0:y1, x0:x1]
            measured = np.full(2, np.nan)
            accepted = False
            if search.shape[0] >= 81 and search.shape[1] >= 81:
                response = cv2.matchTemplate(search, self.template, cv2.TM_CCOEFF_NORMED)
                _, match_score, _, (x, y) = cv2.minMaxLoc(response)
                sub = []
                for values, index in [(response[y, :], x), (response[:, x], y)]:
                    offset = 0.0
                    if 0 < index < len(values) - 1:
                        a, b, c = map(float, values[index - 1 : index + 2])
                        denominator = a - 2 * b + c
                        if denominator < -1e-8:
                            offset = float(np.clip(0.5 * (a - c) / denominator, -0.5, 0.5))
                    sub.append(offset)
                measured = np.array([x0 + x + 40 + sub[0], y0 + y + 40 + sub[1]])
                accepted = bool(
                    match_score >= (0.65 if self.missing < 10 else 0.8)
                    and 0 < x < response.shape[1] - 1
                    and 0 < y < response.shape[0] - 1
                )
                if accepted:
                    refined = detect_halo(frame, measured, 15, self.allow_white_core)
                    refined_point = np.array([refined["x_px"], refined["y_px"]])
                    accepted = bool(
                        refined["candidate"] and np.linalg.norm(refined_point - measured) < 12
                    )
                    if accepted:
                        measured = refined_point
            det = {
                "candidate": accepted,
                "x_px": measured[0],
                "y_px": measured[1],
                "halo_score": np.nan,
                "core_contrast": np.nan,
                "method": "fixed_image_template_NCC_then_local_light_centroid",
            }
            status = "tracked_template" if flow_ok else "reacquired_template"
        innovation = (
            float(np.linalg.norm(measured - predicted)) if predicted is not None else np.nan
        )
        if accepted:
            self.point = measured
            self.missing = 0
        else:
            self.missing += 1
            if flow_ok:
                self.point = predicted  # Internal prediction is never emitted as a measurement.
            status = "missing_" + status.removeprefix("missing_")
        self.gray = gray
        return {
            **det,
            "candidate": accepted,
            "x_px": det["x_px"] if accepted else np.nan,
            "y_px": det["y_px"] if accepted else np.nan,
            "track_status": status,
            "flow_valid_points": flow_points,
            "flow_fb_error_px": fb_error,
            "template_ncc": float(match_score),
            "detector_innovation_px": innovation,
            "consecutive_missing": self.missing,
        }
