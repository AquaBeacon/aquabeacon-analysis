# Nanyo海上実験：GNSS・音響・子機位置・地図

2026-10-31、前回同期・分割した001/002を解析した。RTKは未使用。GNSS・音響の評価と条件付き相対位置、子機画像のみのCOLMAPを分けて保存した。
**追加解析あり：** [子機の絶対位置・位置拘束マッピング](../absolute_position/report.md)。このページの位置・地図に関する未確定事項は初回解析時点の記録。固定取付けの確認、絶対姿勢ログの追加解釈、真北・CAD補正を適用した最新結果はリンク先を参照。


## GNSS：単独測位とPPK

UBXの3,437エポックは全てcarrier_solution=0・非差分測位。RAWX/SFRBXからRINEXを生成し、GEONET 950437（約5.25 km）を主局、041133（約8.47 km）を比較局にした。RTKLIB EX 2.5.1、kinematic/combined、GPS+Galileo+QZSS、仰角15°、30秒基準局観測・最大age15秒。AR continuousとAR offの両方を保存。

全収録の主局解は固定340・Float 2,291・DGNSS 806時刻。RINEXヘッダの基準局座標を用い、測地系epochやアンテナ高の追加補正は未実施。船の実移動を含むため静止誤差評価とはしない。

基準局変更による水平差：中央値 0.104 m、95%点 0.730 m。測量真値との差ではない。

[全SPP/PPK CSVとmanifest](../evaluation/) / [比較局結果](../evaluation/base_comparison.json)

![GNSS俯瞰](../evaluation/gnss_overhead.png)

![航空写真](../evaluation/gnss_aerial.png)

航空写真は国土地理院。位置の真値ではない。Web Mercator軸は地表距離そのものではない。

## 区間別の音響と位置候補

| 区間 | 全shot | 音響候補 | 候補率 | PPK固定/Float/DGNSS | カメラ相対位置候補 |
|---|---:|---:|---:|---|---:|
| 001 | 2842 | 1870 | 65.8% | 137/877/29 | 552 |
| 002 | 872 | 558 | 64.0% | 31/273/15 | 295 |

音響候補はstate3かつ正値。今回のprofile0、音速1500 m/s、補正0 µsを保持し、前回の55 µsを流用していない。検出候補率は測位成功率や正しい直接波の割合ではない。設定64組の集計CSVも各区間へ保存した。

圧力水深と距離の差は主に負になる。親機も水中に入り、親機深度・レバーアーム・海水音速・圧力基準が未確定のため、この差を測距誤差や失敗判定にはしない。

位置候補は親機光学右/下/前座標。光点候補＋最寄り全shot（0.3秒以内）と共通水中内部校正を用いた。光点同定・水中判定は未検証、音響/光学中心の一致を仮定。船上・欠測は補完しない。親機カメラが動くので固定世界の子機軌跡にはならない。

今回の光学/機体extrinsicと取付けが確認できないため、GNSS座標に重ねた子機の絶対3D位置や、それを拘束条件にしたメートル尺度地図は未確定。

## 子機だけの画像マッピング

前回のORB-SLAM3用Dockerが応答せず、今回はCOLMAP 1 Hz・960×540・CLAHE・固定の共通水中内部校正・逐次5近傍照合で実行。native120fps VSLAM/VI-SLAMの再実行結果ではない。光学条件の完全一致は未確認。地図は任意尺度の独立断片である。

画像時刻は公称1 Hzビン（最大1秒幅の近似）であり、原フレームPTSは未保存。同期済み姿勢・圧力との精密な融合には再抽出が必要。

### 20261031_nanyo_001

登録 317/1043画像（30.4%）、モデル 4個。未登録はframe_status.csvに残した。

![音響と水深](../../20261031_nanyo_001/evaluation/acoustic_depth.png)

![相対位置候補](../../20261031_nanyo_001/relative_position/relative_candidates.png)

[実映像と光点の確認](../../20261031_nanyo_001/relative_position/light_review.jpg)

![地図登録範囲](../../20261031_nanyo_001/visual_mapping/coverage.png)

代表画像の目視ではモデル1～3に海底の岩・構造物が見える。静的な海底を含む復元候補として保持するが、魚や浮遊物の点を個別に除去・検証した地図ではない。モデル0は水面上であり海底地図から除外する。

モデル0: 3画像、176点、平均再投影残差 1.000 px（物理精度ではない）。

![点群](../../20261031_nanyo_001/visual_mapping/model_0.png)

水面・陸岸・空の3画像。水中内部校正の適用条件外であり、海底地図としては不採用。

![登録画像の先頭・中間・末尾](../../20261031_nanyo_001/visual_mapping/model_0_views.png)

[PLY点群](../../20261031_nanyo_001/visual_mapping/model_0.ply) / [カメラ位置CSV](../../20261031_nanyo_001/visual_mapping/model_0_poses.csv)

