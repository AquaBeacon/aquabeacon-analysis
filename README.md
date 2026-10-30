# AquaBeacon Analysis

別のPCへの導入は **[セットアップと新しい実験の解析](docs/another_pc.md)** を参照。
解析コード・設定・共通カメラ校正・親子の軸校正と、
約12 MBの[校正根拠データ](calibration/reference_data/README.md)をGitHubに収録している。
元動画や全実験のログを移さずに校正値を確認・数値再計算できる。
新しい実験の原本はローカルの`data/raw/<experiment_id>/`へ配置する。

長田高校の[研究発表用レポート](results/20261028_ngt_000/research_report/report.md)と
[実映像の照合・12事例](results/20261028_ngt_000/research_report/media_review.md)を追加した。
親機の90°表示回転と内部校正の不整合を確認したため、既存の子機位置・軌跡・位置priorを使う地図尺度は再評価が必要。
成立フラグの集計と座標精度の保証を区別する。図の再生成はこのレポート内だけを対象とする。

```powershell
python scripts/prepare_ngt_research_report.py --experiment 20261028_ngt_000 --phase prepare
python scripts/prepare_ngt_research_report.py --experiment 20261028_ngt_000 --phase clips
python scripts/write_ngt_research_report.py --experiment 20261028_ngt_000
python scripts/verify_ngt_research_report.py --experiment 20261028_ngt_000
```

長田高校プールの写真付き3D地図・実CAD動画・GNSS俯瞰図は
[発表素材の入口](results/20261028_ngt_000/presentation/index.html) と
[検証・制約](results/20261028_ngt_000/presentation/report.md) を参照。
新しい可視化は元の位置推定CSV・疎点群・rawを上書きしない。

2026-10-30改訂：研究用の細かなブラウザ編集は2次元グラフを対象にする。
発表用の3D映像は構図・光・画質を優先し、MP4と編集用Blenderを渡す。
最新の `presentation_v2/` は磁北基準の実測姿勢を反映した親子CADと、
観測順に増える壁地図（機体あり／マップのみ）を別々に出力する。
後者は既存の位置拘束型COLMAP結果の再生で、子機のみVSLAMへの親機観測追加ではない。

```powershell
python scripts/prepare_ngt_replay_v2.py --experiment 20261028_ngt_004
python scripts/render_ngt_replay_v2.py --experiment 20261028_ngt_004
python scripts/render_ngt_replay_v2.py --experiment 20261028_ngt_004 --clean-existing
python scripts/verify_ngt_presentation_v2.py --experiment 20261028_ngt_004
python scripts/style_ngt_gnss_presentation.py --experiment 20261028_ngt_001
python scripts/open_ngt_research_figures.py --experiment 20261028_ngt_001 --experiment 20261028_ngt_004
```

グラフ一覧から対象を選び、見た目の設定だけを保存できる。`--catalog-only` はサーバーを
起動せず一覧を保存する。エディタは保存して終了、Ctrl+C、または30分無操作で終了する。
共通エディタの範囲外の特殊Artistは、元のPythonコードとFigureから編集する。
PowerPointは作らず、単体で貼れるPNG/SVG/PDFを渡す。3Dの`*_clean_1080.png`は
映像の見出し帯を外した静止画で、`*_clean.blend`から調整できる。
3Dの姿勢は磁北で精度未検証、子機CADの前方向対応と測定中心は表示上の暫定設定。
点の出現時刻は最終地図の2回目の画像観測であり、オンラインSLAMの生成時刻ではない。

```powershell
python scripts/export_ngt_textured_map.py --experiment 20261028_ngt_004
python scripts/verify_ngt_textured_map.py --experiment 20261028_ngt_004
python scripts/reconstruct_ngt_dense_patch.py --experiment 20261028_ngt_004
node scripts/verify_ngt_textured_map.mjs
python scripts/render_ngt_presentation.py --experiment 20261028_ngt_004
python scripts/render_ngt_cad_positions.py --experiment 20261028_ngt_004
python scripts/plot_ngt_gnss_overhead.py --experiment 20261028_ngt_001 --experiment 20261028_ngt_002 --experiment 20261028_ngt_003 --experiment 20261028_ngt_004
python scripts/plot_ngt_aerial_tracks.py --experiment 20261028_ngt_000
python scripts/plot_ngt_aerial_hires.py --experiment 20261028_ngt_000
python scripts/package_ngt_presentation.py --experiment 20261028_ngt_000
```

Blenderは一度に1プロセス・2スレッドで実行する。旧 `presentation_mapping/` のGLB/OBJは
単眼尺度未確定の疎な写真投影面で、密な完成モデルではない。
実CADの設計原点・取付けは未校正で、子機カメラとライトの実配置はCADと逆。
GNSS円は真値誤差や信頼区間ではなく、水平差または静止時の平均まわりRMSを示す。

子機のみの地図表示・IMU/深度DR融合・VI-SLAM試行は
[結果レポート](results/20261028_ngt_000/child_sensor_fusion/report.md) と
[オフライン3D地図](results/20261028_ngt_000/child_sensor_fusion/maps.html) を参照。
各コマンドで001〜004を`--experiment`の繰り返しで選択する。004の再現例：

```powershell
python scripts/prepare_ngt_child_fusion.py --experiment 20261028_ngt_004
python scripts/run_ngt_child_vislam.py --build
python scripts/run_ngt_child_vislam.py --experiment 20261028_ngt_004
python scripts/fuse_ngt_child_dead_reckoning.py --experiment 20261028_ngt_004
python scripts/prepare_ngt_child_fusion.py --experiment 20261028_ngt_004 --camera-imu
python scripts/run_ngt_child_vislam.py --experiment 20261028_ngt_004 --camera-imu
python scripts/visualize_ngt_child_maps.py --experiment 20261028_ngt_004
python scripts/report_ngt_child_fusion.py --experiment 20261028_ngt_004
python scripts/verify_ngt_child_fusion.py --experiment 20261028_ngt_004 --tests
```

VI実行にはDocker Desktopと既存ORB-SLAM3イメージ、準備済みの子機単眼画像が必要。
既存runは上書きしない。地図の尺度、圧力への残差、独立した真値誤差を区別する。

2026-10-30: 沖縄の水中内部校正を親機・子機の共通既定として
[`calibration/camera_defaults.json`](calibration/camera_defaults.json) に登録。
長田高校プールの子機のみ単眼VSLAMは次で実行・評価する。原動画の約120 fpsを維持し、
親機観測を入力しない。子機だけで同期し、回転角変化量の一致性を評価する。

