# 子機単眼＋IMU VI-SLAM 実行環境

## Backend

- ORB-SLAM3 commit `4452a3c4ab75b1cde34e5505a36ec3f9edcdc4c4`、単眼慣性モード。
- Pangolin commit `dd801d244db3a8e27b7fe8020cd751404aa818fd`。
- Ubuntu 22.04 ベースのDockerイメージで固定する。
- 公式EuRoC実行例 `mono_inertial_euroc` を用いる。
- ORB-SLAM3はGPLv3。再配布時はライセンスを確認する。

## 現在の確認範囲

- 公式ソースとEuRoC入出力を確認済み。
- Python入力変換の自動テスト・lintは実行可能。
- Windows 11、WSL 2.7.14、Docker Desktop 4.91.0のLinuxエンジンで
  `aquabeacon-orbslam3:4452a3c` のビルドに成功（2026-09-23）。
- コンテナ内で `mono_inertial_euroc` が実行可能で、語彙ファイルが存在し、
  `ldd` に未解決の共有ライブラリがないことを確認済み。
- 引数なしで実行し、想定どおり使用方法を表示することを確認済み。
- 3画像、子機IMU 17,989行、圧力172行のEuRoC形式への試験変換を確認済み。
  これはフレーム対応を未確定とした `test_only` 出力であり、軌跡推定には使用しない。
- 水中カメラ内部校正の暫定候補は取得済み。子機カメラ–IMU固定変換、IMUノイズは未測定。
  023について暫定設定の短区間試験を行い、その結果を正式な精度比較には採用しない。

## Windowsホストのセットアップ

