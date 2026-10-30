# 今後の実験用CADモデルとセンサー配置

2026-10-27に `cad_20261027` として採取した。対象は今後の実験。
既存2026-09-22のデータ・校正・Blenderシーンには適用していない。
`child_child_horizontal_plate_frame` に対応するCADフォルダ名は
`mechanical/assemblies/child_horizontal_plate_frame`。

## 保存したもの

- [数値スナップショット](../calibration/rigs/cad_20261027.json): 4構成・672部品、
  9つのセンサー参照点、原典ファイルのSHA-256とGit SHA・変更状態。
- `data/interim/cad/cad_20261027/`: `parent.glb` / `child.glb`（独立body frame）、
  `rig.json`、`sensor_references.csv`、`assets.json`。
- `blender/output/cad_20261027/`: `cad_rig.blend`、`cad_rig.glb`、
  `overview.png`、親子それぞれの`*_exterior.png` / `*_interior.png`、`manifest.json`。

CADのSTLを部品ごとに配置する。外装と内装で重複する容器、および名前に
keepout / reservation / sweepを含む予約包絡は除外し、理由をJSONに保存する。
SpresenseのIMU包絡とカメラケースの仮形状は元のCADの一部として残す。
TD04Aの旧本体モデルは使わず、現行の固定治具だけを表示する。
メッシュ・Blenderファイルは生成物でGitに入れず、配置JSONと生成コードを管理する。

## 座標系と解析用インターフェース

数値ファイルはm、列ベクトル、`p_body = T_body_component @ p_component`。
`T_body_component` は部品モデルの設計上の配置で、センサー測定軸の変換ではない。

| body frame | 原点・軸 | 内装→bodyの変換 |
|---|---|---|
| `parent_body` | parent_exteriorの箱底中心、+Z上。X/Yは同CADの軸 | 同一箱モデルを基準に並進(-105,-115,0) mm |
| `child_body` | 横置き外装の原点、+X右・+Yドーム・+Z上。チューブ軸Z138 mm | 同一チューブを基準にRx(-90°)、並進(0,-100,138) mm |

内装と外装の変換は、同じ購入品・同じローカル座標の配置行から計算する。
子機のチューブは軸対称なので、これはCADのロール基準の整合であり、
実機の内部基板がそのロール角で固定されたことの実測確認ではない。
親子のworld frameや水平方位はこの段階では定めない。

glTFは標準のY-upで、`[x,y,z]_gltf = [x,z,-y]_body`。
Blenderの通常読込後はCADと同じZ-up・mになる。追加のmm換算や軸補正は不要。

## 必須センサーの確認結果

下表は**CAD参照点**のbody座標（mm）。光学中心・測定中心・音響中心ではない。
値の種類を `reference_kind` に、制限を `limitation` に記録した。

| ID | body内の参照座標 X/Y/Z（mm） | 確認した根拠・残る制限 |
|---|---|---|
| parent_camera | -40 / -40 / -91.95 | 外装camera_case_referenceのモデル原点。ケース厚41 mmは実測、幅・高さ・M5軸オフセットは仮値。光学中心・光軸・内蔵IMU位置は未確定 |
| child_camera | -183.95 / 95 / 90 | 横置き外装side_camera_case_referenceのモデル原点。上記と同じ制限 |
| parent_spresense | 97.34 / 93 / 67.2 | parent_hand_serviceのSpresense拡張基板モデル原点。公式Sony STEP、z方向の基板嵌合高さは仮値 |
| parent_imu | 75.75 / 68 / 81.65 | Spresenseの20.6×50×9.1 mm仮包絡の中心。各IMUの素子位置・測定軸ではない |
| parent_magnetometer | -51.11 / 92.7 / 72 | LIS3MDL公式EAGLEのIC1=(12.7,8.89)、配置回転R270。XYを基板下面Z=0へ投影し、assemblyのZ90°と持上げ16 mmを反映。ダイ高さ・測定軸は未確定 |
| child_imu | 29 / -56 / 133.4 | hardwareの実PCBでBNO085のJ_BNO1=(24,15)、角度0°を確認。KiCad Y反転・基板厚1.6 mm・内装配置・横置き変換を反映した、C-MAIN上面のフットプリント原点。モジュール／チップ中心ではない |
| child_magnetometer | 29 / -56 / 133.4 | 同じBNO085モジュールの磁気観測。個別のダイ中心・軸とログでの磁気使用設定は別途確認 |
| parent_td04a | 53 / -55 / -85 | Rev.6治具のM4穴列中点。設計の送受信面は-Z。12 mmピッチ・耳厚3 mmは仮値、本体・音響中心は未確定 |
| child_td04a | 154 / -74 / 173 | Rev.6治具のM4穴列中点。設計の送受信面は+Z。上記と同じ制限 |

C-MAINの古い部品説明にあるJ_BNO1座標(11,61.2)は採用せず、現在の
`hardware/pcb/CHILD_MAIN/CHILD_MAIN.kicad_pcb`から(24,15)を読む。
同PCBのSHA-256を記録し、版が変わったら再採取を求める。
SpresenseはP-MAIN基板と別の独立ロガーであり、P-MAINの原点をIMU位置に代用しない。

全9行の `measurement_center_body_m` / `T_body_sensor` は `null`。
`measurement_axes_verified` / `as_built_verified` はfalse。
`aquabeacon_analysis.cad_rig.sensor_transform(profile, sensor_id)` はこの状態で
ValueErrorを返し、仮の参照点によるレバーアーム補正を拒否する。
CADの部品回転やEAGLEのR270を、センサー出力軸の回転として転記しない。

## 再生成と今後のデータでの利用

