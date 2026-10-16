# 親機カメラによる子機ライトの検出・追跡・仮角度算出レポート

**対象実験:** `20260922_cmain_022`、`20260922_cmain_023`、`20260922_cmain_024`  
**解析日:** 2026-09-23  
**対象映像:** 各実験の `data/processed/<experiment_id>/tof_window/camera_video/P_MAIN*_proxy.mp4`  
**撮影条件:** 親機 `P_MAIN_CAM`、DJI Osmo Action 6、Standard (Dewarp)、EIS Off、公式防水ケース装着、水中撮影（[撮影条件記録](../../experiments/20260922_camera_setup.json)）

## 要旨

親機の1280×720 proxy動画から子機ライトの画像座標を求め、目視確認点を起点とする拡張カルマンフィルタ（EKF）で約2 Hzの時系列を追跡した。3実験で計955サンプルのうち、653サンプルには画像観測または目視確認点に基づく座標が得られた。短時間の予測のみが17サンプル、未追跡が285サンプルだった。

独立に目視注釈した可視8サンプルでは、30 px以内の一致が単独の検出候補で7/8、EKF追跡で8/8だった。画面外と注釈した2サンプルでは、座標の誤出力が検出候補で2/2、EKF追跡で0/2だった。注釈は少数かつ約5～10 pxの読み取り幅があり、この結果を絶対精度とはみなさない。

水中・防水ケース装着状態のカメラ校正値がないため、正式な視線角度はまだ求められない。試算として**水平画角90°を仮定**し、観測に基づく653サンプルの親機カメラ相対方位角・仰角を別CSVに出力した。この90°は実測値でもDJIのStandard設定の仕様値でもない。

## 1. 入力と座標系

- 親機カメラは `P_MAIN_CAM`。子機カメラ `C_MAIN_CAM` の映像を親機視点の解析に使用しない。
- 画像座標は未回転proxy動画の左上を原点とし、右向きを `x_px` 正、下向きを `y_px` 正とする。単位は1280×720画像のpixel。
- `video_time_s` はproxy動画先頭からの相対秒。動画のstream copyによる先頭ずれがあり、UTCへ自動変換していない。
- 角度は `P_MAIN_CAM` の光軸を基準とし、右正の方位角、上正の仰角。内部単位はradで、閲覧用にdegも併記する。親機本体座標や地理座標への変換は含まない。

## 2. 解析方法

### 2.1 ライト候補の検出と目視確認

各動画を約0.5秒間隔で読み、640 px幅へ縮小したフレームから白い高輝度領域を抽出した。局所コントラスト、発光領域の面積・形状、周囲の水色の割合、近くのオレンジ色筐体を候補の評価に用いた。大きな発光に生じる非対称なにじみでは明るい中心部から座標を求める。候補CSVの `candidate_unreviewed` は確定したライト位置を意味しない。

別途、目視で確認した29点（022: 8点、023: 10点、024: 11点）を `reviewed_light.csv` に固定し、追跡の基準点とした。目視確認点は[確認点記録](../../experiments/light_review_20260922.json)に保存している。

### 2.2 EKF追跡

EKFの状態は作業画像上の `(x, y, vx, vy, log A)` とした。`A` はライトの白色発光領域の面積で、面積観測は `A = exp(log A)` の非線形モデルを用いる。位置・速度は等速モデルで予測し、候補位置と予測分布のMahalanobis距離、候補の見た目、前後の確認点に基づく探索範囲で対応付けた。

出力状態は次のとおり。

| 状態 | 意味 | 仮角度の出力 |
|---|---|---|
| `anchor` | 目視確認点 | あり |
| `observed` | 候補の観測でEKFを更新 | あり |
| `reacquired` | 強い候補から追跡を再初期化 | あり |
| `predicted` | 観測がなく短時間のモデル予測のみ | なし |
| `untracked` | 位置を確定できない | なし |

予測のみの出力は最大1.5秒、モデル上の位置標準偏差45 px以下に限る。`position_sigma_px` はモデルの不確かさであり、実ライトとの測位誤差ではない。`observed` と `reacquired` の全行を目視確認したわけではなく、反射などの誤対応が残り得る。