[MicrosoftのWSL導入ガイド](https://learn.microsoft.com/en-us/windows/wsl/install) と
[Docker DesktopのWindows版](https://docs.docker.com/desktop/setup/install/windows-install/) に従う。
このPCではDocker DesktopのWSL 2 backendが動作しており、Ubuntuの個別ディストリビューションは不要。
新規ホストでは `docker version` のServer欄にLinuxエンジンが表示されることを確認してから
以下の `-Build` を実行する。

## 必要な校正

1. 子機カメラの水中・防水ケース装着状態での `fx, fy, cx, cy` と歪み。
   入力するproxy動画は1280×720なので、その解像度へ換算した値を使う。
   2026-09-24のチェッカーボード動画による暫定候補は親機・子機共通の
   `calibration/parent_camera_underwater_20260924_provisional.json` にある。
   この撮影に対応する`.LRF` proxyの1280×720画像から直接推定した値を優先する。
   2026-09-25には処理済み2026-09-22子機proxyを使ってSLAMを再実行したが、
   校正動画との直接画角照合は未実施。
2. `IMU.T_b_c1`（camera frameから子機本体IMU frameへのSE(3)）。
3. IMUのgyro/accel白色雑音とrandom walk。
4. 圧力深度用の水面圧力基準、水密度、圧力センサとカメラの取付位置。

`calibration/child_orbslam3.yaml` はこれらが測定されてから作成する。
試験用の `calibration/child_orbslam3_provisional_023.yaml` は、Action 6 Standard
（Dewarp）の1280×720 proxyを歪みゼロのピンホールと仮定し、水中有効水平FOVを
**仮に90°**（fx=fy=640 px）とした。[DJI仕様](https://www.dji.com/osmo-action-6/specs)の
155°はレンズFOVであり、[DJI FAQ](https://www.dji.com/osmo-action-6/faq)にある
Standard（Dewarp）画角や水中ケース使用時の実測値ではない。IMU回転は023の角速度相関による
暫定推定、並進0 m、ノイズ値は試行設定である。DJIテレメトリ姿勢軸と画像光学軸の
対応も未確認。これらを校正済みの値として扱わない。
公式EuRoC YAMLの数値を子機の値として使わない。

## 入力変換

`python scripts/prepare_orbslam3.py EXPERIMENT_ID --first-camera-frame N --alignment-note '照合した画像と方法'`。
出力は `data/interim/orbslam3/EXPERIMENT_ID/`。画像時刻とIMU時刻は共通UTCのns整数。
画像はグレースケールPNGの `mav0/cam0/data/<utc_ns>.png`、時刻列は `timestamps.txt`、
IMUは `mav0/imu0/data.csv`（gyro rad/s、accel m/s²）、
圧力は `mav0/pressure0/data.csv` に保存する。
画像とIMUの元CSVは変更しない。
IMUのUTC時刻に逆行がある場合は出力のみ時刻順に並べ、件数をmanifestに記録する。
重複UTCは変換を停止する。
既存の角速度照合で得た残差時刻補正は `--imu-time-offset-s` でIMU側に適用する。
022は-0.025秒、023・024は0秒。IMU終端を超えるカメラフレームは除外し、
`camera_frames_dropped_for_imu_coverage` に件数を残す。022では末尾1枚を除外した。

`N` は処理済みproxy動画の先頭デコードフレームに対応する `camera_frame`。
stream copyが先頭をキーフレーム単位でずらすため、手動または画像照合で確定する。
画像照合手順と確定値を実験manifestに記録するまでは本解析を開始しない。
`--max-frames` は形式確認用で、全実験の結果には使わない。
試験出力を残して全区間を別場所に作る場合は
`--output-root data/interim/orbslam3_full` を指定し、実行時に
`-DatasetRoot data/interim/orbslam3_full` を指定する。
022・023・024のそれぞれで元動画と処理済み動画の5か所が画素完全一致した。
処理済み先頭の `camera_frame` は順に8940・10080・28320。
各 `results/<experiment_id>/vislam_depth/frame_alignment.json` に照合箇所を記録した。
023の元動画開始はcamera_frame 1620、024は21060、022は0と置いた。
元動画の開始番号はGOP先頭からの推定であり、別途映像時刻による確認が必要。
`timestamps.txt` はLinux実行例がファイル名にCRを含めないようLFで出力する。

## 起動

```powershell
./scripts/run_orbslam3.ps1 -Build
./scripts/run_orbslam3.ps1 -Pilot -ExperimentId 20260922_cmain_023 -SettingsPath calibration/child_orbslam3_provisional_023.yaml
./scripts/run_orbslam3.ps1 -ExperimentId 20260922_cmain_023 -SettingsPath calibration/child_orbslam3.yaml
```

出力は `results/<experiment_id>/vislam_depth/orbslam3_raw/`。
ここにORB-SLAM3の軌跡が保存された後、追跡欠損を元画像時刻と照合し、
圧力深度を別工程で拘束として適用する。圧力を単眼画像の画素深度として扱わない。
Ground Truthがないため真値誤差は算出しない。
軌跡が得られた試験区間には
`python scripts/apply_orbslam3_pressure_depth.py 20260922_cmain_023 --pilot --settings calibration/child_orbslam3_provisional_023.yaml`
を使用できる。SLAM世界座標の+Zが上という暫定仮定の下、圧力を最大1秒の隣接区間内で
線形補間し、最初のSLAM poseを基準に鉛直成分を置換する。水平成分と姿勢は保持する。
軌跡欠損フレームは生成せず、件数をmanifestに残す。圧力基準は最初のログ値、
カメラ–圧力センサの鉛直レバーアームは暫定0 m。慣性初期化と座標軸が
確認できない軌跡への適用結果は比較評価に使わない。

## 022・023・024の暫定実行結果

2026-09-23に全3実験の全区間をORB-SLAM3単眼慣性モードで実行した。
カメラ内部モデルは全て仮定水平FOV 90°、各実験のカメラ–IMU回転は
`calibration/child_orbslam3_provisional_<実験番号>.yaml` に記録した。

| 実験 | 出力画像 | IMU時刻補正 | 全区間exit | 中盤区間 | VI軌跡 |
|---|---:|---:|---:|---|---|
| 022 | 4776 | -25 ms | 139 | 1800画像、exit 0、姿勢7点 | なし |
| 023 | 5416 | 0 ms | 139 | 後半1816画像はexit 139。別軸設定の900画像はexit 0、姿勢2点 | なし |
| 024 | 4053 | 0 ms | 139 | 1800画像、exit 139 | なし |

全区間実行は最後に地図のキーフレームが0となり、ORB-SLAM3の軌跡保存時に
exit 139で終了した。exit 0の短区間出力もIMU初期化の証拠がなく、
`usable_vi_trajectory=false` とした。圧力深度補正は3実験とも未適用。
各 `results/<experiment_id>/vislam_depth/trial_summary.json`、`run_manifest.json`、
`run.log` に設定SHA-256、入力数、実行範囲、終了状態を記録した。

### 023の追加試行

2026-09-23に `calibration/child_orbslam3_provisional_023.yaml` で先頭900画像、
全5416画像、後半1816画像を試行した。角速度フィットの座標軸に加え、
`calibration/child_orbslam3_provisional_023_optical_trial.yaml` で既存の
親カメラ暫定光学軸割当てを合成した後半900画像も試行した。
4試行とも **比較に使える慣性初期化済みのVI軌跡は得られていない**。
最初の3試行は追跡リセット後に異常終了（exit 139）。軸割当て変更の試行は
exit 0だが、最終地図の2姿勢のみで慣性初期化の証拠がない。
`results/20260922_cmain_023/vislam_depth/` の各 `run_manifest.json` と `run.log` に
設定SHA-256、範囲、欠損・終了状態を記録した。圧力深度補正は有効VI軌跡がないため
未適用。後処理スクリプトは `usable_vi_trajectory=false` を拒否する。
ログには「追跡喪失」「IMU未初期化」「運動不足」が繰り返し現れる。
後半には特徴点の多い画像もあるが、持続した地図にはならなかった。
暫定カメラFOV、カメラ–IMU軸対応、時刻同期、映像ブレ・環境の切り分けは未完了。

## 融合姿勢・低コントラスト画像を用いた追加試行

`scripts/diagnose_vislam_features.py` で各実験の18組の画像対（3フレーム間隔）を
同じ暫定カメラ内部行列で比較した。数値は画像対の対応点数中央値であり、
軌跡精度やSLAM成功率ではない。

| 実験 | 元画像ORB | CLAHE画像ORB | CLAHE画像SIFT |
|---|---:|---:|---:|
| 022 | 679 | 1101 | 786 |
| 023 | 373 | 613 | 755 |
| 024 | 157 | 263 | 566 |

ORBでも対応点が多い画像対があり、失敗原因を一律に特徴点不足とは断定できない。
15内点未満の組は022が0/18、023が4/18、024が2/18だった。
CLAHE画像ORBでは024の該当組が0/18になったが、023の4/18は残った。
ただしCLAHEによる局所コントラスト補正と検出点上限2500・FASTしきい値3で
ORB対応点は増えたため、ORB-SLAM3互換の入力として採用した。
SIFTは対応点診断に使用したが、固定コミットのORB-SLAM3の特徴抽出器には
直接置き換えていない。

子機の `child_fused_attitude.csv` のquaternionを差分して角速度を求め、
元IMUのUTC nsへ補間する。加速度は子機本体IMU値をジャイロ照合で得た暫定取付回転で
DJIカメラ姿勢座標系へ回し、`data/interim/orbslam3_fused/<実験ID>/data.csv` に保存した。
元のEuRoC IMUファイルは残す。`manifest.json` に入力SHA-256、Git SHA、
元gyroとの相関・差のRMS・端点外挿件数を記録する。元gyroとの相関は022が0.998、
023が0.857、024が0.961だった。これは姿勢ストリーム同士の整合指標で、
SLAMの位置精度ではない。融合quaternionはORB-SLAM3の絶対姿勢拘束にはならず、
導出した角速度として間接的に利用される。画像光学軸との取付関係は依然未校正。

`scripts/prepare_orbslam3_clahe_trial.py` は元の全区間EuRoC exportから指定画像範囲を
CLAHE clipLimit=2.0・tileGridSize=8×8で処理した試験データセットを作る。
元画像・元IMUに書き込まない。`run_orbslam3.ps1` の `-ImuOverridePath` は
合成IMUをDockerのEuRoCパスに読み取り専用で重ね、実験IDの `fused_...` 出力へ保存する。
試行設定は `calibration/child_orbslam3_fused_trial_<番号>.yaml` にあり、
焦点距離・光学軸・雑音値・取付並進はいずれも暫定である。

2026-09-23の試行結果は次のとおり。いずれも慣性初期化の証拠がなく、
`usable_vi_trajectory=false`。短い出力姿勢も正式なVI-SLAM軌跡として扱わない。

| 実験 | 元画像範囲 | 終了コード | 軌跡姿勢点数 | 追跡リセット | 慣性初期化 | 圧力補正 |
|---|---:|---:|---:|---:|---|---|
| 022 | 2100–3899 | 0 | 5 | 43 | なし | 未実施 |
| 023 | 3600–5415 | 139 | 0 | 43 | なし | 未実施 |
| 024 | 2100–3899 | 139 | 0 | 74 | なし | 未実施 |

手動確認は各 `results/<実験ID>/vislam_depth/fused_pilot_orbslam3_raw/run.log` と
`run_manifest.json` で行った。ログには局所地図の追跡喪失と再初期化が繰り返しあり、
022・023では運動不足による慣性初期化失敗も記録された。
CLAHEで対応点は増えたが、VI-SLAM成立にはつながらなかった。
各 `fused_trial_summary.json` に実行範囲、終了状態、姿勢点数、ログ件数、
設定・入力・診断結果のSHA-256を記録した。
カメラ内部校正とカメラ光学軸–IMU軸の取付校正、映像とIMUの残差同期、
十分な視差と励起を持つ撮影区間の切り分けが次の確認事項となる。

## 水中校正値を適用した子機単眼VSLAM（2026-09-25）

`calibration/parent_camera_underwater_20260924_provisional.json` の
1280×720 proxy画像から直接求めた内部行列・歪み係数を
`scripts/prepare_child_calibrated_orbslam3_settings.py` で子機用ORB-SLAM3設定へ転記した。
ファイル名にparentとあるのは校正撮影時の装着先を示す。ユーザー記録では子機と同じカメラ・
撮影設定であり、この試験の観測入力は各実験の`C_MAIN`動画のみである。
カメラ内部値は暫定候補で、2026-09-22動画と校正撮影の画角一致は未確認。
`IMU.T_b_c1`とIMUノイズは既存の暫定試験設定を引き継ぐため、校正済み外部変換ではない。

まず023の900フレームを校正値で単眼慣性実行したが、慣性初期化を維持できず
exit 139、軌跡0点で終了した。同じ画像・内部値の単眼モードでは
895/900フレームが保存姿勢に対応し、151キーフレームを保存した。
したがって全3実験で単眼モードを実行し、022・024にはCLAHE短区間も追加試験した。
各試行は固有`-RunName`で旧結果と分離してある。

`scripts/summarize_child_vslam.py` はORB-SLAM3の浮動小数点出力時刻を元のUTC nsに
2 ms以内で照合し、全入力フレームの`camera_trajectory_status.csv`と欠損区間を含む
`coverage_summary.json`を生成する。姿勢が保存されないフレームは`not_saved`とし、
元の`CameraTrajectory.txt`に同じフレームの姿勢が複数ある場合はCSVに最後の1件を載せ、
重複件数をJSONへ記録する。複数地図間の姿勢座標は同一基準と見なさない。
単眼の並進はSLAM単位で、圧力深度のmへ無校正で換算しない。
ORB-SLAM3のEuRoC軌跡保存はAtlas最大地図だけを含むため、`not_saved`を
そのまま画像別tracking lostとみなさない。他地図に姿勢があった可能性がある。
全区間の保存姿勢対応率は022が2204/4776（46.1%）、023が1952/5416（36.0%）、
024が1393/4053（34.4%）。長い姿勢未保存区間を含むので全区間を連続軌跡として使わない。
入力・試行・欠損とCLAHE比較の詳細は
[`reports/child_vslam_calibrated_20260922.md`](reports/child_vslam_calibrated_20260922.md)。
地図点群を含む再実行は各`results/<実験ID>/vislam_depth/report.md`に映像・画像・
地図別PLYとともに整理する。単眼Atlasの地図間座標を結合しない。
