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

## Commands
実装開始時に以下の実コマンドを確定し、この節を更新すること。

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev,media,analysis,vision]"
python -m pytest
python -m ruff check .
python -m aquabeacon_analysis --help
python -m aquabeacon_analysis preprocess-tof
python scripts/extract_p_main_tof.py
python -m aquabeacon_analysis detect-lights
python -m aquabeacon_analysis track-lights
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
