#!/usr/bin/env bash
# Arduino Web IDE - GitHub auto updater
# GitHub Push -> signed webhook -> this script -> staged validation -> atomic-ish deployment -> health check
set -euo pipefail
umask 077

APP_DIR="/opt/arduino-helper"
CONF_DIR="/etc/arduino-helper"
UPDATE_DIR="/var/lib/arduino-helper-updater"
ENV_FILE="$CONF_DIR/github-webhook.env"
SERVICE="arduino-helper"
LOCK_FILE="$UPDATE_DIR/update.lock"
KEEP_BACKUPS="${KEEP_BACKUPS:-3}"

die() {
  printf 'エラー: %s\n' "$*" >&2
  exit 1
}

log() {
  printf '[arduino-helper-update] %s\n' "$*"
}

[ "$(id -u)" -eq 0 ] || die "root 権限で実行してください"
[ -r "$ENV_FILE" ] || die "$ENV_FILE がありません"

# shellcheck disable=SC1090
. "$ENV_FILE"

: "${GITHUB_REPO:?GITHUB_REPO がありません}"
: "${GITHUB_BRANCH:=main}"

command -v git >/dev/null 2>&1 || die "git がありません"
command -v python3 >/dev/null 2>&1 || die "python3 がありません"
command -v curl >/dev/null 2>&1 || die "curl がありません"
command -v systemctl >/dev/null 2>&1 || die "systemctl がありません"

case "$GITHUB_BRANCH" in
  ""|*[!A-Za-z0-9._/-]*) die "不正なブランチ名です" ;;
esac

mkdir -p "$UPDATE_DIR"
chmod 0700 "$UPDATE_DIR"

# 同時実行を防止
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  log "別の更新処理が実行中です。終了します。"
  exit 0
fi

stage="$(mktemp -d "$UPDATE_DIR/stage.XXXXXX")"
backup=""
cleanup() {
  rm -rf "$stage"
}
trap cleanup EXIT

repo="$stage/repo"

log "GitHub から最新版を取得: $GITHUB_REPO ($GITHUB_BRANCH)"
git clone \
  --depth 1 \
  --single-branch \
  --branch "$GITHUB_BRANCH" \
  "$GITHUB_REPO" \
  "$repo" >/dev/null

cd "$repo"

# デプロイ対象は明示的な allowlist のファイルだけ。
# install.sh、設定、秘密情報、systemd定義、Arduino CLI等はPushでは変更しない。
FILES=(
  "arduino-helper.py"
  "arduino-build-run"
  "www/index.html"
  "www/admin.html"
)

for f in "${FILES[@]}"; do
  [ -f "$f" ] || die "必須ファイルがありません: $f"
  [ ! -L "$f" ] || die "symlink は許可しません: $f"
done

# PythonファイルをPythonとして検証する。
# arduino-build-run は Bash ではなく Python なので /bin/bash -n は使用しない。
/usr/bin/python3 -m py_compile arduino-helper.py
/usr/bin/python3 -m py_compile arduino-build-run

# 主要ファイル以外を実行ファイルとして持ち込まないことを確認。
# Git管理下にある不要ファイルはデプロイしないので、ここでは allowlist のみを検証対象にする。

# 現在の稼働ファイルをバックアップ。
timestamp="$(date +%Y%m%d-%H%M%S)"
backup="$UPDATE_DIR/backup.$timestamp"
mkdir -p "$backup"

for f in "${FILES[@]}"; do
  src="$APP_DIR/$f"
  if [ -e "$src" ] && [ ! -L "$src" ]; then
    mkdir -p "$backup/$(dirname "$f")"
    cp -a "$src" "$backup/$f"
  fi
done

# 新版を .new として配置してから置換。
install -d -o root -g root -m 0755 "$APP_DIR" "$APP_DIR/www"

install -m 0644 -o root -g root \
  "$repo/arduino-helper.py" \
  "$APP_DIR/arduino-helper.py.new"

install -m 0755 -o root -g root \
  "$repo/arduino-build-run" \
  "$APP_DIR/arduino-build-run.new"

install -m 0644 -o root -g root \
  "$repo/www/index.html" \
  "$APP_DIR/www/index.html.new"

install -m 0644 -o root -g root \
  "$repo/www/admin.html" \
  "$APP_DIR/www/admin.html.new"

mv -f "$APP_DIR/arduino-helper.py.new" "$APP_DIR/arduino-helper.py"
mv -f "$APP_DIR/arduino-build-run.new" "$APP_DIR/arduino-build-run"
mv -f "$APP_DIR/www/index.html.new" "$APP_DIR/www/index.html"
mv -f "$APP_DIR/www/admin.html.new" "$APP_DIR/www/admin.html"

commit="$(git rev-parse --short HEAD)"
log "ファイルを $commit に更新しました"

# 更新後のPythonを再検証。
/usr/bin/python3 -m py_compile "$APP_DIR/arduino-helper.py"
/usr/bin/python3 -m py_compile "$APP_DIR/arduino-build-run"

log "サービスを再起動します"
if ! systemctl restart "$SERVICE"; then
  log "サービス再起動失敗。バックアップから復元します。" >&2
  for f in "${FILES[@]}"; do
    if [ -f "$backup/$f" ]; then
      install -D -m 0644 -o root -g root "$backup/$f" "$APP_DIR/$f"
    fi
  done
  # arduino-build-run は実行可能属性を戻す
  [ -f "$backup/arduino-build-run" ] && chmod 0755 "$APP_DIR/arduino-build-run"
  systemctl restart "$SERVICE" || true
  exit 1
fi

# 起動直後のクラッシュを検出。
sleep 2
if ! systemctl is-active --quiet "$SERVICE"; then
  log "サービスが active ではありません。バックアップから復元します。" >&2
  for f in "${FILES[@]}"; do
    if [ -f "$backup/$f" ]; then
      mode=0644
      [ "$f" = "arduino-build-run" ] && mode=0755
      install -D -m "$mode" -o root -g root "$backup/$f" "$APP_DIR/$f"
    fi
  done
  systemctl restart "$SERVICE" || true
  exit 1
fi

# ローカルヘルスチェック
if ! curl -fsS --max-time 10 "http://127.0.0.1:8765/ping" >/dev/null; then
  log "ヘルスチェック失敗。バックアップから復元します。" >&2
  for f in "${FILES[@]}"; do
    if [ -f "$backup/$f" ]; then
      mode=0644
      [ "$f" = "arduino-build-run" ] && mode=0755
      install -D -m "$mode" -o root -g root "$backup/$f" "$APP_DIR/$f"
    fi
  done
  systemctl restart "$SERVICE" || true
  exit 1
fi

# バックアップを古い順に整理
find "$UPDATE_DIR" -maxdepth 1 -type d -name 'backup.*' -printf '%T@ %p\n' \
  | sort -nr \
  | awk 'NR > '"$KEEP_BACKUPS"' {sub(/^[^ ]+ /, ""); print}' \
  | while IFS= read -r old; do
      [ -n "$old" ] && rm -rf -- "$old"
    done

log "GitHub update applied: $commit"
