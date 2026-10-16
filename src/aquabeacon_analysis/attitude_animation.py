"""Self-contained, orbitable 3D attitude animations for experiment review."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

HTML_TEMPLATE = r"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  :root { color-scheme: dark; font-family: system-ui, sans-serif; background: #0d1421; color: #e8eef9; }
  body { margin: 0 auto; padding: 20px; max-width: 1500px; }
  h1 { font-size: 1.4rem; margin: 0 0 6px; }
  .sub { color: #aebdd2; margin: 0 0 14px; }
  canvas { width: 100%; height: min(65vh, 620px); min-height: 370px; display: block;
           background: #111c2d; border: 1px solid #33435b; border-radius: 8px; touch-action: none; }
  .controls { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; margin-top: 14px; }
  button, select { background: #243b58; color: #f3f7ff; border: 1px solid #566d8b;
                   border-radius: 6px; padding: 9px 14px; font: inherit; }
  button { cursor: pointer; }
  input[type=range] { flex: 1; min-width: 220px; }
  .time { min-width: 118px; font-variant-numeric: tabular-nums; }
  .details { display: flex; flex-wrap: wrap; gap: 12px; margin-top: 14px; }
  .detail { border: 1px solid #33435b; border-radius: 6px; padding: 9px 12px;
            min-width: 190px; flex: 1; font-variant-numeric: tabular-nums; }
  .detail strong { display: block; margin-bottom: 4px; }
  .note { color: #aebdd2; font-size: 0.9rem; line-height: 1.4; }
  .axes { color: #d9e4f2; }
</style>
</head>
<body>
<h1>__TITLE__</h1>
<p class="sub">__SUBTITLE__</p>
<canvas id="scene" role="img" aria-label="回転可能な3D姿勢アニメーション"></canvas>
<div class="controls">
  <button id="play" type="button">▶ 再生</button>
  <label for="time-range">時刻</label><input id="time-range" type="range" min="0" step="1">
  <span id="time" class="time"></span>
  <label for="speed">速度</label>
  <select id="speed"><option value="1">1×</option><option value="4">4×</option>
    <option value="8" selected>8×</option><option value="16">16×</option></select>
  <button id="reset-view" type="button">視点を戻す</button>
</div>
<div id="details" class="details"></div>
<p class="note">ドラッグで視点を回転。色付きの軸は各センサのX（赤）、Y（緑）、Z（青）。
同じ初期姿勢に揃えたセンサの動きを比較します。方位は地理的な北を示しません。</p>
<script id="attitude-data" type="application/json">__DATA__</script>
<script>
(() => {
  'use strict';
  const data = JSON.parse(document.getElementById('attitude-data').textContent);
  const canvas = document.getElementById('scene');
  const ctx = canvas.getContext('2d');
  const range = document.getElementById('time-range');
  const timeLabel = document.getElementById('time');
  const play = document.getElementById('play');
  const details = document.getElementById('details');
  const speed = document.getElementById('speed');
  let t = data.times[0], running = false, lastFrame = null;
  let yaw = -0.68, pitch = 0.43, dragging = false, lastX = 0, lastY = 0;
  const end = data.times[data.times.length - 1];
  range.max = String(Math.round((end - data.times[0]) * 1000));
  const colors = ['#71c8ff', '#f8c675', '#d8abff'];
  const axisColors = ['#ff6767', '#80d987', '#7aa9ff'];

  function dot(a,b) { return a[0]*b[0]+a[1]*b[1]+a[2]*b[2]; }
  function cross(a,b) { return [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]]; }
  function rotate(q,v) {
    const u=[q[1],q[2],q[3]], uv=cross(u,v), uuv=cross(u,uv);
    return v.map((x,i)=>x+2*(q[0]*uv[i]+uuv[i]));
  }
  function slerp(a,b,f) {
    if (!a || !b) return null;
    let d=a.reduce((sum,x,i)=>sum+x*b[i],0);
    if(d<0) { b=b.map(x=>-x); d=-d; }
    if(d>0.9995) { const q=a.map((x,i)=>x*(1-f)+b[i]*f); const n=Math.hypot(...q); return q.map(x=>x/n); }
    const theta=Math.acos(Math.min(1,d)), s=Math.sin(theta);
    return a.map((x,i)=>x*Math.sin((1-f)*theta)/s+b[i]*Math.sin(f*theta)/s);
  }
  function sample(qs, target) {
    let lo=0, hi=data.times.length-1;
    while(hi-lo>1) { const mid=(lo+hi)>>1; if(data.times[mid]<target) lo=mid; else hi=mid; }
    if(target<=data.times[0]) return qs[0];
    if(target>=end) return qs[qs.length-1];
    const f=(target-data.times[lo])/(data.times[hi]-data.times[lo]);
    return slerp(qs[lo],qs[hi],f);
  }
  function angles(q) {
    const [w,x,y,z]=q;
    const roll=Math.atan2(2*(w*x+y*z),1-2*(x*x+y*y));
    const pitch=Math.asin(Math.max(-1,Math.min(1,2*(w*y-z*x))));
    const heading=Math.atan2(2*(w*z+x*y),1-2*(y*y+z*z));
    return [roll,pitch,heading].map(x=>(x*180/Math.PI).toFixed(1));
  }
  function angleBetween(a,b) {
    return (2*Math.acos(Math.min(1,Math.abs(a.reduce((s,x,i)=>s+x*b[i],0))))*180/Math.PI).toFixed(2);
  }
  function project(v,cx,cy,scale) {
    const view=[Math.cos(pitch)*Math.cos(yaw),Math.cos(pitch)*Math.sin(yaw),Math.sin(pitch)];
    const right=[-Math.sin(yaw),Math.cos(yaw),0], up=cross(view,right);
    return [cx+dot(v,right)*scale,cy-dot(v,up)*scale];
  }
  function line(a,b,color,width=2) {
    ctx.strokeStyle=color; ctx.lineWidth=width; ctx.beginPath(); ctx.moveTo(...a); ctx.lineTo(...b); ctx.stroke();
  }
  function drawPose(q,cx,cy,scale) {
    const origin=project([0,0,0],cx,cy,scale);
    const size=[1.35,0.76,0.24], verts=[];
    for(const x of [-1,1]) for(const y of [-1,1]) for(const z of [-1,1])
      verts.push(rotate(q,[x*size[0]/2,y*size[1]/2,z*size[2]/2]));
    const edges=[[0,1],[0,2],[0,4],[1,3],[1,5],[2,3],[2,6],[3,7],[4,5],[4,6],[5,7],[6,7]];
    for(const [a,b] of edges) line(project(verts[a],cx,cy,scale),project(verts[b],cx,cy,scale),'#d9e4f2',2);
    [[1,0,0],[0,1,0],[0,0,1]].forEach((v,i)=>{
      const end=project(rotate(q,v.map(x=>x*1.15)),cx,cy,scale);
      line(origin,end,axisColors[i],3);
      ctx.fillStyle=axisColors[i]; ctx.font='bold 15px system-ui'; ctx.fillText('XYZ'[i],end[0]+5,end[1]-5);
    });
    ctx.fillStyle='#f8fbff'; ctx.beginPath(); ctx.arc(...origin,3,0,2*Math.PI); ctx.fill();
  }
  function render() {
    const rect=canvas.getBoundingClientRect(), dpr=window.devicePixelRatio||1;
    if(canvas.width!==Math.round(rect.width*dpr)||canvas.height!==Math.round(rect.height*dpr)) {
      canvas.width=Math.round(rect.width*dpr); canvas.height=Math.round(rect.height*dpr);
    }
    ctx.setTransform(dpr,0,0,dpr,0,0); ctx.clearRect(0,0,rect.width,rect.height);
    const n=data.series.length, panelW=rect.width/n, cy=rect.height*0.49;
    const scale=Math.min(panelW*0.31,rect.height*0.30);
    const poses=data.series.map(s=>sample(s.quaternions,t));
    for(let i=0;i<n;i++) {
      const cx=panelW*(i+0.5), q=poses[i];
      if(i) line([panelW*i,20],[panelW*i,rect.height-20],'#33435b',1);
      ctx.fillStyle=colors[i%colors.length]; ctx.textAlign='center'; ctx.font='600 17px system-ui';
      ctx.fillText(data.series[i].name,cx,35);
      for(let j=0;j<3;j++) {
        const basis=[0,0,0]; basis[j]=0.85;
        line(project([0,0,0],cx,cy,scale),project(basis,cx,cy,scale),'#40516a',1);
      }
      if(q) drawPose(q,cx,cy,scale);
      else { ctx.fillStyle='#f8c675'; ctx.font='15px system-ui'; ctx.fillText('IMU 時刻欠損',cx,cy); }
    }
    timeLabel.textContent=`${t.toFixed(2)} / ${end.toFixed(2)} s`;
    range.value=String(Math.round((t-data.times[0])*1000));
    details.innerHTML=poses.map((q,i)=>{
      const a=q?angles(q):null, peer=i&&poses[0]&&q?` · 基準との差 ${angleBetween(poses[0],q)}°`:'';
      return `<div class="detail"><strong style="color:${colors[i%colors.length]}">${data.series[i].name}</strong>`+
        (a?`roll ${a[0]}° · pitch ${a[1]}° · yaw ${a[2]}°${peer}`:'欠損区間')+'</div>';
    }).join('');
  }
  function frame(stamp) {
    if(running&&lastFrame!==null) { t+=Math.min((stamp-lastFrame)/1000,0.1)*Number(speed.value); if(t>=end) { t=end; running=false; play.textContent='▶ 再生'; } }
    lastFrame=stamp; render(); requestAnimationFrame(frame);
  }
  play.addEventListener('click',()=>{ running=!running; play.textContent=running?'⏸ 停止':'▶ 再生'; if(running&&t>=end)t=data.times[0]; });
  range.addEventListener('input',()=>{ t=data.times[0]+Number(range.value)/1000; render(); });
  document.getElementById('reset-view').addEventListener('click',()=>{ yaw=-0.68; pitch=0.43; render(); });
  canvas.addEventListener('pointerdown',e=>{ dragging=true; lastX=e.clientX; lastY=e.clientY; canvas.setPointerCapture(e.pointerId); });
  canvas.addEventListener('pointermove',e=>{ if(!dragging)return; yaw+=(e.clientX-lastX)*0.008; pitch=Math.max(-1.45,Math.min(1.45,pitch+(e.clientY-lastY)*0.008)); lastX=e.clientX; lastY=e.clientY; render(); });
  canvas.addEventListener('pointerup',()=>{ dragging=false; });
  window.addEventListener('resize',render);
  requestAnimationFrame(frame);
})();
</script>
</body>
</html>
"""


def save_attitude_animation(
    path: Path,
    title: str,
    subtitle: str,
    times_s: np.ndarray,
    series: dict[str, np.ndarray],
    *,
    max_frames: int = 1800,
) -> None:
    if len(times_s) < 2 or np.any(np.diff(times_s) <= 0):
        raise ValueError("animation needs increasing timestamps")
    if not series or any(len(q) != len(times_s) for q in series.values()):
        raise ValueError("animation series must match timestamps")
    indices = np.unique(np.r_[np.linspace(0, len(times_s) - 1,
                                          min(max_frames, len(times_s))).astype(int),
                                 len(times_s) - 1])
    payload = {
        "times": np.round(times_s[indices], 5).tolist(),
        "series": [{
            "name": name,
            "quaternions": [None if not np.isfinite(row).all() else
                            np.round(row, 6).tolist() for row in q[indices]],
        } for name, q in series.items()],
    }
    html = (HTML_TEMPLATE.replace("__TITLE__", title)
            .replace("__SUBTITLE__", subtitle)
            .replace("__DATA__", json.dumps(payload, ensure_ascii=False, separators=(",", ":"))))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
