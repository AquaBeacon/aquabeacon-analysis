# 実験機体の形状参照（2026-09-22）

実験に使用した機体は `../mechanical/assemblies/parent_simple` と
`../mechanical/assemblies/child_simple_assembly`。解析用の取付面情報と原典の
SHA-256は [`../calibration/experiment_body_geometry_20260922.json`](../calibration/experiment_body_geometry_20260922.json)
に保存する。`Body_Models` はこのリポジトリの兄弟ディレクトリで、Git管理されていないため、
ファイルのハッシュを参照版の識別子にする。

| 機体 | モデル座標系 | 確認できたカメラ取付基準 |
|---|---|---|
| 親機 | 箱底中心原点、底板上面 z=0、mm | 底板下面の穴群中心 x=-48, y=0 mm、ピッチ22×32 mm |
| 子機 | x=左右、+y=カメラ正面、+z=上、チューブ下端 z=0、mm | 前面座面中心 (0,82,100) mm、4穴 x=±20, z=80/120 mm |

親機の `parent_simple` 組立には外付けカメラ本体・取付具・内部基板が入っていない。
子機の `child_simple_assembly` 組立にもカメラ、TD04A、ライト、内装と各専用マウントは
入っていない。内装は別の `mechanical/assemblies/child_interior`、親機内装は
`mechanical/assemblies/parent_interior_p1` にある。したがって上表は**取付面の位置**であり、カメラ光学中心や基板上IMU、
GPSアンテナ、圧力センサ、ライト中心、音響素子の位置ではない。

子機の `child_interior` は、チューブ下端 z=0、+YをLCD表示面とする座標系で、
C-MAIN基板のモデル配置は平行移動 (44,1,20) mm、回転 (0,-90,-90)°。
実験時にはC-MAIN基板をカメラ取付面と同じ側へ固定した（ユーザー確認）。
子機外装のカメラ取付面も+Y側だが、両モデルを組み付けた後の光学中心・基板IMU位置は未測定。

親機の `parent_interior_p1` は箱の左下手前を原点とし、`parent_simple` の箱底中心へは
(-105,-115,0) mmの並進で対応する。モデルのP-MAIN平行移動は
(15.5,103.5,24.8) mm、Spresenseは(188,15.5,24.8) mm・z軸90°回転。
実験時にはSpresenseをP-MAINと同じ側の反対の角に置き、SDカードを外向きにした
（ユーザー確認）。`parent_interior_p1` は部品構成と概略配置の参照に使うが、
親機内装は実験時に変更されたため、モデルの基板位置・回転を実測値として転記しない。
Spresenseの3DモデルはSDスロットとIMU位置を仮に置いた簡易形状であり、
「SDカードが外向き」だけから実IMUの軸と中心は定まらない。

カメラ光学中心・姿勢、各IMU/基板上の測定基準点、音響素子、ライト、圧力センサの
実装位置を実測または実験時の組立記録で特定してから、同一body frameでSE(3)変換を校正する。
現行の相対位置・GPS・圧力・VSLAM結果にはレバーアーム補正を適用せず、
出力に記載済みの未校正条件を維持する。

実機との目視照合用3Dシーンは
`results/20260922_cmain_023/body_geometry_review/body_geometry_review.blend`、
確認図と未確認点は同フォルダの`report.md`に置く。外装・内装モデルを重ねた
表示であり、実機と合っているかの確認が済むまでモデル座標を実測値として採用しない。
