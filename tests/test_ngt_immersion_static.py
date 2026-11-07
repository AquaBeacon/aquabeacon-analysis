import importlib.util
from pathlib import Path

import numpy as np

spec = importlib.util.spec_from_file_location(
    "immersion", Path(__file__).resolve().parents[1] / "scripts/evaluate_ngt_immersion_static.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_half_open_window_preserves_invalid_and_zero():
    rows = [{"parent_video_s": str(t), "x": str(x), "y": "0", "z": "0", "valid": ok}
            for t, x, ok in [(0, 99, "True"), (1, 0, "True"), (2, 2, "True"),
                              (3, 100, "False"), (61, 99, "True")]]
    selected = module.select(rows, 1, 60)
    assert len(selected) == 3
    m = module.metrics(selected, ["x", "y", "z"], "valid")
    assert m["count"] == 2
    assert m["available_fraction"] == 2/3
    np.testing.assert_allclose(m["centered_rms_3d_m"], 1)
    assert m["first_last_10s_mean_displacement_m"] is None