モデル1: 94画像、8376点、平均再投影残差 0.606 px（物理精度ではない）。

![点群](../../20261031_nanyo_001/visual_mapping/model_1.png)

代表画像に海底の構造物・岩面が見える。任意尺度の海底復元候補。

![登録画像の先頭・中間・末尾](../../20261031_nanyo_001/visual_mapping/model_1_views.png)

[PLY点群](../../20261031_nanyo_001/visual_mapping/model_1.ply) / [カメラ位置CSV](../../20261031_nanyo_001/visual_mapping/model_1_poses.csv)

モデル2: 31画像、4379点、平均再投影残差 0.554 px（物理精度ではない）。

![点群](../../20261031_nanyo_001/visual_mapping/model_2.png)

代表画像に海底の岩面・ロープ状の構造と魚が見える。動く魚の点の混入は未検証。

![登録画像の先頭・中間・末尾](../../20261031_nanyo_001/visual_mapping/model_2_views.png)

[PLY点群](../../20261031_nanyo_001/visual_mapping/model_2.ply) / [カメラ位置CSV](../../20261031_nanyo_001/visual_mapping/model_2_poses.csv)

モデル3: 189画像、5063点、平均再投影残差 0.687 px（物理精度ではない）。

![点群](../../20261031_nanyo_001/visual_mapping/model_3.png)

代表画像に海底が見える。濁り・浮遊物を含むため全点の静止性は未検証。

![登録画像の先頭・中間・末尾](../../20261031_nanyo_001/visual_mapping/model_3_views.png)

[PLY点群](../../20261031_nanyo_001/visual_mapping/model_3.ply) / [カメラ位置CSV](../../20261031_nanyo_001/visual_mapping/model_3_poses.csv)

### 20261031_nanyo_002

登録 33/320画像（10.3%）、モデル 3個。未登録はframe_status.csvに残した。

![音響と水深](../../20261031_nanyo_002/evaluation/acoustic_depth.png)

![相対位置候補](../../20261031_nanyo_002/relative_position/relative_candidates.png)

[実映像と光点の確認](../../20261031_nanyo_002/relative_position/light_review.jpg)

![地図登録範囲](../../20261031_nanyo_002/visual_mapping/coverage.png)

代表画像は船内または船体付近の気泡が中心。今回の点群を静的な海底地図として採用する根拠は得られなかった。全断片は失敗・不確実性を確認する診断用として保持する。

モデル0: 2画像、39点、平均再投影残差 0.347 px（物理精度ではない）。

![点群](../../20261031_nanyo_002/visual_mapping/model_0.png)

船内の2画像。水中内部校正の適用条件外であり、海底地図としては不採用。

![登録画像の先頭・中間・末尾](../../20261031_nanyo_002/visual_mapping/model_0_views.png)

[PLY点群](../../20261031_nanyo_002/visual_mapping/model_0.ply) / [カメラ位置CSV](../../20261031_nanyo_002/visual_mapping/model_0_poses.csv)

モデル1: 11画像、810点、平均再投影残差 0.427 px（物理精度ではない）。

![点群](../../20261031_nanyo_002/visual_mapping/model_1.png)

気泡とロープ状の物体が画面を占め、静的な海底形状の復元とは確認できない。診断用。

![登録画像の先頭・中間・末尾](../../20261031_nanyo_002/visual_mapping/model_1_views.png)

[PLY点群](../../20261031_nanyo_002/visual_mapping/model_1.ply) / [カメラ位置CSV](../../20261031_nanyo_002/visual_mapping/model_1_poses.csv)

モデル2: 20画像、1294点、平均再投影残差 0.422 px（物理精度ではない）。

![点群](../../20261031_nanyo_002/visual_mapping/model_2.png)

船体付近の気泡が画面を占め、静的な海底形状の復元とは確認できない。診断用。

![登録画像の先頭・中間・末尾](../../20261031_nanyo_002/visual_mapping/model_2_views.png)

[PLY点群](../../20261031_nanyo_002/visual_mapping/model_2.ply) / [カメラ位置CSV](../../20261031_nanyo_002/visual_mapping/model_2_poses.csv)

10画像未満のモデルは低支持の小断片として保持し、完成した地図とは扱わない。

## 検証・再現

関連自動試験17件合格。UBXチェックサム、RTKゼロ、候補数・距離ノルム、全地図サンプルの登録数を確認。2D図はPNG/SVG/figure.pickle/plot.jsonを保持。設定config/20261031_nanyo_000/evaluation.json、スクリプトscripts/*nanyo*.py。

入力原本・旧解析は変更していない。比較局、PPK設定とコマンド、ダウンロード要求、ハッシュ、マッピング実行ログは実験ID配下に保持。再計算はREADMEのコマンドを参照。

[GEONETデータ提供元](https://terras.gsi.go.jp/) / [RTKLIB EX 2.5.1](https://github.com/rtklibexplorer/RTKLIB/releases/tag/v2.5.1)。