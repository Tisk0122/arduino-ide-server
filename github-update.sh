#!/usr/bin/env bash
# GitHub Push + periodic check -> staged validation -> rollback-capable deployment
set -Eeuo pipefail
umask 077

APP_DIR=/opt/arduino-helper
CONF_DIR=/etc/arduino-helper
UPDATE_DIR=/var/lib/arduino-helper-updater
ENV_FILE="$CONF_DIR/github-webhook.env"
SERVICE=arduino-helper
UPDATER_PATH=/usr/local/libexec/arduino-helper-update
SYSTEMD_DIR=/etc/systemd/system
LOCK_FILE="$UPDATE_DIR/update.lock"
KEEP_BACKUPS="${KEEP_BACKUPS:-3}"
CHECK_ONLY=0

case "${1:-}" in
  "") ;;
  --check) CHECK_ONLY=1 ;;
  *) printf '使用方法: %s [--check]\n' "$0" >&2; exit 2 ;;
esac

log() {
  local line="[arduino-helper-update] $*"
  printf '%s\n' "$line"
  if command -v logger >/dev/null 2>&1; then
    logger -t arduino-helper-update -- "$*" || true
  fi
}

die() {
  log "エラー: $*"
  exit 1
}

trap 'status=$?; log "処理に失敗しました (終了コード $status、行 $LINENO)"; exit "$status"' ERR

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
case "$KEEP_BACKUPS" in
  ''|*[!0-9]*) die "KEEP_BACKUPS は0以上の整数で指定してください" ;;
esac

mkdir -p "$UPDATE_DIR"
chmod 0700 "$UPDATE_DIR"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  log "別の更新処理が実行中です。次回の確認で再試行します。"
  exit 0
fi

FILES=(
  "arduino-helper.py"
  "arduino-build-run"
  "www/index.html"
  "www/admin.html"
  "www/docs.html"
  "www/teacher.html"
  "configure-github-webhook.sh"
  "github-update.sh"
  "arduino-helper-update.service"
  "arduino-helper-update.timer"
)

target_for() {
  case "$1" in
    configure-github-webhook.sh) printf '%s\n' /usr/local/libexec/arduino-helper-configure-github-webhook ;;
    github-update.sh) printf '%s\n' "$UPDATER_PATH" ;;
    arduino-helper-update.service|arduino-helper-update.timer)
      printf '%s/%s\n' "$SYSTEMD_DIR" "$1"
      ;;
    *) printf '%s/%s\n' "$APP_DIR" "$1" ;;
  esac
}

mode_for() {
  case "$1" in
    arduino-build-run|configure-github-webhook.sh|github-update.sh) printf '0700\n' ;;
    *) printf '0644\n' ;;
  esac
}

stage="$(mktemp -d "$UPDATE_DIR/stage.XXXXXX")"
backup=""
deployment_started=0
deployment_succeeded=0

rollback() {
  local f target mode
  log "配置済みバックアップへロールバックします"
  for f in "${FILES[@]}"; do
    target="$(target_for "$f")"
    if [ -f "$stage/had/$f" ]; then
      mkdir -p "$(dirname "$target")"
      cp -a "$backup/$f" "$target" || log "復元失敗: $target"
    else
      rm -f -- "$target" "$target.new.$$" || log "新規ファイルの除去に失敗: $target"
    fi
  done
  systemctl daemon-reload || log "systemd daemon-reload に失敗しました"
  if [ -f "$stage/had/arduino-helper-update.timer" ]; then
    systemctl restart arduino-helper-update.timer || log "更新確認timerの復旧に失敗しました"
  else
    systemctl disable --now arduino-helper-update.timer >/dev/null 2>&1 || true
  fi
  if ! systemctl restart "$SERVICE"; then
    log "ロールバック後のサービス再起動にも失敗しました"
  fi
}

run_logged() {
  local status=0 line
  "$@" > "$stage/command-output" 2>&1 || status=$?
  while IFS= read -r line; do
    log "$line"
  done < "$stage/command-output"
  return "$status"
}

cleanup() {
  local status=$?
  local f target
  trap - EXIT
  if [ "$deployment_started" -eq 1 ] && [ "$deployment_succeeded" -eq 0 ]; then
    rollback
  elif [ -n "$backup" ] && [ "$deployment_succeeded" -eq 0 ]; then
    rm -rf -- "$backup"
  fi
  for f in "${FILES[@]}"; do
    target="$(target_for "$f")"
    rm -f -- "$target.new.$$"
  done
  rm -f -- "$UPDATE_DIR/applied-commit.new.$$"
  rm -rf -- "$stage"
  exit "$status"
}
trap cleanup EXIT

