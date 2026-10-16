# Blender Visualization

Blenderは発表・展示用の可視化にのみ使用する。正式な数値評価はPython等の解析結果を使用する。

想定入力:
- trajectory CSV
- camera pose
- parent / child position
- PLY point cloud
- OBJ / PLY mesh

想定用途:
- 親機・子機の3D配置
- 音響距離線・光学視線の表示
- AquaBeacon / IMU / VSLAM / VI-SLAM軌跡比較
- 3Dマップ生成アニメーション
- AquaBeacon拘束型マッピングとVSLAMマッピングの比較

## 親子機姿勢・相対位置シーン

Blender 5.2で各実験の `calibrated_estimate/fused_position.csv` と親子機融合姿勢CSVを
読み込み、実験別の `.blend` と静止画を生成する。PowerShellでの例:

```powershell
$root = (Get-Location).Path
foreach ($id in @('20260922_cmain_022','20260922_cmain_023','20260922_cmain_024')) {
  & 'C:\Program Files\Blender Foundation\Blender 5.2\blender.exe' -b --python blender/create_pose_scene.py -- $root $id
}
```

出力は `results/<experiment_id>/calibrated_estimate/pose_trajectory.blend`、
`blender_preview.png`、`blender_scene_manifest.json`。Blenderで `.blend` を開き、
タイムラインを動かすと姿勢と位置を確認できる。子機の位置が無い区間は子機proxyと
親子機を結ぶ線を非表示にし、軌跡を分断する。

表示座標は親機カメラを固定原点、+Xを初期光軸の水平投影、+Yを右、+Zを上とする。
親機と子機の水平姿勢基準は未校正で、子機の初期姿勢は表示のため任意に合わせる。
子機BNO085の磁気観測は別ログに存在するが、このシーンのQuaternionは磁気を使わない
Game Rotation Vector由来である。磁気を使った親子機方位合わせは未適用。
簡略形状は実機寸法ではない。Blenderシーンを評価計算には使用しない。
