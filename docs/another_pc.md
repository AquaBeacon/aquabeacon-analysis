# 別PCでのセットアップと新しい実験の解析

## 最初のセットアップ

Windows / PowerShell、Python 3.11以降とGitを前提とする。解析リポジトリをcloneすると、
共通カメラ内部校正、親子の軸校正、実機配置とCAD配置JSON、約12 MBの校正根拠データが揃う。
Git LFSや過去の全実験データのダウンロードは不要。

```powershell
git clone https://github.com/AquaBeacon/aquabeacon-analysis.git
cd aquabeacon-analysis
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev,media,analysis,vision,mapping]"
.venv\Scripts\python scripts/verify_calibration_bundle.py --refit
.venv\Scripts\python -m pytest -q
.venv\Scripts\python -m ruff check .
.venv\Scripts\python -m aquabeacon_analysis --help
```

校正確認は動画・センサ原本・隣のfirmwareリポジトリなしで実行できる。
動画を使う処理ではFFmpeg/FFprobeもPATHに置く。`media`依存のimageio-ffmpegにはFFprobeが
含まれないため、FFprobeを呼ぶスクリプトには別途インストールが必要。

バイナリログを読む場合は同じ親フォルダにfirmwareリポジトリもcloneする。
今回の読取コードは次のcommitで公開済み。既存のfirmware作業コピーは上書きせず、
新規cloneに対してのみcheckoutする。

```powershell
git clone https://github.com/AquaBeacon/firmware-experiments.git ../firmware-experiments
git -C ../firmware-experiments checkout ab7857cf7e246bb388e9d28e310e677ca0499c69
```

## 新しいデータを入れる

例のIDとログ番号を実際の取得日に置き換える。同じIDの既存結果を上書きしない。

```powershell
.venv\Scripts\python -m aquabeacon_analysis init-experiment 20261031_pool_001 --c-main-suffix 22
.venv\Scripts\python -m aquabeacon_analysis show-experiment 20261031_pool_001
```

取り込み担当者が原本を次へコピーする。解析開始後はrawを移動・改名・編集しない。

```text
data/raw/20261031_pool_001/
  C_MAIN/       # 子機ログ
  P_MAIN/       # 親機ログ
  Spresense/    # 親機IMU
  C_MAIN_CAM/   # 子機MP4・LRF
  P_MAIN_CAM/   # 親機MP4・LRF
```

`experiments/20261031_pool_001/experiment.json` の`raw_sources`を実際のファイルへ更新し、
`config_paths`と`calibration_paths`に使う設定・校正を登録する。
校正参照の登録だけで数値が自動適用されるわけではない。使う処理の入力形式と校正指定を確認する。

| 再利用する校正 | 条件 |
|---|---|
| `calibration/camera_defaults.json` | 親機・子機共通の水中内部校正。main/proxy、解像度、crop、Dewarp、EIS、ケース、媒質を確認 |
| `calibration/20261029_child_axis_003_imu_frd.json` | BNO085→子機FRD。同一IMU取付けで使用 |
| `calibration/20261029_parent_axis_011_axes.json` | Spresense→親機FRD。同一取付けで使用。内包する時計変換は校正収録内限定 |
| `calibration/20261028_parent_lis3mdl_cad_axes.json` | 親機磁気軸の暫定取付け。磁北・精度未検証 |
| `calibration/child_mount_20261028_actual.json` / `calibration/rigs/` | 実機配置の記録。測定中心・光学extrinsicの校正ではない |

新しい撮影の時刻同期、カメラとIMUの対応、座標・有効フラグを確認してから解析する。
機体取付けや撮影条件が変わったら対応する校正を取り直す。

## 解析の入口と適用範囲

CSVベースの共通CLIには`sync-raw`、`analyze-sync`、`preprocess-tof`、`localize-imu`、
`detect-lights`、`track-lights`がある。各`--help`と[データ仕様](spec.md)を確認する。
対応するCSV・カメラテレメトリの形式が揃っている場合の順序例：

```powershell
.venv\Scripts\python -m aquabeacon_analysis sync-raw --experiment 20261031_pool_001
.venv\Scripts\python -m aquabeacon_analysis analyze-sync --experiment 20261031_pool_001
.venv\Scripts\python -m aquabeacon_analysis preprocess-tof --experiment 20261031_pool_001
```

バイナリ形式は先に`scripts/decode_ngt_logs.py --help`で対象機器・出力先を確認し、
`--experiment`、`--firmware-root`、`--source`を明示して復号する。
CSVへ復号しただけで共通CLIの全入力形式が揃うわけではない。

`scripts/*ngt*`には2026-10-28のファイル名・収録区間・時計対応を固定した処理も含む。
任意のデータを置くだけで全解析を自動実行する機能はない。別実験では共通CLIを使うか、
NGT処理の入力対応を新しい台帳・設定に合わせて追加する。別日の収録へ既存のoffsetや区間を転用しない。
結果は`results/<experiment_id>/<analysis_name>/`、中間・処理済みは同じIDの`data/interim/`・
`data/processed/`へ保存する。

VSLAMにはDocker Desktop/WSL2とORB-SLAM3イメージが必要。
`./scripts/run_orbslam3.ps1 -Build`でソースから構築できる。
[専用手順](vislam_environment.md)を参照。COLMAP/pycolmap、Blenderは該当処理でのみ導入する。
実CADのSTEP/GLB、動画、生成マップ、過去の解析結果は同梱しないため、
可視化の再生成にはそれぞれの入力も必要。

## GitHubへ含めるもの

解析コード、テスト、依存定義、実験台帳、設定、校正JSONと校正用の抜粋データを管理する。
`data/raw/`、`data/interim/`、`data/processed/`、`results/`、仮想環境はローカルに保持する。
README等の`results/`へのリンクは既存PCの成果物への案内であり、clone先には存在しない。
校正の持ち運びには[校正根拠データ](../calibration/reference_data/README.md)を使う。

## 配布内容の検証記録（2026-10-31）

Gitの公開予定treeを一時ディレクトリに展開し、raw・resultsの実データと隣接firmwareが
存在しない状態で確認した。Windows、Python 3.13.5、NumPy 2.4.2、SciPy 1.18.0、
OpenCV 5.0.0を使用。検証用venvは既存の数値ライブラリを参照し、解析パッケージ自体は
wheelをビルドしてそのvenvへインストールし、読み込み先も確認した。

- パッケージビルド・導入、CLI help：成功。
- 校正関連33ファイルのSHA-256：一致。
- MP4 34枚・LRF 35枚のカメラ再計算：内部行列差1.5e-7 px以下。
- 親機・子機の軸再計算：保存行列と一致。方向残差最大は親機2.361°、子機2.612°。
- 自動テスト248件：成功。不足・改変された根拠を拒否する検証も含む。
- 全体Ruff：成功。
- 公開対象の点検：raw・全収録ログ・動画・生成地図・仮想環境を除外。

これは同じPC上の新規チェックアウト相当の検証であり、別の実機PCや未取得の新規データでの
解析実行、Docker/COLMAP/Blenderの再構築は今回実行していない。
