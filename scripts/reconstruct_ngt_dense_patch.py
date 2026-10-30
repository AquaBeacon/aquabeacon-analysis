"""Bounded child-only three-photo-supported stereo reconstruction; no hole filling."""
from __future__ import annotations

import os

os.environ.setdefault('OMP_NUM_THREADS','2')
os.environ.setdefault('OPENBLAS_NUM_THREADS','1')

import argparse
import hashlib
import json
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import cv2
import numpy as np
import trimesh
from PIL import Image, ImageDraw, ImageFont
from scipy.spatial import cKDTree
from scipy.spatial.transform import Rotation

from aquabeacon_analysis.ngt_dense_patch import (
    consistent_disparity,
    grid_surface,
    independent_photo_support,
    relative_camera_pose,
    triangulate_rectified,
)
from aquabeacon_analysis.ngt_textured_map import camera_projection

ROOT=Path(__file__).resolve().parents[1]


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def write(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')


def preview_and_verify(experiment):
    """Render measured local triangles without a browser, server or graphics worker."""
    output=ROOT/'results'/experiment/'dense_photogrammetry'
    manifest=json.loads((output/'manifest.json').read_text(encoding='utf-8'))
    metrics=json.loads((output/'pair_metrics.json').read_text(encoding='utf-8'))
    npoints=nfaces=0
    for record in metrics:
        if 'retained_points' not in record:
            continue
        pair=record['pair_index'];npz=np.load(output/f'pair_{pair:02d}_correspondences.npz')
        vertices=npz['world_xyz'];faces=npz['faces']
        assert len(vertices)==record['retained_points']
        assert (npz['disparity_px']>=1.5).all()
        assert (npz['left_right_error_px']<=1).all()
        assert (npz['third_photo_ncc']>=.7).all()
        xy=npz['rectified_xy'];p1=npz['projection_left'];p2=npz['projection_right']
        axis=int(abs(p2[1,3])>abs(p2[0,3]))
        rect=triangulate_rectified(xy,npz['disparity_px'],p1,p2,axis=axis) if len(xy) else np.empty((0,3))
        a=record['images'][0];rcw=Rotation.from_quat(a['quaternion_xyzw']).as_matrix()
        np.testing.assert_allclose(rect@npz['rectified_left_rotation']@rcw.T+a['position'],vertices,atol=1e-10)
        assert np.isfinite(vertices).all()
        valid=npz['valid_grid'];ids=np.full(valid.shape,-1);ids[valid]=np.arange(valid.sum())
        y,x=np.where(valid)
        if len(faces):
            assert faces.min()>=0 and faces.max()<len(vertices)
            assert (np.ptp(x[faces],axis=1)<=1).all() and (np.ptp(y[faces],axis=1)<=1).all()
            z=npz['depth_grid'][valid][faces]
            assert (z.max(axis=1)/z.min(axis=1)<=1.05+1e-12).all()
        npoints+=len(vertices);nfaces+=len(faces)
    assert npoints==manifest['retained_sample_points'] and nfaces==manifest['local_triangles']
    if nfaces:
        model=trimesh.load(output/'dense_local_surface.glb',force='scene',process=False)
        assert sum(len(g.faces) for g in model.geometry.values())==nfaces
        assert all(g.visual.material.doubleSided for g in model.geometry.values())
    best=max((r for r in metrics if r.get('retained_points',0)),key=lambda r:r['retained_points']) if npoints else None
    if best:
        pair=best['pair_index'];npz=np.load(output/f'pair_{pair:02d}_correspondences.npz')
        vertices,faces=npz['world_xyz'],npz['faces'];xy=npz['rectified_xy']
        photo=cv2.imread(str(output/f'pair_{pair:02d}_left_rectified.jpg'))
        rotation=Rotation.from_quat(best['images'][0]['quaternion_xyzw']).as_matrix()
        local=(vertices-np.median(vertices,axis=0))@rotation
        turn=Rotation.from_euler('y',25,degrees=True).as_matrix()
        local=local@turn
        scale=430/max(np.ptp(local[:,0]),np.ptp(local[:,1]),1e-9)
        screen=local[:,:2]*scale+[300,260]
        view=np.full((520,600,3),245,np.uint8)
        for face in faces[np.argsort(local[faces,2].mean(axis=1))[::-1]]:
            dest=screen[face].astype(np.float32)
            source_xy=xy[face].astype(np.float32)
            x0,y0,w,h=cv2.boundingRect(dest)
            if x0<0 or y0<0 or x0+w>600 or y0+h>520:
                continue
            transform=cv2.getAffineTransform(source_xy,(dest-[x0,y0]).astype(np.float32))
            patch=cv2.warpAffine(photo,transform,(w,h))
            mask=np.zeros((h,w),np.uint8);cv2.fillConvexPoly(mask,np.rint(dest-[x0,y0]).astype(int),255)
            view[y0:y0+h,x0:x0+w][mask>0]=patch[mask>0]
        annotated=photo.copy()
        for px,py in xy.astype(int):
            cv2.circle(annotated,(px,py),2,(0,80,255),-1)
        canvas=Image.new('RGB',(1280,680),'#f5f5f5');draw=ImageDraw.Draw(canvas)
        font=ImageFont.truetype('C:/Windows/Fonts/meiryo.ttc',21)
        small=ImageFont.truetype('C:/Windows/Fonts/meiryo.ttc',16)
        draw.text((24,14),'実写真の対応点から三角測量した局所パッチ',font=font,fill='#18323f')
        canvas.paste(Image.fromarray(cv2.cvtColor(annotated,cv2.COLOR_BGR2RGB)),(20,110))
        canvas.paste(Image.fromarray(cv2.cvtColor(view,cv2.COLOR_BGR2RGB)),(670,85))
        draw.text((24,65),'赤点：第三の実写真でも支持された測定画素',font=small,fill='#333333')
        draw.text((680,65),f'右：{best["retained_points"]}点 / {best["triangles"]}三角形の局所拡大',font=small,fill='#333333')
        draw.text((24,565),'補間・穴埋めなし。残ったごく小さい領域だけを表示。全プールの密な復元ではありません。',font=small,fill='#333333')
        draw.text((24,600),'同じ推定地図の任意尺度。25度斜視・自動拡大表示。実寸・形状精度は未検証。',font=small,fill='#333333')
        canvas.save(output/'measured_patch_preview.png')
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt

        from aquabeacon_analysis.editable_plots import save_editable_figure
        fig,ax=plt.subplots(figsize=(8,4.5))
        points=ax.scatter(xy[:,0],xy[:,1],c=npz['disparity_px'],s=8,cmap='viridis',
                          label='Measured correspondences supported by third photo')
        ax.set(xlim=(0,640),ylim=(360,0),xlabel='Rectified left image x (px)',
               ylabel='Rectified left image y (px)',title='Retained actual stereo samples / best local patch')
        ax.set_aspect('equal');ax.legend(fontsize=7,loc='lower right')
        fig.colorbar(points,ax=ax,label='Measured disparity (px)')
        fig.tight_layout();save_editable_figure(fig,output/'retained_disparities.png',dpi=160)
        plt.close(fig)
    # The preview phase changes only diagnostics/provenance; no stereo recomputation.
    for relative,expected in manifest['input_sha256'].items():
        if relative!='scripts/reconstruct_ngt_dense_patch.py':
            assert sha(ROOT/relative)==expected,relative
    manifest['input_sha256']['scripts/reconstruct_ngt_dense_patch.py']=sha(Path(__file__).resolve())
    manifest['total_unique_pair_trials']=8
    manifest['attempt_history']=[{'directory':'attempt_01_forward_motion','pairs':4,'retained_points':0},
                                {'directory':'.','pairs':4,'retained_points':npoints}]
    manifest['artifacts_sha256']={p.relative_to(output).as_posix():sha(p) for p in output.rglob('*') if p.is_file() and p.name not in ['manifest.json','report.md','verification.json']}
    write(output/'manifest.json',manifest)
    report=output/'report.md'
    text=report.read_text(encoding='utf-8')
    heading='## 8組の試行と未達成範囲'
    if heading not in text:
        text+='''
## 8組の試行と未達成範囲

**密で連続した3Dモデルは達成していない。** 保存できたのは269点・301三角形の小さい局所パッチだけ。
初回4組は前後移動に偏り、極端な整列画像のcrop・低テクスチャ・一部非正焦点距離となり、
第三写真の支持を持つ点は0だった。[初回の記録](attempt_01_forward_motion/pair_metrics.json)と
[失敗画像](attempt_01_forward_motion/pair_00_diagnostic.jpg)を残した。
姿勢だけを使う走査で横方向のベースラインを選び直した追加4組で269点を得た。
総試行は8組。校正値・左右一致・第三写真支持の閾値は緩和していない。

[実写真と局所パッチの代表レンダリング](measured_patch_preview.png) /
[左右実画像・視差・支持画素の比較](pair_01_diagnostic.jpg) /
[検証記録](verification.json) /
[実測視差の編集可能図](retained_disparities.figure.pickle)

実測視差図はPNG/SVG/pickle/plot.jsonを共通保存処理で保存。
NPZ・原画像・config・生成コードを保持し、画像の見た目と測定値を分ける。
原本・元の疎点・姿勢・既存presentation_mappingは変更していない。
'''
        report.write_text(text,encoding='utf-8')
    result={'status':'passed','checked_utc':datetime.now(UTC).isoformat(),'points':npoints,'triangles':nfaces,
            'checks':['all vertices independently retriangulated from saved actual positive disparities',
                      'left-right consistency<=1px; third-photo NCC>=0.7','local grid faces bridge no invalid samples',
                      'face depth ratio<=1.05; indices finite and valid','GLB face count and double-sided material',
                      'source hashes unchanged; preview phase no stereo recomputation'],
            'metric_accuracy':'unvalidated','browser_rendering':'not used',
            'limitations':'Only tiny independent photo-supported patches, no dense continuous pool reconstruction.'}
    write(output/'verification.json',result);print(json.dumps(result))


def choose_pairs(data,k):
    points=np.asarray(data['points']['xyz']);frames=data['keyframes'];pairs=[]
    for start in [0,10,445,460]:
        candidates=[]
        for end in range(start+12,min(start+51,len(frames)),3):
            a,b=frames[start],frames[end]
            baseline=np.asarray(b['position'])-a['position']
            forward=Rotation.from_quat(a['quaternion_xyzw']).as_matrix()[:,2]
            lateral_angle=float(np.rad2deg(np.arccos(np.clip(abs(baseline@forward)/np.linalg.norm(baseline),0,1))))
            if lateral_angle<60:
                continue
            uv1,z1=camera_projection(points,a['position'],a['quaternion_xyzw'],k)
            uv2,z2=camera_projection(points,b['position'],b['quaternion_xyzw'],k)
            good=(z1>0)&(z2>0)&(uv1[:,0]>5)&(uv1[:,0]<635)&(uv1[:,1]>5)&(uv1[:,1]<355)&(uv2[:,0]>5)&(uv2[:,0]<635)&(uv2[:,1]>5)&(uv2[:,1]<355)
            if good.sum()<100:
                continue
            ray1=points[good]-a['position'];ray2=points[good]-b['position']
            angle=np.rad2deg(np.arccos(np.clip(np.sum(ray1*ray2,axis=1)/(np.linalg.norm(ray1,axis=1)*np.linalg.norm(ray2,axis=1)),-1,1)))
            median=float(np.median(angle))
            r,t=relative_camera_pose(a,b)
            _,_,p1,p2,_,_,_=cv2.stereoRectify(k,np.zeros(5),k,np.zeros(5),(640,360),r.copy(),t.reshape(3,1),flags=cv2.CALIB_ZERO_DISPARITY,alpha=0)
            if not (k[0,0]*.5<=p1[0,0]<=k[0,0]*3 and p2[0,0]>0):
                continue
            candidates.append((abs(median-3),start,end,int(good.sum()),median,lateral_angle))
        if candidates:
            _,a,b,count,angle,lateral=min(candidates)
            pairs.append({'left':a,'right':b,'third':(a+b)//2,
                          'sparse_common_points':count,'sparse_median_parallax_deg':angle,
                          'baseline_angle_to_camera_forward_deg':lateral})
    return pairs


def run(experiment):
    started=datetime.now(UTC).isoformat();clock=time.monotonic();cv2.setNumThreads(2)
    output=ROOT/'results'/experiment/'dense_photogrammetry';output.mkdir(parents=True,exist_ok=True)
    scene_path=ROOT/'results'/experiment/'presentation_mapping/scene.json'
    source=json.loads(scene_path.read_text(encoding='utf-8'))
    assert source['parent_observations_used'] is False
    calibration_path=ROOT/'calibration/parent_camera_underwater_20260924_provisional.json'
    calibration=json.loads(calibration_path.read_text(encoding='utf-8'))
    k=np.diag([1/3,1/3,1])@np.asarray(calibration['camera_matrix_px'])
    distortion=np.asarray(calibration['distortion_coefficients'])
    config={'image_size':[640,360],'pairs_maximum':4,'global_time_limit_s':300,
            'pair_selection':'Corrective pass: anchors0,10,445,460 from pose-only lateral-motion scan; baseline angle>=60deg, positive rectified focal in [0.5,3]x source focal; minimize deviation from 3deg median sparse common-point parallax',
            'attempt_history':'attempt_01_forward_motion/ retains initial4 failed forward-motion pairs; root authorized4 additional lateral pairs (8total)',
            'sgbm':{'num_disparities':128,'block_size':5,'uniqueness_ratio':12,'speckle_window':60,'speckle_range':2},
            'minimum_disparity_px':1.5,'left_right_tolerance_px':1,
            'grid_step_px':2,'third_image_ncc_minimum':.7,'third_image_patch_width':5,
            'third_image_minimum_patch_std':3,'third_image_maximum_mean_difference':20,
            'minimum_measured_parallax_deg':.5,'maximum_depth_ratio_per_face':1.05,
            'maximum_world_edge_fraction_of_sparse_radius':.02,
            'maximum_distance_from_sparse_center_in_radii':2,
            'maximum_positive_rectified_depth_in_sparse_radii':4,
            'calibration_K':k.tolist(),'distortion':distortion.tolist(),
            'hole_filling':False,'smoothing':False,'parent_observations_used':False}
    write(output/'config.json',config)
    pairs=choose_pairs(source,k);write(output/'selected_pairs.json',pairs)
    all_points=[];all_colors=[];records=[];models=trimesh.Scene();inputs=[scene_path,calibration_path,
        ROOT/'calibration/camera_defaults.json',Path(__file__).resolve(),
        ROOT/'src/aquabeacon_analysis/ngt_dense_patch.py',ROOT/'src/aquabeacon_analysis/ngt_textured_map.py']
    width,height=640,360;size=(width,height);sparse=np.asarray(source['points']['xyz']);tree=cKDTree(sparse)
    mask_full=np.full((height,width),255,np.uint8)
    for pair_index,pair in enumerate(pairs):
        if time.monotonic()-clock>300:
            records.append({'pair':pair_index,'status':'not_attempted_total_time_limit'});break
        frames=source['keyframes'];a,b,c=[frames[pair[n]] for n in ['left','right','third']]
        r,t=relative_camera_pose(a,b)
        rr1,rr2,p1,p2,_,_,_=cv2.stereoRectify(k,distortion,k,distortion,size,r.copy(),t.reshape(3,1),flags=cv2.CALIB_ZERO_DISPARITY,alpha=0)
        axis=int(abs(p2[1,3])>abs(p2[0,3]))
        if p2[axis,3]>0:
            a,b=b,a
            r,t=relative_camera_pose(a,b)
            rr1,rr2,p1,p2,_,_,_=cv2.stereoRectify(k,distortion,k,distortion,size,r.copy(),t.reshape(3,1),flags=cv2.CALIB_ZERO_DISPARITY,alpha=0)
            axis=int(abs(p2[1,3])>abs(p2[0,3]))
        if not (p1[0,0]>0 and p2[0,0]>0):
            records.append({'pair_index':pair_index,'status':'rejected_nonpositive_rectification_focal'})
            continue
        images=[]
        for frame in [a,b,c]:
            image_path=ROOT/frame['source_image'];inputs.append(image_path)
            image=cv2.imread(str(image_path))
            if image is None or image.shape[:2]!=(720,1280):
                raise ValueError('Expected true 1280x720 extracted MP4 child image')
            images.append(cv2.resize(image,size,interpolation=cv2.INTER_AREA))
        rectified=[];valids=[]
        for image,rotation,projection in zip(images[:2],[rr1,rr2],[p1,p2],strict=True):
            mx,my=cv2.initUndistortRectifyMap(k,distortion,rotation,projection[:3,:3],size,cv2.CV_32FC1)
            rectified.append(cv2.remap(image,mx,my,cv2.INTER_LINEAR))
            valid=cv2.remap(mask_full,mx,my,cv2.INTER_LINEAR)>254
            valids.append(cv2.erode(valid.astype(np.uint8),np.ones((7,7),np.uint8)).astype(bool))
        gray=[cv2.cvtColor(im,cv2.COLOR_BGR2GRAY) for im in rectified]
        disparities=[]
        for index in [0,1]:
            matcher=cv2.StereoSGBM_create(minDisparity=0 if index==0 else -128,numDisparities=128,
                blockSize=5,P1=8*25,P2=32*25,disp12MaxDiff=1,uniquenessRatio=12,
                speckleWindowSize=60,speckleRange=2,preFilterCap=31,mode=cv2.STEREO_SGBM_MODE_SGBM)
            first,second=gray[index],gray[1-index]
            disparity=matcher.compute(first.T.copy() if axis else first,second.T.copy() if axis else second).astype(float)/16
            disparities.append(disparity.T if axis else disparity)
        good,lr_error=consistent_disparity(*disparities,*valids,axis=axis)
        sampled=good[::2,::2];ys,xs=np.indices(sampled.shape);xy=np.column_stack((xs[sampled]*2,ys[sampled]*2)).astype(float)
        d=disparities[0][::2,::2][sampled]
        rect_points=triangulate_rectified(xy,d,p1,p2,axis=axis) if len(d) else np.empty((0,3))
        rcw=Rotation.from_quat(a['quaternion_xyzw']).as_matrix()
        world=rect_points@rr1@rcw.T+a['position']
        geometry=(np.isfinite(world).all(axis=1)&(rect_points[:,2]>0)&(rect_points[:,2]<source['radius']*4)&(np.linalg.norm(world-source['center'],axis=1)<source['radius']*2))
        ray1=world-a['position'];ray2=world-b['position']
        angle=np.rad2deg(np.arccos(np.clip(np.sum(ray1*ray2,axis=1)/np.maximum(np.linalg.norm(ray1,axis=1)*np.linalg.norm(ray2,axis=1),1e-12),-1,1)))
        geometry&=angle>=.5
        left_xy,_=camera_projection(world,a['position'],a['quaternion_xyzw'],k)
        third_xy,third_z=camera_projection(world,c['position'],c['quaternion_xyzw'],k)
        undist=[cv2.undistort(im,k,distortion) for im in [images[0],images[2]]]
        support,ncc=independent_photo_support(cv2.cvtColor(undist[0],cv2.COLOR_BGR2GRAY),cv2.cvtColor(undist[1],cv2.COLOR_BGR2GRAY),left_xy,third_xy) if len(world) else (np.empty(0,bool),np.empty(0))
        original_valid=cv2.erode((cv2.undistort(mask_full,k,distortion)>254).astype(np.uint8),np.ones((7,7),np.uint8))
        for projected in [left_xy,third_xy]:
            pp=np.rint(np.nan_to_num(projected,nan=-1,posinf=-1,neginf=-1)).astype(int)
            inside=(pp[:,0]>=0)&(pp[:,0]<width)&(pp[:,1]>=0)&(pp[:,1]<height)
            support&=inside&original_valid[np.clip(pp[:,1],0,height-1),np.clip(pp[:,0],0,width-1)].astype(bool)
        keep=geometry&support&(third_z>0)
        retained=np.zeros(sampled.shape,bool);retained[sampled]=keep
        depths=np.zeros(sampled.shape);depths[sampled]=rect_points[:,2]
        faces=grid_surface(retained,depths)
        vertices=world[keep];colors=cv2.cvtColor(rectified[0],cv2.COLOR_BGR2RGB)[xy[keep,1].astype(int),xy[keep,0].astype(int)]
        if len(faces):
            edges=np.linalg.norm(vertices[faces]-np.roll(vertices[faces],1,axis=1),axis=2).max(axis=1)
            faces=faces[edges<=source['radius']*.02]
        name=f'pair_{pair_index:02d}'
        np.savez_compressed(output/(name+'_correspondences.npz'),rectified_xy=xy[keep],disparity_px=d[keep],world_xyz=vertices,third_photo_ncc=ncc[keep],third_projected_xy=third_xy[keep],left_right_error_px=lr_error[::2,::2][sampled][keep],valid_grid=retained,depth_grid=depths,faces=faces,rectified_left_rotation=rr1,projection_left=p1,projection_right=p2)
        cv2.imwrite(str(output/(name+'_left_rectified.jpg')),rectified[0])
        cv2.imwrite(str(output/(name+'_right_rectified.jpg')),rectified[1])
        cv2.imwrite(str(output/(name+'_third_undistorted.jpg')),undist[1])
        display=cv2.applyColorMap(np.uint8(np.clip(disparities[0]/128*255,0,255)),cv2.COLORMAP_TURBO)
        display[~good]=0
        mask_image=np.zeros_like(rectified[0]);mask_image[::2,::2][retained]=rectified[0][::2,::2][retained]
        diagnostic=np.vstack([np.hstack(rectified),np.hstack([display,mask_image])])
        cv2.imwrite(str(output/(name+'_diagnostic.jpg')),diagnostic)
        cv2.imwrite(str(output/(name+'_valid_mask.png')),retained.astype(np.uint8)*255)
        if len(vertices):
            all_points.append(vertices);all_colors.append(colors)
            trimesh.points.PointCloud(vertices,colors).export(output/(name+'_points.ply'))
        if len(faces):
            mesh=trimesh.Trimesh(vertices=vertices,faces=faces,process=False)
            uv=np.column_stack([xy[keep,0]/width,1-xy[keep,1]/height])
            mesh.visual=trimesh.visual.texture.TextureVisuals(uv=uv,material=trimesh.visual.material.PBRMaterial(baseColorTexture=Image.fromarray(cv2.cvtColor(rectified[0],cv2.COLOR_BGR2RGB)),doubleSided=True,metallicFactor=0,roughnessFactor=1))
            models.add_geometry(mesh,node_name=name)
        distances=tree.query(vertices,workers=1)[0] if len(vertices) else np.empty(0)
        record={'pair_index':pair_index,'status':'retained' if len(vertices) else 'no_three_photo_supported_points',
                'images':[{'source_video_s':f['source_video_s'],'position':f['position'],'quaternion_xyzw':f['quaternion_xyzw'],'source_image':f['source_image']} for f in [a,b,c]],
                'baseline_slam_units':float(np.linalg.norm(t)),'rectification_axis':'vertical' if axis else 'horizontal',
                'rectified_focal_px':float(p1[0,0]),'rectification_valid_left_pixels':int(valids[0].sum()),
                'positive_disparity_pixels':int((disparities[0]>=1.5).sum()),'lr_consistent_pixels':int(good.sum()),
                'sampled_consistent_correspondences':len(world),'geometry_gate_pass':int(geometry.sum()),
                'independent_third_photo_gate_pass':int(support.sum()),'retained_points':len(vertices),
                'triangles':len(faces),'parallax_deg_median':float(np.median(angle[keep])) if keep.any() else None,
                'third_photo_ncc_median':float(np.median(ncc[keep])) if keep.any() else None,
                'distance_to_original_sparse_map_units_quantiles':np.quantile(distances,[0,.5,.9,1]).tolist() if len(distances) else [],
                'diagnostic':name+'_diagnostic.jpg'}
        records.append(record);write(output/'pair_metrics.json',records)
        print(json.dumps(record),flush=True)
    total_points=np.concatenate(all_points) if all_points else np.empty((0,3))
    total_colors=np.concatenate(all_colors) if all_colors else np.empty((0,3))
    if len(total_points):
        trimesh.points.PointCloud(total_points,total_colors).export(output/'dense_measured_points.ply')
    triangles=sum(len(g.faces) for g in models.geometry.values())
    if triangles:
        (output/'dense_local_surface.glb').write_bytes(models.export(file_type='glb'))
    result={'experiment_id':experiment,'status':'local_three_photo_supported_reconstruction' if len(total_points) else 'diagnostic_failure_no_validated_points',
            'started_utc':started,'ended_utc':datetime.now(UTC).isoformat(),'elapsed_s':time.monotonic()-clock,
            'process':{'pid':os.getpid(),'opencv_threads':2,'background_processes_started':[]},
            'git_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True,timeout=10).strip(),
            'coordinate_frame':source['coordinate_frame'],'units':source['units'],'source_window_s':[source['time']['keyframe_start_s'],source['time']['keyframe_end_s']],
            'pairs_attempted':len(records),'retained_sample_points':len(total_points),'local_triangles':triangles,
            'duplicate_overlap_between_pairs_removed':False,'sparse_reference_points':len(sparse),
            'parent_observations_used':False,'independent_photo_support':'5x5 patch NCC at calibrated projection in a third genuine photo, without local search; not ground-truth validation',
            'hole_filling':False,'interpolated_geometry':False,'metric_scale_verified':False,'full_pool_reconstruction':False,
            'input_sha256':{p.relative_to(ROOT).as_posix():sha(p) for p in inputs},
            'artifacts_sha256':{p.relative_to(output).as_posix():sha(p) for p in output.rglob('*') if p.is_file() and p.name not in ['manifest.json','report.md']}}
    write(output/'manifest.json',result)
    report=f'''# 実画像の多視点対応からの局所ステレオ復元

状態: {result['status']}。最大{len(records)}組を試行し、独立した第三写真の支持を持つ
{len(total_points):,}個の測定対応点と{triangles:,}個の隣接サンプル三角形を保持した。
同じ場所の別ペアの重複点を含むため、点数だけで品質向上を判断しない。

画像は子機だけ、共通沖縄水中校正をMP4全画面640×360へ縮小。元のMap2姿勢・任意尺度を保持。
実画像対応から三角測量し、疎点を補間・平滑化・平面化したものではない。SGBMの通常の
画素間平滑化正則化は使用するが、欠損画素の埋め戻しや表面補間は実施しない。

左右視差一致、元画像境界、正深度、0.5度以上の視差角、第三写真の5×5局所相関0.7以上、
局所輝度差20以下、局所標準偏差3以上を適用。隣接する有効2px格子だけを深度比1.05以内で結ぶ。
第三写真支持は独立した実画像との整合であり、位置・尺度の真値検証ではない。
パラメータは[config.json](config.json)、各段階の残数は[pair_metrics.json](pair_metrics.json)。
各 *_correspondences.npz に測定画素・実視差・三角測量点・第三写真相関・有効マスク・面を保存。
各 *_diagnostic.jpg は上段が左右の実画像、左下が左右一致後の視差、右下が第三写真支持後の画素。

入力280秒のうち、既存地図が記録された12.53秒内の局所試行。全プール、未保存区間、
実寸精度、完全な遮蔽判定は達成していない。原本・元の疎点・姿勢は変更していない。
疎点への最近傍距離は同じ推定地図との整合指標であり、真値誤差ではない。
'''
    (output/'report.md').write_text(report,encoding='utf-8');print(json.dumps(result),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--experiment',required=True)
    parser.add_argument('--phase',choices=['reconstruct','preview-verify'],default='reconstruct')
    args=parser.parse_args()
    if args.phase=='preview-verify':
        preview_and_verify(args.experiment)
    else:
        run(args.experiment)

