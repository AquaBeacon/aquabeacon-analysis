"""Coverage reporting includes missing frame spans and EuRoC timestamp rounding."""

import importlib.util
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "summarize_child_vslam", Path(__file__).resolve().parents[1] /
    "scripts/summarize_child_vslam.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_matches_rounded_timestamps_and_preserves_gaps():
    frame_ns = [1_000_000_000, 1_033_333_333, 1_066_666_666, 1_099_999_999]
    matched = MODULE.match_pose_indices(frame_ns, [1_000_000_128, 1_066_666_560])
    assert matched == {0, 2}
    assert MODULE.gaps(matched, 4) == [
        {"start_index": 1, "end_index_exclusive": 2},
        {"start_index": 3, "end_index_exclusive": 4},
    ]
    assert MODULE.pose_lookup(frame_ns, [
        ["1000000128.000000", "1", "2", "3", "0", "0", "0", "1"]
    ]) == {0: ["1", "2", "3", "0", "0", "0", "1"]}
