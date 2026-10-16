"""Check frame selection and PLY parsing for child VSLAM artifacts."""

import importlib.util
from pathlib import Path

import numpy as np

SPEC = importlib.util.spec_from_file_location(
    "render_child_vslam_artifacts",
    Path(__file__).resolve().parents[1] / "scripts/render_child_vslam_artifacts.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_longest_saved_span_and_ascii_ply(tmp_path):
    rows = [{"pose_status": "not_saved"}] * 4
    rows += [{"pose_status": "saved"}] * 35
    rows += [{"pose_status": "not_saved"}] * 3
    rows += [{"pose_status": "saved"}] * 31
    assert MODULE.longest_saved_span(rows) == (4, 39)
    ply = tmp_path / "map.ply"
    ply.write_text("ply\nformat ascii 1.0\nelement vertex 2\n"
                   "property float x\nproperty float y\nproperty float z\n"
                   "end_header\n1 2 3\n4 5 6\n", encoding="utf-8")
    np.testing.assert_array_equal(MODULE.read_ply_xyz(ply), [[1, 2, 3], [4, 5, 6]])
