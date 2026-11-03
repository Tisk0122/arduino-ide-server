# Arduino Helper v5.2 / 2.10.1 適用手順

今回の障害の直接原因は、サービスが使用する `/usr/local/libexec/arduino-build-run` の許可リスト不足です。既存のv5.1.xからは、まずラッパーだけを安全に差し替える方法が最小変更です。サービス再起動は通常不要です。

## A. 最小修正（現在のv5.1.xサーバー向け・推奨）

### 1. Windowsからラッパーを転送

ZIPを展開し、プロジェクト直下の `arduino-build-run` をWindowsのDownloadsに置いてからPowerShellで実行します。

```powershell
scp "$env:USERPROFILE\Downloads\Arduino-Helper-v5.2-arduino-build-run" tisk@sabakan:~/arduino-build-run.new
```

`sabakan` がSSHホスト名として解決できない場合は、普段SSH接続に使っているホスト名またはIPアドレスに置き換えてください。

### 2. sabakanで内容と構文を確認

```bash
ls -lh ~/arduino-build-run.new
python3 -m py_compile ~/arduino-build-run.new
```

### 3. 既存ラッパーをバックアップして配置

```bash
set -euo pipefail
STAMP="$(date +%Y%m%d-%H%M%S)"
sudo cp -a /usr/local/libexec/arduino-build-run \
  "/usr/local/libexec/arduino-build-run.backup.$STAMP"
sudo install -o root -g root -m 0755 \
  ~/arduino-build-run.new \
  /usr/local/libexec/arduino-build-run
sudo python3 -m py_compile /usr/local/libexec/arduino-build-run
sudo stat -c '%U:%G %a %n' /usr/local/libexec/arduino-build-run
```

期待値は `root:root 755 /usr/local/libexec/arduino-build-run` です。`0755` は、`arduino-helper` がsudoで切り替えた `arduino-build` ユーザーからラッパーを実行できるようにするために必要です。

### 4. 読み取り系コマンドを確認

```bash
sudo -u arduino-helper sudo -n -u arduino-build \
  /usr/local/libexec/arduino-build-run --timeout 30 -- version

sudo -u arduino-helper sudo -n -u arduino-build \
  /usr/local/libexec/arduino-build-run --timeout 30 -- config dump --json \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); u=d.get("board_manager",{}).get("additional_urls",[]); print("設定読取成功:", len(u), "件")'
```

2つ目が `設定読取成功: N 件` と表示すれば、追加ボードソース設定の読み取り経路も通っています。設定内容そのものは画面に出しません。

### 5. 管理画面で同期を再実行

管理画面の「ボード管理」から「インデックス同期」を押します。管理ジョブが完了したら、対応ボード一覧を再読み込みします。正常時には失敗カードではなく成功状態になり、ボード名とFQBNが表示されます。

今回のラッパーだけの差し替えでは、通常サービス再起動は不要です。同期が完了しない場合は `sudo journalctl -u arduino-helper -n 100 --no-pager` と管理ジョブのログを確認してください。

## B. プロジェクト全体をv5.2へ更新する場合

ZIPを展開したプロジェクトルートで既存の安全なデプロイ手順を実行してください。v5.2では次のファイルが更新対象です。

- `arduino-helper.py`
- `arduino-build-run`
- `github-update.sh`
- `www/admin.html`
- `UPDATE-NOTES-V5.2.md`
- `SERVER-OPERATIONS.md`

`install.sh` は初回導入や再構築向けです。既存サーバーへの単純な上書き目的で実行せず、既存の `github-update.sh` によるバックアップ付き更新、または上記Aの最小修正を優先してください。

## ロールバック

配置後に問題があれば、手順3で表示されたバックアップ名を使って戻します。

```bash
sudo cp -a "/usr/local/libexec/arduino-build-run.backup.YYYYMMDD-HHMMSS" \
  /usr/local/libexec/arduino-build-run
sudo chmod 0755 /usr/local/libexec/arduino-build-run
sudo chown root:root /usr/local/libexec/arduino-build-run
```

`YYYYMMDD-HHMMSS` は実際に作成されたバックアップ名に置き換えてください。
