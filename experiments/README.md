# 実験台帳

派生登録：[`20261103_field_review_000`](20261103_field_review_000/experiment.json)は、
長田高校プール（10月28日）と南予（10月31日・11月1日）の横断整理・モデル比較。
IDの日付は整理日であり、新しい取得日ではない。入力実験・区間・静止窓を専用設定で明示し、原本参照は各取得台帳へ戻る。

新しい解析は `experiments/<experiment_id>/experiment.json` を入口にする。
生データの配置は [../data/README.md](../data/README.md)、処理コマンドは
[../README.md](../README.md#別の実験を追加するとき) を参照。

## 登録済み

| 実験ID | 内容 | 台帳 |
|---|---|---|
| `20261028_ngt_000` | 学校プール・全収録・校正監査 | [experiment.json](20261028_ngt_000/experiment.json) |
| `20261028_ngt_001` | 親動画4:00–8:00・親GNSS静止 | [experiment.json](20261028_ngt_001/experiment.json) |
| `20261028_ngt_002` | 親動画17:30–18:30・子機静止 | [experiment.json](20261028_ngt_002/experiment.json) |
| `20261028_ngt_003` | 親動画18:30–21:00・範囲限界 | [experiment.json](20261028_ngt_003/experiment.json) |
| `20261028_ngt_004` | 親動画21:00–25:40・親子同時移動 | [experiment.json](20261028_ngt_004/experiment.json) |
| `20260922_cmain_022` | 9月22日・子機基準区間022 | [experiment.json](20260922_cmain_022/experiment.json) |
| `20260922_cmain_023` | 9月22日・子機基準区間023 | [experiment.json](20260922_cmain_023/experiment.json) |
| `20260922_cmain_024` | 9月22日・子機基準区間024 | [experiment.json](20260922_cmain_024/experiment.json) |
| `20260922_pmain_022` | 9月22日・親機ToF区間022 | [experiment.json](20260922_pmain_022/experiment.json) |
| `20260922_pmain_023` | 9月22日・親機ToF区間023 | [experiment.json](20260922_pmain_023/experiment.json) |
| `20260922_pmain_024` | 9月22日・親機ToF区間024 | [experiment.json](20260922_pmain_024/experiment.json) |
| `20260924_parent_camera_calibration` | 9月24日・水中チェッカーボード校正 | [experiment.json](20260924_parent_camera_calibration/experiment.json) |

P_MAINとC_MAINの番号が同じでも、同一区間とは限らない。対応はUTCの重なりから確認する。
一部のC_MAIN・Spresense・カメラ原本はこのcheckoutには存在せず、台帳はそれを補完しない。
一覧表示は `python -m aquabeacon_analysis list-experiments`、不足参照の確認は
`python -m aquabeacon_analysis show-experiment <experiment_id>`。

## 台帳形式（schema_version: 1）

| フィールド | 意味 |
|---|---|
| `experiment_id` | フォルダ名と一致する一意なID。新規は `YYYYMMDD_location_NNN` |
| `description` | 実験の目的・条件の短い説明 |
| `raw_root` | リポジトリ相対の原本ルート。`data/raw/` 内に限定 |
| `raw_sources` | 所属が確認できた原本ファイル・機器フォルダの参照。欠損はshowで表示 |
| `c_main_suffix` / `p_main_suffix` | 実際の5桁ログ番号文字列、未確認・対象外はnull。ID末尾から推測しない。初期化時は `--c-main-suffix` / `--p-main-suffix` で指定可能 |
| `config_paths` | 実験用設定の明示参照。例: `config/<experiment_id>/analysis.json` |
| `calibration_paths` | 使用条件を確認する校正の明示参照。未校正なら空配列 |
| `notes` | 時刻基準・撮影条件・制約など |

パスは `/` 区切りで保存し、絶対パスやリポジトリ外への参照を拒否する。
台帳ファイルと出力先を別の場所へ向けるsymlink/junctionも拒否する。
入力原本は共有参照できるが、出力先は台帳で上書きできず、必ず実験IDから決まる。
`config_paths` と `calibration_paths` の登録は宣言であり、数値処理のパラメータを自動で差し替えない。
適用値と補正内容は各解析manifestに記録する。

## 従来ファイル

`example.yaml` は初期の参考様式であり、主CLIの台帳としては読み込まない。
`20260922_camera_setup.json`、`light_review_20260922.json`、`light_validation_20260922.json` は
9月22日の既存スクリプトが参照する資料として現在位置に保持する。新規実験へ暗黙に流用しない。

追加登録：[`20261029_child_axis_003`](20261029_child_axis_003/experiment.json) は子機軸校正003の監査。IDの日付は登録日、収録UTCは未確定。29567はマーカー参照に基づく。

追加登録：[`20261029_parent_axis_011`](20261029_parent_axis_011/experiment.json)：親機4姿勢・カメラ0025。I00013/T00013をparent_sessionで選択。

追加登録：[`20261031_nanyo_000`](20261031_nanyo_000/experiment.json)：Nanyo海上実験の全収録。親機DJI2本、子機DJI1本。原本Nanyo_ocean_testを参照。

Nanyo切出し区間：[`20261031_nanyo_001`](20261031_nanyo_001/experiment.json) と [`20261031_nanyo_002`](20261031_nanyo_002/experiment.json)。水深または音響候補で抽出し前後10秒を付加。

Nanyo 000/001/002の追加結果：[子機絶対位置・位置拘束地図](../results/20261031_nanyo_000/absolute_position/report.md)。固定取付確認により保存済みFRD軸を適用し、真北ENU・CAD基準点補正を追加。

Nanyoの現行入口：[センサー航法・写真地図・同期親子映像](../results/20261031_nanyo_000/navigation_replay/report.md)。config/20261031_nanyo_000/navigation_fusion.jsonを明示使用。SfMの位置・速度を航法へ逆入力しない。

Nanyo day2：20261101_nanyo_000（全収録）、001〜005（子機入水 AND 親機測距）。原本Nanyo_ocean_test_day2、カメラ再起動前後を独立同期。
