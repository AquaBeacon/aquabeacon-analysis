from __future__ import annotations

import json
import re

import numpy as np

from aquabeacon_analysis.attitude_animation import save_attitude_animation


def test_animation_keeps_missing_frames_explicit(tmp_path) -> None:
    times = np.arange(0, 2, 0.1)
    q = np.tile([1.0, 0.0, 0.0, 0.0], (len(times), 1))
    missing = q.copy()
    missing[5] = np.nan
    path = tmp_path / "attitude.html"
    save_attitude_animation(path, "test", "motion", times, {"camera": q, "body": missing})
    html = path.read_text(encoding="utf-8")
    payload = json.loads(re.search(
        r'<script id="attitude-data" type="application/json">(.*?)</script>', html
    ).group(1))
    assert payload["series"][1]["quaternions"][5] is None
    assert "NaN" not in html
    assert "requestAnimationFrame" in html
