#!/usr/bin/env bash
# GitHub webhook の初回設定。root 専用。
set -euo pipefail
CONF_DIR=/etc/arduino-helper
ENV_FILE="$CONF_DIR/github-webhook.env"
[ "$(id -u)" -eq 0 ] || { echo "sudo bash configure-github-webhook.sh で実行してください" >&2; exit 1; }
install -d -o root -g root -m 0755 "$CONF_DIR"
printf 'GitHub repository URL (例: https://github.com/Tisk0122/arduino-ide-server.git): '
read -r repo
printf 'GitHub branch [main]: '
read -r branch
branch="${branch:-main}"
printf 'Webhook Secret: '
read -r secret
case "$repo" in https://github.com/*/*|git@github.com:*/*) ;; *) echo 'リポジトリURLが不正です' >&2; exit 2;; esac
case "$branch" in ''|*[^A-Za-z0-9._/-]*) echo 'ブランチ名が不正です' >&2; exit 2;; esac
[ "${#secret}" -ge 32 ] || { echo 'Webhook Secret は32文字以上にしてください' >&2; exit 2; }
case "$repo" in
  https://github.com/*/*) full_name="${repo#https://github.com/}" ;;
  git@github.com:*) full_name="${repo#git@github.com:}" ;;
  *) echo 'リポジトリURLが不正です' >&2; exit 2;;
esac
full_name="${full_name%.git}"
case "$full_name" in */*) ;; *) echo 'GitHub owner/repo を取得できません' >&2; exit 2;; esac
cat > "$ENV_FILE" <<EOF2
GITHUB_REPO=$repo
GITHUB_WEBHOOK_REPO=$full_name
GITHUB_BRANCH=$branch
EOF2
chmod 0600 "$ENV_FILE"
# 秘密鍵は arduino-helper.py が読む別の環境ファイルに保存する。
grep -v '^GITHUB_WEBHOOK_SECRET=' /etc/arduino-helper/helper.env 2>/dev/null > /tmp/helper.env.$$ || true
printf 'GITHUB_WEBHOOK_SECRET=%s\n' "$secret" >> /tmp/helper.env.$$
chown root:root /tmp/helper.env.$$
chmod 0600 /tmp/helper.env.$$
mv -f /tmp/helper.env.$$ /etc/arduino-helper/helper.env
systemctl restart arduino-helper
printf '\n[OK] GitHub webhook を有効化しました。\n'
printf 'Payload URL: https://<あなたの公開URL>/webhook/github\n'
printf 'Content type: application/json\n'
printf 'Events: Push only\n'
printf 'Branch: %s\n' "$branch"
