"""Pool review preservation and metric placement must not invent observations or scale."""
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"scripts"))
from map_pool_optical_followup import placement

from aquabeacon_analysis.pool_optical import merge_pool_detection, reviewed_pool_rows


def original(status="missing"):
    return {"status": status, "frame_pts_s": "12.123456", "frame_index": "363",
            "x_px": "123.456" if status == "candidate" else "", "y_px": "55" if status == "candidate" else "",
            "reason": "old", "confidence": "0.75"}


def detection():
    return {"candidate": True, "x_px": 122.8, "y_px": 54.5}


def test_baseline_measurement_and_timestamp_are_preserved_exactly():
    old = original("candidate")
    merged = merge_pool_detection(old, detection())
    reviewed = reviewed_pool_rows([merged], [], [])[0]
    assert all(reviewed[k] == v for k, v in old.items())
    assert not reviewed["recovered_candidate"]


@pytest.mark.parametrize("status,veto", [("excluded", False), ("missing", True), ("candidate", True)])
def test_edge_and_identity_exclusions_cannot_be_recovered(status, veto):
    merged = merge_pool_detection(original(status), detection(), identity_excluded=veto)
    assert not merged["proposed_addition"]
    with pytest.raises(ValueError):
        reviewed_pool_rows([merged], [0], [])


def test_complete_review_is_required_and_missing_rejections_remain_missing():
    rows = [merge_pool_detection(original(), detection()) for _ in range(2)]
    with pytest.raises(ValueError):
        reviewed_pool_rows(rows, [0], [])
    with pytest.raises(ValueError):
        reviewed_pool_rows(rows, [0], [0, 1])
    final = reviewed_pool_rows(rows, [0], [1])
    assert final[0]["status"] == "candidate" and final[0]["recovered_candidate"]
    assert final[0]["frame_pts_s"] == "12.123456"
    assert final[1]["status"] == "missing" and final[1]["x_px"] == ""


def test_nonfinite_reviewed_point_is_rejected():
    row = merge_pool_detection(original(), {**detection(), "x_px": float("nan")})
    with pytest.raises(ValueError):
        reviewed_pool_rows([row], [0], [])


def test_static_pool_scatter_never_establishes_metric_scale():
    t = np.linspace(-2, 2, 12); cloud = np.c_[t, t**2, t**3]
    fit = placement(cloud, 3*cloud, "20261028_ngt_002", 100, .25)
    assert not fit["accepted"] and "static" in fit["reason"]


def test_pool_metric_fit_needs_motion_and_enough_points():
    t = np.linspace(-2, 2, 12); cloud = np.c_[t, t**2, t**3]
    fit = placement(cloud, 3*cloud+[1, 2, 3], "20261028_ngt_004", 100, .25)
    assert fit["accepted"] and fit["scale"] == pytest.approx(3)
    assert not fit["accuracy_validated"]
    assert not placement(cloud, cloud, "20261028_ngt_004", 20, .25)["accepted"]
    assert not placement(cloud, cloud*.001, "20261028_ngt_004", 100, .25)["accepted"]
