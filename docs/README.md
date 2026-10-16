# ドキュメント

全体概要は [../README.md](../README.md)、作業規約は [../AGENTS.md](../AGENTS.md) を参照。

| ドキュメント | 内容 |
|---|---|
| [reports/aquabeacon_summary_20260928.docx](reports/aquabeacon_summary_20260928.docx) | これまでの解析結果と現状をまとめた3ページのWord文書（2026-09-28） |
| [spec.md](spec.md) | 実験データ解析、時刻同期、位置推定、比較評価、マッピング、GitHub管理、Blender出力の仕様・受け入れ条件 |
| [tof_sound_speed.md](tof_sound_speed.md) | C_MAIN・P_MAIN ToFの海水音速補正式、出典、仮定、再実行方法 |
| [reports/cmain_tof_depth_20260922.md](reports/cmain_tof_depth_20260922.md) | 2026-09-22のC_MAIN 3実験・対応するP_MAIN ToFと圧力換算水深の比較レポート |
| [reports/parent_camera_light_tracking_20260922.md](reports/parent_camera_light_tracking_20260922.md) | 親機カメラによる子機ライト検出、EKF追跡、仮画角と親機IMU姿勢による角度算出レポート |
| [reports/parent_camera_underwater_calibration_20260924.md](reports/parent_camera_underwater_calibration_20260924.md) | 水中チェッカーボード動画からの親機カメラ内部校正の暫定結果と適用条件 |
| [reports/calibrated_pose_position_20260922.md](reports/calibrated_pose_position_20260922.md) | 水中カメラ内部校正を適用した3実験の親子機姿勢・位置再推定 |
| [reports/child_vslam_calibrated_20260922.md](reports/child_vslam_calibrated_20260922.md) | 子機のみの水中校正値適用VSLAM・CLAHE比較・追跡欠損と制約 |
| [reports/parent_child_positions_20260922.md](reports/parent_child_positions_20260922.md) | 仮ライト角度・ToF・親機GPSを使った親子機位置試算と制約 |
| [reports/child_imu_comparison_20260922.md](reports/child_imu_comparison_20260922.md) | 子機IMUデッドレコニングとToF＋ライト相対位置の移動量比較 |
| [reports/fused_relative_position_20260922.md](reports/fused_relative_position_20260922.md) | ToF・ライト・子機IMU・圧力の条件付き融合と内部検証 |
| [vislam_environment.md](vislam_environment.md) | ORB-SLAM3実行環境、入力変換、校正待ち項目 |
| [experiment_body_geometry.md](experiment_body_geometry.md) | 実験で使用した親機・子機モデルの取付面座標、内装変更と未校正点 |
