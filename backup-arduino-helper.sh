#!/usr/bin/env bash
set -euo pipefail
umask 077

if [ "$(id -u)" -ne 0 ]; then
  echo "エラー: root 権限で実行してください: sudo arduino-helper-backup" >&2
  exit 1
fi
if [ -r /etc/arduino-helper/backup.env ]; then
  set -a
  . /etc/arduino-helper/backup.env
  set +a
fi
exec 9>/run/lock/arduino-helper-backup.lock
if ! flock -n 9; then
  echo "エラー: 別のバックアップ処理が実行中です" >&2
  exit 1
fi

backup_dir="${1:-${HELPER_BACKUP_DIR:-/var/backups/arduino-helper}}"
backup_dir="$(realpath -m "$backup_dir")"
retention="${HELPER_BACKUP_RETENTION:-14}"
remote="${HELPER_BACKUP_REMOTE:-}"
if ! [[ "$retention" =~ ^[0-9]+$ ]] || [ "$retention" -lt 1 ] || [ "$retention" -gt 3650 ]; then
  echo "エラー: HELPER_BACKUP_RETENTION は1〜3650の整数にしてください" >&2
  exit 1
fi
if [ -n "$remote" ]; then
  if [[ "$remote" != *:* ]] || ! command -v rclone >/dev/null 2>&1; then
    echo "エラー: HELPER_BACKUP_REMOTE には rclone remote:path を指定し、rclone をインストールしてください" >&2
    exit 1
  fi
fi
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
tmp_checksum="$(mktemp "$backup_dir/.arduino-helper-checksum-XXXXXX")"
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
  if [ -n "$tmp_checksum" ]; then
    rm -f "$tmp_checksum"
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
  etc/systemd/system/arduino-helper-backup.service \
  etc/systemd/system/arduino-helper-backup.timer \
  etc/systemd/system/arduino-helper-verify-backup.service \
  etc/systemd/system/arduino-helper-verify-backup.timer \
  usr/local/sbin/arduino-helper-verify-backup \
  etc/sudoers.d/arduino-helper
if [ "$was_active" -eq 1 ]; then
  systemctl start arduino-helper.service
  was_active=0
fi
chmod 0600 "$tmp_archive"
digest="$(sha256sum "$tmp_archive" | awk '{print $1}')"
printf '%s  %s\n' "$digest" "$(basename "$archive")" > "$tmp_checksum"
chmod 0600 "$tmp_checksum"
mv "$tmp_archive" "$archive"
tmp_archive=""
mv "$tmp_checksum" "$archive.sha256"
tmp_checksum=""
if [ -n "$remote" ]; then
  rclone copyto "$archive" "$remote/$(basename "$archive")"
  rclone copyto "$archive.sha256" "$remote/$(basename "$archive").sha256"
fi
mapfile -t archives < <(find "$backup_dir" -maxdepth 1 -type f -name 'arduino-helper-*.tar.gz' -printf '%f\n' | sort -r)
if [ "${#archives[@]}" -gt "$retention" ]; then
  for old in "${archives[@]:$retention}"; do
    rm -f -- "$backup_dir/$old" "$backup_dir/$old.sha256"
  done
fi
echo "バックアップを作成しました: $archive"
