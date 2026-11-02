# AGENTS.md

> このプロジェクトでコードを書くすべてのAIエージェント共通の作業規約。作業前に読むこと。

## ドキュメントの役割
- `README.md`: 人間向け概要・使い方・データ管理方針
- `AGENTS.md`: 作業規約・検証ループ・ドキュメント同期規約
- `docs/spec.md`: 実装仕様・MVP・受け入れ条件
- `docs/README.md`: docs索引

## Tech Stack
| レイヤー | 技術 |
|---|---|
| 主解析 | Python 3.11+ |
| SfM / Photogrammetry | COLMAP |
| VSLAM / VI-SLAM | 外部実装と連携 |
| 可視化 | Matplotlib等 + Blender |
| 実行環境 | ローカルPC |

## 重要原則
1. `data/raw/` は絶対に書き換えない。
2. 生動画・生CSV・抽出フレーム・点群・メッシュ・COLMAP workspaceをGitへ追加しない。
3. 解析結果は必ずexperiment ID単位で保存する。
4. 各解析結果から使用したconfig、calibration、Git commit SHAを追跡可能にする。
5. Ground Truthがない指標を「絶対精度」「真値誤差」と表現しない。
6. Blenderは発表用可視化に限定し、正式な評価計算には使用しない。

## 複数実験の分離
- 新しい取得データは、原本の取り込み担当者が `data/raw/<experiment_id>/` 内を機器別に配置する。
  解析エージェントは原本を移動・改名・編集せず、取り込み済みのデータを参照する。
- 解析前に `experiments/<experiment_id>/experiment.json` を登録する。中間データは
  `data/interim/<experiment_id>/`、処理済みデータは `data/processed/<experiment_id>/`、
  結果は `results/<experiment_id>/<analysis_name>/` に置く。
- 主CLIは `--experiment <id>` を優先する。対象省略の一括解析は禁止し、必要な全件処理だけ
  `--all-experiments` で明示する。別日・別場所で重複する数値suffixから実験を推測しない。
- 新規実験へ9月22日用の設定・校正・レビュー点・固定セッション一覧を流用しない。
  台帳の校正参照は宣言であり、自動適用ではない。適用条件を確認し、実使用値を解析manifestへ記録する。
- 既存の平置きrawと `data/interim/orbslam3*/` は互換配置。移動前にmanifestとスクリプトの参照を調べる。
  新規のSLAM等の派生物は実験ID配下に方式・run名を付け、既存出力へ混ぜない。
- 配置・台帳形式は `data/README.md` と `experiments/README.md` を参照。

## Commands
別PC用の校正根拠検証（raw・既存結果・firmware checkout不要）：

```powershell
python scripts/verify_calibration_bundle.py --refit
```

`calibration/reference_data/`は公開用に選んだ校正出力の抜粋のみを保存する。
追加時にはコピー元・SHA-256・単位・選択条件をmanifestとREADMEに記録する。
校正JSONを変更した場合は対応する根拠とmanifestを明示的に更新し、検証する。

実装開始時に以下の実コマンドを確定し、この節を更新すること。

研究発表用レポートと実映像の照合：

```powershell
python scripts/prepare_ngt_research_report.py --experiment 20261028_ngt_000 --phase prepare
python scripts/prepare_ngt_research_report.py --experiment 20261028_ngt_000 --phase clips
python scripts/write_ngt_research_report.py --experiment 20261028_ngt_000
python scripts/verify_ngt_research_report.py --experiment 20261028_ngt_000
```

2026-10-30の映像監査で、親機LRFは自動回転後720×1280なのに光点へ1280×720用Kを適用していたことを確認。
`results/20261028_ngt_000/research_report/orientation_audit.json`を参照。
旧成立フラグは可用性の記録として保持するが、旧位置ばらつき・角度・子機軌跡・位置prior依存地図の尺度を確定精度として使わない。
回転を戻した画素と光学/機体extrinsicを整合させ、座標依存の解析を再計算する必要がある。感度差を真値誤差と呼ばない。

発表用可視化の再生成（単眼地図の尺度・欠測と実CADの未校正取付けを保持）：

2026-10-30改訂版（観測履歴の再生、実測姿勢、2D研究グラフの一覧）：

