# Data

実験データ本体はGitリポジトリでは管理しない。

## Directory
- `sample/`: CI・テスト・解析例に使う小型データ。Git管理対象。
- `raw/`: 実験機器から取得したオリジナルデータ。Git管理対象外。編集禁止。
- `interim/`: 時刻同期、動画フレーム抽出等の中間生成物。Git管理対象外。
- `processed/`: 位置推定結果、点群、メッシュ等。Git管理対象外。

## Rules
1. `raw/` のデータは上書きしない。
2. 解析結果は `interim/`, `processed/`, `results/` のいずれかへ保存する。
3. 各実験はexperiment IDで管理する。
4. 本番CSVをGitに入れない。小型sample CSVと軽量metrics CSVのみ必要に応じて追跡する。