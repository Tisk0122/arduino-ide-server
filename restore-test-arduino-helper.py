#!/usr/bin/env python3
import json
import os
import pathlib
import shutil
import socket
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import uuid

try:
    import pwd
except ImportError:
    pwd = None


MAX_ARCHIVE_ENTRIES = 250000
REQUIRED_PATHS = (
    "opt/arduino-helper/arduino-helper.py",
    "opt/arduino-helper/www/index.html",
    "opt/arduino-helper/www/admin.html",
    "opt/arduino-helper/www/docs.html",
    "opt/arduino-cli/arduino-cli",
    "var/lib/arduino-build/.arduino15/arduino-cli.yaml",
    "etc/arduino-helper/helper.env",
    "etc/arduino-helper/backup.env",
    "etc/systemd/system/arduino-helper.service",
    "etc/systemd/system/arduino-helper-backup.service",
    "etc/systemd/system/arduino-helper-backup.timer",
    "etc/systemd/system/arduino-helper-verify-backup.service",
    "etc/systemd/system/arduino-helper-verify-backup.timer",
    "etc/systemd/system/arduino-helper-full-restore.service",
    "etc/systemd/system/arduino-helper-full-restore.timer",
    "etc/systemd/system/arduino-helper-failure-notice@.service",
    "etc/systemd/system/arduino-helper-update.service",
    "etc/systemd/system/arduino-helper-update.timer",
    "etc/sudoers.d/arduino-helper",
    "usr/local/libexec/arduino-build-run",
    "usr/local/libexec/arduino-helper-update",
    "usr/local/libexec/arduino-helper-backup-status",
    "usr/local/libexec/arduino-helper-restore-test",
    "usr/local/sbin/arduino-helper-backup",
    "usr/local/sbin/arduino-helper-verify-backup",
)


class RestoreTestError(Exception):
    pass


def _safe_name(name):
    path = pathlib.PurePosixPath(name)
    if path.is_absolute() or not path.parts or any(part in ("", ".", "..") for part in path.parts):
        raise RestoreTestError("安全でないアーカイブパス: %s" % name)
    return path


def _link_target(path, target):
    link = pathlib.PurePosixPath(target)
    if link.is_absolute() or not target:
        raise RestoreTestError("安全でないシンボリックリンク: %s" % path)
    stack = list(path.parts[:-1])
    for part in link.parts:
        if part in ("", "."):
            continue
        if part == "..":
            if not stack:
                raise RestoreTestError("アーカイブ外を指すシンボリックリンク: %s" % path)
            stack.pop()
        else:
            stack.append(part)


def inspect_archive(archive, max_bytes):
    entries = {}
    total_size = 0
    with tarfile.open(str(archive), "r:gz") as tar:
        for member in tar:
            if len(entries) >= MAX_ARCHIVE_ENTRIES:
                raise RestoreTestError("アーカイブのファイル数が上限を超えています")
            path = _safe_name(member.name.rstrip("/"))
            key = path.as_posix()
            if key in entries:
                raise RestoreTestError("重複したアーカイブパス: %s" % key)
            if member.isdir():
                kind = "dir"
            elif member.isfile():
                kind = "file"
                total_size += member.size
                if member.size < 0 or total_size > max_bytes:
                    raise RestoreTestError("アーカイブ展開サイズが上限を超えています")
            elif member.issym():
                kind = "symlink"
                _link_target(path, member.linkname)
            else:
                raise RestoreTestError("未対応または危険なアーカイブ項目: %s" % key)
            entries[key] = (member, kind)

    symlinks = {key for key, (_, kind) in entries.items() if kind == "symlink"}
    for key in entries:
        parts = pathlib.PurePosixPath(key).parts
        if any(pathlib.PurePosixPath(*parts[:index]).as_posix() in symlinks
               for index in range(1, len(parts))):
            raise RestoreTestError("シンボリックリンク配下にアーカイブ項目があります: %s" % key)
    missing = [path for path in REQUIRED_PATHS if path not in entries or entries[path][1] != "file"]
    if missing:
        raise RestoreTestError("復元に必要なファイルがアーカイブにありません: %s" % ", ".join(missing))
    cli = entries["opt/arduino-cli/arduino-cli"][0]
    if cli.mode & 0o111 == 0:
        raise RestoreTestError("Arduino CLI の実行属性がありません")
    return entries, total_size


