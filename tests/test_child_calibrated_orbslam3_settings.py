"""Check measured child proxy intrinsics are carried into ORB-SLAM3 settings."""

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "prepare_child_calibrated_orbslam3_settings",
    ROOT / "scripts/prepare_child_calibrated_orbslam3_settings.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_calibrated_settings_preserve_imu_and_use_direct_proxy_fit():
    camera = json.loads((ROOT / "calibration/parent_camera_underwater_20260924_provisional.json")
                        .read_text(encoding="utf-8"))
    template = (ROOT / "calibration/child_orbslam3_provisional_023.yaml").read_text(
        encoding="utf-8"
    )
    output = MODULE.calibrated_settings(camera, template, "calibration/source.json", "abc")
    assert "Camera1.fx: 671.492807165" in output
    assert "Camera1.k1: 0.362990788897" in output
    assert "IMU.T_b_c1: !!opencv-matrix" in output
    assert "-0.116487353, -0.011875725" in output
    assert "Camera1.fx: 640.0" not in output
    native = MODULE.calibrated_settings(camera, template, "calibration/source.json", "abc",
                                        source_120fps=True)
    assert "Camera.fps: 120" in native
    assert "Camera1.fx: 671.492807165" in native
