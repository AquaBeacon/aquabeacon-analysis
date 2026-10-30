# Blender Visualization

Blenderは発表・展示用の可視化にのみ使用する。正式な数値評価はPython等の解析結果を使用する。

## 長田高校・実CADを使う発表素材

最新は `python scripts/prepare_ngt_replay_v2.py --experiment 20261028_ngt_004` の後に
`python scripts/render_ngt_replay_v2.py --experiment 20261028_ngt_004` を実行する。
`presentation_v2/render/` の `position_attitude`、`map_vehicles`、`map_only` に
各 `.blend`、1080p静止画、MP4を保存する。Eeveeで実CADを照明し、姿勢の拡大表示を分ける。
続けて同じコマンドに`--clean-existing`を付け、見出し帯のない`*_clean_1080.png`と
`*_clean.blend`を別保存する。地図のみの単独図は全点に合わせた別カメラ、局所図は
3 mmの点記号を使用する。元の動画の点記号・時刻・カメラは変えない。
世界座標内のCADは実寸、拡大表示は姿勢確認専用で位置や機体の寸法を表さない。
計測FRD→磁北NED姿勢をENUへ変換。親機CAD軸は明示設定、子機CADのドーム側を前とする
対応は表示用の暫定値で校正済みとは扱わない。欠測時は実機表示を隠す。
最終地図の2回目の画像観測に合わせて点を表示し、オンライン生成の再現とは区別する。
機体あり・マップのみは同じ点・時刻・カメラを使う。点の記号を再構成面とは呼ばない。
1レンダラー・2スレッド・20分上限、全保存サンプルと再読込を検証する。

以下は旧出力の再現コマンド（中立姿勢の表示を含む）。

`python scripts/render_ngt_presentation.py --experiment 20261028_ngt_004` は
`presentation_mapping/scene.json` の写真面を撮影順に表示し、任意尺度の地図と実CADを別枠へ置く。
`python scripts/render_ngt_cad_positions.py --experiment 20261028_ngt_004` は
既存5Hz位置へ実寸CADを配置し、全サンプルをCONSTANT補間、欠測は全部品のvisibility driverで隠す。
出力はそれぞれ `results/<id>/presentation_animation/` と `presentation_positions/`。
両ラッパーは別々に実行し、1レンダラー・2スレッド・制限時間内で終了を確認する。

GLBとrig.jsonの凍結ハッシュを検証し、現行機械設計の変更は自動取り込みしない。
CAD設計原点と測定中心の対応、機体向き、子機カメラ・ライト交換後の正確な変換は未校正。
向きは中立表示、地図へのCAD実寸合わせは行わない。`.blend` とPNG/MP4、元サンプルCSVと
manifestを保持し、別用途に編集するときも尺度・欠測・近似表面という条件を残す。

## 長田高校の親子軌跡と壁地図

`create_ngt_visualizations.py --experiment <id>` は `visualize_ngt_positions.py` の出力を使う。
002〜004を明示指定し、`results/<id>/position_visualization/parent_child.blend` に保存する。
1フレーム0.2秒、CONSTANT補間で欠測時は子機と観測線を非表示にする。
親機の高さ0 m・子機の高さは負の相対下向き成分。測位のGNSS高度を変更せず、表示用座標を分離する。
機体は識別用の簡略形状であり、CAD寸法・未測定取付け・未適用姿勢を再現したとは扱わない。

004では `map_visualization/wall_map.blend` も保存。4断片の全体シーンと個別拡大シーンを選べる。
入力点群の共通位置prior座標をそのまま用い、追加の位置合わせをしない。
表示点の八面体は見やすさのための記号で、再構成した面ではない。

## 今後の実験用CADアセット

```powershell
python scripts/prepare_cad_rig.py --rig-id cad_20261027
& 'C:\Program Files\Blender Foundation\Blender 5.2\blender.exe' -b -t 4 --python blender/create_cad_rig_scene.py -- --rig-id cad_20261027
```

`output/cad_20261027/cad_rig.blend` と `cad_rig.glb`、親子それぞれの外観・内部PNG、
全体PNG、`manifest.json` を生成する。1 Blender unit = 1 m。
親子それぞれのCADコレクションをアセット登録し、内部部品を個別に選択できる。
全体シーンの親子間隔は表示用で、実験での距離ではない。
`__REFERENCE_ONLY` のEmptyはCAD参照点であり、光学中心や音響中心を表さない。
TD04A本体は元CADに存在しないため、固定治具のみを表示する。

新しい実験用シーンから `cad_assets.load_cad_body(bundle, "parent" / "child")` を呼ぶと、
原点・軸を保つroot Emptyと部品群を取得できる。rootへ `T_world_body` を設定・
キーフレーム登録して使用する。入力GLBと配置JSONのハッシュを読み込み時に検査する。
メートル換算とglTFのY-up変換は済んでいるため、追加の0.001スケールや90°補正は不要。
既存の `create_pose_scene.py` は従来の実験機体のままとする。
詳細は [../docs/cad_rig.md](../docs/cad_rig.md)。

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