def extract_archive(archive, root, entries):
    for key, (_, kind) in sorted(entries.items(), key=lambda item: item[0].count("/")):
        if kind == "dir":
            (root / pathlib.Path(*pathlib.PurePosixPath(key).parts)).mkdir(
                mode=0o755, parents=True, exist_ok=True)
    with tarfile.open(str(archive), "r:gz") as tar:
        for member in tar:
            key = pathlib.PurePosixPath(member.name.rstrip("/")).as_posix()
            _, kind = entries[key]
            if kind != "file":
                continue
            target = root.joinpath(*pathlib.PurePosixPath(key).parts)
            target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            source = tar.extractfile(member)
            if source is None:
                raise RestoreTestError("アーカイブファイルを読み取れません: %s" % key)
            with source, target.open("xb") as output:
                shutil.copyfileobj(source, output, 1024 * 1024)
            target.chmod(member.mode & 0o777)
    for key, (member, kind) in entries.items():
        if kind == "symlink":
            target = root.joinpath(*pathlib.PurePosixPath(key).parts)
            target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            os.symlink(member.linkname, target)
    for key, (member, kind) in entries.items():
        if kind == "dir":
            target = root.joinpath(*pathlib.PurePosixPath(key).parts)
            target.chmod(member.mode & 0o777 or 0o755)


def check_database(root):
    database = root / "var/lib/arduino-helper/accounts.sqlite3"
    if not database.is_file():
        if any((root / ("var/lib/arduino-helper/accounts.sqlite3" + suffix)).exists()
               for suffix in ("-wal", "-shm")):
            raise RestoreTestError("アカウントDBのWAL/SHMだけがアーカイブにあります")
        return
    connection = sqlite3.connect(str(database), timeout=30)
    try:
        result = connection.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise RestoreTestError("復元したアカウントDBの integrity_check に失敗しました")
    finally:
        connection.close()


