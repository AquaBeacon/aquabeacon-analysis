"""Check magnetic-field diagnostics against a constant synthetic world field."""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from diagnose_child_magnetometer import inspect_events


def test_constant_world_field_has_stable_direction():
    rows = []
    for index, (x, y, qz, qw) in enumerate(((20, 0, 0, 1), (0, -20, np.sqrt(.5), np.sqrt(.5)))):
        time_s = index * 0.1
        rows.append({"sensor_id": "8", "window_time_s": str(time_s),
                     "x": "0", "y": "0", "z": str(qz), "w": str(qw)})
        rows.append({"sensor_id": "3", "window_time_s": str(time_s),
                     "x": str(x), "y": str(y), "z": "0", "w": "0"})
    summary, _, norm, direction = inspect_events(rows)
    assert summary["paired_reports"] == 2
    np.testing.assert_allclose(norm, 20)
    np.testing.assert_allclose(direction, 0, atol=1e-6)
    assert summary["game_frame_heading_concentration"] > 0.999