```powershell
python scripts/run_ngt_child_vslam.py --experiment 20261028_ngt_001 --experiment 20261028_ngt_002 --experiment 20261028_ngt_003 --experiment 20261028_ngt_004
python scripts/evaluate_ngt_child_vslam.py --experiment 20261028_ngt_001 --experiment 20261028_ngt_002 --experiment 20261028_ngt_003 --experiment 20261028_ngt_004
python scripts/verify_ngt_child_vslam.py --experiment 20261028_ngt_001 --experiment 20261028_ngt_002 --experiment 20261028_ngt_003 --experiment 20261028_ngt_004
```

出力は各実験の `results/<id>/child_only_vslam/`、比較は
[`results/20261028_ngt_000/child_only_vslam/report.md`](results/20261028_ngt_000/child_only_vslam/report.md)。
位置の真値・単眼尺度・光学―機体回転が未確定のため、位置ATE/RPE[m]や絶対姿勢誤差とは区別する。

> AquaBeacon実験で取得した親機・子機・Spresense IMU・2台のカメラ動画を統合し、時刻同期、位置推定、マッピング、比較評価、発表用可視化まで行う後処理解析基盤。

## 主な機能
- **データ品質確認** — CSV・動画・タイムスタンプ・欠損・サンプリング周期を確認
- **時刻同期** — GPS PPS / RTC / GPS時刻 / カメラ録画開始時刻を共通時間軸へ統合し、IMU相互相関で微調整
- **位置推定** — AquaBeacon / IMU Dead Reckoning / 単眼VSLAM / Visual-Inertial SLAMを比較
- **マッピング** — AquaBeacon位置拘束型COLMAP/SfMと単眼VSLAMの3Dモデルを比較
- **可視化** — 定量評価用グラフとBlender向け発表用データを出力

詳細な受け入れ条件は [docs/spec.md](docs/spec.md) を参照。

## 2026-10-28 学校プール実験

今回の入口は [解析レポート](results/20261028_ngt_000/report.md)。
`data/raw/ngtHighSchoolExperiment1/` を移動せず、全収録を `20261028_ngt_000`、
親機静止4:00–8:00を001、子機静止17:30–18:30を002、
範囲限界18:30–21:00を003、同時移動21:00–25:40を004として登録した。
切出し動画・センサCSVは `data/interim/<id>/synchronized/`、
GNSS・姿勢・深度の結果と編集可能な図は `results/<id>/`。

カメラ内IMUと機体IMUの動きから時計のoffset・driftを推定する。
保存済みの前処理に対する再実行例は以下。実験を指定せずに全rawを走査しない。

```powershell
python scripts/prepare_ngt_cameras.py --experiment 20261028_ngt_000
python scripts/synchronize_ngt.py --experiment 20261028_ngt_000
python scripts/split_ngt_experiments.py --experiment 20261028_ngt_000
python scripts/analyze_ngt_gnss.py --experiment 20261028_ngt_000 --camera-start-utc 2026-10-28T05:03:01.343757+00:00 --camera-time-scale 0.999975135251818
python scripts/track_ngt_light.py --experiment 20261028_ngt_000
python scripts/fuse_ngt_sensors.py --experiment 20261028_ngt_000
python scripts/plot_ngt_aerial_tracks.py --experiment 20261028_ngt_000
python scripts/select_ngt_gnss.py --experiment 20261028_ngt_000
python scripts/ppk_ngt_geonet.py --experiment 20261028_ngt_000
python scripts/compare_ngt_ppk_bases.py --experiment 20261028_ngt_000
python scripts/report_ngt_ppk_comparison.py --experiment 20261028_ngt_000
python scripts/verify_ngt_artifacts.py --experiment 20261028_ngt_000
```

初回のバイナリ復号は `scripts/decode_ngt_logs.py --experiment <id> --firmware-root ../firmware-experiments --source <raw内のファイル>`。
入力と読み取りコードのハッシュ・CRC検証結果を保存し、既存の復号CSVを上書きしない。
PPKの再現コード・設定は `data/interim/20261028_ngt_000/gnss/reproduce_ppk.py` に保存。
元の混在MSM4/7と、追尾切れ情報を保持したMSM7の比較を残した。
真値座標はないため、静止結果は平均まわりのばらつきとして評価する。
10月30日の追加評価では、沖縄の共通水中内部校正と今回の親機磁北姿勢を使い、
音響距離×ライト光線の相対位置を算出した。測定中心と光学外部校正が未確定のため、
磁北相対位置と、偏角0°・ゼロレバーアームを仮定したGNSS合成参考座標を区別する。
[位置推定評価](results/20261028_ngt_000/position_evaluation/report.md) と
[同時移動区間のプール壁](results/20261028_ngt_004/wall_mapping/report.md) を参照。

```powershell
python scripts/check_ngt_optical_axes.py --experiment 20261028_ngt_000
python scripts/evaluate_ngt_position.py --experiment 20261028_ngt_002 --experiment 20261028_ngt_003 --experiment 20261028_ngt_004
python scripts/report_ngt_position_evaluation.py --experiment 20261028_ngt_000
# 壁用COLMAP Python wheelは今回の派生物フォルダだけへ配置
python -m pip install --only-binary=:all: --no-deps --target data/interim/20261028_ngt_004/wall_mapping/runtime pycolmap==4.2.1
python scripts/evaluate_ngt_wall.py --experiment 20261028_ngt_004 --phase prepare
python scripts/evaluate_ngt_wall.py --experiment 20261028_ngt_004 --phase features
python scripts/evaluate_ngt_wall.py --experiment 20261028_ngt_004 --phase visual-only
python scripts/evaluate_ngt_wall.py --experiment 20261028_ngt_004 --phase prior
python scripts/report_ngt_wall.py --experiment 20261028_ngt_004
python scripts/verify_ngt_position_evaluation.py --experiment 20261028_ngt_000
python scripts/visualize_ngt_positions.py --experiment 20261028_ngt_002 --experiment 20261028_ngt_003 --experiment 20261028_ngt_004
& 'C:\Program Files\Blender Foundation\Blender 5.2\blender.exe' -b -t 2 --python-exit-code 1 --python blender/create_ngt_visualizations.py -- --experiment 20261028_ngt_002 --experiment 20261028_ngt_003 --experiment 20261028_ngt_004
node scripts/verify_ngt_viewer.mjs
python scripts/run_ngt_restart_mapping.py --experiment 20261028_ngt_004 --phase prepare
python scripts/run_ngt_restart_mapping.py --experiment 20261028_ngt_004 --phase features
python scripts/run_ngt_restart_mapping.py --experiment 20261028_ngt_004 --phase matching
python scripts/run_ngt_restart_mapping.py --experiment 20261028_ngt_004 --phase map
python scripts/extend_ngt_restart_mapping.py --experiment 20261028_ngt_004 --window 15 --window 16 --window 17 --window 18
python scripts/report_ngt_restart_mapping.py --experiment 20261028_ngt_004
python scripts/verify_ngt_restart_mapping.py --experiment 20261028_ngt_004
& 'C:\Program Files\Blender Foundation\Blender 5.2\blender.exe' -b -t 2 --python-exit-code 1 --python blender/create_ngt_restart_map.py -- --experiment 20261028_ngt_004
& 'C:\Program Files\Blender Foundation\Blender 5.2\blender.exe' -b -t 2 --python-exit-code 1 --python blender/verify_ngt_visualizations.py
```

