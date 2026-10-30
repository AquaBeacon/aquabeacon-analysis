"""Frozen actual CAD at unchanged metric NGT positions; presentation only."""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import bpy
from mathutils import Vector

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cad_assets import load_cad_body
from create_ngt_visualizations import (
    camera,
    constant_keyframes,
    curves,
    finite,
    material,
    open_camera_view,
    render,
    setup,
)

ROOT = Path(__file__).resolve().parents[1]


def set_visibility_driver(obj, root, prop):
    driver = obj.driver_add(prop).driver
    driver.type = 'SCRIPTED'
    variable = driver.variables.new()
    variable.name = 'valid'
    variable.targets[0].id = root
    variable.targets[0].data_path = '["position_valid"]'
    driver.expression = 'valid < 0.5'


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.context.preferences.filepaths.save_version = 0
    out = ROOT/'results/20261028_ngt_004/presentation_positions'
    out.mkdir(parents=True, exist_ok=True)
    source = ROOT/'results/20261028_ngt_004/position_visualization/scene_data.json'
    data = json.loads(source.read_text(encoding='utf-8'))
    bundle = ROOT/'data/interim/cad/cad_20261028'
    scene = bpy.context.scene
    setup(scene)
    scene.render.resolution_x, scene.render.resolution_y = 1920, 1080
    scene.frame_start, scene.frame_end, scene.render.fps = 1, len(data['times']), 5
    scene['coordinate_policy'] = data['notes']
    scene['orientation_policy'] = 'Neutral identity rotation, unmeasured. Never inferred from trajectory.'
    scene['placement_policy'] = 'CAD design origins placed at estimated antenna/optical positions; centres/extrinsics uncalibrated.'
    scene['child_mount_discrepancy'] = 'Actual camera/light positions swapped relative to nominal CAD; transforms unspecified, nominal geometry shown.'
    scene['time_formula'] = 'parent_video_s = 1260 + (frame-1)/5; constant sample hold'
    bodies = {}
    for body, color in [('parent', (.53, .70, .80)), ('child', (.87, .68, .49))]:
        root, imported = load_cad_body(bundle, body)
        bodies[body] = root, imported
        root['position_valid'] = 0.0
        root['orientation_unmeasured'] = True
        root['position_origin_is_measured_center'] = False
        root.rotation_euler = (0, 0, 0)
        assert len([o for o in imported if o.type == 'MESH']) > 20
        for obj in imported:
            for prop in ['hide_render', 'hide_viewport']:
                set_visibility_driver(obj, root, prop)
        for index, point in enumerate(data[body]):
            root['position_valid'] = float(finite(point))
            if finite(point):
                root.location = point
            root.keyframe_insert(data_path='location', frame=index+1)
            root.keyframe_insert(data_path='["position_valid"]', frame=index+1)
        constant_keyframes(root)
        curves(scene, body+'_path_NO_GAP_BRIDGES', data[body], data[body+'_segments'],
               material(body+' pale path', color), .006)
    points = [p for p in data['parent']+data['child'] if finite(p)]
    overview = camera(scene, 'Overview_metric', points, minimum=3)
    sample = next(i for i in range(390, len(data['times']))
                  if finite(data['parent'][i]) and finite(data['child'][i]))
    close = camera(scene, 'Close_follow_metric_actual_CAD',
                   [data['parent'][sample], data['child'][sample]], minimum=2.3)
    direction = Vector((.8, -1.3, .8)).normalized()
    for index, point in enumerate(data['parent']):
        if not finite(point):
            continue
        p = Vector(point)
        c = Vector(data['child'][index]) if finite(data['child'][index]) else p
        center = (p+c)/2
        close.location = center+direction*6
        close.rotation_euler = (center-close.location).to_track_quat('-Z', 'Y').to_euler()
        close.data.ortho_scale = max(2.3, (p-c).length*1.4+.8)
        for prop in ['location', 'rotation_euler']:
            close.keyframe_insert(data_path=prop, frame=index+1)
        close.data.keyframe_insert(data_path='ortho_scale', frame=index+1)
    constant_keyframes(close)
    constant_keyframes(close.data)
    # Metric axes near the still sample; these are axes, not a surveyed pool model.
    center = Vector(data['parent'][sample])
    axes = [(Vector((1, 0, 0)), (.75, .12, .10), 'East_1m'),
            (Vector((0, 1, 0)), (.10, .55, .22), 'North_1m'),
            (Vector((0, 0, 1)), (.10, .30, .75), 'Up_1m')]
    for direction_axis, color, name in axes:
        curves(scene, name, [center, center+direction_axis], [[0, 1]],
               material(name, color), .008)
    checks = []
    for index in range(len(data['times'])):
        scene.frame_set(index+1)
        for body, (root, imported) in bodies.items():
            valid = finite(data[body][index])
            assert abs(root['position_valid']-float(valid)) < 1e-7
            if valid:
                assert (root.location-Vector(data[body][index])).length < 1e-5
            assert all(obj.hide_render == (not valid) for obj in imported)
            assert all(obj.hide_viewport == (not valid) for obj in imported)
            assert root.scale == Vector((1, 1, 1))
            assert all(abs(v) < 1e-8 for v in root.rotation_euler)
        if index in [0, sample, len(data['times'])-1]:
            checks.append({'index': index, 'parent_video_s': data['times'][index],
                           'parent_valid': finite(data['parent'][index]),
                           'child_valid': finite(data['child'][index])})
    scene.frame_set(sample+1)
    render(scene, close, out/'cad_positions_1080.png')
    render(scene, overview, out/'cad_positions_overview_1080.png')
    scene.camera = close
    open_camera_view()
    blend = out/'cad_positions.blend'
    bpy.ops.wm.save_as_mainfile(filepath=str(blend))
    # Reopen the actual deliverable and verify saved drivers/units/locations.
    names = {body: root.name for body, (root, _) in bodies.items()}
    bpy.ops.wm.open_mainfile(filepath=str(blend))
    scene = bpy.context.scene
    assert scene.unit_settings.scale_length == 1
    for body, name in names.items():
        root = bpy.data.objects[name]
        assert len(root.children_recursive) > 20
        for index in [0, sample, len(data['times'])-1]:
            scene.frame_set(index+1)
            valid = finite(data[body][index])
            if valid:
                assert (root.location-Vector(data[body][index])).length < 1e-5
            assert all(o.hide_render == (not valid) for o in root.children_recursive)
    frames = out/'frames'
    frames.mkdir(exist_ok=True)
    scene.render.resolution_x, scene.render.resolution_y = 1280, 720
    records = []
    for output_index in range(96):
        source_index = round(output_index*(len(data['times'])-1)/95)
        scene.frame_set(source_index+1)
        render(scene, scene.camera, frames/f'{output_index:04d}.png')
        record = {'preview_frame': output_index, 'source_index': source_index,
                  'blend_frame': source_index+1, 'parent_video_s': data['times'][source_index]}
        for body in ['parent', 'child']:
            point = data[body][source_index]
            record[body+'_valid'] = finite(point)
            for axis, name in enumerate(['east_m', 'north_m', 'surface_up_m']):
                record[body+'_'+name] = point[axis] if finite(point) else ''
        records.append(record)
    with (out/'preview_samples.csv').open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(records[0]))
        writer.writeheader()
        writer.writerows(records)
    (out/'blender_verification.json').write_text(json.dumps({
        'all_source_samples_checked': len(data['times']), 'location_tolerance_m': 1e-5,
        'identity_orientation_all_frames': True, 'all_hierarchy_visibility_drivers_checked': True,
        'reopened_saved_blend_checked': True, 'unit_scale_m': 1, 'selected_checks': checks,
        'still_source_index': sample, 'preview_frames': 96, 'preview_fps': 12,
        'full_timeline_hz': 5, 'source_times_preserved': True}, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
