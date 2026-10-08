#!/usr/bin/env bash
# GitHub push -> arduino-helper 自動更新。root 専用。リポジトリ内のスクリプトは実行しない。
set -euo pipefail

APP_DIR=/opt/arduino-helper
CONF_DIR=/etc/arduino-helper
UPDATE_DIR=/var/lib/arduino-helper-updater
ENV_FILE="$CONF_DIR/github-webhook.env"
SERVICE=arduino-helper

[ "$(id -u)" -eq 0 ] || { echo "root only" >&2; exit 1; }
[ -r "$ENV_FILE" ] || { echo "GitHub webhook が設定されていません" >&2; exit 2; }
# shellcheck disable=SC1090
. "$ENV_FILE"

: "${GITHUB_REPO:?GITHUB_REPO がありません}"
: "${GITHUB_BRANCH:=main}"

case "$GITHUB_REPO" in
  https://github.com/*/*|git@github.com:*/*) ;;
  *) echo "許可されていない GitHub リポジトリURLです" >&2; exit 3 ;;
esac
case "$GITHUB_BRANCH" in
  ''|*[^A-Za-z0-9._/-]*) echo "ブランチ名が不正です" >&2; exit 3 ;;
esac

mkdir -p "$UPDATE_DIR"
chmod 0700 "$UPDATE_DIR"
exec 9>"$UPDATE_DIR/update.lock"
flock -n 9 || { echo "更新はすでに実行中です"; exit 0; }

stage="$(mktemp -d "$UPDATE_DIR/stage.XXXXXX")"
cleanup() { rm -rf -- "$stage"; }
trap cleanup EXIT

# リポジトリ内の install.sh / deploy.sh 等は絶対に実行しない。
git clone --depth 1 --single-branch --branch "$GITHUB_BRANCH" "$GITHUB_REPO" "$stage/repo" >/dev/null
cd "$stage/repo"

# 必須ファイルを固定。これ以外は本番ディレクトリへコピーしない。
for f in arduino-helper.py arduino-build-run www/index.html; do
  [ -f "$f" ] || { echo "必須ファイルがありません: $f" >&2; exit 4; }
done

# リポジトリ側から実行可能なパス経由で何かを実行しない。構文だけ検査する。
/usr/bin/python3 -m py_compile arduino-helper.py
/bin/bash -n arduino-build-run

# symlink を本番へ持ち込ませない。
for f in arduino-helper.py arduino-build-run www/index.html; do
  [ ! -L "$f" ] || { echo "symlink は許可しません: $f" >&2; exit 5; }
done

# 同一ファイルシステム上の一時配置から root 所有で反映。
install -d -o root -g root -m 0755 "$APP_DIR" "$APP_DIR/www"
install -m 0644 -o root -g root "$stage/repo/arduino-helper.py" "$APP_DIR/arduino-helper.py.new"
install -m 0755 -o root -g root "$stage/repo/arduino-build-run" "$APP_DIR/arduino-build-run.new"
install -m 0644 -o root -g root "$stage/repo/www/index.html" "$APP_DIR/www/index.html.new"

# Python / wrapper / HTML の更新を順番に反映。秘密情報はリポジトリから同期しない。
mv -f "$APP_DIR/arduino-helper.py.new" "$APP_DIR/arduino-helper.py"
mv -f "$APP_DIR/arduino-build-run.new" "$APP_DIR/arduino-build-run"
mv -f "$APP_DIR/www/index.html.new" "$APP_DIR/www/index.html"

systemctl restart "$SERVICE"
sleep 2
curl -fsS http://127.0.0.1:8765/ping >/dev/null

echo "GitHub update applied: $(git -C "$stage/repo" rev-parse --short HEAD)"
