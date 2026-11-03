# Arduino Helper v5 適用手順

この更新では `arduino-helper.py` とWeb画面を更新します。サーバーの利用履歴、ユーザーDB、スケッチ、バックアップは変更しません。

## 1. ZIPをWindowsからsabakanへ転送

PowerShellで実行します（ZIPをDownloadsへ保存した場合）。

```powershell
scp "$env:USERPROFILE\Downloads\arduino-helper-complete-v5.zip" tisk@sabakan:~/arduino-helper-complete-v5.zip
```

`sabakan` がSSHの接続先として解決できない環境では、普段使用しているSSH接続先に置き換えてください。

## 2. サーバーで展開・内容確認

```bash
set -e
mkdir -p "$HOME/arduino-helper-v5"
unzip -o "$HOME/arduino-helper-complete-v5.zip" -d "$HOME/arduino-helper-v5"
test -s "$HOME/arduino-helper-v5/arduino-helper.py"
test -s "$HOME/arduino-helper-v5/www/admin.html"
test -s "$HOME/arduino-helper-v5/www/index.html"
grep -o 'data-ui-build="[^"]*"' "$HOME/arduino-helper-v5/www/admin.html"
```

期待するビルド表示は `2026-11-03.5` です。

## 3. バックアップと適用

以下は3ファイルだけを置き換えます。先に既存ファイルを `/var/backups/arduino-helper-v5-日時` へ保存します。

```bash
set -euo pipefail
SRC="$HOME/arduino-helper-v5"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="/var/backups/arduino-helper-v5-$STAMP"

sudo mkdir -p "$BACKUP"
sudo cp -a /opt/arduino-helper/arduino-helper.py "$BACKUP/arduino-helper.py"
sudo cp -a /opt/arduino-helper/www/admin.html "$BACKUP/admin.html"
sudo cp -a /opt/arduino-helper/www/index.html "$BACKUP/index.html"

sudo install -o root -g root -m 0644 "$SRC/arduino-helper.py" /opt/arduino-helper/arduino-helper.py
sudo install -o root -g root -m 0644 "$SRC/www/admin.html" /opt/arduino-helper/www/admin.html
sudo install -o root -g root -m 0644 "$SRC/www/index.html" /opt/arduino-helper/www/index.html

sudo /usr/bin/python3 -m py_compile /opt/arduino-helper/arduino-helper.py
sudo systemctl restart arduino-helper
sudo systemctl is-active arduino-helper
sudo systemctl status arduino-helper --no-pager

grep -o 'data-ui-build="[^"]*"' /opt/arduino-helper/www/admin.html
```

`systemctl is-active` が `active`、ビルド表示が `2026-11-03.5` であることを確認してください。

## 4. 適用後

ブラウザーでIDEと管理画面を開き、`Ctrl + Shift + R` で再読み込みしてください。管理者画面の「ボード管理」から、インストール済みボード、対応ボード、パッケージ検索、追加ボードソースを確認できます。

## 5. ロールバック

適用時に表示されたバックアップパスを使い、次のように戻します。`BACKUP` は実際に作成されたディレクトリ名へ置き換えてください。

```bash
BACKUP="/var/backups/arduino-helper-v5-YYYYMMDD-HHMMSS"
sudo install -o root -g root -m 0644 "$BACKUP/arduino-helper.py" /opt/arduino-helper/arduino-helper.py
sudo install -o root -g root -m 0644 "$BACKUP/admin.html" /opt/arduino-helper/www/admin.html
sudo install -o root -g root -m 0644 "$BACKUP/index.html" /opt/arduino-helper/www/index.html
sudo systemctl restart arduino-helper
sudo systemctl status arduino-helper --no-pager
```