branch_ref="refs/heads/$GITHUB_BRANCH"
if [ "$CHECK_ONLY" -eq 1 ]; then
  log "GitHub の更新を確認しています: $GITHUB_REPO ($GITHUB_BRANCH)"
  if ! git ls-remote "$GITHUB_REPO" "$branch_ref" > "$stage/remote-refs" 2> "$stage/remote-error"; then
    while IFS= read -r line; do log "$line"; done < "$stage/remote-error"
    die "GitHub の更新確認に失敗しました"
  fi
  remote_commit="$(awk -v ref="$branch_ref" '$2 == ref {print $1}' "$stage/remote-refs")"
  [[ "$remote_commit" =~ ^[0-9a-fA-F]{40,64}$ ]] || die "GitHub ブランチのコミットIDを取得できません"
  applied_commit=""
  if [ -r "$UPDATE_DIR/applied-commit" ]; then
    IFS= read -r applied_commit < "$UPDATE_DIR/applied-commit" || true
  fi
  if [ "$remote_commit" = "$applied_commit" ]; then
    log "最新です: ${remote_commit:0:12}"
    exit 0
  fi
  log "未適用の更新を検出しました: ${applied_commit:-未記録} -> ${remote_commit:0:12}"
fi

stage_repo="$stage/repo"
log "GitHub から最新版を取得: $GITHUB_REPO ($GITHUB_BRANCH)"
run_logged git clone --depth 1 --single-branch --branch "$GITHUB_BRANCH" "$GITHUB_REPO" "$stage_repo"
cd "$stage_repo"

for f in "${FILES[@]}"; do
  [ -f "$f" ] || die "必須ファイルがありません: $f"
  [ ! -L "$f" ] || die "symlink は許可しません: $f"
done

run_logged python3 -m py_compile arduino-helper.py arduino-build-run

mkdir -p "$stage/had"
timestamp="$(date +%Y%m%d-%H%M%S)"
backup="$UPDATE_DIR/backup.$timestamp"
mkdir -p "$backup"

for f in "${FILES[@]}"; do
  target="$(target_for "$f")"
  if [ -L "$target" ]; then
    die "配置先 symlink は許可しません: $target"
  fi
  if [ -e "$target" ]; then
    [ -f "$target" ] || die "配置先が通常ファイルではありません: $target"
    mkdir -p "$backup/$(dirname "$f")" "$stage/had/$(dirname "$f")"
    cp -a "$target" "$backup/$f"
    : > "$stage/had/$f"
  fi
done

install -d -o root -g root -m 0755 "$APP_DIR" "$APP_DIR/www" "$SYSTEMD_DIR" /usr/local/libexec
for f in "${FILES[@]}"; do
  target="$(target_for "$f")"
  mode="$(mode_for "$f")"
  install -m "$mode" -o root -g root "$stage_repo/$f" "$target.new.$$"
done

deployment_started=1
for f in "${FILES[@]}"; do
  target="$(target_for "$f")"
  mv -f -- "$target.new.$$" "$target"
done

log "配置したコミット: $(git rev-parse --short HEAD)"
run_logged python3 -m py_compile "$APP_DIR/arduino-helper.py" "$APP_DIR/arduino-build-run"
run_logged systemctl daemon-reload
log "サービスを再起動します"
run_logged systemctl restart "$SERVICE"
sleep 2
systemctl is-active --quiet "$SERVICE" || die "サービスが active ではありません"

health_port="$(systemctl show --property=Environment --value "$SERVICE" |
  python3 -c '
import re
import sys

matches = re.findall(r"(?:^|\s)HELPER_PORT=([0-9]+)(?:\s|$)", sys.stdin.read())
port = int(matches[-1]) if matches else 8765
if not 1 <= port <= 65535:
    raise SystemExit("HELPER_PORT が範囲外です")
print(port)
')"

if ! curl -fsS --max-time 10 "http://127.0.0.1:$health_port/ping" -o "$stage/ping.json"; then
  die "ローカルヘルスチェックに失敗しました"
fi
python3 - "$stage/ping.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as response:
    result = json.load(response)
if result.get("ok") is not True:
    raise SystemExit("ローカルヘルスチェックの応答が不正です")
PY

if ! curl -fsS --max-time 10 "http://127.0.0.1:$health_port/docs" -o "$stage/docs-health.html" \
    || ! grep -q "Arduino Web IDE" "$stage/docs-health.html"; then
  die "ドキュメントページのヘルスチェックに失敗しました"
fi
if ! curl -fsS --max-time 10 "http://127.0.0.1:$health_port/account" -o "$stage/account-health.html" \
    || ! grep -q "アカウントとクラウド保存" "$stage/account-health.html"; then
  die "アカウントページのヘルスチェックに失敗しました"
fi

run_logged systemctl enable --now arduino-helper-update.timer
run_logged systemctl restart arduino-helper-update.timer
systemctl is-enabled --quiet arduino-helper-update.timer || die "更新確認timerが enabled ではありません"
systemctl is-active --quiet arduino-helper-update.timer || die "更新確認timerが active ではありません"

commit="$(git rev-parse HEAD)"
printf '%s\n' "$commit" > "$UPDATE_DIR/applied-commit.new.$$"
chmod 0600 "$UPDATE_DIR/applied-commit.new.$$"
mv -f -- "$UPDATE_DIR/applied-commit.new.$$" "$UPDATE_DIR/applied-commit"

deployment_succeeded=1
log "更新を適用しました: $commit"

find "$UPDATE_DIR" -maxdepth 1 -type d -name 'backup.*' -printf '%T@ %p\n' \
  | sort -nr \
  | awk 'NR > '"$KEEP_BACKUPS"' {sub(/^[^ ]+ /, ""); print}' \
  | while IFS= read -r old; do
      [ -n "$old" ] && rm -rf -- "$old"
    done
