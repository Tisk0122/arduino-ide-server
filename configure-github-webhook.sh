#!/usr/bin/env bash
# GitHub webhook の初回設定・再設定。root 専用。
set -euo pipefail

CONF_DIR=/etc/arduino-helper
ENV_FILE="$CONF_DIR/github-webhook.env"
HELPER_ENV="$CONF_DIR/helper.env"

[ "$(id -u)" -eq 0 ] || { echo "sudo bash configure-github-webhook.sh で実行してください" >&2; exit 1; }
install -d -o root -g root -m 0755 "$CONF_DIR"

printf 'GitHub repository URL (例: https://github.com/Tisk0122/arduino-ide-server.git): '
read -r repo
printf 'GitHub branch [main]: '
read -r branch
branch="${branch:-main}"
printf 'Webhook Secret: '
read -r secret

if [[ "$repo" =~ ^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(\.git)?$ ]] ||
   [[ "$repo" =~ ^git@github\.com:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(\.git)?$ ]]; then
  :
else
  echo 'リポジトリURLが不正です' >&2
  exit 2
fi
case "$branch" in
  ''|*[^A-Za-z0-9._/-]*) echo 'ブランチ名が不正です' >&2; exit 2 ;;
esac
[ "${#secret}" -ge 32 ] || { echo 'Webhook Secret は32文字以上にしてください' >&2; exit 2; }

case "$repo" in
  https://github.com/*/*) full_name="${repo#https://github.com/}" ;;
  git@github.com:*) full_name="${repo#git@github.com:}" ;;
  *) echo 'リポジトリURLが不正です' >&2; exit 2 ;;
esac
full_name="${full_name%.git}"
case "$full_name" in
  */*) ;;
  *) echo 'GitHub owner/repo を取得できません' >&2; exit 2 ;;
esac

# 更新スクリプトが読む設定。source しても値がシェルコードにならないよう引用する。
printf -v repo_quoted '%q' "$repo"
printf -v full_name_quoted '%q' "$full_name"
printf -v branch_quoted '%q' "$branch"
cat > "$ENV_FILE" <<EOF2
GITHUB_REPO=$repo_quoted
GITHUB_WEBHOOK_REPO=$full_name_quoted
GITHUB_BRANCH=$branch_quoted
EOF2
chown root:root "$ENV_FILE"
chmod 0600 "$ENV_FILE"

# arduino-helper.service が読む設定にも repo/branch/secret を同期する。
# 既存の他設定は保持し、Webhook関連だけ置き換える。
tmp="$(mktemp "$CONF_DIR/helper.env.XXXXXX")"
if [ -f "$HELPER_ENV" ]; then
  grep -vE '^(GITHUB_WEBHOOK_SECRET|GITHUB_WEBHOOK_REPO|GITHUB_WEBHOOK_BRANCH)=' "$HELPER_ENV" > "$tmp" || true
fi
printf 'GITHUB_WEBHOOK_SECRET=%s\n' "$secret" >> "$tmp"
printf 'GITHUB_WEBHOOK_REPO=%s\n' "$full_name" >> "$tmp"
printf 'GITHUB_WEBHOOK_BRANCH=%s\n' "$branch" >> "$tmp"
chown root:root "$tmp"
chmod 0600 "$tmp"
mv -f "$tmp" "$HELPER_ENV"

systemctl restart arduino-helper

printf '\n[OK] GitHub webhook を有効化しました。\n'
printf 'Payload URL: https://<あなたの公開URL>/webhook/github\n'
printf 'Content type: application/json\n'
printf 'Events: Push only\n'
printf 'Branch: %s\n' "$branch"
