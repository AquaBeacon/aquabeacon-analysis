import importlib.util
from pathlib import Path

import numpy as np

spec = importlib.util.spec_from_file_location(
    'textured', Path(__file__).parents[1] / 'scripts/export_child_textured_map.py')
# Other project script tests add scripts to sys.path through their imports.
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_projection_uses_camera_to_world_pose_and_bottom_left_uv():
    k = np.array([[100., 0, 100], [0, 100., 100], [0, 0, 1]])
    local = np.array([[-.002, -.002, 1], [.002, -.002, 1],
                      [-.002, .002, 1], [.002, .002, 1], [10, 10, -1]])
    translation = np.array([4., 5, 6])
    vertices, uv, faces = module.projected_surface(
        local + translation, translation, [0, 0, 0, 1], k, 200, 200)
    assert len(faces) == 2
    assert np.allclose(np.sort(uv, axis=0), [[.499, .499], [.499, .499],
                                           [.501, .501], [.501, .501]])
    assert np.allclose(np.sort(vertices, axis=0), np.sort(local[:4] + translation, axis=0))


def test_depth_discontinuity_is_not_bridged():
    k = np.array([[100., 0, 100], [0, 100., 100], [0, 0, 1]])
    points = np.array([[-.2, -.2, 1], [.2, -.2, 1], [-.4, .4, 2], [.4, .4, 2]])
    _, _, faces = module.projected_surface(points, np.zeros(3), [0, 0, 0, 1], k, 200, 200)
    assert len(faces) == 0
