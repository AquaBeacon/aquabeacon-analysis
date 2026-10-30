# Calibration

校正値はGitでバージョン管理する。

別PC用に[校正根拠データ](reference_data/README.md)も同梱する。
カメラ検出交点、親子4姿勢のIMU抜粋、監査・来歴・SHA-256を保存し、
`python scripts/verify_calibration_bundle.py --refit`で原動画なしに数値再計算できる。
取り付け条件や撮影条件の適合確認は[別PC導入手順](../docs/another_pc.md)を参照。

2026-10-30ユーザー指定：沖縄の水中校正を親機・子機の共通既定とする。
`camera_defaults.json` が両機器から `parent_camera_underwater_20260924_provisional.json`
への参照を保持する。ファイル名は来歴互換のため残す。原MP4の全画面縮小には
`camera_matrix_px` を解像度に合わせて拡縮し、LRFにはproxy直接校正を使う。
画角・Dewarp・EIS・ケース・撮影媒質の条件と、実使用値・SHA-256を解析manifestに残す。
水上区間への水中モデル適用は条件不一致として扱う。取付回転や時計はこの既定に含まない。
Pythonでは `aquabeacon_analysis.camera_calibration.load_camera_intrinsics` を使い、
`C_MAIN_CAM` / `P_MAIN_CAM`、出力寸法、main/proxyを明示する。cropや縦横比変更は拒否する。

想定ファイル:
- `parent_camera.yaml`: 親機カメラ内部パラメータ・歪み
- `child_camera.yaml`: 子機カメラ内部パラメータ・歪み
- `parent_imu.yaml`: 親機側IMU軸・バイアス等
- `child_imu.yaml`: 子機側IMU軸・バイアス等
- `extrinsics.yaml`: 各body/camera/IMU frame間の固定SE(3)変換

各実験結果から使用した校正バージョンを追跡できるようにすること。

`parent_camera_underwater_20260924_provisional.json` は水中動画から推定した
1920×1080画像用の暫定候補。40 mmマス間隔と親機・子機で共通のカメラ・設定は
ユーザー確認済み。対応`.LRF`の1280×720画像から直接推定した値も格納した。
既存2026-09-22動画は手元にないため、過去の解析にはまだ適用していない。根拠と制約は
`docs/reports/parent_camera_underwater_calibration_20260924.md` を参照。

`child_orbslam3_underwater_trial_022.yaml`、`023.yaml`、`024.yaml` は上記JSONの
1280×720 proxy直接校正値を子機用ORB-SLAM3試行設定に転記したもの。
各ファイルに校正元SHA-256を記載する。単眼モードではIMU外部変換を使わず、
単眼慣性モードの取付変換とIMU雑音は未校正の暫定値のままである。

2026-09-22の実験機体の取付面座標と原典ハッシュは
[`experiment_body_geometry_20260922.json`](experiment_body_geometry_20260922.json)を参照。
センサ中心・基板位置は未確定であり、レバーアーム補正値には使わない。

今後の実験用CADスナップショットは [`rigs/cad_20261027.json`](rigs/cad_20261027.json)。
672部品を親機・子機のbody frameへ統合し、9つのセンサー参照点を記録する。
配置の設計値とセンサー測定中心を分離し、`T_body_sensor` は未確定のまま `null`。
原典CAD・PCBのSHA-256、作業時のGit SHA・変更状態を含む。
既存の `experiment_body_geometry_20260922.json` を置換しない。
[座標・未確認点・使い方](../docs/cad_rig.md) を参照。

## 2026-10-29 子機軸校正の追加

`child_mount_20261028_actual.json` に本番と校正時のカメラ／水中ライト位置交換を記録。IMU→機体FRDは別校正JSONで保存済み。カメラ・ライトの数値extrinsicは未確定。監査結果は `results/20261029_child_axis_003/axis_calibration/report.md`。29567追加済み。姿勢4のdrop増加276は保持し、100Hz IMUの区間全体に連番・時刻欠損がないことを確認して軸算出に使用した。

`20261029_child_axis_003_camera_imu.json` は校正マーカー内だけで検証したカメラIMU取付回転と当該動画の時計変換。光学座標・並進・他クリップの時刻変換ではない。磁気方位の試行補正は不採用。

`20261029_parent_axis_011_axes.json`：親機Spresense→FRDとカメラ内IMU→FRDのオフライン校正。磁気センサ軸・絶対方位は不採用。時計変換は今回の起動・動画限定、取付回転は同じ取り付け条件でのみ再利用する。

`20261028_parent_lis3mdl_cad_axes.json`：2026-10-30ユーザー確認のX→GNSSと前方GNSS→Spresense、CADの基板上面上向き仮定による独立磁気軸。CADの面内向きに180°の実機上書きを記録。測定中心は校正しない。親機magnetic_ned姿勢で使用し、精度検証未達でも暫定値として利用可能。
