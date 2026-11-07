# ドキュメント

長田高校プール・南予海上実験の現行入口は **[統合研究レポートの索引](reports/field_experiments_20261103.md)**。
修正依頼順ではなく研究上の論点順で読み、旧結果と採用結果を区別する。

長田高校の[マルチパス観測解析](../results/20261028_ngt_000/multipath/report.md)：
可変3連送信の受信包絡線、後続ピーク候補、約1秒周期の反復、編集可能な6図と指標CSV。

全体概要は [../README.md](../README.md)、作業規約は [../AGENTS.md](../AGENTS.md) を参照。

位置フォールバック仕様と受け入れ条件は[spec.md](spec.md)、「2026-11-03 プール・南予の統合研究報告」節を参照。設定は`config/20261103_field_review_000/position_fallback.json`。

| ドキュメント | 内容 |
|---|---|
| [海・プールの子機BNO085＋深度比較](../results/20261103_field_review_000/child_dead_reckoning/comparison_20261106_v2/report.html) | 全10区間の現在子機位置との変位比較、初速0、欠測リセット、5/10/30/60秒窓、共通センサと未校正方位の留保。仕様・受け入れ条件はspec.md |
| [optical_detection.md](optical_detection.md) | プール・南予の光源検出改善、親機全画面による欠測原因、共通時刻の位置評価、旧新フォールバックの地図、プール004の対応分断と位置拘束の比較 |
| [another_pc.md](another_pc.md) | 別PCへの導入、校正根拠の検証、新しい実験の配置と解析の適用範囲 |
| [長田高校・位置と地図尺度の再評価](../results/20261028_ngt_000/coordinate_reassessment/report.md) | 最新版。親機90°・子機180°の画像座標を整合した位置、同一形状の尺度比較、33,564画像で再構成した壁地図 |
| [長田高校・研究発表用レポート（修正前の監査）](../results/20261028_ngt_000/research_report/report.md) / [実映像と12事例](../results/20261028_ngt_000/research_report/media_review.md) | 成立・欠測の全区間台帳、親子同期動画、映像とログの考察。修正前の座標不整合と旧尺度の留保を記録 |
| [長田高校・発表素材の入口](../results/20261028_ngt_000/presentation/index.html) / [検証記録](../results/20261028_ngt_000/presentation/report.md) | 親子の位置・姿勢、観測順の地図再生（機体あり／なし）、写真地図、GNSS俯瞰と2D研究グラフ |
| [長田高校・子機単独解析の詳細レポート](../results/20261028_ngt_000/child_only_comprehensive_report/report.md) / [PDF](../results/20261028_ngt_000/child_only_comprehensive_report/report.pdf) | 校正・磁北姿勢・全4実験の単眼VSLAM・地図・DR深度融合・VI-SLAM・失敗履歴・評価の限界を集約 |
| [子機地図・DR深度融合・VI-SLAM](../results/20261028_ngt_000/child_sensor_fusion/report.md) | 親機観測なしの地図3D表示、子機IMU・磁気・圧力融合、慣性初期化の成否・尺度の制約 |
| [../results/20261028_ngt_000/report.md](../results/20261028_ngt_000/report.md) | 10月28日学校プール実験：IMU同期・区間保存・RTK/PPK・親子の姿勢と観測可能な位置成分・未確定事項 |
| [reports/aquabeacon_summary_20260928.docx](reports/aquabeacon_summary_20260928.docx) | これまでの解析結果と現状をまとめた3ページのWord文書（2026-09-28） |
| [spec.md](spec.md) | 実験データ解析、時刻同期、位置推定、比較評価、マッピング、GitHub管理、Blender出力の仕様・受け入れ条件 |
| [長田高校プールの位置推定評価](../results/20261028_ngt_000/position_evaluation/report.md) | ライト光線と音響距離の統合、静止ばらつき、成立率、欠測、実験計画との対応 |
| [同時移動区間の壁マッピング](../results/20261028_ngt_004/wall_mapping/report.md) | プール壁のCOLMAP再構成、AquaBeacon位置priorの有無、単眼尺度と寸法評価の制約 |
| [全フレームで再開する壁マッピング](../results/20261028_ngt_004/wall_mapping/native120_restart/report.md) | 単眼SLAMと同じ33,564フレーム、時間窓ごとの再構成、推定位置への配置、全フレームの成否 |
| [親機と子機の軌跡とBlender](../results/20261028_ngt_000/position_visualization/report.md) | 002〜004の個別・重ね合わせ軌跡、時刻操作3D、水面基準の表示、欠測を保持したBlender再生 |
| [再構成した壁地図の可視化](../results/20261028_ngt_004/map_visualization/report.md) | 位置拘束4断片、画像のみの比較、元画像、疎点群の回転表示とBlender |
| [../data/README.md](../data/README.md) | 複数実験のフォルダ配置・新規取り込み手順・既存データの所属 |
| [../experiments/README.md](../experiments/README.md) | 実験台帳の一覧・schema・実験を指定するCLI |
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
| [cad_rig.md](cad_rig.md) | 今後の実験用のCAD配置スナップショット、センサー参照点・未確定中心、Blender/GLB再利用と検証 |