壁の5 Hz診断は同じ4コマンドへ `--dense-sampling` を追加する。
別出力の `five_hz/` に保存し、最初の2 Hz試行を保持する。推定継続率の分母は
全時間の5 Hzセル、静止ばらつきの分母は記録された音響イベント。欠測を補間しない。

[親機と子機の軌跡・3D・Blender](results/20261028_ngt_004/position_visualization/report.md)、
[壁地図の回転表示・Blender](results/20261028_ngt_004/map_visualization/report.md)を追加。
HTMLは単独で開ける。3D軌跡では親機を高さ0、子機を相対下向き成分だけ下げて表示する。
元のGNSS高度・評価値は変更しない。壁は元の疎点群を表示し、断片の隙間を面で補わない。

## 技術スタック
- Python 3.11+ を中心としたローカル解析
- COLMAPを基本としたSfM / Photogrammetry
- 単眼VSLAM / Visual-Inertial SLAMは外部実装と連携
- Blenderは発表用可視化専用
- GitHubはコード・設定・校正値・実験メタデータ・軽量結果の管理に使用

## セットアップ

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev,media,analysis,vision]"
```

## グラフをブラウザで編集する

今後グラフを生成すると、PNGと同じ場所にSVG、編集用の `.figure.pickle`、
生成情報の `.plot.json` が保存される。次のコマンドでローカルの編集画面を開く。

```powershell
python -m aquabeacon_analysis.editable_plots results/<experiment_id>/<analysis_name>/<figure>.figure.pickle
```

軸ラベル・タイトル・凡例・フォント・文字サイズ・軸範囲・目盛り間隔・グリッド、
系列ごとの色・線幅・線種・点の形・大きさ・縁・透明度を変更し、プレビューで確認できる。
画面上部の「フォント・文字サイズ」欄で、このPCのフォントを選び、全体の文字サイズを
一括指定できる。図全体タイトル・各グラフのタイトル・軸ラベル・目盛り・凡例も
スライダー／数値入力で個別指定できる。個別サイズが一括サイズより優先され、設定は保存される。
数値項目はスライダーと数値入力の両方で調整できる。`xlim`・`ylim` は左上の
「表示範囲」で最小値・最大値を指定する。スライダーの範囲外の値も数値入力で指定できる。
図の右端・下端・右下の青いハンドルをドラッグすると幅・高さを変更でき、離すと図を再描画する。
ハンドルにフォーカスして矢印キーでも調整可能。表示だけでなく、保存画像の寸法にも反映する。
幅・高さはinchとcmで確認でき、保存した寸法・表示範囲は次回も保持する。
「編集内容と画像を保存」で `.style.json` と `.edited.png/.svg/.pdf` を保存する。
同じ編集用ファイルを開けば保存済みの設定を読み込み、続きを編集できる。
散布図のサイズは面積 pt²、折れ線の点のサイズは直径 pt。
PNG/SVG/PDFのリンクは現在の見た目をダウンロードするが、設定の保存には保存ボタンを使う。
終了時は「保存して終了」または端末で Ctrl+C。常駐サービスや外部サービスは不要。

生成コードでは `save_editable_figure(fig, output, dpi=...)` を使う。
計測値や解析処理は変えず、見た目の変更をJSONに分離する。
既存のPNGだけの図は、対応する生成スクリプトで対象を明示して再生成すると編集用ファイルが付く。
元の生成スクリプト・入力CSV・config/calibration・解析manifestを残す。
pickleは自分のPCで生成したものだけを開き、生成時と同じMatplotlib環境を使う
（バージョンは `.plot.json` に記録）。長期保管の正本は生成コードと元の解析データ。
再生成時も `.style.json` は保持するため、系列構成を変えた場合は設定の対象を確認する。

## 今後の実験向けCAD機体モデル

`parent_hand_service` / `parent_exterior` / `child_horizontal_plate_frame` /
`child_interior` の配置を `cad_20261027` に固定した。センサー参照点・座標系・
原典ハッシュと再生成手順は [docs/cad_rig.md](docs/cad_rig.md) を参照。
**今後取得するデータ向け**であり、既存2026-09-22解析への自動適用は行わない。

```powershell
python -m pip install -e ".[dev,cad]"
python scripts/prepare_cad_rig.py --rig-id cad_20261027
python scripts/prepare_cad_rig.py --rig-id cad_20261027 --verify
& 'C:\Program Files\Blender Foundation\Blender 5.2\blender.exe' -b -t 4 --python blender/create_cad_rig_scene.py -- --rig-id cad_20261027
```

`data/interim/cad/cad_20261027/` にメートル単位の親子GLB、配置JSON、参照点CSVを保存。
`blender/output/cad_20261027/cad_rig.blend` は部品別表示と再利用可能な機体コレクションを
持つ。カメラ光学中心、IMU測定中心、TD04A音響中心は未確定で、CAD取付点を
校正済み外部変換として使わない。CADが更新された場合は新しいrig IDで採取し直す。

## 別の実験を追加するとき

実験ごとに `experiments/<experiment_id>/experiment.json` を作り、生データの参照先と
使用予定の設定・校正を登録する。新規IDは `YYYYMMDD_location_NNN`（同日でも試行ごとに別ID）。

```powershell
python -m aquabeacon_analysis list-experiments
# 以下のID・ログ番号は例。実際の取得日・場所・C_MAINログ番号に置き換える。
python -m aquabeacon_analysis init-experiment 20261027_pool_001 --c-main-suffix 22
python -m aquabeacon_analysis show-experiment 20261027_pool_001
```

初期化は台帳と実験別の中間・処理済み・結果フォルダを作る。原本は操作しない。
原本を取り込む担当者が `data/raw/<experiment_id>/` 内を機器別に配置し、以降は読み取り専用で扱う。
設定・校正の参照先を台帳に記入してから、次のように対象を指定する。

```powershell
python -m aquabeacon_analysis sync-raw --experiment 20261027_pool_001
python -m aquabeacon_analysis analyze-sync --experiment 20261027_pool_001
python -m aquabeacon_analysis preprocess-tof --experiment 20261027_pool_001
# 親機ログも使用する場合は、台帳にp_main_suffixを登録して実行する。
python scripts/extract_p_main_tof.py --experiment 20261027_pool_001
python -m aquabeacon_analysis localize-imu --experiment 20261027_pool_001
```

対象指定なしの解析はエラーにする。登録前の従来データは `--session <完全な実験ID>`、
全件処理を意図する場合は `--all-experiments` を使う。重複する数字のログ番号だけでの選択は拒否する。
`--experiment` とルートパスの上書きは併用できない。
校正参照の登録は数値処理への自動適用を意味しない。実際の適用値は各解析のmanifestで確認する。

配置と既存データの対応は [data/README.md](data/README.md)、
実験台帳は [experiments/README.md](experiments/README.md) を参照。
以下の9月22日向けスクリプトには日付・校正値の固定が残るため、新しい実験への実行前に入力と適用条件を確認する。

## C_MAIN RTC基準の時刻合わせ

`data/raw/C_MAIN/` の各IMUログのUTC換算済みRTC範囲を実験区間とし、C_MAIN、対応する
Spresense `G/I`、2台のカメラを共通の `experiment_time_s` へ揃える。

```bash
python -m aquabeacon_analysis sync-raw --raw-root data/raw --all-experiments
```

- 元データは変更せず、同期済みCSV・トリミング動画を
  `data/interim/<experiment_id>/` へ保存する。
- 同期根拠、SONY時計回帰、元動画内オフセット、警告は
  `results/<experiment_id>/synchronization/manifest.json` に保存する。
- DJI動画の固有 `djmd` は一般的なMP4再多重化では保持できないため、LRFから姿勢・加速度等を
  `camera_telemetry/*.csv` へ抽出する。切り出し動画は映像・音声のみである。
- カメラの粗同期はQuickTime `creation_time` を使用する。IMU相互相関による微調整は別工程とする。

### IMU相互相関・同期図・品質確認

```bash
python -m aquabeacon_analysis analyze-sync --experiment 20260922_cmain_023
```

- SONY SpresenseはP_MAIN取付として、`SONY IMU ↔ P_MAIN_CAM` を比較する。
- C_MAINは `C_MAIN IMU ↔ C_MAIN_CAM` を比較する。
- 加速度ノルムを30 Hzへ補間し、1秒移動平均を除いた波形で±5秒のoffsetを探索する。
- 補正済みカメラCSVは `data/interim/<experiment_id>/refined_sync/` に保存する。
- offset、品質表、タイムライン、相関診断図、サンプリング周期図は
  `results/<experiment_id>/synchronization/refined/` に保存する。

## ToF有効区間の前処理

P_MAINの生HEALTH_RANGEログを追加した場合は、次のコマンドで各ファイルのUTCアンカーから
時刻を復元し、継続する有効ToF区間を切り出す。

```bash
python scripts/extract_p_main_tof.py --all-experiments
```

出力は `data/processed/<date>_pmain_<suffix>/tof_window/p_main/` のCSVと
`results/<date>_pmain_<suffix>/preprocessing/manifest.json`。既存のC_MAIN ToF窓と
UTCで重なる行は、対応する `data/processed/<c_main_experiment_id>/tof_window/p_main/`
にも保存する。ファイル番号は機器間の対応付けに使わない。

```bash
python -m aquabeacon_analysis preprocess-tof --experiment 20260922_cmain_023
```

`data/interim/` の各実験で、`C_MAIN_HEALTH_RANGE` の `tof_us > 0` かつ
`range_state` が2または3の行を有効とする。起動時の単発値を除くため、5行以上連続する
最初の有効行から、最後の有効行の次に無効となる時刻までを切り出す。途中の無効行は残し、
health CSVに `tof_valid` 列を付ける。

出力は `data/processed/<experiment_id>/tof_window/` にセンサCSV、補正済みカメラ時刻CSV、
親機・子機動画の通常画質版とproxy版を保存する。各CSVの `window_time_s` は区間開始からの秒数。
C_MAIN動画は回転メタデータを0度にして横表示にする。画素の再圧縮はしない。
切り出しは高速なstream copyのため動画先頭にはキーフレーム単位のずれがあり得る。
フレームの正確なUTC対応には `refined_sync_utc_us` を使用する。
切り出し条件・入力・Git SHA・config/calibrationのSHA-256は
`results/<experiment_id>/preprocessing/manifest.json` に記録する。

## IMU姿勢・親機相対位置の推定

`data/processed/<experiment_id>/tof_window/` の実験に対し、次を実行する。

```bash
python -m aquabeacon_analysis localize-imu --session 20260922_cmain_022 --session 20260922_cmain_023 --session 20260922_cmain_024
```

`results/<experiment_id>/localization/` に親機P_MAINカメラ・SONY、子機C_MAIN本体・
カメラのセンサ姿勢CSV、親機IMU軌跡 `parent_imu.csv`、XY図、manifest、reportを保存する。
親機の融合姿勢は、角速度の動きからSONY→カメラの取付回転を推定し、SONY角速度で姿勢を
逐次予測してP_MAINカメラQuaternionへ0.5秒時定数で補正する。`parent_fused_attitude.csv`
と `parent_imu.csv` の `fused_*` 列に保存する。取付角度は実験中の動きからの推定値であり、
外部校正値ではない。3軸の動きから±0.1秒の残留時刻ずれも調べ、角速度残差が
10%以上改善したときだけ適用してmanifestに記録する。

`parent_attitude_3d.html` と `child_attitude_3d.html` はブラウザで開ける自己完結型の
3Dアニメーション。再生・時刻スライダー・速度変更・ドラッグによる視点回転に対応する。
親機はカメラ、融合姿勢、SONY角速度のみを同一初期姿勢で表示。子機は本体IMUとカメラを
角速度から推定した軸回転と初期姿勢で揃えて表示する。子機本体IMUの時刻ギャップは
欠損として表示し、補間して見せない。
子機の `child_fused_attitude.csv` は両Quaternionを球面補間で融合する。両者の差が5°以下なら
等重み、5〜20°は本体IMUの重みを下げ、20°以上または時刻欠損ではカメラのみを使う。
各行の差・重み・状態をCSVに残す。子機アニメーションには融合姿勢も表示する。

親機軌跡はP_MAINカメラ加速度とSONY加速度をそれぞれ単独、等重み平均、融合姿勢を
使う平均の4通りで重力除去・二重積分したもの。
初期位置・速度は0、加速度バイアスは0としており、ドリフト補正は行わない。

座標は重力方向だけが定まった**局所座標**であり、水平X/Yの東・北対応は未検証。
カメラ・SONYから親機本体原点への取付位置も未校正。したがって出力は親機の実位置や
絶対ENU座標ではなく、IMUのみの比較baselineとして扱う。Ground Truthはない。
姿勢融合後も位置の二重積分ドリフトは解消しない。

### SONY GPSによる親機の水平位置

同じコマンドでSONY Spresense `G*.CSV` の測位値をWGS84から、実験最初の有効測位を
原点とする局所ENUへ変換する。`parent_gps_observations.csv` は約1 Hzの元測位と品質を保持し、
`parent_gps_position.csv` は同じGPS区間内だけをカメラ時刻へ線形補間した水平位置。
補間値は新たな30 Hzの測位を意味しない。`parent_gps_imu_diagnostic.png` はGPSとIMU積分を
別々の軸で表示する。親機カメラCSVのGPS値は実験中一定だったため、位置更新には使わない。

fix・衛星数・時刻間隔を確認し、前後が低速なのに1秒で2 m/s超の孤立した移動がある場合は
GPS軌跡を分割する。分割点をまたいだ位置は補間せず欠損とする。GPSアンテナから親機本体・
カメラへの取付位置は未校正で、GPS高度の変動も大きいため、カメラ時刻の鉛直位置は出さない。
カメラ姿勢の水平方位とENUの対応も未検証なので、IMU水平加速度をENUへ回して位置融合した
ことにはしない。Ground TruthがないためGPSの絶対精度は評価できない。

### 水中カメラ内部校正を使った再推定

2026-09-24の水中チェッカーボードから得た1280×720 proxy用の内部行列と歪み係数を、
2026-09-22のライト追跡座標へ適用して親子機の相対位置を再計算する。

```bash
python scripts/estimate_calibrated_positions.py
& 'C:\Program Files\Blender Foundation\Blender 5.2\blender.exe' -b --python blender/create_body_geometry_review.py -- 20260922_cmain_023
```

各実験の `results/<experiment_id>/calibrated_estimate/` に視線角、親子機相対位置、
圧力優先の融合位置、manifest、`report.md`、結果グラフと姿勢3D表示をまとめて保存する。
旧90°仮画角の結果は別ディレクトリに残る。
Blender可視化は[手順](blender/README.md)に従って同フォルダに `.blend` と静止画を生成する。
子機BNO085の磁気ログは `python scripts/diagnose_child_magnetometer.py` で診断図を作れる。
記録済みQuaternionは磁気を使わないGame Rotation Vectorで、磁気観測による
親子機の方位合わせは現時点では適用していない。
親機・子機の姿勢CSVと親機GPS水平位置は `localize-imu` の出力を使う。
実験機体モデルには取付面と内装の参照配置があるが、カメラ光学軸からIMU軸への
取付回転と各センサ中心の位置は実測されていないため、
子機のbody原点位置やENU絶対位置は確定しない。結果の条件は
[再推定レポート](docs/reports/calibrated_pose_position_20260922.md)を参照。

## VI-SLAM 実行環境（暫定校正試験）

単眼＋IMU の比較基盤には [ORB-SLAM3](https://github.com/UZ-SLAMLab/ORB_SLAM3) を使用する。
固定コミットをビルドする設定は `docker/orbslam3/Dockerfile` にある。
Windows では WSL 2 と Docker Desktop を使用する。このPCでは2026-09-23に
Docker Desktop 4.91.0上でイメージをビルドし、実行ファイルと語彙ファイルを確認済み。
  022・023・024について暫定カメラモデルで全区間と短区間を試験した。生成軌跡があっても
  校正完了まで定量的な精度比較には使わない。
  3実験とも慣性初期化済みの比較用軌跡が得られていない。
  結果と残る切り分けは [docs/vislam_environment.md](docs/vislam_environment.md) を参照。

```powershell
./scripts/run_orbslam3.ps1 -Build
python scripts/verify_orbslam3_alignment.py 20260922_cmain_023 --source-offset 8460 --source-first-camera-frame 1620 --rotation 90
python scripts/prepare_orbslam3.py 20260922_cmain_023 --first-camera-frame 10080 --max-frames 900 --alignment-note 'results/20260922_cmain_023/vislam_depth/frame_alignment.json'
./scripts/run_orbslam3.ps1 -Pilot -ExperimentId 20260922_cmain_023 -SettingsPath calibration/child_orbslam3_provisional_023.yaml
# 022の全区間: IMU時刻の残差-25 msを出力側に適用
python scripts/prepare_orbslam3.py 20260922_cmain_022 --first-camera-frame 8940 --imu-time-offset-s -0.025 --output-root data/interim/orbslam3_full --alignment-note 'results/20260922_cmain_022/vislam_depth/frame_alignment.json'
./scripts/run_orbslam3.ps1 -ExperimentId 20260922_cmain_022 -SettingsPath calibration/child_orbslam3_provisional_022.yaml -DatasetRoot data/interim/orbslam3_full
# 全区間を出力し、校正値を確認してから正式実行する場合:
# python scripts/prepare_orbslam3.py 20260922_cmain_023 --first-camera-frame 10080 --alignment-note 'results/20260922_cmain_023/vislam_depth/frame_alignment.json'
# ./scripts/run_orbslam3.ps1 -ExperimentId 20260922_cmain_023 -SettingsPath calibration/child_orbslam3.yaml
```

`prepare_orbslam3.py` は `data/processed/<experiment_id>/tof_window/` を読み、
`data/interim/orbslam3/<experiment_id>/` にEuRoC形式の画像・IMUと別系列の圧力CSVを出す。
動画はstream copy切り出しのため先頭に余分なフレームがあり、`--first-camera-frame` は
**処理済み動画のデコード0枚目**に対応するテレメトリの `camera_frame` を映像照合で確定して指定する。
推測値で本解析を走らせない。圧力はORB-SLAM3には直接入力されず、後段の深度補正用に保持する。
暫定値は `calibration/child_orbslam3_provisional_023.yaml` に明示する。
1280×720 proxy用の水中カメラ内部パラメータには2026-09-24撮影の暫定候補がある。
子機のカメラ→IMU変換とIMUノイズの測定、過去動画との画角照合後に
`calibration/child_orbslam3.yaml` を確定し、VI-SLAMの本解析を実行する。
EuRoC例の校正値は流用しない。
実行条件と未解決事項は [docs/vislam_environment.md](docs/vislam_environment.md) に記載。

子機のみの校正値適用単眼VSLAMは次のコマンドで再実行できる。水中校正JSONは
親子機共通カメラの撮影から得た値で、親機の実験観測は使用しない。単眼の距離スケールは未確定。

```powershell
python scripts/prepare_child_calibrated_orbslam3_settings.py 023
./scripts/run_orbslam3.ps1 -Build
./scripts/run_orbslam3.ps1 -ExperimentId 20260922_cmain_023 -DatasetRoot data/interim/orbslam3_full -SettingsPath calibration/child_orbslam3_underwater_trial_023.yaml -SensorMode Monocular -RunName calibrated_mono_023_full
python scripts/summarize_child_vslam.py 20260922_cmain_023 calibrated_mono_023_full
python scripts/plot_child_vslam_coverage.py
```

出力`results/child_vslam_20260922_coverage.png`は3実験の姿勢保存区間を示す。
全区間の追跡欠損とCLAHE比較は
[子機VSLAMレポート](docs/reports/child_vslam_calibrated_20260922.md)に記載。

各実験のVSLAM映像・代表画像・Atlasの疎3D点群（PLY）は
`results/<experiment_id>/vislam_depth/` の `report.md` と同じフォルダに保存する。
点群を書き出すには地図出力パッチを含むDockerイメージを`-Build`で再作成し、
新しい実行名で単眼VSLAMを実行する。

```powershell
./scripts/run_orbslam3.ps1 -Build
./scripts/run_orbslam3.ps1 -ExperimentId 20260922_cmain_023 -DatasetRoot data/interim/orbslam3_full -SettingsPath calibration/child_orbslam3_underwater_trial_023.yaml -SensorMode Monocular -RequireMapExport -RunName calibrated_mono_map_full
python scripts/summarize_child_vslam.py 20260922_cmain_023 calibrated_mono_map_full
python scripts/render_child_vslam_artifacts.py 20260922_cmain_023 --run-name calibrated_mono_map_full
```

動画は子機画像に姿勢保存状態を重ねた後処理表示。PLYは地図ごとに座標系が独立し、
距離単位は未確定の単眼SLAM単位である。

元動画の`120000/1001 fps`をテレメトリ対応区間で間引かず使用する実行では、先に
`verify_child_120fps_alignment.py`でプロキシ画像`i`と元動画`4i`の対応を確認する。
30 Hzの子機カメラテレメトリをUTCアンカーとし、中間フレーム時刻を線形補間する。
対応区間より前のUTC未確定フレームは入力しない。採用した元動画の各フレームは
1280×720へ縮小して、同解像度の水中校正値を用いる。
追跡後の特徴点と3D地図点対応はORB-SLAM3本体から書き出す。各フレームの
検出数・3D対応数は全件記録し、可視化用CSVには3D対応点の全件と
未対応の検出点を10点に1点保存する。

```powershell
python scripts/verify_child_120fps_alignment.py 20260922_cmain_023
python scripts/prepare_child_calibrated_orbslam3_settings.py 023 --source-120fps
python scripts/prepare_child_orbslam3_120fps.py 20260922_cmain_023
./scripts/run_orbslam3.ps1 -Build
./scripts/run_orbslam3.ps1 -ExperimentId 20260922_cmain_023 -DatasetRoot data/interim/orbslam3_120fps -SettingsPath calibration/child_orbslam3_underwater_trial_120fps_023.yaml -SensorMode Monocular -RequireMapExport -RunName calibrated_mono_120fps_full
python scripts/summarize_child_vslam.py 20260922_cmain_023 calibrated_mono_120fps_full --dataset-root data/interim/orbslam3_120fps
python scripts/render_child_vslam_120fps.py 20260922_cmain_023
```

同じ手順を022・024にも適用する。`features_120fps.mp4`には全入力フレームを
約119.88 fpsで収録し、検出特徴点と連続フレームで同じ3D地図点へ対応した点を表示する。
動画・代表画像・3D点群と実行証跡は各実験の`vislam_depth/report.md`から参照する。

元動画の色を保持する再実行には`--color-jpeg`を指定する。JPEGは3チャンネルの
BGR画像として書き出し、`Camera.RGB: 0`でORB-SLAM3へ渡す。
ORB-SLAM3は特徴点計算時に内部で輝度画像へ変換するが、入力と可視化映像はカラーで保持する。
レポートには以前の白黒実行を比較用に残し、カラーの動画・画像・PLYを別名で追加する。

```powershell
python scripts/prepare_child_orbslam3_120fps.py 20260922_cmain_023 --color-jpeg
./scripts/run_orbslam3.ps1 -ExperimentId 20260922_cmain_023 -DatasetRoot data/interim/orbslam3_color_120fps -SettingsPath calibration/child_orbslam3_underwater_trial_120fps_023.yaml -SensorMode Monocular -RequireMapExport -RunName calibrated_mono_color_120fps_full
python scripts/summarize_child_vslam.py 20260922_cmain_023 calibrated_mono_color_120fps_full --dataset-root data/interim/orbslam3_color_120fps
python scripts/render_child_vslam_120fps.py 20260922_cmain_023 --color
```

画像を3D空間へ配置した版と、疎点群表面へ画像を投影した版を追加する場合:

```powershell
python -m pip install -e ".[mapping]"
python scripts/export_child_textured_map.py 20260922_cmain_023
```

各実験の`vislam_depth/report.md`と同じフォルダへ、画像埋め込みの操作可能なHTMLと
テクスチャ付きGLBを出力する。HTMLは画像平面・表面投影を切り替えられる。
画像平面は推定姿勢での可視化、表面投影は疎点群からの局所三角形であり、密な表面復元ではない。

特徴点不足の切り分けと子機融合姿勢を使う暫定試験は次の手順で行う。
CLAHE試験データは `data/interim` に置き、元画像と元IMUは変更しない。

```powershell
python scripts/diagnose_vislam_features.py 20260922_cmain_024
python scripts/prepare_orbslam3_fused_imu.py 20260922_cmain_024
python scripts/prepare_orbslam3_clahe_trial.py 20260922_cmain_024 --start-image 2100 --end-image 3900
./scripts/run_orbslam3.ps1 -Pilot -ExperimentId 20260922_cmain_024 -DatasetRoot data/interim/orbslam3_clahe_trial -SettingsPath calibration/child_orbslam3_fused_trial_024.yaml -ImuOverridePath data/interim/orbslam3_fused/20260922_cmain_024/data.csv
```

融合quaternionは角速度へ変換して入力する。ORB-SLAM3が融合絶対姿勢を直接使う構成ではない。
このため `child_fused_attitude.csv` の姿勢がSLAM軌跡へ厳密に反映される保証はない。

## 子機ライトの画像座標

親機 `P_MAIN_CAM` のproxy動画を解析し、0.5秒間隔のライト座標を出す。

```bash
python -m aquabeacon_analysis detect-lights --experiment 20260922_cmain_023
python -m aquabeacon_analysis detect-lights --session 20260922_cmain_023
```

出力は `results/<experiment_id>/light_detection/parent_light.csv` と `manifest.json`。
全自動の座標は `candidate_unreviewed` で、反射による誤候補が含まれる。
目視で確認した10秒間隔のサンプルは `python scripts/export_reviewed_lights.py` で
`reviewed_light.csv` に抽出する。座標原点は未回転動画の左上、単位はproxy動画のpixel。
検出できない時刻は `status=not_detected` と空欄で残す。`video_time_s` は動画先頭からの秒数で、
stream copyの先頭ずれがあるためUTCと同一視しない。

実測した水中・防水ケース装着状態の内部校正値があれば、
`--fx ... --fy ... --cx ... --cy ...` を指定してカメラ相対の右正方位角・上正仰角を
radで計算できる。現時点では校正値がなく、角度列は空欄。
DJIの公称155°はレンズ全体の画角であり、撮影設定のStandard (Dewarp)と
防水ケース越しの水中画角を定めないため、その値を校正値として代用しない。
公称値とStandard (Dewarp) の区別は
[DJI仕様](https://www.dji.com/osmo-action-6/specs)・
[DJI FAQ](https://www.dji.com/osmo-action-6/faq)を参照。

### EKFによる追跡

確認済みキーフレームを起点に、位置・速度・ライト白色領域の対数面積を状態とした
拡張カルマンフィルタで0.5秒間隔の候補を追う。

```bash
python scripts/export_reviewed_lights.py
python -m aquabeacon_analysis track-lights --experiment 20260922_cmain_023
python scripts/render_light_track_review.py
python scripts/evaluate_light_tracking.py
```

`tracked_light.csv` の `anchor` は目視確認点、`observed` は画像候補で更新した点、
`reacquired` は強い発光候補から再初期化した点、`predicted` は短い観測欠損での
推定点、`untracked` は位置を確定できない点。
反射候補は予測位置からのMahalanobis距離、前後の確認点を結ぶ探索範囲、
水色背景・筐体との近さで除外する。`position_sigma_px` はフィルタのモデル上の
標準偏差であり、Ground Truthに対する誤差ではない。角度は校正値がなければ空欄のまま。
確認画像は `tracking_review.jpg`、独立目視点との比較は `tracking_validation.csv` に保存する。

校正までの試算は `python scripts/estimate_provisional_light_angles.py` で出力する。
`calibration/parent_camera_provisional.json` に記録した**仮の水平画角90°**から、
1280×720画像の `fx=fy=640 px, cx=640 px, cy=360 px` を導く。
各実験の `provisional_light_angles.csv` に右正方位角・上正仰角をradとdegで保存し、
`provisional_angle_manifest.json` に仮定、追跡CSV、config、Git SHAの追跡情報を記録する。
`angle_status=hypothetical_uncalibrated` は実測角度ではない。水中の防水ケースによる
屈折・歪みを補正しておらず、真の方向とのずれは評価できない。
`predicted` と `untracked` の角度は空欄にする。実測校正後は元の追跡座標から再計算する。

2026-09-24撮影の水中チェッカーボード動画から1920×1080用の内部校正候補を
`calibration/parent_camera_underwater_20260924_provisional.json` に記録した。
40 mmマス間隔と親機・子機で共通のカメラ・設定はユーザー確認済み。
対応する`.LRF`の1280×720画像も直接校正した。過去のproxy動画は手元にないため、
上記の角度計算にはまだ未適用。
詳細は `docs/reports/parent_camera_underwater_calibration_20260924.md` を参照。

親機の融合姿勢を使った重力基準の角度は
`python scripts/estimate_attitude_light_angles.py` で算出する。切り出し動画の先頭を
元動画と画素照合し、IMUの `window_time_s` に合わせてQuaternionを補間する。
`attitude_corrected_light_angles.csv` は、画角90°に加え、
`calibration/parent_camera_imu_provisional.json` の**仮の光学軸→IMU軸回転**を用いる。
`local_azimuth_*` は最初の親機姿勢の光軸水平投影を0°とした相対方位角、
`gravity_elevation_*` は重力基準の仰角で、磁北やENU方位ではない。
IMU姿勢がない区間は `no_parent_attitude`、ライト角度がない区間は `no_light_angle` とし、
値を外挿しない。画角と取付回転が未校正のため角度の真値誤差は評価できない。
`relative_position/parent_child_position.csv` はこの角度CSVを入力として再計算する。

`python scripts/plot_light_angles.py` で実験ごとの
`results/<experiment_id>/light_detection/angle_before_after.png` を生成する。
左列にカメラ相対の仮角度、右列に親機姿勢を適用した局所角度を置き、
上段は方位角、下段は仰角とする。点のみを描き、欠損は補間しない。
右列の方位角は±180°で折り返し、左右の角度は基準座標系が異なるため、
2曲線の差を精度の改善量とは解釈しない。入力のSHA-256と描画条件は
`angle_plot_manifest.json` に記録する。

### ToFとライト角度による親子機位置の試算

```bash
python scripts/estimate_parent_child_positions.py
```

3実験それぞれの `results/<experiment_id>/relative_position/parent_child_position.csv` に、
①親機カメラ光学中心を固定原点とした子機ライトの3D相対座標と、②SONY GPSで求めた
親機アンテナのENU水平位置、およびそれへ相対ベクトルを足した仮の子機水平座標を保存する。
映像先頭のキーフレームずれは元動画との先頭フレーム照合で求め、ToFは補正済み有効値のうち
±0.75秒以内の最近傍を使う。無効なライト角度やToFは位置欠損として残す。

カメラ座標は右・下・前を正とする。固定親機の重力基準座標とGPSへ足した子機座標は、
仮の光学軸→IMU軸回転と、最初の光軸方位を水平0とした**条件付き試算**である。
カメラ内部校正、光学軸とIMUの取付回転、GPSアンテナと親機原点のレバーアーム、
光源と音響トランスデューサの位置差が未校正なので、現時点で子機の確定した絶対位置は出せない。
親機の鉛直位置も信頼できる値がないため、子機の絶対高度は出力しない。
`manifest.json` に仮定、入力ハッシュ、Git SHA、映像時刻補正を記録する。

### 子機IMUデッドレコニングとの比較

```bash
python scripts/compare_child_imu_dead_reckoning.py
```

`results/<experiment_id>/relative_position/child_imu_comparison.csv` に、最初に共通する
通常ToF観測時刻からのToF＋ライトの移動量と、C_MAIN本体IMUの未補正二重積分による
移動量を同時刻で保存する。IMUの初期位置・速度は0、加速度バイアス補正なし。
比較するのは**開始点からの移動量の大きさ**であり、両者の水平方位は未校正なので
ベクトル差を位置誤差とは呼ばない。短距離・単発大値のToFはフラグ付きで残す。
実験別の解釈は `docs/reports/child_imu_comparison_20260922.md` を参照。

### ToF・ライト・子機IMU・圧力の条件付き融合

```bash
python scripts/fuse_relative_position.py
```

各実験の `results/<experiment_id>/relative_position/fused_position.csv` に、
ToF＋ライト角度の水平観測と入水基準の圧力深度を子機IMU加速度で時間方向に平滑化し、
鉛直位置は圧力を優先した相対位置を保存する。親機カメラは水面下約3 cm、
子機ライトと子機カメラの距離は約20 cmとして、いずれも概数・未校正と記録する。
これらの仮定と採用閾値は `calibration/relative_fusion_provisional.json` に保存する。
`data/interim` のC_MAIN圧力上昇から入水開始を検出し、入水前中央値を水面圧として
`pressure_depth_immersion.csv` をm単位で保存する。既存のToF比較CSVは1013.25 mbar基準の
まま保つ。水面基準を直してもToF・ライト位置との系統差が大きい。圧力を鉛直主観測とし、
ToFとライト方位の水平成分を別に使うため、不整合点の3Dベクトル長はToF斜距離と
一致しない。各行に幾何不整合フラグを残す。水平IMU方位の一致度が不足した実験では
水平加速度を採用しない。圧力または光学観測の欠測時刻には位置を出さない。
3D表示の観測点 `pressure_referenced_observation_*` は、ToF＋ライトの水平座標と
圧力深度の鉛直座標を親機カメラ原点にそろえたもの。仮のライト仰角から直接得た
`optical_z_up_m` は診断値として別に残し、同じ3D観測点としては表示しない。
詳細は `docs/reports/fused_relative_position_20260922.md` を参照。
既存の3D表示の軌跡データは `python scripts/refresh_fused_visualization.py <表示HTMLのパス>`
で融合CSVから更新できる。

## データ管理の原則
- `data/raw/` は実験生データ。Git管理外・上書き禁止
- `data/interim/` は同期済みCSVや抽出フレーム等。Git管理外
- `data/processed/` は点群・メッシュ等。Git管理外
- `data/sample/` の小型データのみGit管理可
- 動画、巨大点群、COLMAP生成物はGitに入れない
- 各解析結果には experiment ID、Git commit SHA、config、calibration versionを記録する

## フォルダ構成

```text
aquabeacon-analysis/
├── README.md
├── AGENTS.md
├── .gitignore
├── docs/
│   ├── README.md
│   └── spec.md
├── config/
├── calibration/
├── experiments/
├── src/aquabeacon_analysis/
├── scripts/
├── tests/
├── data/
│   ├── sample/
│   ├── raw/
│   ├── interim/
│   └── processed/
├── results/
└── blender/
```

## 実験ID

`YYYYMMDD_location_NNN` を使用する。

例: `20260921_pool_001`

## 開発者・AIエージェント向け
- 作業規約・検証ループ: [AGENTS.md](AGENTS.md)
- 仕様・受け入れ条件: [docs/spec.md](docs/spec.md)
- docs索引: [docs/README.md](docs/README.md)

## ライセンス
未定

## 開発とライセンス

[開発ガイド](DEVELOPMENT.md)にセットアップ・入口・確認方法をまとめています。自作部分は [Apache License 2.0](LICENSE)。[第三者素材・依存の扱い](THIRD_PARTY.md)も参照してください。

子機の追加軸校正の監査：`python scripts/audit_child_axis_calibration.py --experiment 20261029_child_axis_003 --firmware-root ../firmware-experiments`。結果は [校正監査レポート](results/20261029_child_axis_003/axis_calibration/report.md)。追加ログから軸変換を算出し、本番BNO機体姿勢を別出力した。カメラ全区間の校正は未採用。カメラ／水中ライト位置交換は実機構成として登録済み。

軸変換算出・本番適用：`python scripts/calibrate_child_axes.py --experiment 20261029_child_axis_003 --firmware-root ../firmware-experiments --target-experiment 20261028_ngt_000`。結果は `results/20261028_ngt_000/child_body_attitude/`。

校正区間の動画・ログ切り出しと磁気方位監査：`python scripts/clip_child_calibration.py --experiment 20261029_child_axis_003 --firmware-root ../firmware-experiments`。今回のマーカー内だけカメラ時計・IMU回転を採用し、磁気方位は独立姿勢検証不合格。詳しくは `results/20261029_child_axis_003/calibration_clip/report.md`。

長田高校プールの子機姿勢を校正済みFRDで保存：
`python scripts/estimate_ngt_child_attitude.py --experiment 20261028_ngt_000 --calibration calibration/20261029_child_axis_003_imu_frd.json`
`--experiment`を繰り返して001〜004も指定可能。CSVは`data/processed/<id>/child_attitude/`、レポート・編集可能な図は`results/<id>/child_attitude/`。重力基準のroll/pitchと任意基準yawを出力し、検証未達の北基準方位はabsolute_heading_valid=falseとして扱う。

親機の追加軸校正011：`python scripts/calibrate_parent_axes.py --experiment 20261029_parent_axis_011 --firmware-root ../firmware-experiments`。IMU軸と校正区間内カメラIMU回転は検証済み、磁気軸・絶対方位は不採用。詳細は `results/20261029_parent_axis_011/axis_calibration/report.md`。GPS同期UTCはなく、共通の端末時刻参照による単調時計対応を使う。子機の時計・取付け情報を親機へ流用しない。

磁北基準の暫定絶対姿勢を使う場合：
`python scripts/estimate_ngt_child_attitude.py --experiment 20261028_ngt_000 --calibration calibration/20261029_child_axis_003_imu_frd.json --magnetic-heading`
出力は`data/processed/<id>/child_magnetic_attitude/attitude.csv`。`absolute_attitude_available=True`で`heading_magnetic_deg`または`magnetic_ned_q*`を使用。精度未検証、真北偏角未適用。区間001〜004も--experimentの繰返しで選択できる。

親機の磁北方位・姿勢（独立LIS3MDLをCADと実機方向で軸合わせ）：
`python scripts/estimate_ngt_parent_attitude.py --experiment 20261028_ngt_000 --calibration calibration/20261028_parent_lis3mdl_cad_axes.json`
`--experiment`の繰返しで001〜004を選択可能。出力は`data/processed/<id>/parent_magnetic_attitude/attitude.csv`、図とレポートは`results/<id>/parent_magnetic_attitude/`。absolute_attitude_availableで四元数を選び、tilt_sourceでカメラと瞬時加速度を区別する。前方はGNSS→Spresense、精度未検証・偏角未適用。