```powershell
python scripts/prepare_ngt_replay_v2.py --experiment 20261028_ngt_004
python scripts/render_ngt_replay_v2.py --experiment 20261028_ngt_004
python scripts/render_ngt_replay_v2.py --experiment 20261028_ngt_004 --clean-existing
python scripts/verify_ngt_presentation_v2.py --experiment 20261028_ngt_004
python scripts/style_ngt_gnss_presentation.py --experiment 20261028_ngt_001
python scripts/open_ngt_research_figures.py --experiment 20261028_ngt_001 --experiment 20261028_ngt_004
python scripts/open_ngt_research_figures.py --experiment 20261028_ngt_000 --catalog-only
```

Blenderは旧ラッパーも含め同時に1つだけ実行する。新しい壁地図は既存の位置拘束型COLMAPの
最終点を観測順に再表示するもので、子機のみVSLAMとは別出力。オンライン点生成を意味しない。

```powershell
python scripts/export_ngt_textured_map.py --experiment 20261028_ngt_004
python scripts/verify_ngt_textured_map.py --experiment 20261028_ngt_004
python scripts/reconstruct_ngt_dense_patch.py --experiment 20261028_ngt_004
node scripts/verify_ngt_textured_map.mjs
python scripts/render_ngt_presentation.py --experiment 20261028_ngt_004
python scripts/render_ngt_cad_positions.py --experiment 20261028_ngt_004
python scripts/plot_ngt_gnss_overhead.py --experiment 20261028_ngt_001 --experiment 20261028_ngt_002 --experiment 20261028_ngt_003 --experiment 20261028_ngt_004
python scripts/plot_ngt_aerial_hires.py --experiment 20261028_ngt_000
python scripts/package_ngt_presentation.py --experiment 20261028_ngt_000
```

