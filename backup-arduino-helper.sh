#!/usr/bin/env bash
set -euo pipefail
umask 077

if [ "$(id -u)" -ne 0 ]; then
  echo "エラー: root 権限で実行してください: sudo arduino-helper-backup" >&2
  exit 1
fi

backup_dir="${1:-/var/backups/arduino-helper}"
backup_dir="$(realpath -m "$backup_dir")"
case "$backup_dir/" in
  /var/lib/arduino-helper/*|/var/lib/arduino-build/*|/etc/arduino-helper/*|\
  /etc/systemd/system/*|/etc/sudoers.d/*)
    echo "エラー: バックアップ先を対象データの中には指定できません" >&2
    exit 1
    ;;
esac
install -d -o root -g root -m 0700 "$backup_dir"

stamp="$(date +%Y%m%d-%H%M%S)"
archive="$backup_dir/arduino-helper-$stamp.tar.gz"
if [ -e "$archive" ]; then
  echo "エラー: 同名のバックアップが既にあります: $archive" >&2
  exit 1
fi
tmp_archive="$(mktemp "$backup_dir/.arduino-helper-backup-XXXXXX.tar.gz")"
was_active=0
if systemctl is-active --quiet arduino-helper.service; then
  was_active=1
fi

finish() {
  result=$?
  trap - EXIT
  if [ -n "$tmp_archive" ]; then
    rm -f "$tmp_archive"
  fi
  if [ "$was_active" -eq 1 ] && ! systemctl start arduino-helper.service; then
    echo "エラー: バックアップ後にサービスを再起動できません。状態を確認してください" >&2
    result=1
  fi
  exit "$result"
}
trap finish EXIT

if [ "$was_active" -eq 1 ]; then
  systemctl stop arduino-helper.service
fi

tar --acls --xattrs -czf "$tmp_archive" -C / \
  var/lib/arduino-helper \
  var/lib/arduino-build \
  etc/arduino-helper \
  etc/systemd/system/arduino-helper.service \
  etc/sudoers.d/arduino-helper
chmod 0600 "$tmp_archive"
mv "$tmp_archive" "$archive"
tmp_archive=""
sha256sum "$archive" > "$archive.sha256"
chmod 0600 "$archive.sha256"
echo "バックアップを作成しました: $archive"
