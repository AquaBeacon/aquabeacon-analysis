# 開発ガイド

実験データ解析・位置推定・可視化。

## セットアップ

別PCへの導入と校正データの確認は[docs/another_pc.md](docs/another_pc.md)を参照。
`python scripts/verify_calibration_bundle.py --refit`は原動画なしで実行できる。

Python 3.11以降。python -m pip install -e ".[dev,media,analysis,vision]" で開発依存を導入します。
新しい実験は `init-experiment <experiment_id>` で登録し、取り込み担当者が
`data/raw/<experiment_id>/` に機器別の原本を配置します。
[配置ルール](data/README.md)とREADMEの `--experiment` 指定の処理へ進みます。

Pythonの依存を追加する場合は、このリポジトリ専用の仮想環境を推奨します。

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

## 入口

- [pyproject.toml](pyproject.toml)
- [src/aquabeacon_analysis](src/aquabeacon_analysis)
- [tests](tests)

## 検証

```sh
python -m pytest -q
```

## データと関連リポジトリ

このディレクトリがGitリポジトリのルートです。親の AquaBeacon-OSS は複数リポジトリの置き場です。横断開発では各リポジトリを同じ親ディレクトリに配置し、[全体の入口](https://github.com/AquaBeacon/workspace-tools)から関連設計へ進みます。単体checkoutでも本書のローカル入口を使用できます。

生データ・生成物・SDK・仮想環境は手元に保持し、公開対象を.gitignoreで区別します。追跡済みのファイルは.gitignoreだけでは除外されないので、`git status --short`と`git ls-files`を確認します。

ライセンスは[Apache 2.0](LICENSE)、第三者部分は[THIRD_PARTY.md](THIRD_PARTY.md)。

## 公開リポジトリ

GitHub: https://github.com/AquaBeacon/aquabeacon-analysis

```sh
git clone https://github.com/AquaBeacon/aquabeacon-analysis.git
```

公開ブランチはmainです。新設リポジトリは公開対象だけを初期履歴にし、過去の開発履歴と実験ログはローカルに保管します。
