"""Image-only beacon tracking with flow and fixed appearance matching."""

import cv2
import numpy as np

from aquabeacon_analysis.beacon_halo import detect_halo


class BeaconTracker:
    """Flow predicts a search region; image-match coordinates are never smoothed."""

    def __init__(self, seed=None, *, detector=detect_halo, template_size=81,
                 max_missing_frames=None, allow_flow_core=False):
        if template_size < 15 or template_size % 2 == 0:
            raise ValueError("Template size must be odd and at least 15")
        self.point = np.asarray(seed, float) if seed is not None else None
        self.gray = None
        self.template = None
        self.allow_white_core = True
        self.missing = 0
        self.detector = detector
        self.template_size = template_size
        if max_missing_frames is not None and max_missing_frames < 1:
            raise ValueError("Missing-frame limit must be positive")
        self.max_missing_frames = max_missing_frames
        self.allow_flow_core = allow_flow_core
        self.last_measured_point = None

    def update(self, frame, seed=None):
        if (seed is None and self.max_missing_frames is not None
                and self.missing >= self.max_missing_frames):
            self.gray = None
            self.missing += 1
            return {"candidate": False, "x_px": np.nan, "y_px": np.nan,
                    "track_status": "awaiting_reviewed_reinitialization",
                    "method": "missing_limit", "template_ncc": np.nan,
                    "consecutive_missing": self.missing}
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
        core_match_score = np.nan
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
            det = self.detector(frame, predicted, 20)
            status = "initialized" if det["candidate"] else "missing_initial_detection"
            measured = np.array([det["x_px"], det["y_px"]])
            accepted = bool(
                det["candidate"]
                and (predicted is None or np.linalg.norm(measured - predicted) < 20)
            )
            if accepted:
                size = self.template_size
                self.template = cv2.getRectSubPix(gray, (size, size), tuple(measured.astype(float)))
                self.allow_white_core = det["method"] == "reviewed_ROI_compact_white_core"
        else:
            method = "fixed_image_template_NCC_then_local_light_centroid"
            flow_core = False
            compact_recovery = False
            radius = 25 if flow_ok and seed is None else (45 if self.missing < 10 else 90)
            cx, cy = predicted
            half = self.template_size // 2
            x0 = max(0, int(cx) - radius - half)
            y0 = max(0, int(cy) - radius - half)
            x1 = min(gray.shape[1], int(cx) + radius + half + 1)
            y1 = min(gray.shape[0], int(cy) + radius + half + 1)
            search = gray[y0:y1, x0:x1]
            measured = np.full(2, np.nan)
            accepted = False
            if min(search.shape[:2]) >= self.template_size:
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
                measured = np.array([x0 + x + half + sub[0], y0 + y + half + sub[1]])
                accepted = bool(
                    match_score >= (0.65 if self.missing < 10 else 0.8)
                    and 0 < x < response.shape[1] - 1
                    and 0 < y < response.shape[0] - 1
                )
                if accepted:
                    refined = self.detector(frame, measured, 15, self.allow_white_core)
                    refined_point = np.array([refined["x_px"], refined["y_px"]])
                    accepted = bool(
                        refined["candidate"] and np.linalg.norm(refined_point - measured) < 12
                    )
                    if accepted:
                        measured = refined_point
                        if self.allow_flow_core and flow_ok:
                            accepted = bool(np.linalg.norm(measured-predicted) <= 8)
            # The halo/background can change while the luminous core and image
            # motion remain consistent. Flow is only a search prediction: the
            # returned center must be detected in this frame and match the
            # fixed initialization core. This path never operates after loss.
            if (not accepted and self.allow_flow_core and flow_ok
                    and flow_points >= 8 and fb_error <= .15 and self.missing < 3):
                refined = self.detector(frame, predicted, 12, self.allow_white_core)
                point = np.array([refined["x_px"], refined["y_px"]])
                if (refined["candidate"] and np.linalg.norm(point-predicted) <= 8
                        and refined["core_contrast"] >= 8 and refined["halo_score"] >= 4):
                    core_size = min(21, self.template_size)
                    half_core = core_size//2
                    fixed = self.template[half-half_core:half+half_core+1,
                                          half-half_core:half+half_core+1]
                    patch = cv2.getRectSubPix(gray, (core_size, core_size), tuple(point))
                    if fixed.std() >= 1 and patch.std() >= 1:
                        core_match_score = float(cv2.matchTemplate(
                            patch, fixed, cv2.TM_CCOEFF_NORMED)[0, 0])
                        if core_match_score >= .65:
                            measured, accepted, flow_core = point, True, True
                            method = "flow_consistent_measured_core_with_fixed_core_NCC"
            if (not accepted and self.allow_flow_core and self.missing < 10
                    and self.last_measured_point is not None):
                from aquabeacon_analysis.beacon_detection import luminous_candidates

                fixed = self.template[half-10:half+11, half-10:half+11]
                matches = []
                if fixed.shape == (21, 21) and fixed.std() >= 1:
                    for candidate in luminous_candidates(frame, self.last_measured_point, 120):
                        if candidate["halo_score"] < 10 or candidate["core_contrast"] < 25:
                            continue
                        point = (candidate["x_px"], candidate["y_px"])
                        score = -1.0
                        for size in (11, 15, 21, 27, 33):
                            patch = cv2.resize(cv2.getRectSubPix(gray, (size, size), point), (21, 21))
                            if patch.std() >= 1:
                                score = max(score, float(cv2.matchTemplate(
                                    patch, fixed, cv2.TM_CCOEFF_NORMED)[0, 0]))
                        if score >= .85:
                            matches.append((candidate, score))
                if matches:
                    best, score = matches[0]
                    ambiguous = any(
                        np.hypot(p["x_px"]-best["x_px"], p["y_px"]-best["y_px"]) > 16
                        and p["halo_score"] > best["halo_score"]/1.5 for p, _ in matches[1:])
                    if not ambiguous:
                        measured = np.array([best["x_px"], best["y_px"]])
                        accepted, compact_recovery, core_match_score = True, True, score
                        method = "short_gap_unique_compact_core_with_fixed_multiscale_core_NCC"
            det = {
                "candidate": accepted,
                "x_px": measured[0],
                "y_px": measured[1],
                "halo_score": np.nan,
                "core_contrast": np.nan,
                "method": method,
            }
            status = ("reacquired_compact_core" if compact_recovery else
                      "tracked_flow_core" if flow_core else
                      ("tracked_template" if flow_ok else "reacquired_template"))
        innovation = (
            float(np.linalg.norm(measured - predicted)) if predicted is not None else np.nan
        )
        if accepted:
            self.point = measured
            self.last_measured_point = measured.copy()
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
            "core_template_ncc": float(core_match_score),
            "detector_innovation_px": innovation,
            "consecutive_missing": self.missing,
        }
