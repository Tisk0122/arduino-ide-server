import importlib.util
import io
import json
import os
import re
import sqlite3
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RESTORE_SPEC = importlib.util.spec_from_file_location(
    "arduino_helper_restore_test", ROOT / "restore-test-arduino-helper.py")
restore_test = importlib.util.module_from_spec(RESTORE_SPEC)
RESTORE_SPEC.loader.exec_module(restore_test)


class AutoUpdateContractTests(unittest.TestCase):
    def test_backup_status_metadata_tracks_success_and_verification_without_secrets(self):
        if os.name != "posix":
            self.skipTest("backup status permissions require POSIX")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            backup_dir = root / "backups"
            backup_dir.mkdir()
            archive = backup_dir / "arduino-helper-20260102-030405.tar.gz"
            archive.write_bytes(b"private archive")
            status_path = root / "status" / "backup-status.json"
            environment = os.environ.copy()
            environment["HELPER_BACKUP_STATUS_FILE"] = str(status_path)
            subprocess.run([
                sys.executable, str(ROOT / "backup-status-arduino-helper.py"),
                "backup-success", str(archive), str(backup_dir),
            ], check=True, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            first = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(first["lastBackupArchive"], archive.name)
            self.assertEqual(first["lastBackupSizeBytes"], len(b"private archive"))
            self.assertEqual(first["backupCount"], 1)
            self.assertEqual(first["backupTotalBytes"], len(b"private archive"))
            self.assertNotIn("private archive", status_path.read_text(encoding="utf-8"))
            self.assertEqual(status_path.stat().st_mode & 0o777, 0o640)

            subprocess.run([
                sys.executable, str(ROOT / "backup-status-arduino-helper.py"),
                "verify-failed", "full",
            ], check=True, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            updated = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(updated["lastBackupSuccess"], first["lastBackupSuccess"])
            self.assertIs(updated["lastVerifyOk"], False)
            self.assertEqual(updated["lastVerifyMode"], "full")

    def test_linux_updater_deploys_and_rolls_back_in_isolated_filesystem(self):
        if os.name != "posix":
            self.skipTest("Linux updater integration requires POSIX")
        if os.geteuid() != 0 and not shutil.which("sudo"):
            self.skipTest("root or sudo is required for the updater deployment contract")

        updater = (ROOT / "github-update.sh").read_text(encoding="utf-8")
        match = re.search(r"^FILES=\(\n(.*?)^\)", updater, re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(match)
        files = re.findall(r'"([^"]+)"', match.group(1))

        def run_case(fail_path):
            temporary = tempfile.TemporaryDirectory()
            self.addCleanup(temporary.cleanup)
            test_root = Path(temporary.name)

            def make_writable():
                if os.geteuid() == 0:
                    shutil.chown(test_root, user=os.getuid(), group=os.getgid())
                    for current, dirs, names in os.walk(test_root):
                        for name in dirs + names:
                            shutil.chown(Path(current) / name, user=os.getuid(), group=os.getgid())
                else:
                    subprocess.run(
                        ["sudo", "-n", "chown", "-R",
                         "%d:%d" % (os.getuid(), os.getgid()), str(test_root)],
                        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    )
            self.addCleanup(make_writable)

            (test_root / ".arduino-helper-test-root").touch()
            repo = test_root / "source-repo"
            repo.mkdir()
            for relative in files:
                source = ROOT / relative
                target = repo / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(source), str(target))
            subprocess.run(["git", "init", "-b", "main"], cwd=str(repo), check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            subprocess.run(["git", "add", "."], cwd=str(repo), check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            subprocess.run([
                "git", "-c", "user.name=Integration Test", "-c",
                "user.email=integration@example.invalid", "commit", "-m", "test deployment",
            ], cwd=str(repo), check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

            conf = test_root / "etc/arduino-helper"
            conf.mkdir(parents=True)
            (conf / "github-webhook.env").write_text(
                "GITHUB_REPO=%s\nGITHUB_BRANCH=main\n" % repo, encoding="utf-8")
            shim_dir = test_root / "test-bin"
            shim_dir.mkdir()
            (shim_dir / "systemctl").write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$*\" >> \"$ARDUINO_HELPER_TEST_ROOT/systemctl.log\"\n"
                "if [ \"$1\" = show ]; then printf 'HELPER_PORT=8765\\n'; fi\n"
                "exit 0\n", encoding="utf-8")
            (shim_dir / "curl").write_text(
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                "output=''\nurl=''\n"
                "while [ \"$#\" -gt 0 ]; do\n"
                "  if [ \"$1\" = -o ]; then output=\"$2\"; shift 2; else url=\"$1\"; shift; fi\n"
                "done\n"
                "case \"$url\" in\n"
                "  */ping) body='{\"ok\":true}' ;;\n"
                "  */docs) body='Arduino Web IDE' ;;\n"
                "  */account) body='アカウントとクラウド保存' ;;\n"
                "  *) body='' ;;\n"
                "esac\n"
                "case \"$url\" in *\"${TEST_CURL_FAIL_PATH:-/no-failure}\"*) exit 22 ;; esac\n"
                "if [ -n \"$output\" ] && [ \"$output\" != /dev/null ]; then printf '%s' \"$body\" > \"$output\"; fi\n"
                "exit 0\n", encoding="utf-8")
            for shim in (shim_dir / "systemctl", shim_dir / "curl"):
                shim.chmod(0o755)

            app = test_root / "opt/arduino-helper"
            libexec = test_root / "usr/local/libexec"
            systemd = test_root / "etc/systemd/system"
            for directory in (app, libexec, systemd):
                directory.mkdir(parents=True, exist_ok=True)
            (app / "arduino-helper.py").write_text("previous application\n", encoding="utf-8")
            (libexec / "arduino-helper-update").write_text("previous updater\n", encoding="utf-8")
            update_dir = test_root / "var/lib/arduino-helper-updater"
            update_dir.mkdir(parents=True)
            applied = update_dir / "applied-commit"
            applied.write_text("previous-commit\n", encoding="utf-8")

            environment = os.environ.copy()
            environment.update({
                "ARDUINO_HELPER_TEST_ROOT": str(test_root),
                "TEST_CURL_FAIL_PATH": fail_path or "/no-failure",
                "PATH": str(shim_dir) + os.pathsep + environment["PATH"],
            })
            command = ["bash", str(ROOT / "github-update.sh")]
            if os.geteuid() != 0:
                command = [
                    "sudo", "-n", "env",
                    "ARDUINO_HELPER_TEST_ROOT=" + str(test_root),
                    "TEST_CURL_FAIL_PATH=" + (fail_path or "/no-failure"),
                    "PATH=" + environment["PATH"],
                ] + command
            result = subprocess.run(command, env=environment, text=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    timeout=90, check=False)
            return test_root, result, applied

        deployed_root, deployed, deployed_commit = run_case(None)
        self.assertEqual(deployed.returncode, 0, deployed.stdout)
        self.assertEqual(
            (deployed_root / "opt/arduino-helper/arduino-helper.py").read_bytes(),
            (ROOT / "arduino-helper.py").read_bytes(),
        )
        self.assertNotEqual(deployed_commit.read_text(encoding="utf-8"), "previous-commit\n")

        rollback_root, failed, rollback_commit = run_case("/account")
        self.assertNotEqual(failed.returncode, 0, failed.stdout)
        self.assertIn("ヘルスチェックに失敗", failed.stdout)
        self.assertEqual(
            (rollback_root / "opt/arduino-helper/arduino-helper.py").read_text(encoding="utf-8"),
            "previous application\n",
        )
        self.assertEqual(
            (rollback_root / "usr/local/libexec/arduino-helper-update").read_text(encoding="utf-8"),
            "previous updater\n",
        )
        self.assertEqual(rollback_commit.read_text(encoding="utf-8"), "previous-commit\n")
        self.assertFalse((rollback_root / "etc/systemd/system/arduino-helper-backup.timer").exists())

    def test_updater_deploys_itself_and_all_supported_ui_pages(self):
        updater = (ROOT / "github-update.sh").read_text(encoding="utf-8")
        match = re.search(r"^FILES=\(\n(.*?)^\)", updater, re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(match)
        files = set(re.findall(r'"([^"]+)"', match.group(1)))
        self.assertTrue({
            "arduino-helper.py",
            "backup-status-arduino-helper.py",
            "arduino-build-run",
            "www/index.html",
            "www/admin.html",
            "www/docs.html",
            "www/favicon.png",
            "www/manifest.webmanifest",
            "www/sw.js",
            "www/icons/icon-192.png",
            "www/icons/icon-512.png",
            "configure-github-webhook.sh",
            "github-update.sh",
            "arduino-helper-update.service",
            "arduino-helper-update.timer",
            "arduino-helper-backup.service",
            "arduino-helper-backup.timer",
            "arduino-helper-verify-backup.service",
            "arduino-helper-verify-backup.timer",
            "arduino-helper-full-restore.service",
            "arduino-helper-full-restore.timer",
            "arduino-helper-failure-notice@.service",
            "backup-arduino-helper.sh",
            "verify-arduino-helper-backup.sh",
            "restore-test-arduino-helper.py",
        }.issubset(files))
        self.assertIn('github-update.sh) printf', updater)
        self.assertIn("configure-github-webhook.sh) printf", updater)
        self.assertIn('install -d -o root -g root -m 0755 "$(dirname "$target")"', updater)
        self.assertIn('"http://127.0.0.1:$health_port/account"', updater)
        self.assertIn('for pwa_path in /manifest.webmanifest /sw.js /favicon.png /icons/icon-192.png /icons/icon-512.png', updater)
        self.assertIn("アカウントとクラウド保存", updater)

    def test_periodic_update_checks_remote_commit_and_records_success(self):
        updater = (ROOT / "github-update.sh").read_text(encoding="utf-8")
        timer = (ROOT / "arduino-helper-update.timer").read_text(encoding="utf-8")
        service = (ROOT / "arduino-helper-update.service").read_text(encoding="utf-8")
        self.assertIn("git ls-remote", updater)
        self.assertIn('"$UPDATE_DIR/applied-commit"', updater)
        self.assertIn("OnCalendar=*-*-* *:0/5:00", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("arduino-helper-update --check", service)
        self.assertIn('systemctl show --property=Environment --value "$SERVICE"', updater)
        self.assertIn('"http://127.0.0.1:$health_port/ping"', updater)
        self.assertIn('"http://127.0.0.1:$health_port/docs"', updater)
        self.assertIn('"http://127.0.0.1:$health_port/account"', updater)
        self.assertNotIn("http://127.0.0.1:8765/", updater)
        self.assertIn('systemctl is-enabled --quiet arduino-helper-update.timer', updater)
        self.assertIn('systemctl is-active --quiet arduino-helper-update.timer', updater)
        self.assertIn('systemctl enable --now arduino-helper-backup.timer', updater)
        self.assertIn('systemctl is-enabled --quiet arduino-helper-backup.timer', updater)
        self.assertIn('backup-arduino-helper.sh) printf \'%s\\n\' "$SBIN_DIR/arduino-helper-backup"', updater)
        self.assertIn("backup-status-arduino-helper.py", updater)

    def test_installer_and_webhook_setup_enable_the_update_timer(self):
        installer = (ROOT / "install.sh").read_text(encoding="utf-8")
        configure = (ROOT / "configure-github-webhook.sh").read_text(encoding="utf-8")
        self.assertIn('"$HERE/arduino-helper-update.service"', installer)
        self.assertIn('"$HERE/arduino-helper-update.timer"', installer)
        self.assertIn("systemctl enable --now arduino-helper-update.timer", installer)
        self.assertIn('"$HERE/arduino-helper-backup.service"', installer)
        self.assertIn('"$HERE/arduino-helper-backup.timer"', installer)
        self.assertIn("systemctl enable --now arduino-helper-backup.timer", installer)
        self.assertIn("systemctl enable --now arduino-helper-update.timer", configure)
        self.assertIn("arduino-helper-update.service", configure)
        self.assertIn("/usr/bin/systemctl --no-block start arduino-helper-update.service", configure)
        self.assertIn("/usr/bin/systemctl --no-block start arduino-helper-update.service", installer)

    def test_installer_sudoers_allows_arguments_only_for_validating_wrapper(self):
        installer = (ROOT / "install.sh").read_text(encoding="utf-8")
        self.assertIn('echo "$H_USER ALL=($B_USER) NOPASSWD: $WRAP *"', installer)
        self.assertIn('sudo -n -u "$B_USER" "$WRAP" -- --timeout 15 -- version', installer)
        self.assertNotIn('echo "$H_USER ALL=($B_USER) NOPASSWD: ALL"', installer)

    def test_backup_is_scheduled_verified_and_retained(self):
        installer = (ROOT / "install.sh").read_text(encoding="utf-8")
        updater = (ROOT / "github-update.sh").read_text(encoding="utf-8")
        script = (ROOT / "backup-arduino-helper.sh").read_text(encoding="utf-8")
        timer = (ROOT / "arduino-helper-backup.timer").read_text(encoding="utf-8")
        service = (ROOT / "arduino-helper-backup.service").read_text(encoding="utf-8")
        verify_timer = (ROOT / "arduino-helper-verify-backup.timer").read_text(encoding="utf-8")
        verify_service = (ROOT / "arduino-helper-verify-backup.service").read_text(encoding="utf-8")
        verify_script = (ROOT / "verify-arduino-helper-backup.sh").read_text(encoding="utf-8")
        self.assertIn("OnCalendar=*-*-* 03:20:00", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("HELPER_BACKUP_RETENTION=14", installer)
        self.assertIn("backup-status-arduino-helper.py", installer)
        self.assertIn("Environment=HELPER_BACKUP_STATUS_FILE=$H_HOME/backup-status.json", installer)
        self.assertIn("systemctl enable --now arduino-helper-backup.timer", installer)
        self.assertIn("systemctl enable --now arduino-helper-backup.timer", updater)
        self.assertIn("EnvironmentFile=-/etc/arduino-helper/backup.env", service)
        self.assertIn("flock -n 9", script)
        self.assertIn("flock -s -w 14400 8", script)
        self.assertIn("sha256sum", script)
        self.assertIn("etc/systemd/system/arduino-helper-backup.timer", script)
        self.assertIn('rclone copyto "$archive"', script)
        self.assertIn('archives[@]}', script)
        self.assertLess(script.index("systemctl start arduino-helper.service"), script.index("rclone copyto"))
        self.assertIn("OnCalendar=*-*-* 04:10:00", verify_timer)
        self.assertIn("arduino-helper-verify-backup", verify_service)
        self.assertIn("TimeoutStartSec=5h", verify_service)
        full_timer = (ROOT / "arduino-helper-full-restore.timer").read_text(encoding="utf-8")
        full_service = (ROOT / "arduino-helper-full-restore.service").read_text(encoding="utf-8")
        failure_notice = (ROOT / "arduino-helper-failure-notice@.service").read_text(encoding="utf-8")
        self.assertIn("OnCalendar=Mon *-*-* 04:40:00", full_timer)
        self.assertIn("HELPER_RESTORE_TEST_MAX_GIB=20", installer)
        self.assertIn("restore-test-arduino-helper.py", updater)
        self.assertIn("usr/local/libexec/arduino-helper-backup-status", script)
        self.assertIn("OnFailure=arduino-helper-failure-notice@", full_service)
        self.assertIn("journalctl -u %I", failure_notice)
        self.assertIn("opt/arduino-helper", script)
        self.assertIn("opt/arduino-cli", script)
        self.assertIn("systemctl enable --now arduino-helper-full-restore.timer", installer)
        self.assertIn("systemctl enable --now arduino-helper-full-restore.timer", updater)
        self.assertIn("PRAGMA integrity_check", verify_script)
        self.assertIn("sha256sum -c", verify_script)
        self.assertIn('/usr/local/libexec/arduino-helper-restore-test "$archive"', verify_script)
        self.assertIn("flock -s -w 14400 9", verify_script)
        installer = (ROOT / "install.sh").read_text(encoding="utf-8")
        updater = (ROOT / "github-update.sh").read_text(encoding="utf-8")
        self.assertIn("systemctl enable --now arduino-helper-verify-backup.timer", installer)
        self.assertIn("systemctl enable --now arduino-helper-verify-backup.timer", updater)

    def test_backup_restore_check_verifies_an_isolated_sqlite_copy(self):
        script = (ROOT / "verify-arduino-helper-backup.sh").read_text(encoding="utf-8")
        match = re.search(r'python3 - "\$archive" <<.PY.\n(.*?)\nPY', script, re.DOTALL)
        self.assertIsNotNone(match)
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "accounts.sqlite3"
            conn = sqlite3.connect(str(db))
            conn.execute("CREATE TABLE test_data(value TEXT)")
            conn.execute("INSERT INTO test_data VALUES('restored')")
            conn.commit()
            conn.close()
            archive = Path(temp) / "backup.tar.gz"
            with tarfile.open(str(archive), "w:gz") as tar:
                tar.add(str(db), arcname="var/lib/arduino-helper/accounts.sqlite3")
            result = subprocess.run(
                [sys.executable, "-c", match.group(1), str(archive)],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("integrity_check に成功", result.stdout)

    def test_full_restore_inspects_and_extracts_only_safe_archive_entries(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = root / "accounts.sqlite3"
            connection = sqlite3.connect(str(db))
            connection.execute("CREATE TABLE users(id INTEGER PRIMARY KEY)")
            connection.commit()
            connection.close()
            archive = root / "full.tar.gz"
            files = {
                "opt/arduino-helper/arduino-helper.py": b"print('restore')",
                "opt/arduino-helper/www/index.html": b"Arduino IDE",
                "opt/arduino-helper/www/docs.html": b"Arduino docs",
                "opt/arduino-cli/arduino-cli": b"not executed by restore test",
                "opt/arduino-helper/www/admin.html": b"Arduino admin",
            }
            with tarfile.open(str(archive), "w:gz") as tar:
                tar.add(str(db), arcname="var/lib/arduino-helper/accounts.sqlite3")
                for name in restore_test.REQUIRED_PATHS:
                    content = files.get(name, b"restore test fixture")
                    info = tarfile.TarInfo(name)
                    info.size = len(content)
                    info.mode = 0o755 if name.endswith("arduino-cli") else 0o644
                    tar.addfile(info, io.BytesIO(content))

            entries, expanded_size = restore_test.inspect_archive(archive, 1024 * 1024)
            self.assertGreater(expanded_size, 0)
            destination = root / "restored"
            destination.mkdir()
            restore_test.extract_archive(archive, destination, entries)
            restore_test.check_database(destination)
            self.assertEqual(
                (destination / "opt/arduino-helper/www/index.html").read_bytes(),
                b"Arduino IDE")

    def test_full_restore_rejects_archive_traversal_and_symlink_escape(self):
        for link in (False, True):
            with self.subTest(link=link), tempfile.TemporaryDirectory() as temp:
                archive = Path(temp) / "unsafe.tar.gz"
                info = tarfile.TarInfo("opt/escape" if link else "../escape")
                if link:
                    info.type = tarfile.SYMTYPE
                    info.linkname = "../../../../outside"
                else:
                    info.size = 1
                with tarfile.open(str(archive), "w:gz") as tar:
                    tar.addfile(info, io.BytesIO(b"x") if not link else None)
                with self.assertRaises(restore_test.RestoreTestError):
                    restore_test.inspect_archive(archive, 1024)


if __name__ == "__main__":
    unittest.main()
