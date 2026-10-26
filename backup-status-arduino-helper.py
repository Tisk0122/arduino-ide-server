#!/usr/bin/env python3
import argparse
import json
import os
import pathlib
import re
import tempfile
import time


STATUS_FILE = pathlib.Path(os.environ.get(
    "HELPER_BACKUP_STATUS_FILE", "/var/lib/arduino-helper/backup-status.json"))


def update_status(args):
    now = int(time.time())
    try:
        with STATUS_FILE.open("r", encoding="utf-8") as source:
            status = json.load(source)
        if not isinstance(status, dict):
            raise ValueError("status root must be an object")
    except FileNotFoundError:
        status = {"schema": 1}

    if args.event == "backup-start":
        status["lastBackupAttempt"] = now
        status["lastBackupOk"] = None
    elif args.event == "backup-failed":
        status["lastBackupAttempt"] = now
        status["lastBackupOk"] = False
    elif args.event == "backup-success":
        archive = pathlib.Path(args.archive)
        backup_dir = pathlib.Path(args.backup_dir)
        archives = [path for path in backup_dir.glob("arduino-helper-*.tar.gz")
                    if re.fullmatch(r"arduino-helper-[0-9]{8}-[0-9]{6}\.tar\.gz", path.name)]
        status.update({
            "lastBackupAttempt": now,
            "lastBackupOk": True,
            "lastBackupSuccess": now,
            "lastBackupArchive": archive.name,
            "lastBackupSizeBytes": archive.stat().st_size,
            "backupCount": len(archives),
            "backupTotalBytes": sum(path.stat().st_size for path in archives),
        })
    elif args.event == "verify-start":
        status["lastVerifyAttempt"] = now
        status["lastVerifyOk"] = None
        status["lastVerifyMode"] = args.mode
    elif args.event in ("verify-success", "verify-failed"):
        status["lastVerifyAt"] = now
        status["lastVerifyOk"] = args.event == "verify-success"
        status["lastVerifyMode"] = args.mode
    else:
        raise ValueError("unsupported status event")

    parent = STATUS_FILE.parent
    parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".backup-status-", dir=str(parent))
    try:
        group_id = parent.stat().st_gid
        with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
            json.dump(status, destination, separators=(",", ":"), sort_keys=True)
            destination.write("\n")
            destination.flush()
            os.fsync(destination.fileno())
        os.chmod(temporary, 0o640)
        if os.geteuid() == 0:
            os.chown(temporary, 0, group_id)
        os.replace(temporary, str(STATUS_FILE))
        directory_fd = os.open(str(parent), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def main():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="event", required=True)
    subparsers.add_parser("backup-start")
    subparsers.add_parser("backup-failed")
    success = subparsers.add_parser("backup-success")
    success.add_argument("archive")
    success.add_argument("backup_dir")
    subparsers.add_parser("verify-start").add_argument("mode", choices=("light", "full"))
    verify_success = subparsers.add_parser("verify-success")
    verify_success.add_argument("mode", choices=("light", "full"))
    verify_failed = subparsers.add_parser("verify-failed")
    verify_failed.add_argument("mode", choices=("light", "full"))
    update_status(parser.parse_args())


if __name__ == "__main__":
    main()
