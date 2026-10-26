#!/usr/bin/env bash
set -euo pipefail
umask 077

if [ "$(id -u)" -ne 0 ]; then
  echo "エラー: root 権限で実行してください" >&2
  exit 1
fi
if [ -r /etc/arduino-helper/backup.env ]; then
  set -a
  . /etc/arduino-helper/backup.env
  set +a
fi
exec 9>/run/lock/arduino-helper-backup.lock
if ! flock -s -w 14400 9; then
  echo "エラー: バックアップ処理の完了を4時間以内に確認できませんでした" >&2
  exit 1
fi

archive="${1:-}"
if [ -z "$archive" ]; then
  backup_dir="${HELPER_BACKUP_DIR:-/var/backups/arduino-helper}"
  archive="$(find "$backup_dir" -maxdepth 1 -type f -name 'arduino-helper-*.tar.gz' -printf '%f\n' \
    | sort -r | head -n 1)"
  [ -n "$archive" ] || { echo "エラー: 検証対象のバックアップがありません" >&2; exit 1; }
  archive="$backup_dir/$archive"
fi
archive="$(realpath -- "$archive")"
[ -f "$archive" ] || { echo "エラー: バックアップがありません: $archive" >&2; exit 1; }
checksum="$archive.sha256"
[ -f "$checksum" ] || { echo "エラー: SHA-256 ファイルがありません: $checksum" >&2; exit 1; }
(cd "$(dirname "$archive")" && sha256sum -c "$(basename "$checksum")")

python3 - "$archive" <<'PY'
import pathlib
import sqlite3
import sys
import tarfile
import tempfile

archive = pathlib.Path(sys.argv[1])
db_prefix = "var/lib/arduino-helper/accounts.sqlite3"
with tempfile.TemporaryDirectory(prefix="arduino-helper-restore-check-") as temp:
    target = pathlib.Path(temp) / "accounts.sqlite3"
    found = False
    with tarfile.open(str(archive), "r:gz") as tar:
        for member in tar:
            name = pathlib.PurePosixPath(member.name)
            if name.is_absolute() or ".." in name.parts:
                raise SystemExit("エラー: アーカイブ内に安全でないパスがあります")
            if member.name not in (db_prefix, db_prefix + "-wal", db_prefix + "-shm"):
                continue
            if not member.isfile():
                raise SystemExit("エラー: アカウントDBのアーカイブ形式が不正です")
            stream = tar.extractfile(member)
            if stream is None:
                raise SystemExit("エラー: アカウントDBを読み取れません")
            suffix = member.name[len(db_prefix):]
            with stream, open(str(target) + suffix, "wb") as output:
                while True:
                    block = stream.read(1024 * 1024)
                    if not block:
                        break
                    output.write(block)
            found = found or not suffix
    if found:
        conn = sqlite3.connect(str(target), timeout=15)
        try:
            result = conn.execute("PRAGMA integrity_check").fetchone()
            if not result or result[0] != "ok":
                raise SystemExit("エラー: 復元したアカウントDBの integrity_check に失敗しました")
        finally:
            conn.close()
        print("アカウントDBの隔離復元と integrity_check に成功しました")
    else:
        print("アーカイブにアカウントDBがありません。アーカイブ検証のみ成功しました")
PY
echo "バックアップ検証に成功しました: $archive"
