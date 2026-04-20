# RTLens Codex 引き継ぎガイド（GitHubクローン運用）

## 1. 運用方針（最重要）

- Codexの作業対象は **`/home/miokada/mydev/rtlens_repo/RTLens` のみ** とする。
- 正式 remote は `git@github.com:okamitsu320/RTLens.git`。
- 旧作業ディレクトリ（例: `/home/miokada/mydev/mysimview-dev`）は参照専用。編集・commit・pushはしない。

## 2. 初回確認

```bash
cd /home/miokada/mydev/rtlens_repo/RTLens
git status -sb
git remote -v
git branch -vv
```

期待:

- branch は `main`
- `origin` は GitHub SSH URL

## 3. 日常作業フロー

### 3-1. 最新取り込み

```bash
cd /home/miokada/mydev/rtlens_repo/RTLens
git fetch --all --prune
git switch main
git pull --ff-only origin main
```

### 3-2. 開発ブランチ作成（推奨）

```bash
git switch -c <topic-branch>
```

例: `fix/qt-shortcut-crash`, `docs/runbook-update`

### 3-3. 実装・確認

```bash
# Python環境（未作成時）
python3 -m venv .venv
. .venv/bin/activate

# 依存導入
.venv/bin/python -m pip install -e ".[dev]"

# 文法確認
.venv/bin/python -m py_compile rtlens/rtlens/*.py

# 単体テスト
.venv/bin/python -m pytest -q rtlens/tests
```

### 3-4. GUI確認（最小）

```bash
.venv/bin/python -m rtlens --ui qt --filelist RTL/verification/mid_case/vlist --top vm_mid_top
```

必須チェック:

- load/driver 検索（`Include clock deps` の挙動）
- Schematicで選択・ダブルクリック遷移
- Open Externalの `+` / `-` / `Fit` / `Ctrl + double click`
- Qtショートカット（`Ctrl+R`, `Ctrl+Shift+R`, `Ctrl+Shift+W`）

### 3-5. commit / push

```bash
git add <files>
git commit -m "<type>: <summary>"
git push -u origin <topic-branch>
```

`main` 直pushを行う場合は、レビュー方針に従うこと。

## 4. ドキュメント更新ルール

- 挙動を変えた場合は `rtlens/docs/usage.md` を同時更新する。
- セットアップや依存関係に影響した場合は `rtlens/docs/install.md` も更新する。
- 必要に応じて `README.md` の概要・リンクを同期する。

## 5. トラブルシュート

### 5-1. Python/venv

- `pytest` がない: `.venv/bin/python -m pip install -e ".[dev]"`
- `PySide6` がない: 同上（editable installで導入）

### 5-2. 解析・表示まわり

- slang build関連: `rtlens/tools/setup_slang_prefix.py` と `RTLENS_SLANG_ROOT` を確認
- Schematic失敗: `yosys`, `netlistsvg` のPATH/実行確認
- RTL Structure失敗: `node`, `npm`, `dot`（Graphviz）確認

### 5-3. OS差分

- Linux: primary support
- Windows: best-effort（環境差分が出やすい）
- macOS: reference/provisional（ツールチェーン差分に注意）

## 6. 旧ディレクトリの扱い

- `/home/miokada/mydev/mysimview-dev` は履歴参照用として保持してよい。
- ただし、今後の修正コミットは `rtlens_repo/RTLens` でのみ作成する。

