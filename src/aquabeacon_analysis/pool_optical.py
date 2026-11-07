"""Preserve pool observations and gate new image measurements on explicit review."""
from __future__ import annotations

import math


def merge_pool_detection(original, detection, *, identity_excluded=False):
    baseline = original["status"] == "candidate"
    added = (not baseline and original["status"] != "excluded" and not identity_excluded
             and bool(detection["candidate"]))
    row = {**original, "baseline_candidate": baseline, "identity_excluded": identity_excluded,
           "new_algorithm_candidate": bool(detection["candidate"]),
           "new_x_px": detection.get("x_px", math.nan), "new_y_px": detection.get("y_px", math.nan),
           "new_contrast": detection.get("core_contrast", math.nan),
           "new_score": detection.get("halo_score", math.nan), "proposed_addition": added,
           "recovered_candidate": False}
    return row


def reviewed_pool_rows(rows, accepted_indices, rejected_indices):
    """Require a complete, disjoint review of proposed additions; retain other fields."""
    accepted, rejected = set(accepted_indices), set(rejected_indices)
    proposed = {i for i, r in enumerate(rows) if str(r["proposed_addition"]) == "True"}
    if accepted & rejected or accepted | rejected != proposed:
        raise ValueError("Every proposed addition requires exactly one explicit review decision")
    result = []
    for i, source in enumerate(rows):
        row = dict(source)
        if i in accepted:
            point = [float(row["new_x_px"]), float(row["new_y_px"])]
            if (row["status"] != "missing" or str(row["identity_excluded"]) == "True"
                    or not all(math.isfinite(v) for v in point)):
                raise ValueError("Only finite missing measurements outside vetoes may be recovered")
            row.update(x_px=row["new_x_px"], y_px=row["new_y_px"], status="candidate",
                       reason="reviewed_multiscale_luminous_core", recovered_candidate=True)
        row["review_decision"] = "accepted" if i in accepted else "rejected" if i in rejected else "not_proposed"
        result.append(row)
    return result