```powershell
python -m pip install -e ".[dev,cad]"
python scripts/prepare_cad_rig.py --rig-id cad_20261027
python scripts/prepare_cad_rig.py --rig-id cad_20261027 --verify
& 'C:\Program Files\Blender Foundation\Blender 5.2\blender.exe' -b -t 4 --python blender/create_cad_rig_scene.py -- --rig-id cad_20261027
```

同じIDは既存のスナップショットを使い、原典ハッシュを検査してGLBだけ再生成する。
CADを変えたら新しいID（例 `cad_20261028`）で再採取する。
`--verify` は入力の変更を検出するだけでファイルを書き換えない。
コマンドはインストール後にリポジトリ直下から実行する。

新データのexperiment IDが決まってから明示的に紐付ける（下記は書式例、今回は未実行）。

```powershell
python scripts/prepare_cad_rig.py --rig-id cad_20261027 --bind-experiment FUTURE_EXPERIMENT_ID
```

`results/FUTURE_EXPERIMENT_ID/cad_geometry/` にrig.json、manifest.json、report.mdを保存する。
同じ紐付けの上書きは拒否する。センサー補正・動画処理・過去解析は実行しない。
新しい解析側ではこのスナップショットとハッシュを結果に記録し、
実測外部校正が揃ってから `T_body_sensor` を別の校正版として確定する。

Blenderでは `cad_rig.blend` の `parent_CAD` / `child_CAD` コレクションを利用できる。
rootの `parent_body` / `child_body` に姿勢・位置を与える。
自動生成シーンの親子間隔と親機の高さ補正は表示用であり実験位置ではない。
`__REFERENCE_ONLY` のEmptyを選択するとCAD参照点と制限を確認できる。
他のBlenderスクリプトから `blender/cad_assets.py` の `load_cad_body()` も利用できる。

## 新実験前に残る実機確認

1. 実機がこの4構成・このCAD版と一致するか、子機の内装ロール角、基板向き、
   Spresenseの積層高さと取り付け位置を記録する。
2. カメラのケース内の光学中心・光軸・M5位置、内蔵IMUを利用する場合はその中心と軸を特定する。
3. Spresenseの各IMU測定中心／出力軸、BNO085の実装高さ・チップ中心／出力軸、
   LIS3MDLの測定中心高さ／出力軸を特定する。
4. TD04Aの実取付穴・耳厚と、穴列から送受信面／音響基準点までのオフセットを測る。
   現CADには本体が無いため、その外形表示には実物寸法か正しいモデルが必要。
5. 磁気は完成・通電・送信状態で校正し、近傍アンテナの磁石・鋼板・配線の影響を確認する。

これらはCAD前処理から得られない測定情報。配置参照と未確認箇所を残した状態で、
将来のログ仕様・時刻同期・内部校正と合わせて外部校正を行う。

## 検証記録

2026-10-27に次を実施した。Blenderは表示確認に限定し、正式な評価計算には使用しない。

| 確認 | 結果 |
|---|---|
| `python -m pytest -q -p no:cacheprovider` | 115件合格。うち本CAD機能17件。座標演算順・単位・鏡像と面向き・GLB共有形状・未測定extrinsic拒否・入力変更検出・将来実験への紐付けと上書き拒否・verifyの無変更を検証 |
| `python -m ruff check .` | 合格 |
| `git diff --check` | 差分の空白エラーなし |
| `prepare_cad_rig.py --verify` | 全記録済み原典CAD・子機PCBのSHA-256が一致 |
| Blender 5.2.1 LTS、背景実行、4スレッド | 正常終了。親235・子437メッシュ、親5・子4参照Emptyを保存 |
| 実際のBlender読込後の原点・外形 | 親の原点最大2.11e-8 m／外形2.09e-8 m、子の原点最大2.98e-8 m／外形2.88e-8 m。いずれも1 µm以内 |
| 画像目視 | overview、親子のexterior／interiorの計5枚を確認。親の箱・左右フロート・カメラ、横置き子機のフレーム・容器・カメラ治具、外装を除いた内部配置が表示され、CAD部品色と透過が反映される。子機外観の画面端の切れを修正し再確認 |
| 原典・既存実験 | mechanical原典は読込のみ。本処理はrawと既存実験の解析・旧校正に書き込まない。紐付けテストは一時ディレクトリのみで実施 |
| プロセス | 起動した背景Blenderと制御Pythonはすべて終了確認済み。常駐プロセスなし |

詳細な数値と生成スクリプト・アセットのハッシュは
`blender/output/cad_20261027/manifest.json`、原典は配置JSONに保存した。
実機との一致、センサー測定中心・測定軸、実物の締結や強度は今回の確認範囲に含めず、
上の実機確認項目として残している。


## 2026-10-28 学校プール実験の参照

今回の使用構成として、子機 `child_horizontal_plate_frame` / `child_interior`
（BNO085、カメラ、ライト、TD04A）、親機 `parent_exterior` / `parent_hand_service`
（Spresense IMU、磁気センサ、カメラ、TD04A、RTK/PPK GNSS）を記録した。
参照元はAquaBeacon-OSS直下の `mechanical/assemblies/`。今後の可視化もここを確認する。

旧 `cad_20261027` はparent_hand_serviceのplacement.json更新を検出したため、
現在の4構成を `calibration/rigs/cad_20261028.json` へ新規採取した。
669部品、9センサー参照点。子機ライトの構成部品も保持。
新GLBは `data/interim/cad/cad_20261028/parent.glb` と `child.glb`。
全収録20261028_ngt_000に明示紐付けし、校正済み測定中心としては使っていない。
このtaskでは新Blenderシーンを起動せず、数値スナップショットとGLBを保存した。

日本語Git状態の文字コードを明示する再確認コマンド:
`python -X utf8 scripts/prepare_cad_rig.py --rig-id cad_20261028 --verify`。
