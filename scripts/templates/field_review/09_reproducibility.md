# 9 図表と再現性

## 9.1 発表と研究レポートの素材

{{asset_table}}

2次元の定量図はPNGだけでなく、SVG、Matplotlib Figure、plot.jsonを保持する。新しい比較図は共通の`save_editable_figure`で保存した。軸・凡例・文字・系列・範囲・寸法・配置の調整と設定保存・再読込、PNG/SVG/PDF出力は既存の共通エディタを使う。CSVと生成コードから数値の出所へ戻れる。

```powershell
python -m aquabeacon_analysis.editable_plots results/20261103_field_review_000/research_review/observed_simulation_scatter.figure.pickle
```

今回追加した3D図は局所単位の点群表示で、2Dグラフ用編集UIの必須対象ではない。PLY、COLMAPモデル、元画像のPTSとハッシュ、オフラインviewerを併せて保存した。局所モデルを同じ場所へ重ねて、つながった形状に見せる操作はしていない。

## 9.2 再生成

Python 3.11以上と`.[analysis,reports]`の依存を使う（このPCではPython 3.13）。追加SfMにはOpenCV、Pillow、pycolmap 4.2.1も必要である。HTML整形の`Markdown`を`reports`オプションとして登録した。

```powershell
python scripts/analyze_field_review.py --experiment 20261103_field_review_000 --sim-root ../aquabeacon-sim
python scripts/write_field_review.py --experiment 20261103_field_review_000
python scripts/verify_field_review.py --experiment 20261103_field_review_000
```

包絡線の監査は下記のように対象7区間を同じ呼び出しへ明示する。区間ごとの監査に加え、統合表をその対象集合で生成する。追加SfMの実行条件は各runのmanifestに記録している。`refine_nanyo_mapping.py`は既存runへの上書きを拒否するため、再試行時は別名を指定する。

```powershell
python scripts/audit_nanyo_envelopes.py --experiment 20261031_nanyo_001 --experiment 20261031_nanyo_002 --experiment 20261101_nanyo_001 --experiment 20261101_nanyo_002 --experiment 20261101_nanyo_003 --experiment 20261101_nanyo_004 --experiment 20261101_nanyo_005
```

```powershell
python scripts/refine_nanyo_mapping.py --experiment 20261101_nanyo_005 --run new_trial --start 60 --stop 90 --hz 5 --runtime data/interim/20261028_ngt_004/wall_mapping/runtime --mapper-seconds 120
python scripts/review_mapping_refinement.py --experiment 20261101_nanyo_005 --run new_trial --runtime data/interim/20261028_ngt_004/wall_mapping/runtime
```

上記runtimeはこのPCで既に存在したpycolmap 4.2.1を再利用した場所であり、プールの校正を南予へ暗黙適用する指定ではない。別PCは同バージョンを明示して用意する。raw/interimがない状態で可能な追加計算と、原本からの再同期・PPK再計算を区別する。

## 9.3 検証記録

{{verification_summary}}

解析リポジトリ全体のテストは294件成功・1件失敗だった。失敗は今回変更していない`anchor_nanyo_maps.py`の既存3D図が`savefig`を使うことを検出するテストである。今回の追加2D図は共通保存処理を使用している。全体Ruffは既存`prepare_nanyo_geometry.py`のtimezone未指定2箇所で失敗し、今回追加したPythonファイルのRuffは通過した。シミュレーターの数式・往復変換・無雑音・層分離・共分散の44テストと、ワークスペース19リポジトリの構成検査も通過した。

移行台帳の検証、数値再計算の一致、モデルの線形近似との一致、図の編集元、ローカルリンク、追加モデルの有限性・登録数・時刻対応を確認した。既存全体テストに以前から残る失敗があれば、新規変更の検証と分けて記録する。原本の全処理を初めから実行したという意味ではない。

[検証の機械可読記録](verification.json)／[ブラウザ検証](browser_verification.json)／[入力・コード・設定のハッシュ](manifest.json)／[レポート生成来歴](report_manifest.json)／[実行とプロセス終了の記録](execution_summary.json)

## 9.4 元資料への索引

{{source_table}}

全155本の元レポートは[既存レポート台帳](existing_report_catalog.csv)、実験ごとの保存量は[データ台帳](data_inventory.csv)、不足参照は[参照監査](existing_missing_links.csv)に保存した。旧版は過去の判断を追える状態に保ち、現在の結論と混ざらないよう入口と採用表を設けた。