### 2.3 仮のカメラ相対角度

[仮定ファイル](../../calibration/parent_camera_provisional.json)に記録した水平画角90°、正方画素、画像中心の主点、歪みなしのpin-holeモデルから、`fx = fy = 640 px`、`cx = 640 px`、`cy = 360 px` と置いた。座標 `(x, y)` から次式で角度を計算した。

```text
nx = (x - cx) / fx
ny = (cy - y) / fy
azimuth_rad   = atan(nx)
elevation_rad = atan2(ny, sqrt(1 + nx²))
```

水中での防水ケースによる屈折とレンズ歪みを補正していない。角度CSVの `angle_status=hypothetical_uncalibrated` はこの仮定を示す。校正値を得た後は、元の追跡座標から再計算する。

## 3. 結果

| 実験 | 全サンプル | `anchor` | `observed` | `reacquired` | `predicted` | `untracked` | 仮角度あり |
|---|---:|---:|---:|---:|---:|---:|---:|
| 022 | 319 | 8 | 90 | 67 | 16 | 138 | 165 |
| 023 | 363 | 10 | 137 | 119 | 0 | 97 | 266 |
| 024 | 273 | 11 | 95 | 116 | 1 | 50 | 222 |
| **合計** | **955** | **29** | **322** | **302** | **17** | **285** | **653** |

次の数値は各実験で最初の `anchor` 行の**仮角度例**である。観測画角の実測値として解釈しない。

| 実験 | 動画相対時刻 | 画像座標 `(x, y)` px | 方位角（右正） | 仰角（上正） |
|---|---:|---:|---:|---:|
| 022 | 10.01 s | (742.84, 237.01) | +9.13° | +10.74° |
| 023 | 0.00 s | (1192.03, 395.18) | +40.78° | −2.38° |
| 024 | 10.01 s | (725.96, 463.02) | +7.65° | −9.06° |

3実験の全時刻の結果は[022](../../results/20260922_cmain_022/light_detection/provisional_light_angles.csv)、[023](../../results/20260922_cmain_023/light_detection/provisional_light_angles.csv)、[024](../../results/20260922_cmain_024/light_detection/provisional_light_angles.csv)を参照。角度欄が空の行も削除せず残した。

## 4. 目視確認と解釈

EKF基準点とは別に、[独立目視注釈](../../experiments/light_validation_20260922.json)を用意した。可視8点の座標は元動画の該当フレームから約5～10 pxの読み取り幅で記録し、30 px以内かを確認した。画面外2点は座標が出ていないことを確認した。

| 実験 | 可視点の30 px以内: 単独候補 → EKF | 画面外点への誤座標: 単独候補 → EKF |
|---|---:|---:|
| 022 | 2/2 → 2/2 | 対象なし |
| 023 | 3/4 → 4/4 | 2/2 → 0/2 |
| 024 | 2/2 → 2/2 | 対象なし |
| **合計** | **7/8 → 8/8** | **2/2 → 0/2** |

約10秒ごとの接触シート（[022](../../results/20260922_cmain_022/light_detection/tracking_review.jpg)・[023](../../results/20260922_cmain_023/light_detection/tracking_review.jpg)・[024](../../results/20260922_cmain_024/light_detection/tracking_review.jpg)）を目視し、023の画面外区間などを確認した。個別点の比較は各実験の `tracking_validation.csv`、集計は `tracking_report.md` に記録した。少数の確認点に対する一致の改善は見られるが、全区間の検出性能や角度の真値誤差を示すものではない。

## 5. 親機IMU融合姿勢を適用した角度

親機カメラとSONYジャイロを融合した `parent_fused_attitude.csv` を用い、
親機の向きを補正したライト視線を出した。切り出し動画の最初の画像を元動画の
フレームと照合し、stream copyで生じた動画先頭の差を時刻へ反映した。
元動画の時刻0と最初のrefined telemetry行が一致するという前処理の仮定を引き継ぐ。
融合Quaternionは該当時刻の前後で球面線形補間し、範囲外や0.15秒を超える欠損には
角度を出していない。

