"""Rerun all native-frame mapping windows using corrected parent priors and child image geometry."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

os.environ.setdefault('OPENBLAS_NUM_THREADS','2')
os.environ.setdefault('OMP_NUM_THREADS','4')
import cv2
import numpy as np
import psutil
import run_ngt_restart_mapping as legacy

from aquabeacon_analysis.camera_calibration import load_camera_intrinsics
from aquabeacon_analysis.camera_geometry import resize_intrinsics_pixel_centers, rotate_intrinsics
from aquabeacon_analysis.ngt_binary import sha256
from aquabeacon_analysis.ngt_position import bracket_interpolate
from aquabeacon_analysis.restart_mapping import (
    position_registration,
    prior_anchor_indices,
    temporal_windows,
)

ROOT=legacy.ROOT
EXPERIMENT=legacy.EXPERIMENT
RUN='rotation_corrected_20261031'
WORK=ROOT/f'data/interim/{EXPERIMENT}/wall_mapping/{RUN}'
OUT=ROOT/f'results/{EXPERIMENT}/wall_mapping/{RUN}'
OLD_WORK=legacy.WORK
OLD_OUT=legacy.OUT
DATABASE=WORK/'features_full_corrected.db'
IMAGES=legacy.SOURCE_IMAGES
POSITION=ROOT/f'results/{EXPERIMENT}/position_evaluation/{RUN}/positions_5hz.csv'
CONFIG={**legacy.CONFIG,'run':RUN,'image_geometry':'original native JPEG 1280x720, displayed 180 degrees',
    'keypoint_transport_px':[640,36],'colmap_pixel_center_offset':.5,
    'num_threads':4,'final_model_cap_by_window':{str(i):200 if i>=15 else 10 for i in range(19)},
    'hard_timeout_seconds_per_window':240,'total_deadline_seconds':5400,
    'scope':'Parent position correction plus child image-frame correction; child-only VSLAM unchanged'}
read_csv,write_csv,write_json=legacy.read_csv,legacy.write_csv,legacy.write_json
colmap=legacy.colmap


def rows_and_lookup():
    rows=read_csv(WORK/'optimizer_frame_priors.csv')
    return rows,{r['image_name']:r for r in rows}


def descriptor_digest(connection):
    h=hashlib.sha256()
    for key,rows,cols,blob in connection.execute('SELECT image_id,rows,cols,data FROM descriptors ORDER BY image_id'):
        h.update(np.array([key,rows,cols],dtype=np.int64).tobytes());h.update(blob)
    return h.hexdigest()


def prepare():
    if (OUT/'manifest.json').exists():
        m=json.loads((OUT/'manifest.json').read_text(encoding='utf-8'))
        assert m['corrected_position_sha256']==sha256(POSITION)
        print('Prepared corrected database already exists; preserving it',flush=True);return
    if DATABASE.exists():
        raise RuntimeError('Incomplete corrected database exists; inspect it before resuming preparation')
    old_rows=read_csv(OLD_WORK/'optimizer_frame_priors.csv')
    assert len(old_rows)==33564
    positions=read_csv(POSITION)
    t=np.array([float(r['parent_video_s']) for r in positions])
    values=np.array([[float(r['child_east_declination0_m']),float(r['child_north_declination0_m']),-float(r['pressure_depth_m'])]
        if r['magnetic_position_available']=='True' else [np.nan]*3 for r in positions])
    times=np.array([float(r['parent_video_s']) for r in old_rows])
    priors=bracket_interpolate(t,values,times,CONFIG['prior_max_bracket_s'])
    anchors=set(prior_anchor_indices(times,np.isfinite(priors).all(axis=1),1/CONFIG['optimizer_prior_hz']))
    rows=[]
    for i,(old,prior) in enumerate(zip(old_rows,priors,strict=True)):
        row={**old,'legacy_image_name':old['image_name'],'image_name':old['source_image_name'],
            'prior_available':bool(np.isfinite(prior).all()),'optimizer_prior':i in anchors,
            'east_m':prior[0],'north_m':prior[1],'up_m':prior[2]}
        assert row['prior_available']==(old['prior_available']=='True')
        assert row['optimizer_prior']==(old['optimizer_prior']=='True')
        if sha256(IMAGES/row['image_name'])!=row['image_sha256']:
            raise ValueError('Native image changed: '+row['image_name'])
        rows.append(row)
    write_csv(WORK/'optimizer_frame_priors.csv',rows)
    print('All 33564 source-image hashes verified',flush=True)
    k,d,provenance=load_camera_intrinsics(ROOT,'C_MAIN_CAM',(1920,1080),'main')
    small=resize_intrinsics_pixel_centers(k,(1920,1080),(1280,720))
    kd,dd,_,size=rotate_intrinsics(small,d,(1280,720),2)
    k_colmap=kd.copy();k_colmap[:2,2]+=.5
    # Reuse exactly the old SIFT descriptors, but express keypoints in full-image coordinates.
    # This retains the same observed ROI without making a crop whose principal point is outside.
    old_db=OLD_WORK/'features_crop.db'
    before=sha256(old_db)
    with sqlite3.connect(f'file:{old_db.as_posix()}?mode=ro',uri=True) as source:
        descriptors_before=descriptor_digest(source)
        with sqlite3.connect(DATABASE) as target:
            source.backup(target,pages=4096)
    with sqlite3.connect(DATABASE) as db:
        cursor=db.execute('SELECT image_id,rows,cols,data FROM keypoints ORDER BY image_id')
        seen=0
        for image_id,n,cols,blob in cursor:
            p=np.frombuffer(blob,dtype=np.float32).reshape(n,cols).copy()
            p[:,:2]+=np.array([640,36],dtype=np.float32)
            db.execute('UPDATE keypoints SET data=? WHERE image_id=?',(p.tobytes(),image_id));seen+=1
        assert seen==33564
        for row in rows:
            db.execute('UPDATE images SET name=? WHERE image_id=?',(row['image_name'],int(row['image_id'])))
        assert descriptor_digest(db)==descriptors_before
    lookup={r['image_name']:r for r in rows}
    with colmap.Database.open(DATABASE) as db:
        cameras=db.read_all_cameras()
        # The resumed legacy extraction has two identical camera records; retain both IDs.
        assert cameras and all(np.allclose(c.params,cameras[0].params) for c in cameras)
        for camera in cameras:
            camera.width,camera.height=size
            camera.params=np.array([k_colmap[0,0],k_colmap[1,1],k_colmap[0,2],k_colmap[1,2],*dd[:4]])
            assert not camera.has_bogus_params(.1,10,1)
            db.update_camera(camera)
        db.clear_pose_priors();db.clear_two_view_geometries()
        for im in db.read_all_images():
            row=lookup[im.name]
            if row['optimizer_prior']:
                prior=colmap.PosePrior();prior.corr_data_id=im.data_id
                prior.position=[row[k] for k in ['east_m','north_m','up_m']]
                prior.position_covariance=np.diag(CONFIG['prior_sigma_m'])**2
                prior.coordinate_system=colmap.PosePriorCoordinateSystem.CARTESIAN
                db.write_pose_prior(prior)
    pairs=WORK/'pairs.txt'
    pairs.write_text((OLD_WORK/'pairs.txt').read_text().replace('.png','.jpg'),encoding='utf-8')
    options=colmap.TwoViewGeometryOptions()
    options.ransac.max_num_trials=3000;options.ransac.random_seed=7
    print('Re-verifying descriptor matches using corrected child intrinsics',flush=True)
    legacy.with_deadline(lambda token:colmap.verify_matches(DATABASE,pairs,options=options,cancellation_token=token),900)
    assert sha256(old_db)==before
    # Verify that the reused JPEGs are the automatic 180-degree displayed frames.
    video=ROOT/'data/raw/ngtHighSchoolExperiment1/child/DJI/DJI_20261028140748_0186_D.MP4'
    cap=cv2.VideoCapture(str(video),cv2.CAP_FFMPEG,[cv2.CAP_PROP_N_THREADS,2]);camera_checks=[]
    try:
        assert cap.get(cv2.CAP_PROP_ORIENTATION_META)==180
        for index in [0,len(rows)//2,len(rows)-1]:
            cap.set(cv2.CAP_PROP_POS_FRAMES,int(rows[index]['source_frame']));ok,frame=cap.read();assert ok
            resized=cv2.resize(frame,size,interpolation=cv2.INTER_AREA)
            saved=cv2.imread(str(IMAGES/rows[index]['image_name']))
            direct=float(np.mean(np.abs(resized.astype(float)-saved)))
            opposite=float(np.mean(np.abs(cv2.rotate(resized,cv2.ROTATE_180).astype(float)-saved)))
            assert direct<opposite/2
            camera_checks.append({'index':index,'mean_abs_pixel_difference':direct,'opposite_rotation_difference':opposite})
    finally:cap.release()
    with colmap.Database.open(DATABASE) as db:
        database_counts={'images':db.num_images(),'verified_pairs':db.num_verified_image_pairs(),'priors':db.num_pose_priors()}
    inputs=[POSITION,ROOT/'calibration/20261031_ngt_camera_frames.json',ROOT/'calibration/camera_defaults.json',
        Path(provenance['source']),OLD_WORK/'optimizer_frame_priors.csv',OLD_WORK/'pairs.txt',Path(__file__),
        ROOT/'src/aquabeacon_analysis/camera_geometry.py',ROOT/'src/aquabeacon_analysis/restart_mapping.py']
    manifest={'experiment_id':EXPERIMENT,'run':RUN,'created_utc':datetime.now(UTC).isoformat(),'config':CONFIG,
        'git_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        'inputs_sha256':{str(p.relative_to(ROOT)):sha256(p) for p in inputs},'corrected_position_sha256':sha256(POSITION),
        'source_database_sha256':before,'source_database_unchanged':True,'descriptor_digest':descriptors_before,
        'all_native_image_hashes_verified':len(rows),'keypoints_transport_exact':True,
        'parent_interval_s':[float(times[0]),float(times[-1])],'input_frames':len(rows),
        'camera_matrix_opencv_display':kd.tolist(),'camera_matrix_colmap_display':k_colmap.tolist(),'distortion_display':dd.tolist(),
        'child_image_orientation_checks':camera_checks,'database_counts':database_counts,
        'method':'Same native images and SIFT observations; pixel coordinate transport, corrected child K/d, fresh two-view verification and fresh incremental mapping with corrected parent priors',
        'geometry_reuse':'Raw descriptor matches reused; old two-view geometry and old reconstructions not used as initialization',
        'calibration_limits':'Shared underwater K remains provisional; no surveyed wall dimensions or absolute-position truth'}
    write_json(OUT/'manifest.json',manifest);write_json(OUT/'config.json',CONFIG)
    shutil.copy2(__file__,OUT/'generate.py')
    print(json.dumps(database_counts),flush=True)


def fixed_shape_scale_comparison():
    _rows,lookup=rows_and_lookup()
    original=json.loads((OLD_OUT/'models.json').read_text(encoding='utf-8'))
    output=[];fits=[]
    for item in original:
        if not item['structural']:
            continue
        model=colmap.Reconstruction(ROOT/item['folder'])
        paired=[im for im in model.images.values() if im.has_pose and lookup[Path(im.name).with_suffix('.jpg').name]['optimizer_prior']=='True']
        paired.sort(key=lambda im:im.name)
        a=np.array([im.projection_center() for im in paired]).reshape(-1,3)
        b=np.array([[float(lookup[Path(im.name).with_suffix('.jpg').name][k]) for k in ['east_m','north_m','up_m']] for im in paired]).reshape(-1,3)
        fit=position_registration(a,b,CONFIG['position_fit_min_second_first_ratio'],CONFIG['position_fit_huber_delta_m'])
        old=item['position_fit'];old_scale=old.get('scale',float('nan'))
        output.append({'window':item['window'],'model':item['model'],'wall_period':item['wall_period'],
            'old_accepted':item['position_registered'],'new_accepted':fit['accepted'],'anchors':len(paired),
            'old_scale':old_scale,'corrected_scale_same_shape':fit.get('scale',float('nan')),
            'scale_ratio_corrected_over_old':fit.get('scale',float('nan'))/old_scale,
            'old_fit_rms_m':old.get('fit_rms_m',float('nan')),'corrected_fit_rms_m':fit.get('fit_rms_m',float('nan')),
            'reason':fit['reason']})
        fits.append({'window':item['window'],'model':item['model'],'fit':fit})
    write_csv(OUT/'fixed_shape_scale_comparison.csv',output);write_json(OUT/'fixed_shape_fits.json',fits)


def window(number):
    rows,lookup=rows_and_lookup()
    times=np.array([float(r['parent_video_s']) for r in rows])
    windows=temporal_windows(times,CONFIG['window_width_s'],CONFIG['window_stride_s'])
    indices=windows[number];selected=[rows[i] for i in indices]
    model_root=WORK/f'window_{number:02d}';model_root.mkdir(parents=True,exist_ok=True)
    result={'window':number,'input_frames':len(selected),'first_index':int(indices[0]),'last_index':int(indices[-1]),
        'first_parent_s':float(times[indices[0]]),'last_parent_s':float(times[indices[-1]]),
        'prior_frames':sum(r['prior_available']=='True' for r in selected),'models':[],
        'model_limit':CONFIG['final_model_cap_by_window'][str(number)],'runtime_limit_s':120}
    options=colmap.IncrementalPipelineOptions(num_threads=4,random_seed=7,
        image_names=[r['image_name'] for r in selected],max_num_models=result['model_limit'],
        min_model_size=10,init_num_trials=100,ba_refine_focal_length=False,ba_refine_principal_point=False,
        ba_refine_extra_params=False,ba_local_max_num_iterations=15,ba_global_max_num_iterations=25,
        ba_global_max_refinements=2,ba_min_num_residuals_for_cpu_multi_threading=1,max_runtime_seconds=120,
        use_prior_position=True,use_robust_loss_on_prior_position=True)
    options.mapper.num_threads=4;options.mapper.init_min_num_inliers=30;options.mapper.init_min_tri_angle=1.5
    options.mapper.abs_pose_min_num_inliers=20;options.mapper.abs_pose_refine_focal_length=False
    options.mapper.abs_pose_refine_extra_params=False;options.triangulation.min_angle=1.;options.mapper.filter_min_tri_angle=1.
    begun=time.monotonic()
    models=colmap.incremental_mapping(DATABASE,IMAGES,model_root,options=options)
    for number_model,model in models.items():
        result['models'].append({'model':int(number_model),**legacy.export_model(model,model_root/str(number_model),lookup)})
    result.update(status='models_returned' if models else 'no_model',elapsed_s=time.monotonic()-begun)
    result['time_limit_may_have_truncated']=result['elapsed_s']>=120
    result['model_count_limit_reached']=len(models)>=result['model_limit']
    write_json(OUT/f'window_{number:02d}/summary.json',result)
    print(json.dumps({'window':number,'models':len(models),'positioned':sum(m['position_registered'] for m in result['models']),'seconds':result['elapsed_s']}),flush=True)


def run_all(record):
    prepare();fixed_shape_scale_comparison()
    start=time.monotonic()
    record['children']=[]
    for number in range(19):
        if (OUT/f'window_{number:02d}/summary.json').exists():
            continue
        if time.monotonic()-start>CONFIG['total_deadline_seconds']:
            raise TimeoutError('Mapping total time allowance reached')
        command=[sys.executable,'-u',str(Path(__file__).resolve()),'--experiment',EXPERIMENT,'--phase','window','--window',str(number)]
        with (OUT/f'window_{number:02d}.log').open('w',encoding='utf-8') as log:
            child=subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            item={'pid':child.pid,'started_utc':datetime.now(UTC).isoformat(),'owner':'coordinate_reassessment',
                'purpose':f'Corrected mapping window {number:02d}','command':command}
            record['children'].append(item);write_json(OUT/'all.process.json',record)
            write_json(OUT/'progress.json',{'active_window':number,'completed_windows':number,'total_windows':19})
            try:
                code=child.wait(timeout=CONFIG['hard_timeout_seconds_per_window'])
            except subprocess.TimeoutExpired:
                child.terminate()
                try:child.wait(timeout=5)
                except subprocess.TimeoutExpired:child.kill();child.wait(timeout=5)
                code=child.returncode;item['hard_timeout']=True
            item.update(exit_code=code,ended_utc=datetime.now(UTC).isoformat())
            if code!=0:
                write_json(OUT/f'window_{number:02d}/summary.json',{'window':number,'models':[],
                    'status':'hard_timeout' if item.get('hard_timeout') else 'process_failed','exit_code':code})
            write_json(OUT/'all.process.json',record)
            summary=json.loads((OUT/f'window_{number:02d}/summary.json').read_text(encoding='utf-8'))
            print(json.dumps({'finished_window':number,'status':summary['status'],'models':len(summary['models'])}),flush=True)
    write_json(OUT/'progress.json',{'completed_windows':19,'total_windows':19,'status':'completed'})


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment',required=True,choices=[EXPERIMENT])
    parser.add_argument('--phase',required=True,choices=['all','prepare','window','fixed-scale'])
    parser.add_argument('--window',type=int)
    args=parser.parse_args()
    WORK.mkdir(parents=True,exist_ok=True);OUT.mkdir(parents=True,exist_ok=True)
    process=psutil.Process();affinity=process.cpu_affinity()[-4:];process.cpu_affinity(affinity)
    cv2.setNumThreads(2)
    record={'pid':os.getpid(),'owner':'coordinate_reassessment','purpose':args.phase,'started_utc':datetime.now(UTC).isoformat(),
        'status':'running','cpu_affinity':affinity,'threads':4}
    process_path=OUT/(f'window_{args.window:02d}.process.json' if args.phase=='window' else f'{args.phase}.process.json')
    write_json(process_path,record)
    # export_model reads only CONFIG/ROOT from the legacy implementation, with identical acceptance rules.
    legacy.CONFIG.update(CONFIG)
    try:
        if args.phase=='all':run_all(record)
        elif args.phase=='prepare':prepare()
        elif args.phase=='fixed-scale':fixed_shape_scale_comparison()
        else:
            if args.window not in range(19):raise ValueError('Window must be 0..18')
            window(args.window)
        record['status']='completed'
    except BaseException as e:
        record.update(status='failed',error=str(e));raise
    finally:
        record['ended_utc']=datetime.now(UTC).isoformat();write_json(process_path,record)


if __name__=='__main__':main()
