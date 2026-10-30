# Data

実験データ本体はGitリポジトリでは管理しない。

## 新規実験の配置

原本・中間・処理済みを分け、その中を同じ実験IDでそろえる。
新しい取得データを `raw/` 直下へ追加しない。動画のMP4とLRFは同じ機器フォルダにまとめる。

```text
experiments/<experiment_id>/experiment.json   # Git管理する実験台帳
config/<experiment_id>/                      # 必要な実験固有設定だけ
calibration/<experiment_id>/                 # 必要な実験固有校正だけ
data/
  sample/                                   # 公開可能な小型サンプルのみ
  raw/<experiment_id>/                       # 取り込み担当者が配置。以降は編集禁止
    C_MAIN/
    P_MAIN/
    Spresense/
    P_MAIN_CAM/
    C_MAIN_CAM/
  interim/<experiment_id>/                   # 同期済みCSV・動画等
    c_main/
    spresense/
    camera_telemetry/
    camera_video/
    refined_sync/
    <analysis_name>/<run_name>/              # 新規のSLAM等の派生物
  processed/<experiment_id>/tof_window/      # 解析へ渡す前処理済み入力
results/<experiment_id>/<analysis_name>/     # レポート・図・位置推定・manifest
```

未使用のセンサフォルダや設定・校正の空ファイルを作る必要はない。
同じ日に複数の試行を取得した場合も `YYYYMMDD_location_NNN` のNNNを分ける。
実験IDのNNNと機器ログの番号は独立であり、台帳の `c_main_suffix` 等で明示する。
1つの機器フォルダ内に別取得日のログを混ぜない。
同じ原本を複数試行で共有する場合はコピーせず、台帳で既存の `raw_root` と実際のログ番号を参照する。
`sync-raw` の `raw_root` 直下には、上図の機器名フォルダが必要。

## 追加手順

1. `python -m aquabeacon_analysis init-experiment <experiment_id> --c-main-suffix <実ログ番号>`。
   台帳とinterim・processed・resultsの実験別フォルダを作る。既存ID・既存出力は上書きせず拒否する。
   **rawフォルダの作成・コピー・移動は行わない。**
2. 原本の取り込み担当者が `data/raw/<experiment_id>/` に配置する。解析エージェントはrawを変更しない。
3. 台帳へ実際の設定・校正の参照先と撮影条件を記入する。新規台帳の校正一覧は空で、過去値を引き継がない。
4. `python -m aquabeacon_analysis show-experiment <experiment_id>` で配置と不足参照を確認する。
5. 主CLIを `--experiment <experiment_id>` 付きで実行する。追跡処理には同実験の目視確認点も必要。

台帳を登録しても校正値は自動適用されない。各解析が実際に読んだ値は解析manifestを正とする。
登録した設定・校正が欠けていれば主CLIは処理前に停止する。
成功した主CLI処理には `experiment_context_<command>.json` を添付し、台帳のsnapshot・SHA-256・登録参照の
SHA-256・Git SHAを残す。原本動画全体の再ハッシュや再解析は整理コマンドでは実行しない。

## 既存データの所属（2026-10-27時点）

既存原本と生成物は、manifest内のパスと従来スクリプトの互換性を保つため移動していない。
台帳は [../experiments/README.md](../experiments/README.md) からたどれる。

| 既存の配置 | 所属・扱い |
|---|---|
| `raw/P_MAIN_HEALTH_RANGE_00022.csv` ～ `00024.csv` | 2026-09-22のP_MAIN 022～024。各抽出manifestで確認 |
| `raw/DJI_20260924183857_0021_D.MP4` と `.LRF` | 2026-09-24の水中カメラ校正。9月22日の本実験とは別 |
| `raw/V00022.BIN` ～ `V00024.BIN` | 所属・形式の確証がないため未割り当て。番号だけでは関連付けない |
| `interim/20260922_cmain_022/` ～ `024/` | 2026-09-22の同期済み入力。一部の原本はこのcheckoutに存在しない |
| `processed/20260922_cmain_*/` と `20260922_pmain_*/` | 対応する実験のToF窓入力 |
| `interim/orbslam3*/<experiment_id>/` | 既存のSLAM試行。従来の方式→実験ID配置のまま保持 |
| `interim/orbslam3_bin/`、直下のbuild log・試写動画・診断画像 | 従来の共通実行資産・検証用生成物。センサ原本ではない |

既存スクリプトには `20260922`、校正日、全試行一覧が固定されたものがある。
新規実験には入力ルート・対象ID・校正条件を確認して対応させる。
主CLIの6解析コマンドとP_MAIN抽出は実験指定に対応済み。従来の `scripts/` 全体の自動移行はしていない。

P_MAINは台帳に実ログの `p_main_suffix` を登録し、
`python scripts/extract_p_main_tof.py --experiment <experiment_id>` で抽出する。
新規は `raw/<experiment_id>/P_MAIN/`、既存の平置き台帳は `raw/` からその1ファイルだけを読む。
C_MAINも持つ台帳では、先に同実験の `preprocess-tof` を実行する。そのToF窓とP_MAINのUTCが
重なることを確認し、同じ窓のCSVを同実験の `processed/<id>/tof_window/p_main/` へ出す。
親機のみの台帳ではP_MAIN自身の有効ToF窓を使う。
manifestと `experiment_context.json` は `results/<id>/p_main_preprocessing/` へ分け、C_MAINの前処理manifestを上書きしない。
別実験への自動結合はしない。従来の全件UTC結合が必要な場合だけ `--all-experiments` を指定する。

## Gitと保全

- `data/raw/` は作業中に移動・改名・削除・上書きしない。
- `raw/`・`interim/`・`processed/` は既存のGit除外規則により、実験ID配下も一括して除外する。
- 解析結果は `results/<experiment_id>/<analysis_name>/` へ保存する。`processed/` は前処理済み入力を置く。
- `sample/` だけを公開可能な小型データの置き場とする。本番CSVや動画をGitへ追加しない。
- 設定・校正・実験台帳はGit管理する。重複コピーした校正を各実験へ置かず、適用条件の合うバージョンを明示参照する。


## 2026-10-28 学校プール取得分

原本 `raw/ngtHighSchoolExperiment1/` は取り込み時の親/子/Spresense配置を保持する。
全収録 `20261028_ngt_000` と、同じ原本を共有参照する区間001〜004を登録した。
`interim/<id>/synchronized/` に該当区間の動画・全センサCSV、
`processed/20261028_ngt_000/` にGNSS・姿勢・深度・光点候補を保存する。
解析の入口は `results/20261028_ngt_000/report.md`。前日候補ログは校正監査だけに使用し、
別日データを今回の時間列や姿勢変換へ暗黙に結合しない。
