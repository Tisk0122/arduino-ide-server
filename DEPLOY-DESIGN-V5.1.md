# Arduino Helper v5.1 管理画面デザイン更新手順

この更新は管理画面HTMLのみを置き換えます。サーバーAPI、Pythonプロセス、DB、利用履歴、ユーザー、スケッチは変更しません。

## v5 / 2.10.0 がすでに稼働している場合

### 1. Windows PowerShellからHTMLを転送

ダウンロードした `Arduino-Helper-v5.1-admin.html` がDownloadsにある場合:

```powershell
scp "$env:USERPROFILE\Downloads\Arduino-Helper-v5.1-admin.html" tisk@sabakan:~/admin-v5.1.html.new
```

`sabakan` がSSHホスト名として解決できない場合は、普段使っている接続先に置き換えてください。

### 2. sabakanでファイルとビルドIDを確認

```bash
ls -lh ~/admin-v5.1.html.new
grep -o 'data-ui-build="[^"]*"' ~/admin-v5.1.html.new
```

期待値:

```text
data-ui-build="2026-11-03.6"
```

### 3. バックアップ後に管理画面だけ更新

```bash
set -euo pipefail
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="/opt/arduino-helper/www/admin.html.backup.$STAMP"

sudo cp -a /opt/arduino-helper/www/admin.html "$BACKUP"
sudo install -o root -g root -m 0644 \
  "$HOME/admin-v5.1.html.new" \
  /opt/arduino-helper/www/admin.html

sudo sha256sum "$BACKUP" /opt/arduino-helper/www/admin.html
grep -o 'data-ui-build="[^"]*"' /opt/arduino-helper/www/admin.html
```

### 4. ブラウザーで反映を確認

管理画面で `Ctrl + Shift + R` を押してください。通常、このHTML更新だけならサービス再起動は不要です。

## v5 / 2.10.0 がまだ稼働していない場合

このデザイン更新だけではボード管理APIは追加されません。先にv5本体の `DEPLOY-V5.md` に従い、`arduino-helper.py`、`www/admin.html`、`www/index.html` を更新してサービスを再起動してください。その後、このv5.1の `www/admin.html` で管理画面デザインを更新します。

## ロールバック

```bash
sudo cp -a "/opt/arduino-helper/www/admin.html.backup.YYYYMMDD-HHMMSS" \
  /opt/arduino-helper/www/admin.html
```

`YYYYMMDD-HHMMSS` は今回作成した実際のバックアップ名に置き換え、ブラウザーを強制再読み込みしてください。