上記2つのBlenderラッパーは同時に走らせない。写真面の出現は撮影順であり点の生成時刻ではない。

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev,media,analysis,vision]"
python -m pytest
python -m ruff check .
python scripts/prepare_ngt_cameras.py --experiment 20261028_ngt_000
python scripts/synchronize_ngt.py --experiment 20261028_ngt_000
python scripts/split_ngt_experiments.py --experiment 20261028_ngt_000
python scripts/analyze_ngt_gnss.py --experiment 20261028_ngt_000 --camera-start-utc 2026-10-28T05:03:01.343757+00:00 --camera-time-scale 0.999975135251818
python scripts/track_ngt_light.py --experiment 20261028_ngt_000
python scripts/fuse_ngt_sensors.py --experiment 20261028_ngt_000
python scripts/check_ngt_optical_axes.py --experiment 20261028_ngt_000
python scripts/evaluate_ngt_position.py --experiment 20261028_ngt_002 --experiment 20261028_ngt_003 --experiment 20261028_ngt_004
python scripts/report_ngt_position_evaluation.py --experiment 20261028_ngt_000
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
python scripts/plot_ngt_aerial_tracks.py --experiment 20261028_ngt_000
python scripts/select_ngt_gnss.py --experiment 20261028_ngt_000
python scripts/ppk_ngt_geonet.py --experiment 20261028_ngt_000
python scripts/compare_ngt_ppk_bases.py --experiment 20261028_ngt_000
python scripts/report_ngt_ppk_comparison.py --experiment 20261028_ngt_000
python scripts/verify_ngt_artifacts.py --experiment 20261028_ngt_000
python -X utf8 scripts/prepare_cad_rig.py --rig-id cad_20261028 --verify
python -m aquabeacon_analysis.editable_plots results/<experiment_id>/<analysis_name>/<figure>.figure.pickle
python -m aquabeacon_analysis --help
python -m aquabeacon_analysis list-experiments
python -m aquabeacon_analysis show-experiment 20260922_cmain_023
# 新規ID・ログ番号は実験に合わせて置き換える
python scripts/run_ngt_child_vslam.py --experiment 20261028_ngt_001 --experiment 20261028_ngt_002 --experiment 20261028_ngt_003 --experiment 20261028_ngt_004
python scripts/evaluate_ngt_child_vslam.py --experiment 20261028_ngt_001 --experiment 20261028_ngt_002 --experiment 20261028_ngt_003 --experiment 20261028_ngt_004
python scripts/verify_ngt_child_vslam.py --experiment 20261028_ngt_001 --experiment 20261028_ngt_002 --experiment 20261028_ngt_003 --experiment 20261028_ngt_004
python scripts/prepare_ngt_child_fusion.py --experiment 20261028_ngt_004
python scripts/run_ngt_child_vislam.py --build
python scripts/run_ngt_child_vislam.py --experiment 20261028_ngt_004
python scripts/fuse_ngt_child_dead_reckoning.py --experiment 20261028_ngt_004
python scripts/visualize_ngt_child_maps.py --experiment 20261028_ngt_004
python scripts/report_ngt_child_fusion.py --experiment 20261028_ngt_004
python scripts/prepare_ngt_child_fusion.py --experiment 20261028_ngt_004 --camera-imu
python scripts/run_ngt_child_vislam.py --experiment 20261028_ngt_004 --camera-imu
python scripts/verify_ngt_child_fusion.py --experiment 20261028_ngt_004 --tests
python -m aquabeacon_analysis init-experiment 20261027_pool_001 --c-main-suffix 22
python -m pip install -e ".[dev,cad]"
python scripts/prepare_cad_rig.py --rig-id cad_20261027
python scripts/prepare_cad_rig.py --rig-id cad_20261027 --verify
& 'C:\Program Files\Blender Foundation\Blender 5.2\blender.exe' -b -t 4 --python blender/create_cad_rig_scene.py -- --rig-id cad_20261027
python -m aquabeacon_analysis preprocess-tof --experiment 20260922_cmain_023
python scripts/extract_p_main_tof.py --experiment 20260922_pmain_022
python -m aquabeacon_analysis detect-lights --experiment 20260922_cmain_023
python -m aquabeacon_analysis track-lights --experiment 20260922_cmain_023
python scripts/estimate_provisional_light_angles.py
python scripts/estimate_attitude_light_angles.py
python scripts/plot_light_angles.py
python scripts/estimate_parent_child_positions.py
python scripts/compare_child_imu_dead_reckoning.py
python scripts/fuse_relative_position.py
python scripts/estimate_calibrated_positions.py
python scripts/diagnose_child_magnetometer.py
& 'C:\Program Files\Blender Foundation\Blender 5.2\blender.exe' -b --python blender/create_pose_scene.py -- (Get-Location).Path 20260922_cmain_023
& 'C:\Program Files\Blender Foundation\Blender 5.2\blender.exe' -b --python blender/create_body_geometry_review.py -- 20260922_cmain_023
python scripts/refresh_fused_visualization.py <表示HTMLのパス>
python -m aquabeacon_analysis localize-imu --session 20260922_cmain_022 --session 20260922_cmain_023 --session 20260922_cmain_024
python scripts/prepare_orbslam3.py --help
python scripts/diagnose_vislam_features.py --help
python scripts/prepare_orbslam3_fused_imu.py --help
python scripts/prepare_orbslam3_clahe_trial.py --help
python scripts/prepare_child_calibrated_orbslam3_settings.py 023
python scripts/verify_child_120fps_alignment.py 20260922_cmain_023
python scripts/prepare_child_calibrated_orbslam3_settings.py 023 --source-120fps
python scripts/prepare_child_orbslam3_120fps.py 20260922_cmain_023
./scripts/run_orbslam3.ps1 -ExperimentId 20260922_cmain_023 -DatasetRoot data/interim/orbslam3_120fps -SettingsPath calibration/child_orbslam3_underwater_trial_120fps_023.yaml -SensorMode Monocular -RequireMapExport -RunName calibrated_mono_120fps_full
python scripts/summarize_child_vslam.py 20260922_cmain_023 calibrated_mono_120fps_full --dataset-root data/interim/orbslam3_120fps
python scripts/render_child_vslam_120fps.py 20260922_cmain_023
python scripts/prepare_child_orbslam3_120fps.py 20260922_cmain_023 --color-jpeg
./scripts/run_orbslam3.ps1 -ExperimentId 20260922_cmain_023 -DatasetRoot data/interim/orbslam3_color_120fps -SettingsPath calibration/child_orbslam3_underwater_trial_120fps_023.yaml -SensorMode Monocular -RequireMapExport -RunName calibrated_mono_color_120fps_full
python scripts/summarize_child_vslam.py 20260922_cmain_023 calibrated_mono_color_120fps_full --dataset-root data/interim/orbslam3_color_120fps
python scripts/render_child_vslam_120fps.py 20260922_cmain_023 --color
python -m pip install -e ".[mapping]"
python scripts/export_child_textured_map.py 20260922_cmain_023
python scripts/summarize_child_vslam.py 20260922_cmain_023 calibrated_mono_023_full
python scripts/plot_child_vslam_coverage.py
python scripts/render_child_vslam_artifacts.py 20260922_cmain_023 --run-name calibrated_mono_map_full
python scripts/verify_orbslam3_alignment.py --help
python scripts/apply_orbslam3_pressure_depth.py --help
./scripts/run_orbslam3.ps1 -Build
```

## Verification Loop
機能を実装したら完了宣言前に必ず以下を行う。

1. 実装する
2. 自動テストを実行する
3. Lintを実行する
4. `docs/spec.md` の該当受け入れ条件を1つずつ確認する
5. 手動確認が必要な図・SLAM・COLMAP・Blender出力は、確認方法と結果を記録する
6. 失敗があれば修正して2へ戻る
7. 全条件を満たした場合のみ完了とする

未検証の状態で完了扱いにしない。

解析レポートとそのグラフ・姿勢表示は、該当する
`results/<experiment_id>/<analysis_name>/` にまとめる。横断サマリーを`docs/reports/`
へ置く場合も、各実験の結果フォルダに出力へのリンクを持つ`report.md`を保存する。

## ドキュメント同期規約
- 入出力形式、座標系、時刻同期、評価指標、フォルダ構成、Git管理方針を変更した場合は `docs/spec.md` を同じ変更で更新する。
- セットアップ・代表コマンドを変更した場合は `README.md` と本ファイルを更新する。
- docsを増減した場合は `docs/README.md` を更新する。
- 実装と仕様が食い違う場合、コード側だけを正とせず、仕様変更として明示的に扱う。

## コーディング規約

### 研究用グラフと発表用ビジュアル（2026-10-30改訂）

- 研究用は基本的に2次元、必要時は3次元も許可する。細かなブラウザ編集を必須にするのは
  2次元の定量グラフのみ。編集機能の都合で元図の構図・描画品質を落とさない。
- 発表用の図・動画は伝わりやすさ、配色、構図、文字、光、動き、画質を優先する。
  3Dモデル・姿勢アニメーション・マッピング動画に2Dエディタを要求しない。
  完成した画像・動画と、編集可能なBlender等のプロジェクト・再生成コードを渡す。
- 本人が後でPowerPointへ貼るため、現段階ではPPTXを作らず、単体で貼れるPNG/SVG/PDFを渡す。
  図をスライド風の帯・大きな説明欄で囲まない。軸・凡例・単位・必要な注記を中心にする。
  動画とは別に、貼り付け用の簡潔な3D静止画も保存する。

- 新規・再生成する2次元グラフはPNGのみで納品しない。Matplotlibは
  `aquabeacon_analysis.editable_plots.save_editable_figure(fig, path, ...)` を使い、
  同じ場所にSVG、`.figure.pickle`、`.plot.json`を保存する。生成コード、入力CSVと
  解析manifestも保持する。詳細と受け入れ条件は `docs/spec.md` の「編集可能な研究グラフ」。
- 本人がブラウザで軸ラベル・凡例・フォント・文字サイズ・軸範囲・グリッド、
  系列の色・線種・太さ・点の形とサイズ・縁・透明度を調整できること。
  縮尺・縦横比・主副目盛り・枠・注釈・パネル配置も調整できること。
  MATLAB Figureに近い自由度を目標とするが、特殊Artist等の未対応範囲を明記し、
  生成コードと描画オブジェクトによる編集経路を保持する。
  編集設定は `.style.json`、編集後の出力は `.edited.png/.svg/.pdf` に保存する。
- 数値項目はスライダーと直接入力を併設する。図の幅・高さはドラッグでも変更でき、
  `xlim`・`ylim` は専用の表示範囲欄で編集できること。寸法・範囲も保存・再読込する。
- 目盛り間隔等、標準UIにない見た目も生成コードから再現可能に保つ。
  2次元のMatplotlib以外の形式でも同等のブラウザ編集・保存経路を用意する。
  3次元図は必要に応じて既存の編集機能を使うが、2次元と同じ編集UIは必須にしない。
- 位置／姿勢映像は実CADと有効な推定値を使用し、欠測時に中立姿勢や架空の軌跡を補わない。
  CADと機体軸の未校正対応、磁北と真北、測定中心を区別する。
  マッピングの進行は点の観測履歴に基づき、後処理の再生とオンライン生成時刻を区別する。
  機体と位置／姿勢が分かる版・マップだけの版を同じ時刻範囲で用意する。
- 見た目の編集で計測値、単位、欠損、除外条件、解析結果を暗黙に変更しない。
  未依頼の既存図の一括再生成は行わない。pickleはローカル生成物に限り開く。

### 解析とCAD

- 2026-10-28学校プール実験は `20261028_ngt_000` と区間001〜004を明示選択する。
  最新CAD参照は `cad_20261028`。子機は `child_horizontal_plate_frame` /
  `child_interior`（BNO085・カメラ・ライト・TD04A）、親機は
  `parent_exterior` / `parent_hand_service`（Spresense IMU・磁気・カメラ・TD04A・GNSS）。
  `cad_20261027` は原典更新を検出したため今回へ適用しない。
  669部品の新スナップショットも設計参照であり、測定中心・機体軸の校正ではない。
  前日の子機4姿勢軸合わせとBNO085本体校正がともに成功したというユーザー確認を保持するが、保存変換・区間マーカーを
  特定できるまでは重力方向を前方軸・yawへ昇格しない。詳細は今回の解析レポート。

- 今後の実験用CADは `calibration/rigs/` の明示選択したスナップショットを使う。
  `cad_20261027` を2026-09-22等の既存実験へ自動適用しない。
  生成GLBは `data/interim/cad/<rig_id>/`、実験前のBlender確認は
  `blender/output/<rig_id>/`。CAD参照点を測定中心・校正済みextrinsicへ昇格しない。
- 解析処理は可能な限り純粋関数として分離し、I/Oと数値処理を混在させない。
- 時刻は内部で共通表現へ変換し、各センサ固有時刻を直接混在させない。
- 座標変換は明示的なframe名とSE(3)変換で扱う。
- 単位を変数名またはデータ仕様で明示する。時間は秒、距離はm、角度は原則radを内部単位とする。
- 生データの暗黙補正を禁止する。補正値はconfig/calibrationに記録する。
- 外れ値除去や補間を行った場合は、元データと処理内容を追跡可能にする。

## Do NOT
- `data/raw/` に書き込まない
- `.env` や秘密情報をコミットしない
- 動画や巨大バイナリを誤ってGit管理しない
- Ground Truthがない比較をaccuracyとして断定しない
- VSLAM失敗区間を黙って除外しない
- 仕様変更後にdocsを古いまま残さない

### 共通水中カメラ校正（2026-10-30ユーザー指定）

沖縄の海の実験で取得した水中内部校正を、今後の親機・子機の既定にする。
参照は `calibration/camera_defaults.json`、数値原典は
`calibration/parent_camera_underwater_20260924_provisional.json`。
原典のファイル名にparentを含むが、両カメラ共通としてユーザーが適用を指定した。
画像の解像度・crop・Dewarp・EIS・防水ケース・水中条件を明示し、MP4とLRFで対応する
内部行列を使う。全画面縮小では内部行列を拡縮し、歪み係数は保持する。
校正の暫定性は維持し、IMU取付回転・時刻同期・測定中心へ意味を広げない。
今回の長田高校VSLAMはこの共通校正を使うが、長田高校の親機観測は使用禁止。

### 子機実機配置の追加確認（2026-10-29）

カメラと水中ライトの固定位置はcad_20261028と逆で本番も同じ（ユーザー確認）。`calibration/child_mount_20261028_actual.json` を参照。位置交換から回転や測定中心を推測しない。新規軸校正003は別台帳 `20261029_child_axis_003`、29567追加後、4姿勢のIMU連番・時刻が連続し、方向残差最大2.612°を確認。校正JSON `calibration/20261029_child_axis_003_imu_frd.json` の明示参照で機体FRD姿勢に変換する。カメラ後半不一致のためカメラ校正は未採用。

監査コマンド：`python scripts/audit_child_axis_calibration.py --experiment 20261029_child_axis_003 --firmware-root ../firmware-experiments`

軸変換算出・本番適用：`python scripts/calibrate_child_axes.py --experiment 20261029_child_axis_003 --firmware-root ../firmware-experiments --target-experiment 20261028_ngt_000`。結果は `results/20261028_ngt_000/child_body_attitude/`。

校正区間の動画・ログ切り出しと磁気方位監査：`python scripts/clip_child_calibration.py --experiment 20261029_child_axis_003 --firmware-root ../firmware-experiments`。今回のマーカー内だけカメラ時計・IMU回転を採用し、磁気方位は独立姿勢検証不合格。詳しくは `results/20261029_child_axis_003/calibration_clip/report.md`。

子機姿勢の後続解析用出力：`python scripts/estimate_ngt_child_attitude.py --experiment 20261028_ngt_000 --calibration calibration/20261029_child_axis_003_imu_frd.json`。001〜004は--experimentを繰り返して明示。`data/processed/<id>/child_attitude/attitude.csv`を使用し、attitude_usable・clock_validを確認。local_down四元数のyawを北基準と解釈しない。詳細はdocs/spec.md。

親機の追加軸校正011：`python scripts/calibrate_parent_axes.py --experiment 20261029_parent_axis_011 --firmware-root ../firmware-experiments`。IMU軸と校正区間内カメラIMU回転は検証済み、磁気軸・絶対方位は不採用。詳細は `results/20261029_parent_axis_011/axis_calibration/report.md`。GPS同期UTCはなく、共通の端末時刻参照による単調時計対応を使う。子機の時計・取付け情報を親機へ流用しない。

2026-10-30ユーザー指定：精度の合否を条件にせず磁北基準の絶対方位を推定する。`estimate_ngt_child_attitude.py`の同じコマンドに`--magnetic-heading`を追加し、`child_magnetic_attitude/`へ保存。後続解析はabsolute_attitude_availableでmagnetic_ned_q*を選択可能。heading_accuracy_validatedと既存absolute_heading_validは精度検証フラグであり、今回の利用禁止フラグではない。真北偏角は未適用。

親機絶対方位（2026-10-30）：前方はGNSS受信基板→Spresense、LIS3MDLのXはGNSS側（ユーザー確認）。CAD+X前/-Y右/-Z下、基板上面上向きの暫定取付設定`calibration/20261028_parent_lis3mdl_cad_axes.json`を明示使用。元CADの基板面内向きと180°違うことを記録。コマンド：`python scripts/estimate_ngt_parent_attitude.py --experiment 20261028_ngt_000 --calibration calibration/20261028_parent_lis3mdl_cad_axes.json`。後続解析はparent_magnetic_attitude/attitude.csvのabsolute_attitude_availableでmagnetic_ned_q*を選択。磁北・精度未検証、子機と親機の座標規約を混用しない。


Nanyo海上実験の姿勢時刻同期：

```powershell
python scripts/prepare_nanyo_sync.py --experiment 20261031_nanyo_000 --firmware-root ../firmware-experiments
python scripts/synchronize_nanyo.py --experiment 20261031_nanyo_000
python scripts/report_nanyo_sync.py --experiment 20261031_nanyo_000
```

仕様はdocs/spec.md「Nanyo海上実験の姿勢時刻同期」、結果はresults/20261031_nanyo_000/synchronization/report.md。

Nanyo親機動画連結と水中・音響候補の区間切出し：

```powershell
python scripts/prepare_nanyo_segments.py --experiment 20261031_nanyo_000 --firmware-root ../firmware-experiments
python scripts/verify_nanyo_segments.py --experiment 20261031_nanyo_000
```

結果：results/20261031_nanyo_000/segmentation/report.md。区間001/002の動画・ログはdata/processed/<id>/。


Nanyo評価（RTK未使用）：
```powershell
python scripts/reprocess_nanyo_ppk.py --experiment 20261031_nanyo_000
python scripts/analyze_nanyo.py --experiment 20261031_nanyo_000
python scripts/locate_nanyo.py --experiment 20261031_nanyo_001
python scripts/locate_nanyo.py --experiment 20261031_nanyo_002
python scripts/map_nanyo.py --experiment 20261031_nanyo_001
python scripts/map_nanyo.py --experiment 20261031_nanyo_002
python scripts/group_nanyo_acoustic.py --experiment 20261031_nanyo_001 --experiment 20261031_nanyo_002
python scripts/plot_nanyo_aerial.py --experiment 20261031_nanyo_000 --experiment 20261031_nanyo_001 --experiment 20261031_nanyo_002
python scripts/report_nanyo.py --experiment 20261031_nanyo_000
```
結果の入口はresults/20261031_nanyo_000/analysis/report.md。依存：opencv-python-headless、pycolmap。


Nanyo子機の絶対位置推定・位置拘束マッピング（同一取付けのユーザー確認後）：

```powershell
python -m pip install -e ".[analysis,vision,media,nanyo]"
python scripts/prepare_nanyo_geometry.py --experiment 20261031_nanyo_000 --firmware-root <kind16対応firmwareのパス>
python scripts/recover_nanyo_frame_times.py --experiment 20261031_nanyo_001
python scripts/recover_nanyo_frame_times.py --experiment 20261031_nanyo_002
python scripts/audit_nanyo_optical_axes.py --experiment 20261031_nanyo_000
python scripts/estimate_nanyo_absolute.py --experiment 20261031_nanyo_001 --experiment 20261031_nanyo_002
python scripts/bridge_nanyo_maps.py --experiment 20261031_nanyo_001
python scripts/bridge_nanyo_maps.py --experiment 20261031_nanyo_002
python scripts/extend_nanyo_maps.py --experiment 20261031_nanyo_001
python scripts/anchor_nanyo_maps.py --experiment 20261031_nanyo_001 --experiment 20261031_nanyo_002 --use-bridge
python scripts/verify_nanyo_absolute.py --experiment 20261031_nanyo_000
python scripts/report_nanyo_absolute.py --experiment 20261031_nanyo_000
```

入口：results/20261031_nanyo_000/absolute_position/report.md。オフライン3D表示は同じフォルダのviewer.html。
追加依存：pygeomag、plotly。前回の90°画像回転を適用しない。kind16対応デコーダは既存firmware-experimentsより新しいfirmware checkoutを明示指定する。
精度未検証の姿勢・位置を推定に利用できるが、欠測・品質値・CAD基準点の暫定性を保持する。

Nanyoセンサー航法・写真地図・親子映像（SfM位置を航法に使用しない）：

```powershell
python -m pip install -e ".[analysis,vision,media,nanyo,replay,dev]"
python scripts/fuse_nanyo_navigation.py --experiment 20261031_nanyo_000
python scripts/prepare_nanyo_replay_media.py --experiment 20261031_nanyo_000
python scripts/export_nanyo_replay.py --experiment 20261031_nanyo_000
python scripts/verify_nanyo_replay.py --experiment 20261031_nanyo_000
python scripts/report_nanyo_navigation.py --experiment 20261031_nanyo_000
```

結果はnavigation_fusion/navigation_mapping/navigation_replay。親機5秒・子機3秒を超える
水平観測欠測は有効位置として補完しない。SfMの撮影視点は写真投影にだけ使い、航法経路と混同しない。
写真面は既存点群の近傍三角形であり密な再構成ではない。ブラウザ検証は既存Edgeまたは
`--browser-executable`で指定したChromiumを使用する。原動画・初回推定を保持する。


Nanyo指定静止区間の相対位置評価：`python scripts/evaluate_nanyo_static.py --experiment 20261031_nanyo_002`。既定は2026-10-31 15:03:00〜15:04:00 JST。結果は`results/20261031_nanyo_002/static_evaluation/report.md`。平均周りRMSEであり真値誤差ではない。


区間①の初期静止候補の選定・評価：

```powershell
python scripts/select_nanyo_static.py --experiment 20261031_nanyo_001
python scripts/evaluate_nanyo_static.py --experiment 20261031_nanyo_001 --start 2026-10-31T14:41:10+09:00 --selection-note "初期6分の姿勢と親機位置による探索的選定。static_selection/report.md参照"
```

結果は`results/20261031_nanyo_001/static_selection/report.md`と`static_evaluation/report.md`。相対位置RMSEを選定基準に使わない。


Nanyo day2（2026-11-01）の同期・実験分割：

```powershell
python scripts/prepare_nanyo_sync.py --experiment 20261101_nanyo_000 --firmware-root ../firmware-experiments
python scripts/synchronize_nanyo.py --experiment 20261101_nanyo_000
python scripts/report_nanyo_sync.py --experiment 20261101_nanyo_000
python scripts/prepare_nanyo_day2_logs.py --experiment 20261101_nanyo_000 --firmware-root <kind16対応firmwareのパス>
python scripts/prepare_nanyo_day2_segments.py --experiment 20261101_nanyo_000
python scripts/repair_nanyo_day2_video_timing.py --experiment 20261101_nanyo_000
python scripts/verify_nanyo_day2_segments.py --experiment 20261101_nanyo_000
```

先に追加の親機health/range、子機BNO/multipathを同じinterimへCRC検証付きで抽出する。
子機kind16対応デコーダを明示指定する。今回の抽出来歴はinterimのdecode.jsonを参照。
設定はconfig/20261101_nanyo_000/。原本の前日ログは対象suffixとUTCで除外。
親4本・子3本を個別同期し、短い動画は30秒窓を明示設定。再起動前後の時計を共有しない。
開始は子機入水と親機測距開始の遅い方、終了は離水または測距停止。
結果：[day2分割レポート](results/20261101_nanyo_000/segmentation/report.md)。


Nanyoの別PC引き継ぎは `docs/handoff/nanyo_20261102.md` を参照。raw/interimなしの移行確認は `python scripts/verify_nanyo_handoff.py --experiment 20261101_nanyo_000 --verify-csv`。既存manifestの絶対パスは来歴として保持し、移行時に一括置換しない。

Nanyo day2の解析・追跡と2日分の静止比較：

```powershell
python scripts/analyze_nanyo_day2.py --experiment 20261101_nanyo_000 --phase all
python scripts/map_nanyo.py --experiment 20261101_nanyo_001
# 同じCLIを002〜005にも明示実行
python scripts/track_nanyo_stationary.py --experiment 20261101_nanyo_000
python scripts/review_nanyo_tracked_shots.py --experiment 20261101_nanyo_000
python scripts/analyze_nanyo_stationary.py --experiment 20261101_nanyo_000
python scripts/report_nanyo_day2.py --experiment 20261101_nanyo_000
python scripts/verify_nanyo_day2_analysis.py --experiment 20261101_nanyo_000
python -m pytest tests/test_beacon_halo.py tests/test_beacon_tracker.py tests/test_stationary_spectra.py
```

`--window`は追跡・静止解析の部分再実行に使用できる。最終比較表は全窓で再生成する。
追跡の対象は設定の6窓。原動画の観測時刻を使い、予測位置を観測へ混ぜない。
再初期化の目視点は対象同定用であり独立画素真値ではない。新規図は編集マスターも保存する。
位置スペクトルでも無効shotをNaNで残し、採用点間隔から欠測を補間しない。
受け入れ条件・方法はdocs/spec.mdのday2静止周波数評価節を参照。


Nanyo 2日分のGNSS選択とマッピング：

```powershell
python scripts/select_nanyo_mapping_gnss.py --experiment 20261031_nanyo_000 --experiment 20261101_nanyo_000
# 各trialのframe_timesが必要。day2は実際の29.97 fpsのPTSを復元する。
python scripts/recover_nanyo_frame_times.py --experiment 20261101_nanyo_001
# 同じコマンドをday2の002〜005にも実行。
python scripts/track_nanyo_mapping_lights.py --experiment 20261031_nanyo_001 --experiment 20261031_nanyo_002 --experiment 20261101_nanyo_001 --experiment 20261101_nanyo_002 --experiment 20261101_nanyo_003 --experiment 20261101_nanyo_004 --experiment 20261101_nanyo_005
python scripts/fuse_nanyo_navigation.py --experiment 20261031_nanyo_000 --mapping-selection
python scripts/fuse_nanyo_navigation.py --experiment 20261101_nanyo_000 --mapping-selection
python scripts/prepare_nanyo_replay_media.py --experiment 20261031_nanyo_000
python scripts/prepare_nanyo_replay_media.py --experiment 20261101_nanyo_000 --output-name mapping_replay
python scripts/check_nanyo_crossday_overlap.py --experiment 20261102_nanyo_combined_000
python scripts/export_nanyo_combined_mapping.py --experiment 20261102_nanyo_combined_000
python scripts/report_nanyo_mapping.py --experiment 20261102_nanyo_combined_000
python scripts/verify_nanyo_combined_mapping.py --experiment 20261102_nanyo_combined_000
```

光源初期化ROIはconfig/<acquisition>/mapping_light_review.json。追加・変更前に原画像で対象を同定する。
元の静止窓の追跡は実UTCのまま再利用し、trial範囲外を取り込まない。`--reuse-static-only`は既存全区間追跡への静止結果・目視除外の再適用。
GNSS選択は同時刻の各窓平均周りの3D RMSEで、手動揺動は除外。絶対精度に読み替えない。
`--reuse-geometry`は入力が変わっていない表示再生成だけに用いる。
生データ・resultsは公開しない。既存のnavigation_replayは旧解析、新しいmapping_replayが本比較の成果物。
必要なら`python scripts/serve_nanyo_mapping.py`でローカル再生し、終了後サーバーを停止する。

Nanyo静止絶対位置へのFloat設定PPK追加比較：

```powershell
python scripts/compare_nanyo_static_gnss.py --experiment 20261101_nanyo_000
python scripts/verify_nanyo_static_gnss.py --experiment 20261101_nanyo_000
python scripts/report_nanyo_day2.py --experiment 20261101_nanyo_000 --skip-map-render
python scripts/report_nanyo_mapping.py --experiment 20261102_nanyo_combined_000
python -m pytest tests/test_stationary_gnss.py tests/test_static_evaluation.py
```

設定は`config/20261101_nanyo_000/stationary_gnss_comparison.json`。
旧静止相対位置にはGNSS共通並進が残らないため、GNSS方式差を相対RMSE改善と呼ばない。
採用済みの同じshotを使い、子機絶対位置で通常PPKとFloat設定PPKを比較する。
GNSSの時刻合わせは最大1.5秒の有効な隣接区間のみ。光源・測距欠測を補完しない。
両GNSS共通時刻、最近傍感度、共分散、全6窓の俯瞰、圧力深度を保存する。
旧位置・マッピングを更新せず、`stationary_gnss_comparison`へ追加出力する。
