# 長田高校プール・南予海上実験の統合研究レポート

対象は2026年10月28日の長田高校プール実験と、南予水産技術センターの協力を得て10月31日・11月1日に行った海上実験。整理日は11月3日で、新しい取得実験ではない。

**[統合レポートを開く](../../results/20261103_field_review_000/research_review/report.html)** ／ [Markdown](../../results/20261103_field_review_000/research_review/report.md)

| 章 | 内容 |
|---|---|
| [1 目的と主要結果](../../results/20261103_field_review_000/research_review/01_overview.html) | 発表へ使う結果の範囲、全体の読み方 |
| [2 データと採用版](../../results/20261103_field_review_000/research_review/02_data.html) | 実験区間、移行状態、旧結果と修正版の採用関係 |
| [3 方法](../../results/20261103_field_review_000/research_review/03_methods.html) | 時刻、座標、校正、欠測、RMSの定義 |
| [4 長田高校プール](../../results/20261103_field_review_000/research_review/04_pool.html) | GNSS、位置の座標修正、子機SLAM、壁地図、包絡線 |
| [5 南予](../../results/20261103_field_review_000/research_review/05_nanyo.html) | 6静止窓、姿勢補正、GNSS、方向別変動、受信波形の欠落 |
| [6 シミュレーション比較](../../results/20261103_field_review_000/research_review/06_simulation.html) | 実測幾何での56条件、基本雑音と光減衰モデル、比較の制約 |
| [7 マッピング](../../results/20261103_field_review_000/research_review/07_mapping.html) | 採用地図、航法との時間対応、004・005の追加再構成、未成立の尺度 |
| [8 考察](../../results/20261103_field_review_000/research_review/08_discussion.html) | 主張できること、次の実験、発表用の表現 |
| [9 図表と再現性](../../results/20261103_field_review_000/research_review/09_reproducibility.html) | 単体図、編集元、元報告、設定・コード・検証記録 |

各取得実験と区間の`results/<id>/research_review/report.md`からも統合報告へ移動できる。過去の報告は履歴として保持する。プールは画像回転修正版を採用し、旧版の値と混ぜない。南予の子機絶対位置と親子相対位置、平滑化した航法と静止観測のRMSも区別する。

## 追加解析と保存先

- 統合解析：`results/20261103_field_review_000/research_review/`。静止CSVの再集計、変動の分解、モデル比較、保存データと既存報告の台帳。
- 追加局所地図：`results/20261101_nanyo_004/mapping_refinement/field_review_20261103_2hz/`と`results/20261101_nanyo_005/mapping_refinement/field_review_20261103_5hz/`。PLY・姿勢CSV・3D viewer・処理ログを保存。正の尺度が成立せず、現行の実寸地図には加えていない。
- 波形監査：南予7区間の`acoustic_audit_20261103/`。保存包絡線の連続性と取得側の欠落カウンタを照合する。
- 位置フォールバック：統合結果は`results/20261103_field_review_000/position_fallback/report.html`、各入力実験のCSV・manifest・reportは`results/<id>/position_fallback/`。下流解析の明示的な入口は`data/processed/<id>/position_fallback/positions.csv`で、既存成果の自動置換・地図や映像の再生成はしない。

### 位置フォールバックの再生成

設定`config/20261103_field_review_000/position_fallback.json`は長田高校002〜004と南予7区間を列挙する。既存の有限な共同推定位置（修正済みプール位置、レビュー済み南予軌跡）を同じ時刻格子で最優先し、次に`optical_imu_depth`、`acoustic_imu_depth`を選ぶ。IMUは光線と公称レバーアームの回転に使うが、加速度積分は行わない。プールの磁気ENUと南予の真ENU、南予の回転済みCADレバーアームを保持する。

音響距離と深度だけでは方位が決まらない。音響候補は独立した光学由来の音響点世界方位を最大3秒保持し、音響候補自身で方位を更新しない。初期方位がなければ無効。方位保持の1/3/5秒感度も報告する。水面深度offsetは有効な共同推定位置を持つ過去3件以上の異なる音響イベントから因果的に初期化し、直近seedから過去30秒の中央値で更新する。最終更新から60秒を超えたら無効とし、現在行や未来の共同推定値を同じ行のフォールバックへ使わない。閾値は深度>0.3 m、光線の鉛直成分絶対値>=0.15、光学前方距離<=50 m。幾何学的に解けない距離・深度の円は無効。

評価は平滑化・トレンド除去・追加外れ値除去なし。旧方式・選択方式・全候補の水平／各軸／3Dの平均周り母RMSを、全有効イベントと方式間共通イベントに分ける。別掲の固定7窓はプール002と設定済み南予6窓で、手振り窓はラベルを残し集約から除外。単独音響は中心化した距離RMS（m）、単独光学はカメラ方位と世界方位の角度RMS（deg）を報告し、単独センサーを3D位置と呼ばない。10:15の先行14秒も観測範囲に残す。真値がないため精度・真値誤差とは表現しない。

```powershell
python scripts/evaluate_position_fallback.py --experiment 20261103_field_review_000
python scripts/verify_position_fallback.py --experiment 20261103_field_review_000
python scripts/write_field_review.py --experiment 20261103_field_review_000
python -m pytest tests/test_position_fallback.py tests/test_position_fallback_ngt.py tests/test_position_fallback_evaluation.py -q -p no:cacheprovider
```

新規の2D図はPNG/SVG/PDFと`.figure.pickle`・`.plot.json`を保存し、既存のブラウザー編集経路を使う。検証器で時刻格子、方式優先順位、有効フラグ、因果的seed、RMS母数、出力ハッシュと図の編集・出力条件を確認してから受け入れ結果を記録する。

本文・計測CSV・動画・点群・中間モデルはローカル成果物でGit管理外。公開対象は索引、設定、登録台帳、生成・検証コードとテンプレートに限る。

## 再生成

Python 3.11以上で`python -m pip install -e ".[analysis,reports]"`により集計・HTML生成の依存を用意する。追加SfMは既存のvision/mapping依存とpycolmap 4.2.1を使う。

```powershell
python scripts/analyze_field_review.py --experiment 20261103_field_review_000 --sim-root ../aquabeacon-sim
python scripts/write_field_review.py --experiment 20261103_field_review_000
python scripts/verify_field_review.py --experiment 20261103_field_review_000
```

波形監査と追加SfMの実行条件は第9章と各manifestを参照する。全体テストと個別検証の成否は、統合報告の`verification.json`・`execution_summary.json`・`browser_verification.json`に保存する。再処理には必要な入力がローカルに存在することを確認する。原本・初回結果・シミュレーターの設定を上書きしない。
