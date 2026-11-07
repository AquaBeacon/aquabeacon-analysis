"""Re-establish sensor optical axes using correctly oriented images and same-run IMU motion."""
from __future__ import annotations

import argparse
import itertools
import json
import os
from datetime import UTC, datetime
from pathlib import Path

os.environ.setdefault('OPENBLAS_NUM_THREADS','2')
os.environ.setdefault('OMP_NUM_THREADS','2')
import cv2
import numpy as np
from scipy.spatial.transform import Rotation, Slerp

from aquabeacon_analysis.ngt_binary import sha256
from aquabeacon_analysis.ngt_sync import numeric_csv

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'results/20261028_ngt_000/coordinate_reassessment'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment',required=True,choices=['20261028_ngt_000'])
    parser.parse_args()
    OUT.mkdir(parents=True,exist_ok=True)
    process={'pid':os.getpid(),'started_utc':datetime.now(UTC).isoformat(),'owner':'coordinate_reassessment',
        'purpose':'Read-only image and IMU coordinate audit','status':'running'}
    path=OUT/'axis_audit.process.json'
    path.write_text(json.dumps(process,indent=2))
    cv2.setNumThreads(2); cv2.setRNGSeed(7)
    source=ROOT/'data/raw/ngtHighSchoolExperiment1/parent/DJI/DJI_20261028140300_0024_D.LRF'
    telemetry=ROOT/'data/interim/20261028_ngt_000/cameras/parent_telemetry.csv'
    a=numeric_csv(telemetry,['video_time_s','qw','qx','qy','qz'])
    slerp=Slerp(a[:,0],Rotation.from_quat(a[:,[2,3,4,1]]))
    cap=cv2.VideoCapture(str(source),cv2.CAP_FFMPEG,[cv2.CAP_PROP_N_THREADS,2])
    cap.set(cv2.CAP_PROP_ORIENTATION_AUTO,0)
    records=[]; previous=None
    try:
        for t in np.arange(150,240,.5):
            cap.set(cv2.CAP_PROP_POS_FRAMES,round(t*cap.get(cv2.CAP_PROP_FPS)))
            ok,frame=cap.read()
            if not ok:
                raise RuntimeError('Source frame decode failed')
            pts=cap.get(cv2.CAP_PROP_POS_MSEC)/1000
            assert frame.shape[:2]==(720,1280)
            gray=cv2.cvtColor(frame,cv2.COLOR_BGR2GRAY)
            if previous is not None:
                t0,g0=previous
                p=cv2.goodFeaturesToTrack(g0,800,.01,12)
                if p is not None:
                    q,status,_=cv2.calcOpticalFlowPyrLK(g0,gray,p,None)
                    back,status2,_=cv2.calcOpticalFlowPyrLK(gray,g0,q,None)
                    keep=(status[:,0]==1)&(status2[:,0]==1)&(np.linalg.norm(back[:,0]-p[:,0],axis=1)<1)
                    p,q=p[keep,0],q[keep,0]
                    imu=(slerp(t0).inv()*slerp(pts)).as_rotvec()
                    if len(p)>50 and .03<np.linalg.norm(imu)<.7:
                        for focal in [450.,550.,671.]:
                            k=np.array([[focal,0,640],[0,focal,360],[0,0,1.]])
                            e,mask=cv2.findEssentialMat(p,q,k,cv2.RANSAC,.999,1.,maxIters=2000)
                            if e is None or e.shape!=(3,3):
                                continue
                            count,r,_,_=cv2.recoverPose(e,p,q,k,mask=mask)
                            optical=Rotation.from_matrix(r.T).as_rotvec()
                            if count>=30 and np.linalg.norm(optical)<.7:
                                records.append({'t0':t0,'t1':pts,'focal_px':focal,'inliers':count,
                                    'imu':imu.tolist(),'optical_sensor':optical.tolist()})
            previous=(pts,gray)
        ranks={}
        for label,selected in [('train',[r for r in records if r['t0']<195]),
            ('holdout',[r for r in records if r['t0']>=195]),('all',records)]:
            candidates=[]
            for permutation in itertools.permutations(range(3)):
                for signs in itertools.product([-1,1],repeat=3):
                    m=np.eye(3)[:,permutation]@np.diag(signs)
                    if np.linalg.det(m)<0:
                        continue
                    residual=np.array([np.linalg.norm(np.array(r['imu'])-m@r['optical_sensor'])/np.linalg.norm(r['imu']) for r in selected])
                    good=[r for r,v in zip(selected,residual,strict=True) if v<.35]
                    candidates.append({'matrix':m.tolist(),'median_relative_residual':float(np.median(residual)),
                        'consistent_independent_pairs':len({r['t0'] for r in good}),'records':len(selected)})
            ranks[label]=sorted(candidates,key=lambda r:r['median_relative_residual'])
        result={'source':str(source.relative_to(ROOT)),'sensor_size':[1280,720],
            'orientation_auto':False,'metadata_rotation_deg':cap.get(cv2.CAP_PROP_ORIENTATION_META),
            'method':'Essential motion / LK forward-backward, raw sensor pixels, correct principal point; air focal sensitivity 450/550/671 px',
            'scope':'Discrete optical-to-DJI-IMU axis convention, not continuous extrinsic or underwater intrinsic calibration',
            'records':records,'rankings':ranks,'inputs_sha256':{str(p.relative_to(ROOT)):sha256(p) for p in [telemetry,Path(__file__)]}}
        (OUT/'sensor_axis_audit.json').write_text(json.dumps(result,indent=2))
        print(json.dumps({k:v[:2] for k,v in ranks.items()}),flush=True)
        process['status']='completed'
    except BaseException as e:
        process.update(status='failed',error=str(e));raise
    finally:
        cap.release();process['ended_utc']=datetime.now(UTC).isoformat()
        path.write_text(json.dumps(process,indent=2))


if __name__=='__main__':
    main()
