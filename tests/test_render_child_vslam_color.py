"""Check that visual overlays preserve source color."""

import importlib.util
import sys
from pathlib import Path

import numpy as np

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location(
    "render_child_vslam_120fps", SCRIPTS / "render_child_vslam_120fps.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_annotate_preserves_color_background() -> None:
    frame = np.empty((540, 960, 3), dtype=np.uint8)
    frame[:] = (12, 80, 190)
    image, points, matches = MODULE.annotate(frame, [], {}, 0, 0.0, 0, 0, 1)
    assert image[300, 500].tolist() == [12, 80, 190]
    assert points == {}
    assert matches == 0
