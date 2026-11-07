"""Report corrected positions, same-shape scale sensitivity and freshly reconstructed wall maps."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

os.environ.setdefault('OPENBLAS_NUM_THREADS','2')
os.environ.setdefault('OMP_NUM_THREADS','2')
import cv2
import matplotlib
import numpy as np

matplotlib.use('Agg')
import matplotlib.pyplot as plt
from evaluate_ngt_position import numbers, read_csv, write_csv, write_json
from matplotlib.colors import to_hex
from recalculate_ngt_positions import OUT, ROOT, RUN
from remap_ngt_corrected import OLD_OUT, POSITION, WORK, colmap
from remap_ngt_corrected import OUT as MAP_OUT
from visualize_ngt_positions import clean, viewer

from aquabeacon_analysis.editable_plots import save_editable_figure
from aquabeacon_analysis.ngt_binary import sha256
from aquabeacon_analysis.ngt_position import centered_scatter
from aquabeacon_analysis.ngt_research_review import sample_cell_vertices, state_intervals
from aquabeacon_analysis.ngt_visualization import contiguous_segments
from aquabeacon_analysis.restart_mapping import temporal_windows

BLUE,ORANGE,GRAY,GREEN='#00739c','#c76e27','#888888','#176b46'


def table(headers,rows):
    return '\n'.join(['| '+' | '.join(headers)+' |','| '+' | '.join(['---']*len(headers))+' |']
        +['| '+' | '.join(map(str,r))+' |' for r in rows])


def save(fig,name):
    save_editable_figure(fig,OUT/f'{name}.png',dpi=180);plt.close(fig)


def position_comparison(plot):
    comparisons=[];sources=[]
    all_rows={}
    for suffix in ['002','003','004']:
        base=ROOT/f'results/20261028_ngt_{suffix}/position_evaluation'
        old=read_csv(base/'positions_5hz.csv');new=read_csv(base/RUN/'positions_5hz.csv')
        old_metrics=json.loads((base/'metrics.json').read_text(encoding='utf-8'))
        new_metrics=json.loads((base/RUN/'metrics.json').read_text(encoding='utf-8'))
        all_rows[suffix]=(old,new)
        comparisons.append({'experiment':suffix,'event_count':new_metrics['shot_magnetic_positions'],
            'old_centered_rms_3d_m':old_metrics['relative_scatter']['centered_rms_3d_m'],
            'corrected_centered_rms_3d_m':new_metrics['relative_scatter']['centered_rms_3d_m'],
            'availability_fraction':new_metrics['grid_availability_fraction']['magnetic_position_available'],
            'longest_gap_s':new_metrics['longest_gap_s'],**new_metrics['change_from_legacy'],
            'old_pressure_comparison_rms_m':old_metrics['optical_down_minus_pressure']['rms_m'],
            'corrected_pressure_comparison_rms_m':new_metrics['optical_down_minus_pressure']['rms_m']})
        sources.extend([base/'positions_5hz.csv',base/'metrics.json',base/RUN/'positions_5hz.csv',base/RUN/'metrics.json',base/RUN/'manifest.json'])
    write_csv(OUT/'position_comparison.csv',comparisons)
    if not plot:return comparisons,all_rows,sources
    fig,axes=plt.subplots(1,3,figsize=(12,5),layout='constrained')
    for ax,(label,(old,new)) in zip(axes,all_rows.items(),strict=True):
        for rows,x,y,color,name,style in [(old,'child_east_declination0_m','child_north_declination0_m',GRAY,'子機 旧条件','--'),
            (new,'child_east_declination0_m','child_north_declination0_m',ORANGE,'子機 回転補正後','-'),
            (new,'parent_east_m','parent_north_m',BLUE,'親機 GNSS','-')]:
            ax.plot(numbers(rows,[x])[:,0],numbers(rows,[y])[:,0],style,lw=1,color=color,label=name)
        ax.set(xlabel='東 [m]（偏角0の仮定）',ylabel='北 [m]（偏角0の仮定）',title=label)
        ax.set_aspect('equal',adjustable='datalim');ax.grid(alpha=.2)
    axes[0].legend(fontsize=9);save(fig,'trajectories_corrected')
    fig,axes=plt.subplots(3,1,figsize=(10,7),layout='constrained')
    for ax,(label,(old,new)) in zip(axes,all_rows.items(),strict=True):
        keys=['relative_magnetic_north_m','relative_magnetic_east_m','relative_down_m']
        delta=np.linalg.norm(numbers(old,keys)-numbers(new,keys),axis=1)
        ax.plot(numbers(new,['parent_video_s'])[:,0],delta,color=ORANGE,lw=1)
        ax.set(ylabel='旧位置との差 [m]',title=label);ax.grid(alpha=.2)
    axes[-1].set_xlabel('親機元動画の時刻 [s]');save(fig,'position_displacement')
    base=ROOT/'results/20261028_ngt_002/position_evaluation'
    fig,axes=plt.subplots(1,2,figsize=(9,4.4),layout='constrained',sharex=True,sharey=True)
    points=[]
    for ax,folder,color,label in zip(axes,[base,base/RUN],[GRAY,ORANGE],['旧条件','回転補正後'],strict=True):
        rows=read_csv(folder/'positions_at_acoustic_events.csv')
        xyz=numbers(rows,['relative_magnetic_east_m','relative_magnetic_north_m','relative_down_m'])
        ax.plot(xyz[:,0],xyz[:,1],'.',color=color,ms=5)
        stats=centered_scatter(xyz);points.extend(xyz[np.isfinite(xyz).all(axis=1),:2])
        ax.set(title=f"{label}  3D RMS {stats['centered_rms_3d_m']:.3f} m",xlabel='磁東の相対位置 [m]',ylabel='磁北の相対位置 [m]')
        ax.grid(alpha=.2);ax.set_aspect('equal',adjustable='box')
    bounds=np.array(points);center=(bounds.min(0)+bounds.max(0))/2;span=np.ptp(bounds,axis=0).max()*.58
    for ax in axes:ax.set_xlim(center[0]-span,center[0]+span);ax.set_ylim(center[1]-span,center[1]+span)
    save(fig,'static_relative_positions')
    image=ROOT/'results/20261028_ngt_000/research_report/frames/P1_1_parent.png'
    frame=cv2.cvtColor(cv2.imread(str(image)),cv2.COLOR_BGR2RGB)
    cfg=json.loads((ROOT/'calibration/20261031_ngt_camera_frames.json').read_text(encoding='utf-8'))
    oldk=np.array(cfg['intrinsics_source']['camera_matrix_px']);newk=np.array(cfg['parent_proxy_matrix_display'])
    fig,ax=plt.subplots(figsize=(4,6),layout='constrained');ax.imshow(frame)
    ax.plot(oldk[0,2],oldk[1,2],'+',color='#ffbf00',ms=13,mew=2,label='旧主点の適用位置')
    ax.plot(newk[0,2],newk[1,2],'+',color='#00bfff',ms=13,mew=2,label='回転補正後の主点')
    ax.set(xlabel='表示画像 x [px]',ylabel='表示画像 y [px]');ax.legend(loc='upper left',fontsize=9)
    save(fig,'parent_principal_point')
    return comparisons,all_rows,sources


def mapping_results():
    rows=read_csv(WORK/'optimizer_frame_priors.csv');lookup={r['image_name']:r for r in rows}
    structural,set_positioned=set(),set();all_registered=set();models=[];maps=[];source_files=[]
    palette=plt.get_cmap('tab10')
    windows=[]
    for window in range(19):
        summary_path=MAP_OUT/f'window_{window:02d}/summary.json'
        value=json.loads(summary_path.read_text(encoding='utf-8'));windows.append(value);source_files.append(summary_path)
        log_path=MAP_OUT/f'window_{window:02d}.log'
        log=log_path.read_text(encoding='utf-8',errors='replace');source_files.append(log_path)
        value['linear_solver_warning_count']=log.count('Linear solver failure')
        value['prior_alignment_warning_count']=log.count('Final alignment w.r.t. prior positions failed')
        for item in value['models']:
            local_folder=ROOT/item['folder']
            src=local_folder/'position_registered' if item['position_registered'] else local_folder
            model=colmap.Reconstruction(src)
            images=sorted([im for im in model.images.values() if im.has_pose],key=lambda im:im.name)
            names={im.name for im in images};all_registered.update(names)
            if item['structural']:structural.update(names)
            if item['position_registered']:set_positioned.update(names)
            times=np.array([float(lookup[im.name]['parent_video_s']) for im in images])
            wall=bool(np.median(times)>=1415)
            points=list(model.points3D.values());xyz=np.array([p.xyz for p in points]).reshape(-1,3)
            assert np.isfinite(xyz).all()
            rec={'window':window,**item,'wall_period':wall,'all_points_finite':True}
            if len(points)>=3:
                centered=xyz-xyz.mean(axis=0);_,_,basis=np.linalg.svd(centered,full_matrices=False)
                coords=centered@basis.T
                rec['shape']={'units':'conditional m' if item['position_registered'] else 'local units',
                    'p90_principal_span':float(np.ptp(np.percentile(coords[:,0],[5,95]))),
                    'plane_rms':float(np.sqrt(np.mean(coords[:,2]**2)))}
            models.append(rec)
            if not item['structural']:continue
            stride=max(1,int(np.ceil(len(points)/4000)));selected=points[::stride]
            cameras=np.array([im.projection_center() for im in images])
            priors=np.array([[float(lookup[im.name][k]) for k in ['east_m','north_m','up_m']]
                for im in images if lookup[im.name]['prior_available']=='True']).reshape(-1,3)
            color=to_hex(palette(window%10))
            maps.append({'label':f"区間 {window:02d} / {item['model']} / "+('位置あり' if item['position_registered'] else '局所座標'),
                'model':f"{window:02d}_{item['model']}",'metric':item['position_registered'],'valid_structure':True,
                'wall_period':wall,'mode':'aquabeacon_prior','window':window,
                'points':[p.xyz.tolist() for p in selected],'colors':[to_hex(np.asarray(p.color)/255) for p in selected],
                'window_color':color,'cameras':cameras.tolist(),'camera_segments':contiguous_segments(cameras,times,.25),
                'priors':priors.tolist() if item['position_registered'] else [],'image':'',
                'caption':f"回転補正後 / 登録 {len(images)}枚 / 全点 {len(points)} / 親動画 {times.min():.2f}–{times.max():.2f}秒\n位置・尺度は推定位置に依存し、実測壁寸法ではない。",
                'source_pointcloud':str((src/('points.ply' if item['position_registered'] else 'points_local.ply')).relative_to(ROOT)),
                'complete_point_count':len(points),'display_stride':stride})
            source_files.extend([src/'cameras.bin',src/'images.bin',src/'points3D.bin'])
    attempts=np.zeros(len(rows),int)
    for indices in temporal_windows(numbers(rows,['parent_video_s'])[:,0],20,15):
        attempts[indices]+=1
    for index,row in enumerate(rows):
        row.update(registered_any=row['image_name'] in all_registered,
            registered_structural=row['image_name'] in structural,position_registered=row['image_name'] in set_positioned,
            attempted_windows=int(attempts[index]))
    write_csv(MAP_OUT/'frame_evaluation.csv',rows);write_json(MAP_OUT/'models.json',models)
    mt=np.array([float(r['parent_video_s']) for r in rows]);wallmask=(mt>=1415)&(mt<1540)
    states=['positioned' if r['position_registered'] else 'local' if r['registered_structural'] else 'unregistered' for r in rows]
    write_csv(MAP_OUT/'mapping_intervals.csv',state_intervals(mt,states,mt[-1]+np.median(np.diff(mt))))
    wall_maps=[m for m in maps if m['metric'] and m['wall_period']]
    summary={'input_frames':len(rows),'prior_frames':sum(r['prior_available']=='True' for r in rows),
        'optimizer_prior_anchors':sum(r['optimizer_prior']=='True' for r in rows),'windows':19,
        'failed_process_windows':[w['window'] for w in windows if w['status'] in ['hard_timeout','process_failed']],
        'structural_registered_unique':len(structural),'position_registered_unique':len(set_positioned),
        'models':len(models),'structural_models':sum(m['structural'] for m in models),
        'positioned_models':sum(m['position_registered'] for m in models),'wall_positioned_models':len(wall_maps),
        'wall_positioned_points_sum':sum(m['complete_point_count'] for m in wall_maps),
        'wall':{'input_frames':int(wallmask.sum()),
            'structural_registered_unique':sum(r['registered_structural'] for r,w in zip(rows,wallmask,strict=True) if w),
            'position_registered_unique':sum(r['position_registered'] for r,w in zip(rows,wallmask,strict=True) if w)},
        'time_capped_windows':[w['window'] for w in windows if w.get('time_limit_may_have_truncated')],
        'model_capped_windows':[w['window'] for w in windows if w.get('model_count_limit_reached')],
        'linear_solver_warning_windows':[w['window'] for w in windows if w['linear_solver_warning_count']],
        'prior_alignment_warning_windows':[w['window'] for w in windows if w['prior_alignment_warning_count']],
        'nominal_boundary_spill_frames':int((mt>=1540).sum()),'runtime_seconds_sum':sum(w.get('elapsed_s',240) for w in windows)}
    write_json(MAP_OUT/'summary.json',summary)
    write_csv(MAP_OUT/'window_evaluation.csv',[{'window':w['window'],'status':w['status'],
        'models':len(w['models']),'positioned_models':sum(m['position_registered'] for m in w['models']),
        'elapsed_s':w.get('elapsed_s',float('nan')),'time_capped':w.get('time_limit_may_have_truncated',False),
        'model_capped':w.get('model_count_limit_reached',False),
        'linear_solver_warning_count':w['linear_solver_warning_count'],
        'prior_alignment_warning_count':w['prior_alignment_warning_count']} for w in windows])
    combined={'label':'回転補正後の壁断片（推定位置へ配置）','model':'combined','aggregate':True,'metric':True,
        'valid_structure':bool(wall_maps),'wall_period':True,'mode':'aquabeacon_prior','points':[],'colors':[],
        'cameras':[],'camera_segments':[],'priors':[],'image':'',
        'caption':'各窓で再構成した疎地図。重複は残し、欠けた壁を補完していない。尺度は推定位置への適合に基づく。'}
    for m in wall_maps:
        offset=len(combined['cameras']);combined['points'].extend(m['points'])
        combined['colors'].extend([m['window_color']]*len(m['points']));combined['cameras'].extend(m['cameras'])
        combined['camera_segments'].extend([[i+offset for i in s] for s in m['camera_segments']]);combined['priors'].extend(m['priors'])
    pos=read_csv(POSITION);pt=numbers(pos,['parent_video_s'])[:,0]
    parent=numbers(pos,['parent_east_m','parent_north_m','parent_up_m']);parent[:,2]=0
    child=np.array([[float(r['child_east_declination0_m']),float(r['child_north_declination0_m']),-float(r['pressure_depth_m'])]
        if r['magnetic_position_available']=='True' else [np.nan]*3 for r in pos])
    data={'kind':'maps','experiment':f'20261028_ngt_004 / {RUN}','maps':[combined,*maps],
        'description':'親機90°・子機180°の画像座標を整合。33,564画像で再構成した結果。',
        'notes':'位置・尺度は磁北と偏角0、ゼロレバーアーム、暫定内部校正に依存。測量した壁の真値ではない。',
        'estimated_routes':{'parent':parent,'child':child,'parent_segments':contiguous_segments(parent,pt,.25),
            'child_segments':contiguous_segments(child,pt,.25),
            'coordinate_policy':'Corrected horizontal positions; parent z=0 for display, child up=-pressure depth; no gap interpolation'}}
    write_json(MAP_OUT/'map_data.json',clean(data));viewer(MAP_OUT/'wall_viewer.html','長田高校・回転補正後の地図',data)
    return summary,models,rows,wall_maps,source_files


def mapping_figures(summary,models,rows,wall_maps):
    old=read_csv(OLD_OUT/'frame_evaluation.csv');mt=numbers(rows,['parent_video_s'])[:,0]
    fig,axes=plt.subplots(4,1,figsize=(11,5.5),sharex=True,layout='constrained')
    for ax,data,key,label,color in zip(axes,[old,rows,old,rows],['registered_structural','registered_structural','position_registered','position_registered'],
        ['局所形状・旧条件','局所形状・補正後','共通座標・旧条件','共通座標・補正後'],[GRAY,GREEN,GRAY,ORANGE],strict=True):
        x,v=sample_cell_vertices(mt,[r[key] in (True,'True') for r in data],mt[-1]+np.median(np.diff(mt)))
        ax.fill_between(x,0,1,color='#e5e5e5');ax.fill_between(x,0,1,where=v,color=color)
        ax.set(yticks=[],ylabel=label,xlim=(1260,1540));ax.axvline(1415,color='black',lw=.7,ls='--')
    axes[-1].set_xlabel('親機元動画の時刻 [s]');save(fig,'mapping_coverage_comparison')
    scale=read_csv(MAP_OUT/'fixed_shape_scale_comparison.csv')
    selected=[r for r in scale if r['wall_period']=='True' and r['old_accepted']=='True']
    fig,ax=plt.subplots(figsize=(9,4),layout='constrained')
    values=[float(r['scale_ratio_corrected_over_old']) if r['new_accepted']=='True' else np.nan for r in selected]
    ax.plot(range(len(selected)),values,'o-',color=ORANGE);ax.axhline(1,color=GRAY,ls='--')
    ax.set(xticks=range(len(selected)),xticklabels=[f"{int(r['window']):02d}_{r['model']}" for r in selected],
        xlabel='同じ旧局所地図のモデルID',ylabel='修正位置で合わせた尺度 / 旧尺度');ax.grid(alpha=.2)
    save(fig,'fixed_shape_scale_ratios')
    fig,axes=plt.subplots(1,2,figsize=(11,3.3),layout='constrained')
    for m in wall_maps:
        p=np.asarray(m['points'])
        if len(p):
            axes[0].scatter(p[:,0],p[:,1],s=1.3,color=m['window_color'],label=m['model'],linewidths=0)
            axes[1].scatter(p[:,0],p[:,2],s=1.3,color=m['window_color'],linewidths=0)
    axes[0].set(xlabel='東 [m]（偏角0の仮定）',ylabel='北 [m]（偏角0の仮定）')
    axes[1].set(xlabel='東 [m]（偏角0の仮定）',ylabel='上 [m]（圧力深度基準）')
    for ax in axes:ax.set_aspect('equal',adjustable='datalim');ax.grid(alpha=.2)
    if wall_maps:axes[0].legend(fontsize=7,ncol=2,markerscale=4)
    save(fig,'corrected_wall_overview')
    return selected


def media_reassessment(rows,models):
    review=ROOT/'results/20261028_ngt_000/research_report'
    config_path=ROOT/'config/20261028_ngt/research_report.json'
    observed_path=ROOT/'config/20261028_ngt/research_observations.json'
    config=json.loads(config_path.read_text(encoding='utf-8'))
    observed=json.loads(observed_path.read_text(encoding='utf-8'))['cases']
    evidence=read_csv(review/'frame_evidence.csv')
    old=read_csv(OLD_OUT/'frame_evaluation.csv');position=read_csv(POSITION)
    mt=numbers(rows,['parent_video_s'])[:,0]
    inputs=[config_path,observed_path,review/'frame_evidence.csv',review/'frames/P1_1_parent.png']
    comparisons=[]
    tail=[m for m in models if m['window']>=17]
    anchor_range=[min(m['position_fit']['correspondences'] for m in tail),max(m['position_fit']['correspondences'] for m in tail)]
    interpretations={
        'M1':'修正版では形式上の画像登録が生じたが、代表画像で固定壁の模様を確認できないため、壁を正しく復元できた証拠とは扱わない。水面反射などの変動する模様から点が作られた可能性がある。この時刻は壁時間帯の地図集計に含めない。',
        'M2':'壁が写る区間で修正後も局所再構成と共通座標への配置が成立した。ただし、登録画像数は減っており、座標修正を再構成率の向上とは解釈しない。',
        'M3':'画像座標を整合しても未登録のままであり、旧校正の座標不整合だけを失敗原因とは断定できない。壁が見えない、特徴が少ないという説明にも当てはまらず、対応の一貫性・視差・探索時間が引き続き検討対象となる。',
        'M4':f'後半の窓17・18では今回も局所形状と共通座標への配置が分かれた。計{len(tail)}モデルの位置アンカーは{anchor_range[0]}〜{anchor_range[1]}個で、今回維持した6対応以上の採用条件に届かない。位置を119.88 fpsへ補間しても独立したアンカー数は増えない。',
        'M5':'独立した時間窓から壁の断片を再構成し、共通座標へ置ける区間が残った。登録画像数は減っており、断片間の連続した壁面や同一ランドマークの統合が確認できたという結論ではない。'}
    text=['# 画像回転修正後のマッピングと実映像の照合','',
        '位置の成立・欠測条件は旧試行と同一で、P1〜P7のライト画角離脱・遮蔽・測距欠測・姿勢欠測の照合は[既存映像編](../research_report/media_review.md)を参照する。',
        '地図の成立は再構成によって変わるため、M1〜M5を同じ動画範囲・同じ代表画像で再集計する。画像の内容は変更せず、成立状態の台帳だけを更新する。',
        '各代表画像を再確認するが、動画全体の連続視聴や全特徴対応の正解付けは行っていない。','']
    for case in config['cases']:
        if case['focus']!='child':continue
        lo,hi=case['clip_s'];select=(mt>=lo)&(mt<hi)
        old_part=[r for r,v in zip(old,select,strict=True) if v]
        part=[r for r,v in zip(rows,select,strict=True) if v]
        pos=[r for r in position if lo<=float(r['parent_video_s'])<hi]
        item={'case':case['id'],'start_s':lo,'end_s':hi,'input_frames':len(part),
            'old_structural':sum(r['registered_structural']=='True' for r in old_part),
            'old_positioned':sum(r['position_registered']=='True' for r in old_part),
            'corrected_structural':sum(r['registered_structural'] for r in part),
            'corrected_positioned':sum(r['position_registered'] for r in part),
            'position_cells':len(pos),'available_position_cells':sum(r['magnetic_position_available']=='True' for r in pos)}
        comparisons.append(item)
        fig=plt.figure(figsize=(10,3.4),layout='constrained')
        grid=fig.add_gridspec(4,3,height_ratios=[3,.35,.35,.35])
        pics=[r for r in evidence if r['case']==case['id'] and r['role']=='child']
        assert len(pics)==3
        for col,r in enumerate(pics):
            path=review/r['file'];assert sha256(path)==r['sha256'];inputs.append(path)
            ax=fig.add_subplot(grid[0,col]);frame=cv2.cvtColor(cv2.imread(str(path)),cv2.COLOR_BGR2RGB)
            ax.imshow(frame);ax.set_title(f"親動画 {float(r['actual_parent_s']):.2f} s",fontsize=10)
            ax.set_axis_off()
        xlim=(min(lo,min(case['samples_s'])),max(hi,max(case['samples_s'])))
        for row,key,label,color in [(1,'registered_structural','修正後 局所形状',GREEN),
                (2,'position_registered','修正後 共通座標',ORANGE)]:
            ax=fig.add_subplot(grid[row,:]);x,v=sample_cell_vertices(mt,[r[key] for r in rows],mt[-1]+np.median(np.diff(mt)))
            ax.fill_between(x,0,1,color='#e5e5e5');ax.fill_between(x,0,1,where=v,color=color)
            ax.axvspan(lo,hi,facecolor='none',edgecolor='black',lw=.8)
            ax.set(xlim=xlim,yticks=[]);ax.set_ylabel(label,rotation=0,ha='right',va='center',fontsize=9)
            ax.tick_params(axis='x',labelbottom=False)
        ax=fig.add_subplot(grid[3,:]);pt=numbers(position,['parent_video_s'])[:,0]
        x,v=sample_cell_vertices(pt,[r['magnetic_position_available']=='True' for r in position],1540)
        ax.fill_between(x,0,1,color='#e5e5e5');ax.fill_between(x,0,1,where=v,color=BLUE)
        ax.set(xlim=xlim,yticks=[],xlabel='親機元動画の時刻 [s]')
        ax.set_ylabel('位置あり',rotation=0,ha='right',va='center',fontsize=9)
        save(fig,'case_'+case['id']+'_reassessment')
        state='共通座標へ配置できた画像がある' if item['corrected_positioned'] else ('局所形状は得られるが共通座標へ配置できていない' if item['corrected_structural'] else 'この範囲で局所形状へ登録された画像はない')
        text.extend([f"## {case['id']}　親動画 {lo}〜{hi}秒",'',
            f"[同期動画](../research_report/clips/{case['id']}.mp4)",
            f"![実画像と修正後の成立状態](case_{case['id']}_reassessment.png)",'',
            '**映像の事実：** '+observed[case['id']]['observed'],'',
            f"**再評価：** {len(part):,}画像中、局所形状の登録は旧{item['old_structural']:,} → 修正後{item['corrected_structural']:,}、共通座標への配置は旧{item['old_positioned']:,} → 修正後{item['corrected_positioned']:,}。位置は{item['available_position_cells']}/{len(pos)}セル成立。修正版では「{state}」と分類する。",'',
            '**考察：** '+interpretations[case['id']],'',
            '成立画像数の変化は幾何校正・位置prior・再検証・制限時間内の探索を合わせた差である。代表画像の見え方だけから原因や寸法精度を断定しない。',''])
    text.extend(['## 解釈','',
        '位置が分かれば、追跡が続いた地図に限らず各時間窓から局所再構成を再開できる。ただし、画像間の対応・視差が足りず形状が作れない場合と、短い地図に独立した位置アンカーが足りず共通座標へ配置できない場合は残る。',
        '119.88 fpsの画像を5 Hzの位置へ補間しても独立した測位情報は増えない。今回の6アンカー・非直線退化という採用条件は維持した。局所形状の有無、共通座標への配置、測量に対する正しさを区別する。','',
        '[比較台帳](mapping_case_comparison.csv)／[全入力画像の状態](../../20261028_ngt_004/wall_mapping/'+RUN+'/frame_evaluation.csv)／[統合レポート](report.md)'])
    write_csv(OUT/'mapping_case_comparison.csv',comparisons)
    (OUT/'media_reassessment.md').write_text('\n'.join(text)+'\n',encoding='utf-8')
    return comparisons,inputs


def report(comparisons,summary,models,scale,cases):
    old=json.loads((OLD_OUT/'summary.json').read_text(encoding='utf-8'))
    position_table=table(['区間','有効な音響イベント','位置成立率','旧位置との差・中央値 [m]','同95%点 [m]','最長欠測 [s]'],
        [[r['experiment'],r['event_count'],f"{100*r['availability_fraction']:.1f}%",f"{r['median_displacement_m']:.3f}",f"{r['p95_displacement_m']:.3f}",f"{r['longest_gap_s']:.1f}"] for r in comparisons])
    map_table=table(['指標','旧条件','回転補正後'],[
        ['入力画像',f"{old['input_frames']:,}",f"{summary['input_frames']:,}"],
        ['全期間・局所形状に登録',f"{old['registered_structural_unique']:,}",f"{summary['structural_registered_unique']:,}"],
        ['全期間・共通座標に配置',f"{old['position_registered_unique']:,}",f"{summary['position_registered_unique']:,}"],
        ['壁時間帯の入力画像',f"{old['wall_interval']['input_frames']:,}",f"{summary['wall']['input_frames']:,}"],
        ['壁時間帯・局所形状に登録',f"{old['wall_interval']['structural_registered_unique']:,} ({100*old['wall_interval']['structural_fraction']:.2f}%)",f"{summary['wall']['structural_registered_unique']:,} ({100*summary['wall']['structural_registered_unique']/summary['wall']['input_frames']:.2f}%)"],
        ['壁時間帯・共通座標に配置',f"{old['wall_interval']['position_registered_unique']:,} ({100*old['wall_interval']['positioned_fraction']:.2f}%)",f"{summary['wall']['position_registered_unique']:,} ({100*summary['wall']['position_registered_unique']/summary['wall']['input_frames']:.2f}%)"],
        ['壁時間帯の配置済みモデル',old['wall_period_positioned_models'],summary['wall_positioned_models']],
        ['配置済み壁モデルの点数（重複を含む延べ数）',f"{old['wall_period_positioned_points_sum']:,}",f"{summary['wall_positioned_points_sum']:,}"]])
    scale_table=table(['旧モデル','対応数','修正位置で再適合','尺度比 修正/旧','旧適合RMS [m]','修正適合RMS [m]'],
        [[f"{int(r['window']):02d}_{r['model']}",r['anchors'],'成立' if r['new_accepted']=='True' else '不成立',
            f"{float(r['scale_ratio_corrected_over_old']):.3f}" if r['new_accepted']=='True' else '—',
            f"{float(r['old_fit_rms_m']):.3f}",f"{float(r['corrected_fit_rms_m']):.3f}" if r['new_accepted']=='True' else '—'] for r in scale])
    wall=[m for m in models if m['wall_period'] and m['position_registered']]
    model_table=table(['新モデル','親動画の登録範囲 [s]','登録画像','全点数','位置適合RMS [m]','主方向90%幅 [m]'],
        [[f"{m['window']:02d}_{m['model']}",f"{m['first_parent_s']:.2f}–{m['last_parent_s']:.2f}",m['registered_images'],m['points'],
            f"{m['position_fit']['fit_rms_m']:.3f}",f"{m.get('shape',{}).get('p90_principal_span',float('nan')):.3f}"] for m in wall])
    ratios=[float(r['scale_ratio_corrected_over_old']) for r in scale if r['new_accepted']=='True']
    ratio_text=f"{min(ratios):.3f}〜{max(ratios):.3f}倍" if ratios else '採用条件を満たす再適合なし'
    text=f'''# 長田高校プール実験：画像座標の修正と位置・地図尺度の再評価

2026年10月31日／取得台帳 `20261028_ngt_000`／位置：002・003・004／地図：004

## 結果

親機画像の90°回転に合わせて主点・焦点距離の軸・接線歪み・光学座標からIMUへの回転を整合し、位置を再計算した。静止区間002の平均位置からの3次元RMSばらつきは **0.240 m → 0.148 m** となった。位置算出の成立率・欠測区間は旧結果と一致した。

位置依存の尺度について、同じ旧局所地図へ修正位置を再適合した比較では、壁モデルの採用された尺度比は **{ratio_text}** となった。さらに、子機画像の180°回転も内部校正へ反映し、同じ33,564画像から19窓を新規に再構成した。地図の最終的な拡縮だけで済ませず、特徴対応の幾何検証と位置拘束を含む最適化からやり直した。

これらは座標不整合の修正と再評価の結果である。独立した位置真値・実測壁寸法はないため、0.148 mを絶対位置誤差、尺度比を実測寸法への正確さとして扱わない。

## 1. 画像と校正座標をどのように整合したか

原典の沖縄校正LRFは回転0°、長田高校の親機は90°、子機は180°だった。親機光点は720×1280の表示画像座標で記録されているため、1280×720の内部行列をそのまま適用する処理を修正した。90°時計回りでは、表示画素からセンサー画素への逆変換は `(x_s,y_s)=(y_d,719−x_d)` である。

主点の平行移動だけでなく、焦点距離の軸交換と接線歪みの回転も適用した。表示座標で歪み補正する方法と、画素をセンサー座標へ戻して歪み補正してから視線を回す方法が、全採用光点で数値的に一致することを確認した。観測距離と欠測フラグは保持した。

![親機画像の主点の適用位置](parent_principal_point.png)

図1　同じ親機画像に対する旧・修正後の主点。図の変更は校正座標の表示であり、画像内のライトを移動させたものではない。

カメラIMUとの軸規約は、画像を自動回転させず読み、実験前半の空中の視覚回転と同じ動画のIMU回転を照合し直した。150〜195秒の選定区間と195〜240秒の独立確認区間で、24通りの適正な軸回転のうち同じ候補が最良だった。センサー光学座標の `右→IMU Y、下→IMU Z、前→IMU X` が対応する。相対回転残差の中央値は選定0.295、確認0.212で、閾値0.35内にある異なる撮影時刻の画像対はそれぞれ7、3だった。これは離散的な軸・符号の確認であり、高精度の連続取付角校正ではない。

表示基底からIMUへの行列は、軸を確認した結果、旧試行の暫定行列と数値的に同じになった。親機位置の主要な数値変更は、表示画素に合わせた内部行列と歪み係数の適用である。画像の回転を機体が物理的に90°回ったことと混同しない。

子機のMP4由来画像は180°回転済みであることを元動画との照合で確認した。地図用内部行列は回転、1920×1080→1280×720の画素中心基準の縮小、COLMAPとOpenCVの画素中心規約の差0.5 pxを明示した。接線歪みの符号も180°回転に合わせた。主点を含む全画面の座標を使うため、既存の壁ROI特徴点を `(640,36)` だけ平行移動し、元の特徴記述子と画像対応は保持した。[OpenCVの回転プロパティ](https://docs.opencv.org/4.x/d4/d15/group__videoio__flags__base.html)、[COLMAPの外部校正規約](https://colmap.github.io/faq.html#using-calibration-from-opencv-kalibr-or-other-tools)

## 2. 修正後の位置

{position_table}

差は同じ時刻の旧推定位置からの変化量であり、真値誤差ではない。静止のばらつきは独立した音響イベント53点で計算した。003と004は移動を含むので、その散らばりを静止精度として比較しない。

![静止区間の相対位置](static_relative_positions.png)

図2　同じ53個の採用音響イベントを比較。各結果の平均を揃える操作はしていない。RMSは3次元、図は磁東・磁北の2次元投影である。

![親機と子機の軌跡](trajectories_corrected.png)

図3　青：親機GNSS、橙：修正後の子機、灰：旧条件の子機。欠測は線を切ったまま表示する。磁北とGNSSの北を重ねるための偏角0の仮定、ゼロレバーアームは今回変更していない。

![旧位置との差の時系列](position_displacement.png)

図4　画像回転の修正によって位置が変わった量。光学・音響・姿勢の可用性判定は変えていないため、以前の[映像と欠測の考察](../research_report/media_review.md)は引き続き参照できる。

## 3. 同じ形状を使った尺度の比較

親機位置の修正の効果を切り分けるため、旧COLMAP局所地図の点・カメラ位置を固定し、新旧の入力位置へ同じHuber付きSim(3)適合を行った。対応は元の5 Hzセルから選んだアンカーで、6対応以上・非直線退化という採用条件も同じである。

{scale_table}

![同一局所形状に対する尺度比](fixed_shape_scale_ratios.png)

図5　1.0は旧尺度と同じことを表す。この比較は同じ局所座標の形状なので尺度比を直接比較できる。ただし旧地図は旧priorの影響を受けており、これだけを最終修正版とはしない。

新しく再構成した地図のSim(3)係数は、初期化によって局所単位も変わるため、旧モデルの係数と単純に割り算していない。尺度の真値検証には、別途、壁やマーカーの実測寸法が必要である。

## 4. 修正した位置と子機校正による再構成

{map_table}

全33,564画像のSHA256を旧試行と照合した。ネイティブ入力は同じ約119.88 fpsであり、失敗区間も含む。壁時間帯は1415秒以上1540秒未満、全移動は公称1260〜1540秒である。入力をVSLAMと一致させるため、公称終了より約4 ms後の1枚も全期間の分母と台帳に残すが、壁時間帯の分母からは旧試行と同様に除く。モデル単位の「壁時間帯」は登録画像の時刻中央値が1415秒以降という条件で分類し、フレーム単位の成立率と区別する。

旧データベースを読み取り専用で複製し、特徴点の座標を全画像座標へ移した。特徴記述子は一致を検証し、旧の幾何検証結果と位置priorは取り除いて作り直した。各20秒窓を15秒刻みで独立に開始し、窓0〜14は最大10モデル、窓15〜18は旧最終試行と同じ最大200モデル、各120秒のソフト時間上限とした。今回の時間上限到達候補は {summary['time_capped_windows']}、モデル数上限は {summary['model_capped_windows']} である。

[窓ごとの処理記録](../../20261028_ngt_004/wall_mapping/{RUN}/window_evaluation.csv)には、計算時間とモデル数、線形ソルバや位置への最終適合の警告回数も保存した。警告は窓内の一部断片について出ることがあり、窓全体の成功・失敗とは同義ではない。保存した各断片について、点の有限性と位置対応数・退化条件を別途検証する。

![修正前後の地図成立区間](mapping_coverage_comparison.png)

図6　全入力画像の局所形状登録と共通座標への配置を分ける。灰色の背景は不成立。形状ありは50点以上という処理条件であり、寸法が正しいことの判定ではない。

![回転補正後の壁点群](corrected_wall_overview.png)

図7　推定位置に配置した壁時間帯の疎点群。色は窓、表示はモデルごと最大約4,000点の決定的間引きで、全点は元COLMAP/PLYに保存する。重複窓の点は重複を残し、未復元部分を面で補っていない。

[回転・拡大できる修正後の地図](../../20261028_ngt_004/wall_mapping/{RUN}/wall_viewer.html)／[新しい全フレーム台帳](../../20261028_ngt_004/wall_mapping/{RUN}/frame_evaluation.csv)／[全モデルと失敗](../../20261028_ngt_004/wall_mapping/{RUN}/models.json)

{model_table}

主方向90%幅は各点群を主成分軸へ投影した5〜95パーセンタイル幅であり、同一の壁区間を測った寸法ではない。新旧で登録範囲・特徴対応が変わるため、この幅の変化をそのまま尺度の改善率にはしない。適合RMSも入力した推定位置との内部整合性であり、独立した精度指標ではない。

## 5. 再評価の解釈

①画像回転と校正座標をそろえた位置の再計算、②その位置へ依存する地図尺度の再適合と、最適化を含む新規再構成を実施した。これにより、旧結果の座標不整合を残したまま尺度を報告する問題を解消した。

地図の登録数の変化には、親機の位置prior、子機の内部校正、再検証した画像対応、時間上限下での探索経路が関係する。親機位置の修正だけによる改善率とは解釈しない。同一形状の尺度比較と、全工程の再構成比較を分ける理由である。

共通水中内部校正は視点多様性が限られた暫定値であり、カメラ・音響・GNSSの中心間距離、磁気方位の精度、真北偏角は未確定である。圧力深度は従来どおり子機の上下座標に使用した。圧力深度を独立した真値と扱う評価や、『約25 m移動』を正解とした追加の尺度合わせは行っていない。単眼VSLAMの子機単独試行も変更していない。25 m全壁面の連続メッシュや絶対寸法精度を達成したという結論ではない。

## 6. 実映像で確認した成立・不成立の区間

同じM1〜M5の実カメラ画像と動画範囲を使い、地図状態を修正後の台帳で更新した。以下は同じ入力画像に対する比較である。

{table(['事例','親動画 [s]','入力画像','局所形状 旧→修正後','共通座標 旧→修正後'],[[r['case'],f"{r['start_s']}–{r['end_s']}",r['input_frames'],f"{r['old_structural']}→{r['corrected_structural']}",f"{r['old_positioned']}→{r['corrected_positioned']}"] for r in cases])}

[修正後の実映像照合編](media_reassessment.md)では、実画像・成立タイムライン・同期動画へのリンクを事例ごとに掲載した。位置の成立・欠測条件は変わっていないため、P1〜P7の映像による位置欠測の考察も保持する。

M1では修正後に278画像が登録されたが、代表画像に固定壁の模様を確認できない。形式的な登録の成立を、壁を正しく復元できたことと区別する。M3は壁が見えていても今回も未登録であり、M4は局所形状を得た一方で共通座標へ配置できなかった。画像座標を整合したことで、これらの課題がすべて解決したとは判断しない。

表示用の[Blenderプロジェクト](../../20261028_ngt_004/wall_mapping/{RUN}/wall_map.blend)には、壁の全断片、親子軌跡と壁の位置関係、主要断片の個別シーンを保存する。[全体画像](../../20261028_ngt_004/wall_mapping/{RUN}/blender_overview.png)も参照できる。点群の表示専用であり、正式な尺度計算はPython/COLMAPで行った。

## 根拠・再現

- [位置比較CSV](position_comparison.csv)／[同一形状の全尺度比較](../../20261028_ngt_004/wall_mapping/{RUN}/fixed_shape_scale_comparison.csv)
- [画像・IMU軸の監査](sensor_axis_audit.json)／[適用した座標設定](../../../calibration/20261031_ngt_camera_frames.json)
- [今回の来歴](manifest.json)／[地図処理の来歴](../../20261028_ngt_004/wall_mapping/{RUN}/manifest.json)／[検証結果](validation.md)

新図はPNG・SVG・共通編集用Figureとplot.jsonを保存する。軸・ラベル・範囲・図寸法・線・点・文字は共通エディタで変更し、設定を保存・再読込してPNG/SVG/PDFへ出力できる。元値と欠測は保持する。写真パネルの画素編集やパネル構成の組み替えは共通UIの対象外であり、元画像・Figure・生成コードから編集を続けられる。実機形状の新しい推測は行っていない。

```powershell
python scripts/audit_ngt_camera_frames.py --experiment 20261028_ngt_000
python scripts/recalculate_ngt_positions.py --experiment 20261028_ngt_002 --experiment 20261028_ngt_003 --experiment 20261028_ngt_004
python scripts/remap_ngt_corrected.py --experiment 20261028_ngt_004 --phase all
python scripts/report_ngt_coordinate_reassessment.py --experiment 20261028_ngt_000 --phase positions
python scripts/report_ngt_coordinate_reassessment.py --experiment 20261028_ngt_000 --phase final
python scripts/verify_ngt_coordinate_reassessment.py --experiment 20261028_ngt_000
```
'''
    (OUT/'report.md').write_text(text,encoding='utf-8')
    (MAP_OUT/'report.md').write_text('# 回転補正後の壁マッピング\n\n[位置・尺度の再評価レポート](../../../20261028_ngt_000/coordinate_reassessment/report.md)／[修正後の地図](wall_viewer.html)。\n',encoding='utf-8')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment',required=True,choices=['20261028_ngt_000'])
    parser.add_argument('--phase',required=True,choices=['positions','final'])
    args=parser.parse_args();cv2.setNumThreads(2)
    plt.rcParams.update({'font.family':'Yu Gothic','font.size':10,'axes.unicode_minus':False,'axes.spines.top':False,'axes.spines.right':False})
    process={'pid':os.getpid(),'owner':'coordinate_reassessment','purpose':'Report '+args.phase,'status':'running','started_utc':datetime.now(UTC).isoformat()}
    process_path=OUT/f'report_{args.phase}.process.json';write_json(process_path,process)
    try:
        comparisons,_rows,inputs=position_comparison(args.phase=='positions')
        if args.phase=='final':
            summary,models,rows,wall_maps,sources=mapping_results()
            scale=mapping_figures(summary,models,rows,wall_maps)
            cases,media_inputs=media_reassessment(rows,models)
            report(comparisons,summary,models,scale,cases)
            inputs+=sources+media_inputs+[MAP_OUT/'manifest.json',MAP_OUT/'fixed_shape_scale_comparison.csv',
                ROOT/'calibration/20261031_ngt_camera_frames.json',OUT/'sensor_axis_audit.json',Path(__file__),
                ROOT/'scripts/run_ngt_restart_mapping.py',ROOT/'scripts/visualize_ngt_positions.py',
                ROOT/'src/aquabeacon_analysis/ngt_research_review.py',ROOT/'src/aquabeacon_analysis/editable_plots.py']
            write_json(OUT/'manifest.json',{'created_utc':datetime.now(UTC).isoformat(),
                'git_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
                'software_versions':{'python':sys.version,'opencv':cv2.__version__,'numpy':np.__version__,
                    'matplotlib':matplotlib.__version__,'pycolmap':colmap.__version__},
                'inputs_sha256':{str(p.relative_to(ROOT)):sha256(p) for p in inputs},'mapping_summary':summary,
                'outputs_sha256':{p.name:sha256(p) for p in OUT.iterdir() if p.is_file() and
                    (p.suffix in ['.png','.svg','.pickle'] or p.name in ['report.md','media_reassessment.md','position_comparison.csv','mapping_case_comparison.csv'])}})
            shutil.copy2(__file__,OUT/'generate_report.py')
        process['status']='completed'
    except BaseException as error:
        process.update(status='failed',error=str(error));raise
    finally:
        process['ended_utc']=datetime.now(UTC).isoformat();write_json(process_path,process)


if __name__=='__main__':main()
