import importlib.util
import json
import os
import sqlite3
import struct
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMP_HOME = tempfile.TemporaryDirectory()
os.environ["HELPER_HOME"] = TEMP_HOME.name
SPEC = importlib.util.spec_from_file_location("arduino_helper_accounts", ROOT / "arduino-helper.py")
server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(server)


class AccountApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        server.HOME = Path(self.temp.name)
        server.WORK_DIR = server.HOME / "work"
        server.WORK_DIR.mkdir()
        server.ACCOUNT_DB_INITIALIZED_PATH = None
        server.BACKUP_STATUS_FILE = server.HOME / "backup-status.json"
        server.ACCOUNT_QUOTA_MB = 50
        server.ACCOUNT_VERSION_LIMIT = 100
        server.ACCOUNT_SESSION_CLEANUP_LAST = 0
        server.ACCOUNT_SIGNUP_ATTEMPTS.clear()
        server.AUTH_FAILS.clear()
        server.AUTH_BLOCK.clear()
        server.TOKEN = "test-user-token"
        server.ADMIN_TOKEN = "test-admin-token"
        self.httpd = server.Server(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def _stop_server(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)

    def request(self, path, method="GET", body=None, token=None, headers=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request_headers = dict(headers or {})
        if data is not None:
            request_headers["Content-Type"] = "application/json"
        if token:
            request_headers["Authorization"] = "Session " + token
        req = urllib.request.Request(self.base + path, data=data, headers=request_headers, method=method)
        try:
            response = urllib.request.urlopen(req, timeout=5)
        except urllib.error.HTTPError as exc:
            response = exc
        self.last_headers = dict(response.headers.items())
        payload = response.read()
        return response.code, json.loads(payload.decode("utf-8")) if payload else {}

    def register(self, username="member01", password="MemberPass-01", name="Member"):
        status, result = self.request("/account/register", "POST", {
            "username": username, "password": password, "name": name,
        })
        self.assertEqual(status, 201)
        return result

    def login(self, username, password):
        status, result = self.request("/account/login", "POST", {
            "username": username, "password": password,
        })
        self.assertEqual(status, 200)
        return result

    def test_public_registration_creates_personal_account_and_session(self):
        result = self.register()
        self.assertEqual(result["user"]["username"], "member01")
        self.assertEqual(result["user"]["name"], "Member")
        self.assertNotIn("role", result["user"])
        status, data = self.request("/account/me", token=result["token"])
        self.assertEqual(status, 200)
        self.assertEqual(data["user"]["id"], result["user"]["id"])

    def test_registration_validates_fields_and_rejects_duplicate_usernames(self):
        status, error = self.request("/account/register", "POST", {
            "username": "a", "password": "short",
        })
        self.assertEqual(status, 400)
        self.assertIn("ユーザー名", error["error"])

        self.register()
        status, error = self.request("/account/register", "POST", {
            "username": "MEMBER01", "password": "AnotherPass-01",
        })
        self.assertEqual(status, 409)
        self.assertIn("すでに使われています", error["error"])

    def test_login_counts_short_wrong_passwords_toward_rate_limit(self):
        self.register()
        logged_in = self.login("MEMBER01", "MemberPass-01")
        self.assertTrue(logged_in["token"])
        for _ in range(11):
            status, _ = self.request("/account/login", "POST", {
                "username": "member01", "password": "x",
            })
        self.assertEqual(status, 429)

    def test_changing_password_revokes_every_session_and_is_audited(self):
        first = self.register()
        second = self.login("member01", "MemberPass-01")
        status, result = self.request("/account/password", "POST", {
            "currentPassword": "MemberPass-01", "newPassword": "UpdatedPass-01",
        }, token=first["token"])
        self.assertEqual(status, 200)
        self.assertTrue(result["reauthenticate"])
        for token in (first["token"], second["token"]):
            status, _ = self.request("/account/me", token=token)
            self.assertEqual(status, 401)
        updated = self.login("member01", "UpdatedPass-01")
        self.assertTrue(updated["token"])
        with server.account_db() as conn:
            event = conn.execute(
                "SELECT actor_id,action,details FROM account_audit WHERE action='account.password_change'"
            ).fetchone()
        self.assertEqual(event["actor_id"], first["user"]["id"])
        self.assertEqual(json.loads(event["details"]), {})

    def test_password_change_requires_current_password_and_rate_limits_failures(self):
        account = self.register()
        for _ in range(11):
            status, _ = self.request("/account/password", "POST", {
                "currentPassword": "x", "newPassword": "UpdatedPass-01",
            }, token=account["token"])
        self.assertEqual(status, 429)
        status, _ = self.request("/account/password", "POST", {
            "currentPassword": "MemberPass-01", "newPassword": "UpdatedPass-01",
        }, token=account["token"])
        self.assertEqual(status, 429)

    def test_login_limits_active_sessions_per_user(self):
        account = self.register()
        tokens = [account["token"]]
        for _ in range(server.ACCOUNT_SESSION_LIMIT + 2):
            tokens.append(self.login("member01", "MemberPass-01")["token"])
        with server.account_db() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM account_sessions WHERE user_id=?",
                (account["user"]["id"],),
            ).fetchone()[0]
        self.assertEqual(count, server.ACCOUNT_SESSION_LIMIT)
        status, _ = self.request("/account/me", token=tokens[0])
        self.assertEqual(status, 401)
        status, _ = self.request("/account/me", token=tokens[-1])
        self.assertEqual(status, 200)

    def test_personal_sketches_are_isolated_between_users(self):
        first = self.register()
        second = self.register("member02", "SecondPass-01", "Second")
        sketch = {
            "id": "sketch_one",
            "name": "My sketch",
            "files": [{"name": "sketch_one.ino", "content": "void setup() {}"}],
        }
        status, saved = self.request("/account/sketches/sketch_one", "PUT",
                                     {"sketch": sketch, "revision": 0}, first["token"])
        self.assertEqual(status, 200)
        self.assertEqual(saved["revision"], 1)
        status, remote = self.request("/account/sketches", token=second["token"])
        self.assertEqual(status, 200)
        self.assertEqual(remote["sketches"], [])
        status, remote = self.request("/account/sketches", token=first["token"])
        self.assertEqual(status, 200)
        self.assertEqual(remote["sketches"][0]["name"], "My sketch")

    def test_sketch_history_is_private_and_can_be_restored_with_revision_check(self):
        owner = self.register()
        other = self.register("member02", "SecondPass-01", "Second")
        original = {
            "id": "history_one",
            "name": "Original",
            "files": [{"name": "history_one.ino", "content": "void setup() {}"}],
            "buildProfile": {
                "schema": 1, "fqbn": "arduino:avr:uno", "cliVersion": "arduino-cli 1.5.1",
                "capturedAt": 1, "cores": [{"id": "arduino:avr", "version": "1.8.6"}],
                "libraries": [{"name": "Wire", "version": "1.0.0"}],
            },
        }
        status, saved = self.request("/account/sketches/history_one", "PUT",
                                     {"sketch": original, "revision": 0}, owner["token"])
        self.assertEqual(status, 200)
        updated = dict(original, name="Updated",
                       files=[{"name": "history_one.ino", "content": "void setup(){ pinMode(LED_BUILTIN, OUTPUT); }"}])
        status, saved = self.request("/account/sketches/history_one", "PUT",
                                     {"sketch": updated, "revision": saved["revision"]}, owner["token"])
        self.assertEqual(status, 200)
        self.assertEqual(saved["revision"], 2)

        status, versions = self.request(
            "/account/sketches/history_one/versions", token=owner["token"])
        self.assertEqual(status, 200)
        self.assertEqual(versions["versions"][0]["revision"], 1)
        status, snapshot = self.request(
            "/account/sketches/history_one/versions/1", token=owner["token"])
        self.assertEqual(status, 200)
        self.assertEqual(snapshot["sketch"]["name"], "Original")
        self.assertEqual(snapshot["sketch"]["buildProfile"]["cores"][0]["version"], "1.8.6")

        status, _ = self.request(
            "/account/sketches/history_one/versions", token=other["token"])
        self.assertEqual(status, 404)
        status, _ = self.request("/account/sketches/history_one/restore", "POST", {
            "currentRevision": 1, "targetRevision": 1,
        }, owner["token"])
        self.assertEqual(status, 409)
        status, restored = self.request("/account/sketches/history_one/restore", "POST", {
            "currentRevision": 2, "targetRevision": 1,
        }, owner["token"])
        self.assertEqual(status, 200)
        self.assertEqual(restored["revision"], 3)
        status, sketches = self.request("/account/sketches", token=owner["token"])
        self.assertEqual(sketches["sketches"][0]["name"], "Original")
        status, versions = self.request(
            "/account/sketches/history_one/versions", token=owner["token"])
        self.assertEqual({v["revision"] for v in versions["versions"]}, {1, 2})

    def test_history_retention_is_configurable_and_selected_deletion_preserves_current_sketch(self):
        owner = self.register()
        other = self.register("member02", "SecondPass-01", "Second")
        server.ACCOUNT_VERSION_LIMIT = 2
        sketch = {
            "id": "trim_history",
            "name": "Version 1",
            "files": [{"name": "trim_history.ino", "content": "void setup() {}"}],
        }
        status, saved = self.request("/account/sketches/trim_history", "PUT",
                                     {"sketch": sketch, "revision": 0}, owner["token"])
        self.assertEqual(status, 200)
        for revision in range(2, 5):
            sketch = dict(sketch, name="Version %d" % revision)
            status, saved = self.request(
                "/account/sketches/trim_history", "PUT",
                {"sketch": sketch, "revision": saved["revision"]}, owner["token"])
            self.assertEqual(status, 200)

        status, versions = self.request(
            "/account/sketches/trim_history/versions", token=owner["token"])
        self.assertEqual(status, 200)
        self.assertEqual(versions["maxVersions"], 2)
        self.assertEqual({item["revision"] for item in versions["versions"]}, {2, 3})
        before = self.request("/account/storage", token=owner["token"])[1]["usedBytes"]

        status, deleted = self.request("/account/sketches/trim_history/versions", "DELETE",
                                       {"revisions": [2]}, owner["token"])
        self.assertEqual(status, 200)
        self.assertEqual(deleted["deleted"], 1)
        self.assertLess(deleted["storage"]["usedBytes"], before)
        status, current = self.request("/account/sketches", token=owner["token"])
        self.assertEqual(current["sketches"][0]["revision"], 4)
        self.assertEqual(current["sketches"][0]["name"], "Version 4")

        status, _ = self.request("/account/sketches/trim_history/versions", "DELETE",
                                 {"revisions": [3]}, other["token"])
        self.assertEqual(status, 404)
        status, _ = self.request("/account/sketches/trim_history/versions", "DELETE",
                                 {"revisions": [3, 99]}, owner["token"])
        self.assertEqual(status, 404)
        status, _ = self.request("/account/sketches/trim_history/versions", "DELETE",
                                 {"revisions": [3, 3]}, owner["token"])
        self.assertEqual(status, 400)
        status, versions = self.request(
            "/account/sketches/trim_history/versions", token=owner["token"])
        self.assertEqual({item["revision"] for item in versions["versions"]}, {3})

    def test_bulk_history_deletion_handles_maximum_supported_selection(self):
        owner = self.register()
        sketch = {
            "id": "bulk_history",
            "name": "Current",
            "files": [{"name": "bulk_history.ino", "content": "void setup() {}"}],
        }
        status, saved = self.request("/account/sketches/bulk_history", "PUT",
                                     {"sketch": sketch, "revision": 0}, owner["token"])
        self.assertEqual(status, 200)
        with server.account_db() as conn:
            conn.executemany(
                "INSERT INTO account_sketch_versions"
                "(sketch_id,user_id,revision,editor_id,data,created_at) VALUES(?,?,?,?,?,?)",
                [("bulk_history", owner["user"]["id"], revision, owner["user"]["id"],
                  json.dumps(dict(sketch, name="Old %d" % revision)), revision)
                 for revision in range(1, 1001)],
            )

        status, result = self.request(
            "/account/sketches/bulk_history/versions", "DELETE",
            {"revisions": list(range(1, 1001))}, owner["token"])
        self.assertEqual(status, 200)
        self.assertEqual(result["deleted"], 1000)
        status, current = self.request("/account/sketches", token=owner["token"])
        self.assertEqual(current["sketches"][0]["revision"], saved["revision"])
        self.assertEqual(current["sketches"][0]["name"], "Current")

    def test_account_quota_counts_current_sketch_and_history_and_rolls_back_overflow(self):
        owner = self.register()
        server.ACCOUNT_QUOTA_MB = 1
        sketch = {
            "id": "quota_test", "name": "Quota",
            "files": [{"name": "quota_test.ino", "content": "a" * 600_000}],
        }
        status, created = self.request("/account/sketches/quota_test", "PUT",
                                       {"sketch": sketch, "revision": 0}, owner["token"])
        self.assertEqual(status, 200)
        updated = dict(sketch, files=[{"name": "quota_test.ino", "content": "b" * 600_000}])
        status, error = self.request("/account/sketches/quota_test", "PUT",
                                     {"sketch": updated, "revision": created["revision"]}, owner["token"])
        self.assertEqual(status, 413)
        self.assertIn("上限", error["error"])
        self.assertGreater(error["storage"]["usedBytes"], 1024 * 1024)
        status, sketches = self.request("/account/sketches", token=owner["token"])
        self.assertEqual(status, 200)
        self.assertEqual(sketches["sketches"][0]["revision"], 1)
        self.assertEqual(sketches["sketches"][0]["files"][0]["content"], "a" * 600_000)

    def test_export_contains_all_versions_and_build_reproduction_arguments(self):
        owner = self.register()
        sketch = {
            "id": "export_one", "name": "Exported",
            "files": [{"name": "export_one.ino", "content": "void setup() {}"}],
            "buildProfile": {
                "schema": 1, "fqbn": "arduino:avr:uno", "cliVersion": "arduino-cli 1.5.1",
                "capturedAt": 1, "cores": [{"id": "arduino:avr", "version": "1.8.6"}],
                "libraries": [{"name": "Wire", "version": "1.0.0"}],
            },
        }
        status, first = self.request("/account/sketches/export_one", "PUT",
                                     {"sketch": sketch, "revision": 0}, owner["token"])
        self.assertEqual(status, 200)
        updated = dict(sketch, name="Updated")
        status, _ = self.request("/account/sketches/export_one", "PUT",
                                 {"sketch": updated, "revision": first["revision"]}, owner["token"])
        self.assertEqual(status, 200)
        status, exported = self.request("/account/export", token=owner["token"])
        self.assertEqual(status, 200)
        entry = exported["sketches"][0]
        self.assertEqual(entry["name"], "Updated")
        self.assertEqual(entry["history"][0]["sketch"]["name"], "Exported")
        self.assertEqual(entry["reproduction"]["fqbn"], "arduino:avr:uno")
        self.assertEqual(entry["reproduction"]["boardPackageInstallArgs"],
                         [["core", "install", "arduino:avr@1.8.6"]])
        self.assertEqual(entry["reproduction"]["libraryInstallArgs"],
                         [["lib", "install", "Wire@1.0.0"]])

    def test_account_deletion_requires_password_and_removes_cloud_data(self):
        owner = self.register()
        sketch = {
            "id": "delete_one", "name": "Delete me",
            "files": [{"name": "delete_one.ino", "content": "void setup() {}"}],
        }
        status, _ = self.request("/account/sketches/delete_one", "PUT",
                                 {"sketch": sketch, "revision": 0}, owner["token"])
        self.assertEqual(status, 200)
        status, _ = self.request("/account/delete", "POST", {"password": "WrongPass-01"}, owner["token"])
        self.assertEqual(status, 403)
        status, result = self.request("/account/delete", "POST",
                                      {"password": "MemberPass-01"}, owner["token"])
        self.assertEqual(status, 200)
        self.assertTrue(result["deleted"])
        status, _ = self.request("/account/me", token=owner["token"])
        self.assertEqual(status, 401)
        with server.account_db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM account_users").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM account_sketches").fetchone()[0], 0)
            event = conn.execute(
                "SELECT actor_id,details FROM account_audit WHERE action='account.delete'"
            ).fetchone()
            self.assertIsNone(event["actor_id"])
            self.assertEqual(json.loads(event["details"])["actorName"], "member01")

    def test_admin_password_reset_revokes_sessions_and_returns_temporary_password(self):
        owner = self.register()
        status, reset = self.request(
            "/admin/api/accounts/" + owner["user"]["id"] + "/reset-password",
            "POST", {}, headers={"Authorization": "Bearer " + server.ADMIN_TOKEN},
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(reset["temporaryPassword"]), 32)
        status, _ = self.request("/account/me", token=owner["token"])
        self.assertEqual(status, 401)
        logged_in = self.login("member01", reset["temporaryPassword"])
        self.assertTrue(logged_in["token"])
        with server.account_db() as conn:
            self.assertTrue(conn.execute(
                "SELECT 1 FROM account_audit WHERE action='account.admin_password_reset'"
            ).fetchone())

    def test_teacher_and_class_routes_are_removed(self):
        status, error = self.request("/teacher")
        self.assertEqual(status, 404)
        self.assertIn("提供していません", error["error"])
        status, _ = self.request("/teacher/api/classes", token="arbitrary-session-token")
        self.assertEqual(status, 404)
        status, _ = self.request(
            "/admin/api/teachers",
            headers={"Authorization": "Bearer " + server.ADMIN_TOKEN},
        )
        self.assertEqual(status, 404)

    def test_admin_account_list_endpoint_is_reachable(self):
        self.register()
        status, result = self.request(
            "/admin/api/accounts",
            headers={"Authorization": "Bearer " + server.ADMIN_TOKEN},
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(result["users"]), 1)
        self.assertEqual(result["users"][0]["username"], "member01")
        self.assertEqual(result["users"][0]["activeSessionCount"], 1)

    def test_admin_can_revoke_all_sessions_and_review_audited_actions(self):
        owner = self.register()
        another_session = self.login("member01", "MemberPass-01")
        uid = owner["user"]["id"]
        admin_headers = {"Authorization": "Bearer " + server.ADMIN_TOKEN}

        status, forbidden = self.request(
            "/admin/api/accounts/" + uid + "/revoke-sessions", "POST", {},
            headers={"Authorization": "Bearer " + server.TOKEN})
        self.assertEqual(status, 403)
        self.assertIn("管理者", forbidden["error"])

        status, result = self.request(
            "/admin/api/accounts/" + uid + "/revoke-sessions", "POST", {},
            headers=admin_headers)
        self.assertEqual(status, 200)
        self.assertEqual(result["revokedSessions"], 2)
        for token in (owner["token"], another_session["token"]):
            status, _ = self.request("/account/me", token=token)
            self.assertEqual(status, 401)

        status, audit = self.request("/admin/api/accounts/audit?limit=10", headers=admin_headers)
        self.assertEqual(status, 200)
        event = next(item for item in audit["items"]
                     if item["action"] == "account.admin_sessions_revoked")
        self.assertEqual(event["target"], uid)
        self.assertEqual(event["details"], {"actor": "admin-token", "revokedSessions": 2})

        status, _ = self.request(
            "/admin/api/accounts/audit",
            headers={"Authorization": "Bearer " + server.TOKEN})
        self.assertEqual(status, 403)

    def test_admin_toggle_and_delete_actions_keep_audit_actor_details(self):
        owner = self.register()
        uid = owner["user"]["id"]
        admin_headers = {"Authorization": "Bearer " + server.ADMIN_TOKEN}

        status, disabled = self.request(
            "/admin/api/accounts/" + uid + "/toggle", "POST", {}, headers=admin_headers)
        self.assertEqual(status, 200)
        self.assertFalse(disabled["active"])
        status, enabled = self.request(
            "/admin/api/accounts/" + uid + "/toggle", "POST", {}, headers=admin_headers)
        self.assertEqual(status, 200)
        self.assertTrue(enabled["active"])

        status, deleted = self.request(
            "/admin/api/accounts/" + uid, "DELETE", headers=admin_headers)
        self.assertEqual(status, 200)
        self.assertEqual(deleted["deleted"], uid)
        status, audit = self.request("/admin/api/accounts/audit?limit=20", headers=admin_headers)
        self.assertEqual(status, 200)
        actions = {item["action"]: item for item in audit["items"]}
        toggle_states = {
            item["details"]["active"] for item in audit["items"]
            if item["action"] == "account.admin_toggle"
        }
        self.assertEqual(toggle_states, {False, True})
        self.assertEqual(actions["account.admin_delete"]["actorName"], "管理者")
        self.assertEqual(actions["account.register"]["actorName"], "member01")

    def test_admin_overview_exposes_threshold_warnings_and_backup_state(self):
        now = int(server.time.time())
        server.WARN_DISK_PERCENT = 85
        server.WARN_MEMORY_PERCENT = 90
        server.WARN_DATABASE_MB = 512
        server.WARN_BACKUP_AGE_HOURS = 36
        server.WARN_COMPILE_FAIL_PERCENT = 25
        server.WARN_QUEUE_PERCENT = 80
        server.BACKUP_STATUS_FILE.write_text(json.dumps({
            "lastBackupSuccess": now - 48 * 3600,
            "lastBackupArchive": "arduino-helper-old.tar.gz",
            "lastBackupSizeBytes": 2048,
            "backupCount": 2,
            "backupTotalBytes": 4096,
            "lastVerifyOk": False,
            "lastVerifyAt": now - 47 * 3600,
            "lastVerifyMode": "full",
        }), encoding="utf-8")
        records = [{"kind": "compile", "success": False, "state": "error"} for _ in range(5)]
        monitoring = server._admin_monitoring({
            "disk": {"usedPercent": 90},
            "memory": {"usedPercent": 95},
        }, records, server.MAX_QUEUE)
        codes = {warning["code"] for warning in monitoring["warnings"]}
        self.assertTrue({
            "disk_usage", "memory_usage", "compile_queue", "backup_stale",
            "backup_verify_failed", "compile_failures",
        }.issubset(codes))
        self.assertEqual(monitoring["backup"]["archive"], "arduino-helper-old.tar.gz")
        self.assertEqual(monitoring["compileWindow"]["failurePercent"], 100)
        status, overview = self.request(
            "/admin/api/overview",
            headers={"Authorization": "Bearer " + server.ADMIN_TOKEN})
        self.assertEqual(status, 200)
        self.assertIn("warnings", overview["monitoring"])
        self.assertIn("backup", overview["monitoring"])

    def test_favicon_is_available_without_authentication(self):
        expected = (ROOT / "www" / "favicon.png").read_bytes()
        for path in ("/favicon.png?v=2", "/favicon.ico", "/favicon.svg"):
            with self.subTest(path=path):
                response = urllib.request.urlopen(self.base + path, timeout=5)
                body = response.read()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers.get_content_type(), "image/png")
                self.assertEqual(body, expected)
                self.assertEqual(response.geturl().split(self.base, 1)[-1], "/favicon.png?v=2")

    def test_pwa_assets_are_available_without_authentication(self):
        for path, content_type in (
            ("/manifest.webmanifest", "application/manifest+json"),
            ("/sw.js", "application/javascript"),
            ("/icons/icon-192.png", "image/png"),
            ("/icons/icon-512.png", "image/png"),
        ):
            with self.subTest(path=path):
                response = urllib.request.urlopen(self.base + path, timeout=5)
                body = response.read()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers.get_content_type(), content_type)
                self.assertGreater(len(body), 0)
                if path == "/manifest.webmanifest":
                    manifest = json.loads(body.decode("utf-8"))
                    self.assertEqual(manifest["start_url"], "/")
                    self.assertEqual(manifest["display"], "standalone")
                    self.assertEqual(
                        {icon["sizes"] for icon in manifest["icons"]},
                        {"192x192", "512x512"},
                    )
                elif path.startswith("/icons/"):
                    size, _ = struct.unpack(">II", body[16:24])
                    self.assertEqual(size, 192 if "192" in path else 512)
                elif path == "/sw.js":
                    self.assertEqual(response.headers.get("Service-Worker-Allowed"), "/")

    def test_html_pages_set_browser_security_headers(self):
        for path in ("/", "/admin"):
            request = urllib.request.Request(self.base + path)
            with urllib.request.urlopen(request, timeout=5) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers.get("X-Content-Type-Options"), "nosniff")
                self.assertEqual(response.headers.get("X-Frame-Options"), "DENY")
                self.assertEqual(response.headers.get("Referrer-Policy"), "no-referrer")
                self.assertIn(
                    "frame-ancestors 'none'",
                    response.headers.get("Content-Security-Policy", ""),
                )

    def test_admin_overview_includes_live_system_metrics(self):
        status, result = self.request(
            "/admin/api/overview",
            headers={"X-Helper-Token": server.ADMIN_TOKEN},
        )
        self.assertEqual(status, 200)
        metrics = result["server"]["metrics"]
        self.assertIn("cpuPercent", metrics)
        self.assertIn("cpuAvailable", metrics)
        self.assertIn("processCpuPercent", metrics)
        self.assertIn("cpuCores", metrics)
        self.assertIn("loadAverage", metrics)
        self.assertIn("sampledAt", metrics)
        self.assertEqual(
            set(metrics["memory"]),
            {"totalMb", "usedMb", "availableMb", "usedPercent"},
        )
        self.assertEqual(
            set(metrics["disk"]),
            {"totalBytes", "usedBytes", "freeBytes", "usedPercent", "path"},
        )
        for key in ("cpuPercent", "processCpuPercent"):
            if metrics[key] is not None:
                self.assertGreaterEqual(metrics[key], 0)
                self.assertLessEqual(metrics[key], 100)
        if metrics["memory"]["usedPercent"] is not None:
            self.assertGreaterEqual(metrics["memory"]["usedPercent"], 0)
            self.assertLessEqual(metrics["memory"]["usedPercent"], 100)
        if metrics["disk"]["usedPercent"] is not None:
            self.assertGreaterEqual(metrics["disk"]["usedPercent"], 0)
            self.assertLessEqual(metrics["disk"]["usedPercent"], 100)

    def test_linux_cpu_counters_do_not_double_count_guest_time(self):
        with mock.patch.object(server.os, "name", "posix"), mock.patch(
                "builtins.open",
                mock.mock_open(read_data="cpu 100 10 30 800 20 2 3 4 90 9\n")):
            self.assertEqual(server._host_cpu_counters(), (969, 820))

    def test_legacy_school_account_data_is_purged_once_during_schema_upgrade(self):
        db_path = Path(self.temp.name) / "accounts.sqlite3"
        conn = sqlite3.connect(str(db_path))
        conn.executescript("""
            CREATE TABLE account_users (
                id TEXT PRIMARY KEY, username TEXT, display_name TEXT, role TEXT,
                class_id TEXT, password_hash TEXT, password_salt TEXT, active INTEGER,
                must_change_password INTEGER, created_at INTEGER
            );
            CREATE TABLE account_classes (
                id TEXT PRIMARY KEY, name TEXT, school_year TEXT, archived INTEGER, created_at INTEGER
            );
            INSERT INTO account_users VALUES
                ('old-user','oldstudent','Old student','student','old-class','hash','salt',1,0,1);
            INSERT INTO account_classes VALUES ('old-class','Class A','2026',0,1);
        """)
        conn.commit()
        conn.close()

        with server.account_db() as upgraded:
            self.assertEqual(upgraded.execute("SELECT COUNT(*) FROM account_users").fetchone()[0], 0)
            self.assertEqual(upgraded.execute("PRAGMA user_version").fetchone()[0], 1)
            tables = {row[0] for row in upgraded.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
        self.assertNotIn("account_classes", tables)
        backups = list((Path(self.temp.name) / "migration-backups").glob(
            "accounts-pre-v1-*.sqlite3"))
        self.assertEqual(len(backups), 1)
        backup = sqlite3.connect(str(backups[0]))
        try:
            self.assertEqual(backup.execute(
                "SELECT COUNT(*) FROM account_users").fetchone()[0], 1)
            self.assertEqual(backup.execute(
                "SELECT COUNT(*) FROM account_classes").fetchone()[0], 1)
            self.assertEqual(backup.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            backup.close()

    def test_legacy_schema_migration_aborts_when_pre_migration_backup_fails(self):
        db_path = Path(self.temp.name) / "accounts.sqlite3"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE account_classes(id TEXT PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO account_classes VALUES('class1', 'Class')")
        conn.commit()
        conn.close()
        server.ACCOUNT_DB_INITIALIZED_PATH = None

        with mock.patch.object(server, "_backup_legacy_account_db",
                               side_effect=OSError("backup storage unavailable")):
            with self.assertRaisesRegex(OSError, "backup storage unavailable"):
                with server.account_db():
                    pass

        unchanged = sqlite3.connect(str(db_path))
        try:
            self.assertEqual(unchanged.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertEqual(unchanged.execute(
                "SELECT COUNT(*) FROM account_classes").fetchone()[0], 1)
        finally:
            unchanged.close()

    def test_audit_log_is_bounded_and_uses_user_actor_ids(self):
        account = self.register()
        original_limit = server.ACCOUNT_AUDIT_LIMIT
        server.ACCOUNT_AUDIT_LIMIT = 3
        self.addCleanup(setattr, server, "ACCOUNT_AUDIT_LIMIT", original_limit)
        with server.account_db() as conn:
            for index in range(5):
                server._account_audit(conn, account["user"]["id"], "test.action", str(index))
            rows = conn.execute("SELECT actor_id FROM account_audit").fetchall()
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row["actor_id"] == account["user"]["id"] for row in rows))

    def test_cors_is_disabled_by_default_and_allows_only_configured_origins(self):
        previous = server.CORS_ORIGINS
        self.addCleanup(setattr, server, "CORS_ORIGINS", previous)
        server.CORS_ORIGINS = ()
        status, _ = self.request("/ping", headers={"Origin": "https://evil.example"})
        self.assertEqual(status, 200)
        self.assertNotIn("Access-Control-Allow-Origin", self.last_headers)
        server.CORS_ORIGINS = ("https://ide.example",)
        status, _ = self.request("/ping", headers={"Origin": "https://evil.example"})
        self.assertEqual(status, 200)
        self.assertNotIn("Access-Control-Allow-Origin", self.last_headers)
        status, _ = self.request("/ping", headers={"Origin": "https://ide.example"})
        self.assertEqual(status, 200)
        self.assertEqual(self.last_headers["Access-Control-Allow-Origin"], "https://ide.example")
        with self.assertRaises(ValueError):
            server.parse_cors_origins("*")


if __name__ == "__main__":
    unittest.main()
