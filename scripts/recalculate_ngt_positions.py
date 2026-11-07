"""Recompute pool positions with explicit displayed-image intrinsics and verified axis convention."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import cv2
import numpy as np
from evaluate_ngt_position import (
    ACQUISITION,
    ROOT,
    build_observations,
    evaluate,
    figures,
    numbers,
    read_csv,
    write_csv,
    write_json,
)

from aquabeacon_analysis.camera_calibration import load_camera_intrinsics
from aquabeacon_analysis.camera_geometry import (
    display_geometry,
    rotate_intrinsics,
    transform_pixels,
)
from aquabeacon_analysis.ngt_binary import sha256
from aquabeacon_analysis.ngt_position import calibrated_rays

RUN='rotation_corrected_20261031'
OUT=ROOT/f'results/{ACQUISITION}/coordinate_reassessment'
FRAME_PATH=ROOT/'calibration/20261031_ngt_camera_frames.json'


def run(experiments):
    OUT.mkdir(parents=True,exist_ok=True)
    record={'pid':os.getpid(),'owner':'coordinate_reassessment','purpose':'Correct parent positions',
        'started_utc':datetime.now(UTC).isoformat(),'status':'running'}
    write_json(OUT/'positions.process.json',record)
    try:
        axis_audit=OUT/'sensor_axis_audit.json'
        audit=json.loads(axis_audit.read_text(encoding='utf-8'))
        train,hold=[audit['rankings'][k][0] for k in ['train','holdout']]
        assert train['matrix']==hold['matrix'] and hold['consistent_independent_pairs']>=3
        m_sensor=np.array(train['matrix'])
        sources={'calibration':'data/raw/okinawa_experiment/DJI_20260924183857_0021_D.LRF',
            'parent':'data/raw/ngtHighSchoolExperiment1/parent/DJI/DJI_20261028140300_0024_D.LRF',
            'child':'data/raw/ngtHighSchoolExperiment1/child/DJI/DJI_20261028140748_0186_D.LRF'}
        rotations={}; raw_stats={}
        cv2.setNumThreads(2)
        for role,path in sources.items():
            p=ROOT/path; stat=p.stat();raw_stats[path]={'size':stat.st_size,'mtime_ns':stat.st_mtime_ns}
            cap=cv2.VideoCapture(str(p),cv2.CAP_FFMPEG,[cv2.CAP_PROP_N_THREADS,2])
            try:
                assert cap.isOpened(); rotations[role]=int(cap.get(cv2.CAP_PROP_ORIENTATION_META))
            finally:cap.release()
        assert rotations=={'calibration':0,'parent':90,'child':180}
        k,d,provenance=load_camera_intrinsics(ROOT,'P_MAIN_CAM',(1280,720),'proxy')
        kd,dd,r_display,size=rotate_intrinsics(k,d,(1280,720),1)
        m_display=m_sensor@r_display.T
        frame_cfg={'date':'2026-10-31','experiment_id':ACQUISITION,'metadata_rotation_deg':rotations,
            'parent_sensor_size':[1280,720],'parent_display_size':list(size),'parent_proxy_matrix_display':kd.tolist(),
            'parent_proxy_distortion_display':dd.tolist(),'R_display_from_sensor':r_display.tolist(),
            'R_camera_imu_from_sensor_optical':m_sensor.tolist(),'R_camera_imu_from_display_optical':m_display.tolist(),
            'axis_evidence':str(axis_audit.relative_to(ROOT)),'axis_evidence_sha256':sha256(axis_audit),
            'intrinsics_source':provenance,'status':'pixel_frame_consistent_discrete_axes_checked_continuous_extrinsic_provisional',
            'child_mapping_policy':'Rotate main-video intrinsics 180 degrees; account for full-frame pixel-center resize and COLMAP +0.5 pixel convention',
            'limits':['No new continuous optical-to-IMU or lever-arm calibration','Magnetic heading accuracy unvalidated, zero declination retained','Shared underwater calibration remains provisional']}
        write_json(FRAME_PATH,frame_cfg)
        cfg_path=ROOT/'config/20261028_ngt/position_evaluation.json'
        cfg=json.loads(cfg_path.read_text(encoding='utf-8'))
        cfg.update(image_geometry='displayed LRF 720x1280; K and Brown distortion rotated from sensor frame',
            optical_to_camera_imu_rotation=m_display.tolist(),camera_frame_calibration=str(FRAME_PATH.relative_to(ROOT)),
            optical_axis_status=frame_cfg['status'])
        axis_path=ROOT/cfg['parent_body_axes']; axes=json.loads(axis_path.read_text(encoding='utf-8'))
        sync_path=ROOT/f'results/{ACQUISITION}/synchronization/manifest.json'
        sync=json.loads(sync_path.read_text(encoding='utf-8'))['cameras']['parent']
        body_cam=np.array(axes['R_body_from_spresense'])@np.array(sync['R_camera_imu']).T
        paths={'optical':ROOT/f'data/processed/{ACQUISITION}/optical/light_candidates.csv',
            'ranges':ROOT/f'data/processed/{ACQUISITION}/fusion/range_constraints.csv',
            'attitude':ROOT/f'data/processed/{ACQUISITION}/parent_magnetic_attitude/attitude.csv',
            'gnss':ROOT/f'results/{ACQUISITION}/gnss_selection/selected_positions.csv',
            'pressure':ROOT/f'data/processed/{ACQUISITION}/fusion/child_depth.csv',
            'telemetry':ROOT/f'data/interim/{ACQUISITION}/cameras/parent_telemetry.csv'}
        tables={key:read_csv(p) for key,p in paths.items()}
        segments=json.loads((ROOT/'config/20261028_ngt/analysis.json').read_text(encoding='utf-8'))['segments']
        summary={}
        for exp in dict.fromkeys(experiments):
            registry=ROOT/f'experiments/{exp}/experiment.json'
            ledger=json.loads(registry.read_text(encoding='utf-8'))
            if str(FRAME_PATH.relative_to(ROOT)).replace('\\','/') not in ledger['calibration_paths']:
                ledger['calibration_paths'].append(str(FRAME_PATH.relative_to(ROOT)).replace('\\','/'))
                write_json(registry,ledger)
            segment=next(s for s in segments if s['experiment_id']==exp)
            lo,hi=segment['parent_video_interval_s'];t=np.arange(lo,hi,1/cfg['sample_hz'])
            shots=[r for r in tables['ranges'] if lo<=float(r['parent_video_s'])<hi]
            shot_times=numbers(shots,['parent_video_s'])[:,0]
            rows=build_observations(t,**tables,cfg=cfg,sync=sync,k=kd,d=dd,body_cam=body_cam)
            events=build_observations(shot_times,**tables,cfg=cfg,sync=sync,k=kd,d=dd,body_cam=body_cam)
            legacy=read_csv(ROOT/f'results/{exp}/position_evaluation/positions_5hz.csv')
            for new,old in zip(rows,legacy,strict=True):
                for flag in ['optical_available','range_formula_available','magnetic_attitude_available','magnetic_position_available']:
                    assert new[flag]==(old[flag]=='True')
            pixel=numbers(rows,['light_x_px','light_y_px']);a,_,_=display_geometry((1280,720),1)
            inverse=transform_pixels(pixel,np.linalg.inv(a))
            sensor_rays=calibrated_rays(inverse,k,d)
            display_rays=calibrated_rays(pixel,kd,dd)
            np.testing.assert_allclose(display_rays,sensor_rays@r_display.T,atol=1e-10,equal_nan=True)
            xyz=numbers(rows,['relative_magnetic_north_m','relative_magnetic_east_m','relative_down_m'])
            oldxyz=numbers(legacy,['relative_magnetic_north_m','relative_magnetic_east_m','relative_down_m'])
            delta=np.linalg.norm(xyz-oldxyz,axis=1); good=np.isfinite(delta)
            np.testing.assert_allclose(np.linalg.norm(xyz[good],axis=1),numbers(rows,['range_m'])[good,0],atol=1e-10)
            out=ROOT/f'results/{exp}/position_evaluation/{RUN}';out.mkdir(parents=True,exist_ok=True)
            write_csv(out/'positions_5hz.csv',rows);write_csv(out/'positions_at_acoustic_events.csv',events)
            metrics=evaluate(rows,events,lo,hi,cfg)
            metrics['change_from_legacy']={'samples':int(good.sum()),'median_displacement_m':float(np.median(delta[good])),
                'p95_displacement_m':float(np.percentile(delta[good],95)),'max_displacement_m':float(np.max(delta[good]))}
            write_json(out/'metrics.json',metrics);write_csv(out/'outages.csv',metrics['missing_intervals'])
            figures(out,rows,events,segment['label']+' / rotation corrected')
            inputs=[*paths.values(),cfg_path,FRAME_PATH,axis_path,sync_path,axis_audit,registry,
                ROOT/'calibration/camera_defaults.json',Path(provenance['source']),Path(__file__),
                ROOT/'scripts/evaluate_ngt_position.py',ROOT/'src/aquabeacon_analysis/camera_geometry.py',
                ROOT/'src/aquabeacon_analysis/ngt_position.py']
            write_json(out/'manifest.json',{'experiment_id':exp,'run':RUN,'created_utc':datetime.now(UTC).isoformat(),
                'git_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
                'inputs_sha256':{str(p.relative_to(ROOT)):sha256(p) for p in inputs},'config':cfg,'frame_calibration':frame_cfg,
                'R_body_from_camera_imu':body_cam.tolist(),'ground_truth':None,'raw_source_stats':raw_stats,
                'invariants':['all legacy availability flags unchanged','range preserved','sensor and display ray formulations agree to 1e-10']})
            shutil.copy2(__file__,out/'generate.py')
            (out/'report.md').write_text('# 回転補正後の位置推定\n\n[再評価の統合レポート](../../../20261028_ngt_000/coordinate_reassessment/report.md)。\n',encoding='utf-8')
            summary[exp]=metrics;print(exp,json.dumps(metrics['change_from_legacy']),flush=True)
        write_json(OUT/'position_summary.json',summary)
        write_json(OUT/'raw_source_stats.json',raw_stats)
        record['status']='completed'
    except BaseException as error:
        record.update(status='failed',error=str(error));raise
    finally:
        record['ended_utc']=datetime.now(UTC).isoformat();write_json(OUT/'positions.process.json',record)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment',required=True,action='append',choices=[f'20261028_ngt_{i:03}' for i in [2,3,4]])
    run(parser.parse_args().experiment)


if __name__=='__main__':main()
