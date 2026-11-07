"""Presentation-only view of independently restarted, position-registered wall maps."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parent))
from create_ngt_visualizations import (
    camera,
    curves,
    digest,
    material,
    open_camera_view,
    point_mesh,
    render,
    setup,
)

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment', required=True, choices=['20261028_ngt_004'])
    parser.add_argument('--run', default='native120_restart',
                        choices=['native120_restart', 'rotation_corrected_20261031'])
    args = parser.parse_args(sys.argv[sys.argv.index('--')+1:])
    out = ROOT / 'results' / args.experiment / 'wall_mapping' / args.run
    data = json.loads((out / 'map_data.json').read_text(encoding='utf-8'))
    maps = [m for m in data['maps'] if not m.get('aggregate') and m['metric']
            and m['valid_structure'] and m['wall_period']]
    record = {'pid': os.getpid(), 'started_utc': datetime.now(UTC).isoformat(),
        'owner': 'coordinate_reassessment' if args.run == 'rotation_corrected_20261031' else 'native120_restart_mapping',
        'run': args.run, 'purpose': __doc__, 'threads': 2,
        'input_sha256': digest(out / 'map_data.json'), 'generator_sha256': digest(Path(__file__)),
        'helper_sha256': digest(Path(__file__).with_name('create_ngt_visualizations.py')), 'scenes': []}
    path = out / 'blender.process.json'
    path.write_text(json.dumps(record, indent=2))
    try:
        if not maps:
            raise ValueError('No position-registered structural maps to display')
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.context.preferences.filepaths.save_version = 0
        largest = sorted(maps, key=lambda m: m['complete_point_count'], reverse=True)[:5]
        selections = [('All_positioned_fragments', maps), ('Estimated_route_and_wall', maps)] + [(f"Fragment_{m['model']}", [m]) for m in largest]
        for n, (name, selected) in enumerate(selections):
            scene = bpy.context.scene if n == 0 else bpy.data.scenes.new(name)
            scene.name = name
            bpy.context.window.scene = scene
            setup(scene)
            scene['coordinate_policy'] = 'Each fragment independently fitted to available positions; conditional metric frame'
            scene['geometry_policy'] = 'Display-sampled sparse points; overlaps retained; not a reconstructed wall surface'
            points = []
            blue = material('Reconstructed_camera_path', (.35, .4, .45))
            orange = material('Input_positions', (.95, .32, .05))
            for m in selected:
                color = m['window_color'].lstrip('#')
                rgb = tuple(int(color[i:i+2], 16)/255 for i in (0, 2, 4))
                mat = material(m['model'], rgb)
                obj = point_mesh(scene, 'Points_' + m['model'], m['points'], mat, .004)
                obj['source_pointcloud'] = m['source_pointcloud']
                obj['complete_point_count'] = m['complete_point_count']
                obj['display_point_count'] = len(m['points'])
                curves(scene, 'Cameras_' + m['model'], m['cameras'], m['camera_segments'], blue, .003)
                point_mesh(scene, 'Priors_' + m['model'], m['priors'], orange, .006)
                points.extend(m['points'] + m['cameras'] + m['priors'])
            if name == 'Estimated_route_and_wall':
                routes = data['estimated_routes']
                parent_color = material('Estimated_parent_blue', (.03, .4, .7))
                curves(scene, 'Parent_GNSS_route_z0_display', routes['parent'], routes['parent_segments'], parent_color, .025)
                curves(scene, 'Child_estimated_route_pressure_depth', routes['child'], routes['child_segments'], orange, .025)
                points.extend(routes['parent'] + routes['child'])
                scene['route_coordinate_policy'] = routes['coordinate_policy']
            cam = camera(scene, 'View', points, direction=(.6, -1, .8), minimum=.5)
            render(scene, cam, out / ('blender_overview.png' if n == 0 else f'blender_{name}.png'))
            record['scenes'].append({'name': name, 'models': [m['model'] for m in selected],
                'display_points': sum(len(m['points']) for m in selected)})
        bpy.context.window.scene = bpy.data.scenes['All_positioned_fragments']
        open_camera_view()
        bpy.ops.wm.save_as_mainfile(filepath=str(out / 'wall_map.blend'))
        # Reopen saved output and check all declared objects rather than trusting save success.
        bpy.ops.wm.open_mainfile(filepath=str(out / 'wall_map.blend'))
        assert len(bpy.data.scenes) == len(selections)
        for scene_record in record['scenes']:
            scene = bpy.data.scenes[scene_record['name']]
            count = sum(int(obj.get('display_point_count', 0)) for obj in scene.objects)
            assert count == scene_record['display_points']
        record['status'] = 'completed_and_reopened_verified'
    except BaseException as error:
        record['status'], record['error'] = 'failed', str(error)
        raise
    finally:
        record['ended_utc'] = datetime.now(UTC).isoformat()
        path.write_text(json.dumps(record, indent=2))


if __name__ == '__main__':
    main()
