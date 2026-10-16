# Calibration

校正値はGitでバージョン管理する。

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