光学軸とIMU軸の取付回転は未測定であるため、
[仮の取付回転](../../calibration/parent_camera_imu_provisional.json)として
IMU `+X=画像上`、`+Y=画像右`、`+Z=光軸前方` と置いた。
重力基準仰角は上正。局所水平方位角は各実験の最初の融合姿勢における
光軸の水平投影を0°とした相対値であり、磁北・真北からの方位ではない。

| 実験 | カメラ相対の仮角度あり | 姿勢補正角度あり | 姿勢時刻が範囲外 | 例：動画約30秒の局所方位角 / 重力基準仰角 |
|---|---:|---:|---:|---:|
| 022 | 165 | 165 | 0 | −1.01° / −5.65° |
| 023 | 266 | 264 | 2 | +51.03° / −2.59° |
| 024 | 222 | 222 | 0 | −132.27° / −4.86° |

結果は各実験の[022](../../results/20260922_cmain_022/light_detection/attitude_corrected_light_angles.csv)・
[023](../../results/20260922_cmain_023/light_detection/attitude_corrected_light_angles.csv)・
[024](../../results/20260922_cmain_024/light_detection/attitude_corrected_light_angles.csv)に保存した。
カメラ単独姿勢と融合姿勢で同じ視線を回した差の中央値は、それぞれ
0.102°、0.031°、0.067°だった。これは姿勢系列間の差であり、真値に対する改善量ではない。
画角90°と取付回転の仮定に加え、絶対水平方位が未確定のため、
このCSVを確定した光学方向やENU方位として使用できない。
既存の `relative_position/parent_child_position.csv` は別の取付回転仮定で算出されており、
この追加解析によって更新されていない。

## 6. 補正前後の角度グラフ

左列は仮の水平画角90°による**カメラ相対**角度、右列は親機融合姿勢と仮の
光学軸→IMU取付回転を適用した**局所重力基準**角度。上段は方位角、下段は仰角。
横軸は切り出し動画の相対時刻 [s]。欠損を補間せず、観測のある時刻を点で示した。
右上の局所方位角は±180°で折り返す。左右は角度の基準が異なるため、
グラフ間の差を精度改善量と解釈しない。

### 実験022

![実験022の補正前後角度](../../results/20260922_cmain_022/light_detection/angle_before_after.png)

### 実験023

![実験023の補正前後角度](../../results/20260922_cmain_023/light_detection/angle_before_after.png)

### 実験024

![実験024の補正前後角度](../../results/20260922_cmain_024/light_detection/angle_before_after.png)

3枚とも画像を開き、方位角・仰角の軸、左右の基準、欠損区間と±180°の折り返しを
目視確認した。図の入力ハッシュと描画条件は各実験の `angle_plot_manifest.json` に保存した。

## 7. 再実行と追跡可能性

プロジェクト直下で実行する。

```powershell
python -m aquabeacon_analysis detect-lights
python scripts/export_reviewed_lights.py
python -m aquabeacon_analysis track-lights
python scripts/render_light_track_review.py
python scripts/evaluate_light_tracking.py
python scripts/estimate_provisional_light_angles.py
python scripts/estimate_attitude_light_angles.py
python scripts/plot_light_angles.py
```

各実験の `results/<experiment_id>/light_detection/` に、候補 `parent_light.csv`、確認点 `reviewed_light.csv`、追跡 `tracked_light.csv`、仮角度 `provisional_light_angles.csv`、姿勢補正角度 `attitude_corrected_light_angles.csv` を保存する。`tracking_manifest.json`、`provisional_angle_manifest.json`、`attitude_angle_manifest.json` から、使用した動画・追跡CSV・融合姿勢・config・仮定のSHA-256、およびGit commit `3d3724b5fafc2791d763615940716a6d598831a8` を追跡できる。このSHAは解析時のHEADであり、作業ツリー内の未コミット変更は表さない。

## 8. 残る課題

1. 実際の撮影設定・公式防水ケース・水中環境でカメラ内部パラメータと歪みを校正し、仮角度を置き換える。
2. 目視注釈点を増やし、近距離・遠距離、画面端、遮蔽、反射が強い区間で追跡の一致を確認する。
3. 親機本体に対するカメラの外部パラメータと時刻同期を確定した後、必要に応じて本体座標系の視線へ変換する。