- [子機軸校正003の入力監査](../results/20261029_child_axis_003/axis_calibration/report.md)：4姿勢の軸変換・欠落監査・カメラの採用範囲・本番適用。

- [校正区間の切り出しと磁気方位検証](../results/20261029_child_axis_003/calibration_clip/report.md)

- [親機軸校正011と動画切り出し](../results/20261029_parent_axis_011/axis_calibration/report.md)

- [長田高校プールの子機のみ単眼VSLAM](../results/20261028_ngt_000/child_only_vslam/report.md)：001〜004、沖縄共通水中校正、追跡状態・子機姿勢との一致性。

- [Nanyo海上実験の評価](../results/20261031_nanyo_000/analysis/report.md)：RTK未使用、SPP/PPK・音響・相対位置候補・単眼地図。

- [Nanyo子機絶対位置・位置拘束マッピング](../results/20261031_nanyo_000/absolute_position/report.md)

- [Nanyoセンサー航法・写真地図・同期親子映像](../results/20261031_nanyo_000/navigation_replay/report.md)：SfM位置を航法に使わない追加推定、短時間DR、写真面と動画表示。

- [Nanyo指定静止区間の相対位置RMSE](../results/20261031_nanyo_002/static_evaluation/report.md)：15:03〜15:04 JST、姿勢補正比較と静止仮定の診断。

- [区間①の初期静止候補](../results/20261031_nanyo_001/static_selection/report.md) / [相対位置評価](../results/20261031_nanyo_001/static_evaluation/report.md)：14:41:10〜14:42:10 JST。

Nanyo day2：[同期](../results/20261101_nanyo_000/synchronization/report.md) / [5実験の分割](../results/20261101_nanyo_000/segmentation/report.md)。仕様はspec.mdのday2節。

[Nanyo：別PCへの引き継ぎ](handoff/nanyo_20261102.md)：Day2解析・2日分のマッピング・PPK Float比較の現在地、転送対象、検証結果、Canvaの編集権限待ちと再開手順。

- [Nanyo day2解析・day1との静止周波数比較](../results/20261101_nanyo_000/analysis/report.md)：5区間のPPK・音響・SfM、6静止窓の光源追跡、姿勢補正前後のRMSE・FFT・Lomb–Scargle・コヒーレンス。

- [Nanyo Day1・Day2結合マッピング](../results/20261102_nanyo_combined_000/mapping_replay/report.html)：静止RMSEによるGNSS選択、追跡光源による航法、共通ENUでの写真面・航跡・同期映像。

- [PPK Floatを使った静止絶対位置RMSE](../results/20261101_nanyo_000/stationary_gnss_comparison/report.html)：通常PPKと同じ6窓・385測距点、姿勢補正比較、深度・俯瞰・最近傍感度・GNSSとの共分散。仕様はspec.mdの「静止絶対位置へのFloat設定PPK比較」。
