"""Check sparse photo surfaces, original coordinates, GLB/OBJ and provenance hashes."""
from __future__ import annotations

import os

os.environ.setdefault('OMP_NUM_THREADS', '2')
os.environ.setdefault('OPENBLAS_NUM_THREADS', '1')

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import trimesh

from aquabeacon_analysis.ngt_textured_map import camera_projection


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def verify(root, experiment):
    output = root / 'results' / experiment / 'presentation_mapping'
    data = json.loads((output / 'scene.json').read_text(encoding='utf-8'))
    manifest = json.loads((output / 'manifest.json').read_text(encoding='utf-8'))
    points = np.asarray(data['points']['xyz'])
    counts = 0
    assert data['parent_observations_used'] is False
    assert data['points']['first_observed_source_video_s'] is None
    for surface in data['surfaces']:
        vertices = np.asarray(surface['vertices'])
        uv = np.asarray(surface['uv'])
        faces = np.asarray(surface['faces'])
        ids = np.asarray(surface['original_point_indices'])
        np.testing.assert_array_equal(vertices, points[ids])
        assert faces.min() >= 0 and faces.max() < len(vertices)
        assert np.isfinite(uv).all() and (uv >= 0).all() and (uv <= 1).all()
        counts += len(faces)
        kf = data['keyframes'][surface['keyframe_index']]
        pixels, depth = camera_projection(vertices, kf['position'], kf['quaternion_xyzw'],
                                           manifest['calibration']['matrix_px'])
        np.testing.assert_allclose(uv, np.column_stack((pixels[:, 0] / 1280,
                                                        1 - pixels[:, 1] / 720)), atol=1e-12)
        assert (depth > 0).all()
        assert (depth[faces].max(axis=1) / depth[faces].min(axis=1) <= 1.15 + 1e-10).all()
        edges = pixels[faces] - np.roll(pixels[faces], 1, axis=1)
        assert (np.linalg.norm(edges, axis=2).max(axis=1) <= 80 + 1e-9).all()
        assert surface['source_video_s'] == kf['source_video_s']
        assert digest(root / surface['source_image']) == surface['source_image_sha256']
        assert digest(output / surface['review_source_image']) == surface['source_image_sha256']
    assert counts == manifest['triangles']
    mesh = trimesh.load(output / 'textured_map.glb', force='scene', process=False)
    assert len(mesh.geometry) == len(data['surfaces'])
    assert sum(len(g.faces) for g in mesh.geometry.values()) == counts
    for geometry in mesh.geometry.values():
        assert geometry.visual.material.doubleSided
        assert geometry.visual.material.baseColorTexture is not None
        assert np.isfinite(geometry.vertices).all()
    ply = trimesh.load(output / 'colored_points.ply', process=False)
    np.testing.assert_allclose(ply.vertices, points, rtol=1e-6, atol=1e-6)
    assert len(data['maps']) == 5
    for record in data['maps']:
        assert digest(output / record['original_ply']) == digest(root / record['source_ply'])
        assert len(record['points']) == record['point_count']
    for relative, expected in manifest['inputs_sha256'].items():
        assert digest(root / relative) == expected, relative
    for relative, expected in manifest['artifacts_sha256'].items():
        assert digest(output / relative) == expected, relative
    assert (output / 'textured_map.obj').is_file() and (output / 'material.mtl').is_file()
    result = {'status': 'passed', 'checked_utc': datetime.now(UTC).isoformat(),
              'pid': os.getpid(), 'points_preserved': len(points), 'triangles': counts,
              'embedded_photo_materials': len(mesh.geometry),
              'checks': ['all mesh vertices equal original PLY vertices by recorded indices',
                         'UV finite and in [0,1]; index bounds', 'photo-time matches keyframe',
                         'UV equals calibrated projection; positive depth; 80px/1.15 depth gates',
                         'source photos copied without changes', 'five original maps byte-identical',
                         'GLB triangle count, photo material embedding and double-sided flags',
                         'colored PLY coordinates preserved', 'input and output SHA256 hashes',
                         'OBJ and MTL present'],
              'browser_rendering': 'unverified; see viewer_logic_verification.json',
              'geometry_accuracy': 'unvalidated; no ground truth used'}
    (output / 'artifact_verification.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment', required=True)
    args = parser.parse_args()
    verify(Path(__file__).resolve().parents[1], args.experiment)