def cli_smoke_test(root):
    if pwd is None or not hasattr(os, "setuid"):
        raise RestoreTestError("Arduino CLI の復元検査はPOSIX環境でのみ実行できます")
    try:
        build_user = pwd.getpwnam("arduino-build")
    except KeyError as exc:
        raise RestoreTestError("arduino-build 実行ユーザーが存在しません") from exc

    cli_home = root / "restore-test-cli-home"
    directories = {
        "HOME": cli_home,
        "ARDUINO_CONFIG_FILE": cli_home / "arduino-cli.yaml",
        "ARDUINO_DIRECTORIES_DATA": cli_home / "data",
        "ARDUINO_DIRECTORIES_USER": cli_home / "user",
        "ARDUINO_DIRECTORIES_DOWNLOADS": cli_home / "downloads",
        "XDG_CONFIG_HOME": cli_home / ".config",
        "XDG_CACHE_HOME": cli_home / ".cache",
        "TMPDIR": cli_home / "tmp",
    }
    cli_home.mkdir(mode=0o700)
    for path in directories.values():
        if path.suffix == ".yaml":
            continue
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chown(path, build_user.pw_uid, build_user.pw_gid)
    os.chown(cli_home, build_user.pw_uid, build_user.pw_gid)
    config = directories["ARDUINO_CONFIG_FILE"]
    config.write_text(
        "directories:\n"
        "  data: %s\n"
        "  user: %s\n"
        "  downloads: %s\n" % (
            directories["ARDUINO_DIRECTORIES_DATA"],
            directories["ARDUINO_DIRECTORIES_USER"],
            directories["ARDUINO_DIRECTORIES_DOWNLOADS"],
        ),
        encoding="utf-8",
    )
    os.chown(config, build_user.pw_uid, build_user.pw_gid)
    os.chmod(config, 0o600)
    environment = os.environ.copy()
    environment.update({key: str(value) for key, value in directories.items()})
    environment.update({"GOPROXY": "off", "NO_PROXY": "*", "no_proxy": "*"})

    def drop_privileges():
        os.setgroups([])
        os.setgid(build_user.pw_gid)
        os.setuid(build_user.pw_uid)

    cli = root / "opt/arduino-cli/arduino-cli"
    for args, label in ((["version"], "version"), (["core", "list"], "core list")):
        try:
            result = subprocess.run(
                [str(cli)] + args, cwd=str(cli_home), env=environment,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                universal_newlines=True, timeout=60, check=False,
                preexec_fn=drop_privileges,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RestoreTestError("復元した Arduino CLI の %s を実行できません: %s" % (
                label, exc)) from exc
        if result.returncode != 0:
            raise RestoreTestError("復元した Arduino CLI の %s に失敗しました: %s" % (
                label, (result.stdout or "").strip()[-1000:]))


def _request(url, method="GET", body=None, token=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    if token:
        headers["Authorization"] = "Session " + token
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, response.read()


def service_smoke_test(root, log_path):
    if pwd is None:
        raise RestoreTestError("このフル復元検査はPOSIX環境でのみ実行できます")
    try:
        service_user = pwd.getpwnam("arduino-helper")
    except KeyError as exc:
        raise RestoreTestError("arduino-helper サービスユーザーが存在しません") from exc

    home = root / "var/lib/arduino-helper"
    work = home / "work"
    if work.is_symlink() or work.is_file():
        work.unlink()
    elif work.exists():
        shutil.rmtree(str(work))
    work.mkdir(mode=0o2770, parents=True, exist_ok=True)
    os.chown(home, service_user.pw_uid, service_user.pw_gid)
    os.chmod(home, 0o750)
    os.chown(work, service_user.pw_uid, service_user.pw_gid)
    for child in home.iterdir():
        if child.is_file() and not child.is_symlink():
            os.chown(child, service_user.pw_uid, service_user.pw_gid)

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    app = root / "opt/arduino-helper/arduino-helper.py"
    environment = os.environ.copy()
    disabled_wrapper = root / "restore-disabled-build.py"
    disabled_wrapper.write_text("raise SystemExit('compilation is disabled during restore tests')\n",
                                encoding="utf-8")
    environment.update({
        "HELPER_HOME": str(home),
        "HELPER_HOST": "127.0.0.1",
        "HELPER_PORT": str(port),
        "HELPER_TOKEN": "restore-test-user-token",
        "HELPER_ADMIN_TOKEN": "restore-test-admin-token",
        "HELPER_BUILD_USER": "",
        "HELPER_BUILD_WRAPPER": str(disabled_wrapper),
        "HOME": str(home),
    })

    def drop_privileges():
        os.setgroups([])
        os.setgid(service_user.pw_gid)
        os.setuid(service_user.pw_uid)

    with open(str(log_path), "wb") as output:
        process = subprocess.Popen(
            [sys.executable, str(app)], cwd=str(root / "opt/arduino-helper"),
            env=environment, stdout=output, stderr=subprocess.STDOUT,
            start_new_session=True, preexec_fn=drop_privileges,
        )
        try:
            base = "http://127.0.0.1:%d" % port
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RestoreTestError("復元したサービスが起動前に終了しました")
                try:
                    status, _ = _request(base + "/ping")
                    if status == 200:
                        break
                except (OSError, urllib.error.URLError):
                    time.sleep(0.25)
            else:
                raise RestoreTestError("復元したサービスの /ping がタイムアウトしました")

            status, page = _request(base + "/docs")
            if status != 200 or b"Arduino" not in page:
                raise RestoreTestError("復元したサービスの /docs ヘルスチェックに失敗しました")
            username = "restoretest" + uuid.uuid4().hex[:10]
            status, payload = _request(base + "/account/register", "POST", {
                "username": username,
                "password": "Restore-Test-" + uuid.uuid4().hex[:12],
                "name": "Restore test",
            })
            result = json.loads(payload.decode("utf-8"))
            if status != 201 or not result.get("token"):
                raise RestoreTestError("復元したサービスのアカウントAPI検査に失敗しました")
            status, payload = _request(base + "/account/me", token=result["token"])
            if status != 200 or json.loads(payload.decode("utf-8")).get("user", {}).get("username") != username:
                raise RestoreTestError("復元したサービスのセッション検査に失敗しました")
        finally:
            if process.poll() is None:
                os.killpg(process.pid, 15)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, 9)
                    process.wait(timeout=10)


def run(archive, max_gib):
    max_bytes = max_gib * 1024 ** 3
    entries, expanded_size = inspect_archive(archive, max_bytes)
    temp_root = pathlib.Path("/var/tmp")
    if not temp_root.is_dir():
        raise RestoreTestError("フル復元検査用の /var/tmp がありません")
    free_bytes = shutil.disk_usage(str(temp_root)).free
    if free_bytes < expanded_size + 1024 ** 3:
        raise RestoreTestError("一時領域に必要な空き容量がありません (展開 %.2f GiB、空き %.2f GiB)" % (
            expanded_size / 1024 ** 3, free_bytes / 1024 ** 3))
    if hasattr(os, "statvfs") and os.statvfs(str(temp_root)).f_favail < len(entries) + 100:
        raise RestoreTestError("一時領域の空きinodeが不足しています")
    with tempfile.TemporaryDirectory(prefix="arduino-helper-full-restore-", dir=str(temp_root)) as temp:
        root = pathlib.Path(temp)
        root.chmod(0o755)
        extract_archive(archive, root, entries)
        check_database(root)
        cli_smoke_test(root)
        log_path = root / "restore-test-service.log"
        try:
            service_smoke_test(root, log_path)
        except RestoreTestError as exc:
            if log_path.exists():
                lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
                detail = "\n".join(lines[-20:])
                if detail:
                    raise RestoreTestError("%s\n復元サービスのログ:\n%s" % (exc, detail)) from exc
            raise
    print("完全復元検査に成功しました (展開 %.2f GiB、ファイル %d 件)" % (
        expanded_size / 1024 ** 3, len(entries)))


if __name__ == "__main__":
    if len(sys.argv) != 3 or not sys.argv[2].isdigit():
        raise SystemExit("使用方法: restore-test-arduino-helper <archive.tar.gz> <max GiB>")
    max_gib = int(sys.argv[2])
    if not 1 <= max_gib <= 1024:
        raise SystemExit("max GiB は1〜1024の整数で指定してください")
    try:
        run(pathlib.Path(sys.argv[1]), max_gib)
    except (OSError, tarfile.TarError, sqlite3.Error, RestoreTestError) as exc:
        raise SystemExit("エラー: 完全復元検査に失敗しました: %s" % exc)
