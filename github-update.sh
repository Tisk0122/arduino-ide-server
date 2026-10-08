#!/usr/bin/env bash
# GitHub push -> arduino-helper 自動更新。root 専用。
# リポジトリ内のスクリプトは実行せず、許可した固定ファイルだけを検証・反映する。
set -euo pipefail

APP_DIR=/opt/arduino-helper
CONF_DIR=/etc/arduino-helper
UPDATE_DIR=/var/lib/arduino-helper-updater
ENV_FILE="$CONF_DIR/github-webhook.env"
SERVICE=arduino-helper
PORT="${HELPER_PORT:-8765}"

log() {
  printf '[arduino-helper-update] %s\n' "$*" >&2
}

[ "$(id -u)" -eq 0 ] || { log "root only"; exit 1; }
[ -r "$ENV_FILE" ] || { log "GitHub webhook が設定されていません"; exit 2; }
# shellcheck disable=SC1090
. "$ENV_FILE"

: "${GITHUB_REPO:?GITHUB_REPO がありません}"
: "${GITHUB_BRANCH:=main}"

case "$GITHUB_REPO" in
  https://github.com/*/*|git@github.com:*/*) ;;
  *) log "許可されていない GitHub リポジトリURLです"; exit 3 ;;
esac
case "$GITHUB_BRANCH" in
  ''|*[^A-Za-z0-9._/-]*) log "ブランチ名が不正です"; exit 3 ;;
esac

mkdir -p "$UPDATE_DIR"
chmod 0700 "$UPDATE_DIR"
exec 9>"$UPDATE_DIR/update.lock"
flock -n 9 || { log "更新はすでに実行中です"; exit 0; }

stage="$(mktemp -d "$UPDATE_DIR/stage.XXXXXX")"
backup="$(mktemp -d "$UPDATE_DIR/backup.XXXXXX")"
cleanup() {
  rm -rf -- "$stage" "$backup"
}
trap cleanup EXIT

log "GitHubから $GITHUB_BRANCH を取得しています"
git clone --quiet --depth 1 --single-branch --branch "$GITHUB_BRANCH" "$GITHUB_REPO" "$stage/repo"
cd "$stage/repo"

# 本番へ反映するファイルは固定。install.sh 等は絶対に実行・コピーしない。
FILES=(
  "arduino-helper.py"
  "arduino-build-run"
  "www/index.html"
)

for f in "${FILES[@]}"; do
  [ -f "$f" ] || { log "必須ファイルがありません: $f"; exit 4; }
  [ ! -L "$f" ] || { log "symlink は許可しません: $f"; exit 5; }
done

# www 自体も symlink であってはならない。
[ ! -L "www" ] || { log "symlink は許可しません: www"; exit 5; }

# リポジトリ由来のコードを実行せず、構文だけ検査する。
/usr/bin/python3 -m py_compile arduino-helper.py
/usr/bin/python3 -m py_compile arduino-build-run

commit="$(git rev-parse --short=12 HEAD)"
log "検証OK: commit=$commit"

# 新ファイルを本番へ直接上書きせず、一時ファイルとして用意する。
install -d -o root -g root -m 0755 "$APP_DIR" "$APP_DIR/www"
install -m 0644 -o root -g root "$stage/repo/arduino-helper.py" "$APP_DIR/arduino-helper.py.new"
install -m 0755 -o root -g root "$stage/repo/arduino-build-run" "$APP_DIR/arduino-build-run.new"
install -m 0644 -o root -g root "$stage/repo/www/index.html" "$APP_DIR/www/index.html.new"

# 現行版を退避してから3ファイルをまとめて切り替える。
for f in arduino-helper.py arduino-build-run; do
  [ -f "$APP_DIR/$f" ] && cp -a -- "$APP_DIR/$f" "$backup/$f"
done
[ -f "$APP_DIR/www/index.html" ] && cp -a -- "$APP_DIR/www/index.html" "$backup/index.html"

mv -f "$APP_DIR/arduino-helper.py.new" "$APP_DIR/arduino-helper.py"
mv -f "$APP_DIR/arduino-build-run.new" "$APP_DIR/arduino-build-run"
mv -f "$APP_DIR/www/index.html.new" "$APP_DIR/www/index.html"

rollback() {
  log "新バージョンの起動/ヘルスチェックに失敗したためロールバックします"
  if [ -f "$backup/arduino-helper.py" ]; then
    install -m 0644 -o root -g root "$backup/arduino-helper.py" "$APP_DIR/arduino-helper.py"
  fi
  if [ -f "$backup/arduino-build-run" ]; then
    install -m 0755 -o root -g root "$backup/arduino-build-run" "$APP_DIR/arduino-build-run"
  fi
  if [ -f "$backup/index.html" ]; then
    install -m 0644 -o root -g root "$backup/index.html" "$APP_DIR/www/index.html"
  fi
  systemctl restart "$SERVICE" || true
}

log "arduino-helper を再起動しています"
if ! systemctl restart "$SERVICE"; then
  rollback
  exit 10
fi

# 再起動直後は少し待ってからヘルスチェック。最大20秒待つ。
healthy=0
for _ in $(seq 1 20); do
  if curl -fsS "http://127.0.0.1:${PORT}/ping" >/dev/null 2>&1; then
    healthy=1
    break
  fi
  sleep 1
done

if [ "$healthy" -ne 1 ]; then
  rollback
  exit 11
fi

log "GitHub update applied successfully: $commit"
