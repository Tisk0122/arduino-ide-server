import importlib.util
import json
import os
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
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

    def test_favicon_is_available_without_authentication(self):
        status, body = self.request("/favicon.ico")
        self.assertEqual(status, 204)
        self.assertEqual(body, {})

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
