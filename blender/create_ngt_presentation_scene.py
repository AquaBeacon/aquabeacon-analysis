"""Create editable presentation film: independent SLAM replay and separate real CAD inset."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replay_samples(keyframes, frame_count=100, max_hold_s=.5):
    """Sample-and-hold saved observations; gaps never gain invented poses."""
    ordered = sorted(keyframes, key=lambda item: item['source_video_s'])
    if not ordered:
        raise ValueError('No recorded keyframes')
    start, stop = ordered[0]['source_video_s'], ordered[-1]['source_video_s']
    cursor, result = 0, []
    for index in range(frame_count):
        time_s = start + (stop-start)*index/max(1, frame_count-1)
        while cursor+1 < len(ordered) and ordered[cursor+1]['source_video_s'] <= time_s:
            cursor += 1
        sample = ordered[cursor]
        result.append((time_s, sample if time_s-sample['source_video_s'] <= max_hold_s else None))
    return result


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    import bpy
    from mathutils import Euler, Matrix, Vector

    sys.path.insert(0, str(ROOT/'blender'))
    from cad_assets import load_cad_body
    from create_ngt_visualizations import constant_keyframes, curves, material, point_mesh

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment', required=True)
    parser.add_argument('--frames', type=int, default=100)
    parser.add_argument('--render', action='store_true')
    args = parser.parse_args(sys.argv[sys.argv.index('--')+1:])
    if args.experiment != '20261028_ngt_004':
        raise ValueError('Only explicitly reviewed 004 bundle is supported')
    source = ROOT/f'results/{args.experiment}/presentation_mapping/scene.json'
    output = ROOT/f'results/{args.experiment}/presentation_animation'
    output.mkdir(parents=True, exist_ok=True)
    data = json.loads(source.read_text(encoding='utf8'))
    shutil.copy2(source, output/'scene.input.json')
    original_mesh_source = source.parent/data['mesh_glb']
    shutil.copy2(original_mesh_source, output/'textured_map.input.glb')
    if data['parent_observations_used']:
        raise ValueError('Independent child map must not consume parent observations')
    if data['units'] != 'arbitrary_monocular_slam_units':
        raise ValueError('Unexpected map scale contract')
    samples = replay_samples(data['keyframes'], args.frames)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.context.preferences.filepaths.save_version = 0
    scene = bpy.context.scene
    scene.name = 'NGT004_Independent_Map_and_Actual_CAD'
    scene.render.engine = 'BLENDER_WORKBENCH'
    scene.render.threads_mode = 'FIXED'
    scene.render.threads = 2
    scene.render.resolution_x, scene.render.resolution_y = 1920, 1080
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = 'PNG'
    scene.render.fps = 10
    scene.frame_start, scene.frame_end = 1, args.frames
    scene.world = bpy.data.worlds.new('Pale blue background')
    scene.world.color = (.88, .925, .94)
    scene.display.shading.background_type = 'WORLD'
    scene.display.shading.light = 'STUDIO'
    scene.display.shading.color_type = 'TEXTURE'
    scene.display.shading.show_shadows = False
    scene.display.shading.show_cavity = True
    scene.display.shading.cavity_type = 'BOTH'
    scene.view_settings.view_transform = 'Standard'
    scene['purpose'] = 'Presentation only; no numerical evaluation in Blender'
    scene['map_units'] = data['units']
    scene['CAD_map_registration'] = 'NONE; separate magnified design inset'
    scene['point_creation_chronology'] = 'Unavailable; static point map'
    scene['gap_policy'] = 'Saved keyframes sample hold <=0.5s; hidden otherwise'
    bpy.ops.object.camera_add(location=(0, 0, 30))
    camera = bpy.context.object
    camera.name = 'Presentation_Camera_16x9'
    camera.data.type, camera.data.ortho_scale = 'ORTHO', 16
    scene.camera = camera
    ink = material('Text dark', (.015, .025, .03))
    white = material('White lettering', (1, 1, 1))
    blue = material('AquaBeacon blue', (0, .17, .29))
    mint = material('Map points', (.10, .48, .36))
    orange = material('Saved camera path', (.95, .25, .035))
    font = bpy.data.fonts.load('C:/Windows/Fonts/BIZ-UDGothicR.ttc')

    def text(name, body, xy, size=.20, mat=ink):
        curve = bpy.data.curves.new(name, 'FONT')
        curve.body, curve.size, curve.font = body, size, font
        obj = bpy.data.objects.new(name, curve)
        scene.collection.objects.link(obj)
        obj.location = (*xy, 25)
        obj.data.materials.append(mat)
        return obj

    def plate(name, xy, width, height, mat):
        bpy.ops.mesh.primitive_plane_add(size=1, location=(*xy, 24.8))
        obj = bpy.context.object
        obj.name, obj.scale = name, (width, height, 1)
        obj.data.materials.append(mat)

    plate('Title band', (0, 3.96), 16, 1.08, blue)
    text('Title', '長田高校プール：子機から見た地図と実機CAD', (-7.55, 3.78), .36, white)
    text('Map heading', '子機映像だけの独立地図（Map 2）', (-7.45, 3.05), .29)
    text('CAD heading', '実機形状の参照', (3.65, 3.05), .29)
    text('Map scale', '単眼SLAMの任意スケール / 280秒中12.5秒の記録地図', (-7.45, 2.64), .20)
    text('Surface semantics', '三角面は疎点の局所接続。床・壁の確定形状ではない。',
         (-7.45, 2.30), .17)
    text('CAD scale', '凍結CAD / 別枠で拡大表示', (3.65, 2.64), .18)
    text('Map note', '写真面は撮影時刻順の表示。点の生成過程を示すものではない。', (-7.45, -3.08), .18)
    text('Path note', '橙：保存キーフレームの位置。0.5秒超の欠測は表示しない。', (-7.45, -3.46), .18)
    text('No parent', '表示は中央98%の範囲。親機観測は地図生成に使用していません。', (-7.45, -3.84), .20)
    text('CAD limitation', 'cad_20261028：設計時の配置\n本番はカメラとライトの位置が逆\n取付回転・測定中心は未確定', (3.65, -2.65), .18)
    text('CAD separate', '地図との位置合わせ・縮尺対応なし', (3.65, -3.88), .18)
    # Map normalization is display-only and never shared with metric CAD geometry.
    xyz = data['points']['xyz']
    rotation = Matrix(data['basis']).transposed()
    projected = [rotation@Vector(point) for point in xyz]
    # Explicit presentation crop only: retain all inputs and document cropped count.
    columns = [sorted(point[j] for point in projected) for j in range(3)]
    low = Vector([column[int(.01*(len(column)-1))] for column in columns])
    high = Vector([column[int(.99*(len(column)-1))] for column in columns])
    center = (low+high)/2
    scale = min(9.8/max(high.x-low.x, .001), 4.7/max(high.y-low.y, .001))
    shift = Vector((-2.5, -.1, 0))

    def display(point):
        return tuple((rotation@Vector(point)-center)*scale+shift)

    all_points = [display(point) for point in xyz]
    def in_panel(point):
        return -7.4 <= point[0] <= 2.4 and -2.45 <= point[1] <= 2.05
    points = [point for point in all_points if in_panel(point)]
    cloud = point_mesh(scene, 'Final_map_points_static_no_creation_timestamps', points, mint, .004)
    cloud['source_frame'] = data['coordinate_frame']
    before = set(bpy.data.objects)
    mesh_source = output/'textured_map.input.glb'
    bpy.ops.import_scene.gltf(filepath=str(mesh_source))
    imported_map = set(bpy.data.objects)-before
    map_root = bpy.data.objects.new('Photo_surfaces_display_transform_only', None)
    scene.collection.objects.link(map_root)
    # Blender converts glTF raw xyz to (x,-z,y); restore the stored map frame first.
    map_root.rotation_euler = (rotation@Euler((-math.pi/2, 0, 0)).to_matrix()).to_euler()
    map_root.scale = (scale,)*3
    map_root.location = shift-center*scale
    for obj in imported_map:
        if obj.parent not in imported_map:
            obj.parent = map_root
        if obj.type == 'MESH' and obj.name.startswith('photo_'):
            index = int(obj.name.split('_')[1].split('.')[0])
            observed = data['surfaces'][index]['source_video_s']
            reveal = next((i for i, item in enumerate(samples, 1) if item[0] >= observed),
                          args.frames)
            obj['surface_observed_video_s'] = observed
            obj['reveal_semantics'] = 'Photo observation only; not point creation or surface measurement'
            obj.hide_render = obj.hide_viewport = True
            obj.keyframe_insert(data_path='hide_render', frame=0)
            obj.keyframe_insert(data_path='hide_viewport', frame=0)
            obj.hide_render = obj.hide_viewport = False
            obj.keyframe_insert(data_path='hide_render', frame=reveal)
            obj.keyframe_insert(data_path='hide_viewport', frame=reveal)
            constant_keyframes(obj)
    map_collection = bpy.data.collections.new('PHOTO_PATCHES_OBSERVATION_TIME_REPLAY')
    scene.collection.children.link(map_collection)
    for obj in [map_root, *imported_map]:
        for old in list(obj.users_collection):
            old.objects.unlink(obj)
        map_collection.objects.link(obj)
    scene.frame_set(args.frames)
    bpy.context.view_layer.update()
    import bmesh
    for obj in imported_map:
        if obj.type != 'MESH':
            continue
        original = obj.data.copy()
        original.name = obj.name+'_FULL_UNCROPPED_SOURCE'
        original.use_fake_user = True
        mesh = bmesh.new()
        mesh.from_mesh(obj.data)
        outside = [face for face in mesh.faces
                   if any(not in_panel(obj.matrix_world@vertex.co) for vertex in face.verts)]
        bmesh.ops.delete(mesh, geom=outside, context='FACES')
        mesh.to_mesh(obj.data)
        mesh.free()
    camera_points = [display(item['position']) for item in data['keyframes']]
    keyframes = data['keyframes']
    segments, segment = [], []
    for index, item in enumerate(keyframes):
        if index and item['source_video_s']-keyframes[index-1]['source_video_s'] > .5:
            segments.append(segment)
            segment = []
        segment.append(index)
    segments.append(segment)
    curves(scene, 'Saved_path_no_gap_bridges', camera_points, segments, orange, .018)
    bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=2, radius=.10)
    marker = bpy.context.object
    marker.name = 'Recorded_camera_position_sample_hold'
    marker.data.materials.append(orange)
    # A location glyph avoids guessing camera-to-body orientation or physical scale.
    for frame, (_, sample) in enumerate(samples, 1):
        marker.hide_render = marker.hide_viewport = sample is None
        if sample:
            marker.location = display(sample['position'])
        for prop in ('location', 'hide_render', 'hide_viewport'):
            marker.keyframe_insert(data_path=prop, frame=frame)
        scene.timeline_markers.new(f"video {samples[frame-1][0]:.2f}s", frame=frame)
    constant_keyframes(marker)
    bundle = ROOT/'data/interim/cad/cad_20261028'
    cad_counts = {}
    for body, location, size in [('child', (5.2, 1.02, 0), 5),
                                 ('parent', (5.2, -1.02, 0), 4.6)]:
        root, imported = load_cad_body(bundle, body)
        collection = bpy.data.collections.new(body.upper()+'_REAL_CAD_SEPARATE_INSET')
        scene.collection.children.link(collection)
        for obj in [root, *imported]:
            for old in list(obj.users_collection):
                old.objects.unlink(obj)
            collection.objects.link(obj)
        root.location = location
        root.scale = (size,)*3
        root.rotation_euler = (math.radians(58), 0, math.radians(-25))
        root['display_scale_only'] = size
        root['as_built_status'] = 'Nominal CAD; actual child camera/light swapped, exact transform unknown'
        cad_counts[body] = sum(obj.type == 'MESH' for obj in imported)
    text('Child label', '子機', (3.65, 1.98), .22)
    text('Parent label', '親機', (3.65, -.16), .22)
    # Real lights remain editable for a later Eevee/Cycles presentation export.
    for x in (-5, 5):
        bpy.ops.object.light_add(type='AREA', location=(x, -3, 10))
        bpy.context.object.data.energy = 800
        bpy.context.object.data.shape = 'DISK'
        bpy.context.object.data.size = 8
    for frame in range(1, args.frames+1):
        time_s, sample = samples[frame-1]
        label = text('Source time '+str(frame),
                     f'動画時刻 {time_s:.1f} s / '+('記録位置' if sample else '位置の欠測'),
                     (-7.45, -2.68), .20)
        for f, hidden in ((1, True), (frame, False), (min(frame+1, args.frames+1), True)):
            label.hide_render = label.hide_viewport = hidden
            label.keyframe_insert(data_path='hide_render', frame=f)
            label.keyframe_insert(data_path='hide_viewport', frame=f)
        constant_keyframes(label)
    for collection_name, predicate in (
        ('MAP_SAVED_OBSERVATIONS', lambda obj: obj in (cloud, marker)
         or obj.name == 'Saved_path_no_gap_bridges'),
        ('EDITABLE_JAPANESE_LABELS', lambda obj: obj.type == 'FONT'),
        ('CAMERA_AND_LIGHTS', lambda obj: obj.type in ('LIGHT', 'CAMERA')),
    ):
        collection = bpy.data.collections.new(collection_name)
        scene.collection.children.link(collection)
        for obj in list(scene.objects):
            if predicate(obj):
                for old in list(obj.users_collection):
                    old.objects.unlink(obj)
                collection.objects.link(obj)
    bpy.ops.file.pack_all()
    scene.frame_set(args.frames)
    bpy.ops.wm.save_as_mainfile(filepath=str(output/'presentation.blend'))
    scene.render.filepath = str(output/'presentation.png')
    bpy.ops.render.render(write_still=True)
    if args.render:
        scene.render.resolution_x, scene.render.resolution_y = 1280, 720
        (output/'frames').mkdir(exist_ok=True)
        for frame in range(1, args.frames+1):
            scene.frame_set(frame)
            scene.render.filepath = str(output/f'frames/{frame:04d}.png')
            bpy.ops.render.render(write_still=True)
    assets = json.loads((bundle/'assets.json').read_text(encoding='utf8'))
    manifest = {'schema_version': 1, 'experiment': args.experiment,
                'inputs': {str(path.relative_to(ROOT)): sha(path) for path in
                           (source, mesh_source, bundle/'assets.json', bundle/'rig.json',
                            ROOT/'calibration/child_mount_20261028_actual.json', Path(__file__))},
                'cad_hashes_verified_by_loader': assets['assets'], 'cad_mesh_counts': cad_counts,
                'display_map_transform': {'rotation_matrix': [list(row) for row in rotation],
                                          'scale': scale, 'center': list(center),
                                          'crop_projected_axis_percentiles': [1, 99],
                                          'visible_points': len(points),
                                          'source_points': len(all_points)},
                'CAD_map_registration': None, 'parent_observations_used': False,
                'point_chronology': 'unavailable; static final point map',
                'photo_surface_reveal': 'source photograph observation time, not map creation',
                'camera_replay': 'saved keyframe position; sample hold <=0.5 s, gaps hidden',
                'frames': args.frames, 'fps': 10, 'source_video_s': [samples[0][0], samples[-1][0]],
                'render_engine': scene.render.engine, 'blender_version': bpy.app.version_string,
                'git_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'],
                                                     cwd=ROOT, text=True).strip()}
    (output/'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf8')
    shutil.copy2(Path(__file__), output/'create_ngt_presentation_scene.py')
    print('PRESENTATION_COMPLETE', json.dumps(cad_counts))


if __name__ == '__main__':
    main()
