import base64
import functools
import json
import tempfile
import threading
import unittest
import zipfile
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


ROOT = Path(__file__).resolve().parents[1]
USER = {"id": "browser-user", "username": "member01", "name": "Member"}
REMOTE_SKETCH = {
    "id": "history_one",
    "name": "History sketch",
    "files": [{"name": "history_one.ino", "content": "void setup() { digitalWrite(LED_BUILTIN, HIGH); }"}],
    "board": "arduino:avr:uno",
    "createdAt": 1,
    "updatedAt": 2,
    "revision": 2,
}
OLD_SKETCH = {
    **REMOTE_SKETCH,
    "name": "History sketch",
    "files": [{"name": "history_one.ino", "content": "void setup() { pinMode(LED_BUILTIN, OUTPUT); }"}],
    "revision": 1,
}


@unittest.skipIf(sync_playwright is None, "Playwright browser dependencies are not installed")
class BrowserFeatureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        handler = functools.partial(SimpleHTTPRequestHandler, directory=str(ROOT / "www"))
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join(timeout=3)

    def setUp(self):
        self.context = self.browser.new_context(accept_downloads=True, service_workers="block")
        self.page = self.context.new_page()
        self.page.set_viewport_size({"width": 1440, "height": 1000})

    def tearDown(self):
        self.context.close()

    def open_ide(self, token=""):
        if not token:
            self.page.route("**/public-config", lambda route: route.fulfill(
                status=200, content_type="application/json",
                body=json.dumps({"prefillStudentToken": False})))
            self.page.route("**/ping", lambda route: route.fulfill(
                status=200, content_type="application/json",
                body=json.dumps({"ok": True, "cliFound": False, "authed": False, "gen": 1})))
        config = {"u": self.base, "t": token}
        encoded = base64.urlsafe_b64encode(json.dumps(config).encode()).decode().rstrip("=")
        self.page.goto(self.base + "/#cfg=" + encoded, wait_until="domcontentloaded")
        self.page.locator("#loading-screen").wait_for(state="detached", timeout=20000)
        button = self.page.locator("#usb-setup-done-btn")
        if button.is_visible():
            button.click()

    def test_zip_import_export_and_compile_error_guidance(self):
        self.open_ide()
        with tempfile.TemporaryDirectory() as temp:
            archive_path = Path(temp) / "sample.zip"
            profile = {
                "schema": 1, "fqbn": "arduino:avr:uno", "cliVersion": "arduino-cli 1.5.1",
                "capturedAt": 1, "cores": [{"id": "arduino:avr", "version": "1.8.6"}],
                "libraries": [{"name": "Wire", "version": "1.0.0"}],
            }
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("Imported/Imported.ino", "void setup() { pinMode(LED_BUILTIN, OUTPUT); }\n")
                archive.writestr(".ide-meta.json", json.dumps({
                    "version": 1,
                    "sketches": {"Imported": {"name": "Imported", "board": "arduino:avr:uno",
                                              "buildProfile": profile}},
                }))
            self.page.locator("#backup-btn").click()
            with self.page.expect_file_chooser() as chooser_info:
                self.page.get_by_role("button", name="ZIPから復元").click()
            chooser_info.value.set_files(str(archive_path))
            self.page.locator("#sketch-list").get_by_text("Imported", exact=True).wait_for()

            self.page.locator("#backup-btn").click()
            with self.page.expect_download() as download_info:
                self.page.get_by_role("button", name="全スケッチをZIPで保存").click()
            download = download_info.value
            exported = Path(temp) / download.suggested_filename
            download.save_as(str(exported))
            with zipfile.ZipFile(exported) as archive:
                self.assertIn("Imported/Imported.ino", archive.namelist())
                self.assertIn("pinMode(LED_BUILTIN, OUTPUT)", archive.read("Imported/Imported.ino").decode())
                metadata = json.loads(archive.read(".ide-meta.json"))
                self.assertEqual(metadata["sketches"]["Imported"]["buildProfile"]["cores"][0]["version"], "1.8.6")

        findings = self.page.evaluate(
            "(line) => diagnoseCompileErrors([line]).map(item => item.title)",
            "History.ino:4:3: error: expected ';' before '}' token",
        )
        self.assertIn("セミコロンが不足している可能性があります", findings)

    def test_server_history_preview_and_restore(self):
        restored = {**OLD_SKETCH, "revision": 3, "updatedAt": 3}
        restore_requests = []

        def api(route):
            request = route.request
            path = request.url.split(self.base, 1)[-1].split("?", 1)[0]
            if path == "/account/me":
                data = {"user": USER}
            elif path == "/account/storage":
                data = {"usedBytes": 123, "limitBytes": 50 * 1024 * 1024}
            elif path == "/account/sketches":
                data = {"sketches": [REMOTE_SKETCH]}
            elif path == "/account/sketches/history_one/versions":
                data = {"versions": [{"revision": 1, "createdAt": 1}]}
            elif path == "/account/sketches/history_one/versions/1":
                data = {"sketch": OLD_SKETCH, "revision": 1, "createdAt": 1}
            elif path == "/account/sketches/history_one/restore":
                body = request.post_data_json
                restore_requests.append(body)
                data = {"ok": True, "revision": 3, "sketch": restored}
            elif path == "/ping":
                data = {"ok": True, "cliFound": False, "authRequired": True,
                        "authed": True, "admin": False, "gen": 1}
            elif path == "/public-config":
                data = {"prefillStudentToken": False}
            else:
                return route.fulfill(status=404, content_type="application/json",
                                     body=json.dumps({"error": "not found"}))
            return route.fulfill(status=200, content_type="application/json", body=json.dumps(data))

        self.page.route("**/account/**", api)
        self.page.route("**/ping", api)
        self.page.route("**/public-config", api)
        self.page.add_init_script(
            "sessionStorage.setItem('arduino_helper_account_session', 'browser-session')")
        self.open_ide(token="browser-session")
        self.page.locator("#history-open-btn").click()
        self.page.locator("#account-history-section").wait_for(state="visible")
        self.page.locator("#account-history-load-btn").click()
        version = self.page.locator("#account-history-list .history-entry")
        version.wait_for()
        version.click()
        self.page.locator("#account-history-preview").get_by_text(
            "pinMode(LED_BUILTIN, OUTPUT)", exact=False).wait_for()
        self.page.on("dialog", lambda dialog: dialog.accept())
        self.page.locator("#account-history-restore-btn").click()
        self.page.locator("#m-history").wait_for(state="hidden")
        self.assertEqual(restore_requests, [{"currentRevision": 2, "targetRevision": 1}])
        self.assertIn("pinMode(LED_BUILTIN, OUTPUT)", self.page.locator("#code-ta").input_value())

    def test_account_login_synchronizes_sketches(self):
        login_requests = []
        sync_requests = []

        def api(route):
            request = route.request
            path = request.url.split(self.base, 1)[-1].split("?", 1)[0]
            if path == "/account/login":
                login_requests.append(request.post_data_json)
                data = {"token": "signed-in-session", "user": USER}
            elif path == "/account/storage":
                data = {"usedBytes": 0, "limitBytes": 50 * 1024 * 1024}
            elif path == "/account/sketches":
                sync_requests.append(request.method)
                data = {"sketches": []}
            elif path.startswith("/account/sketches/") and request.method == "PUT":
                data = {"ok": True, "revision": 1}
            elif path == "/ping":
                data = {"ok": True, "cliFound": False, "authed": False, "gen": 1}
            elif path == "/public-config":
                data = {"prefillStudentToken": False}
            else:
                return route.fulfill(status=404, content_type="application/json",
                                     body=json.dumps({"error": "not found"}))
            return route.fulfill(status=200, content_type="application/json", body=json.dumps(data))

        self.page.route("**/account/**", api)
        self.page.route("**/ping", api)
        self.page.route("**/public-config", api)
        self.open_ide(token="ide-token")
        self.page.locator("#account-open-btn").click()
        self.page.locator("#account-username").fill("member01")
        self.page.locator("#account-password").fill("MemberPass-01")
        self.page.locator("#account-submit").click()
        self.page.locator("#account-logged-in").wait_for(state="visible")
        self.assertEqual(login_requests, [{"username": "member01", "password": "MemberPass-01"}])
        self.assertIn("GET", sync_requests)
        self.assertIn("Member", self.page.locator("#account-user-label").text_content())

    def test_account_storage_export_and_self_deletion_controls(self):
        export = {"schema": 1, "user": USER, "sketches": [{"id": "one"}]}
        delete_requests = []

        def api(route):
            request = route.request
            path = request.url.split(self.base, 1)[-1].split("?", 1)[0]
            if path == "/account/login":
                data = {"token": "signed-in-session", "user": USER}
            elif path == "/account/storage":
                data = {"usedBytes": 1024, "limitBytes": 50 * 1024 * 1024}
            elif path == "/account/sketches":
                data = {"sketches": []}
            elif path == "/account/export":
                data = export
            elif path == "/account/delete":
                delete_requests.append(request.post_data_json)
                data = {"ok": True, "deleted": True}
            elif path == "/ping":
                data = {"ok": True, "cliFound": False, "authed": False, "gen": 1}
            elif path == "/public-config":
                data = {"prefillStudentToken": False}
            else:
                return route.fulfill(status=404, content_type="application/json",
                                     body=json.dumps({"error": "not found"}))
            return route.fulfill(status=200, content_type="application/json", body=json.dumps(data))

        self.page.route("**/account/**", api)
        self.page.route("**/ping", api)
        self.page.route("**/public-config", api)
        self.open_ide(token="ide-token")
        self.page.locator("#account-open-btn").click()
        self.page.locator("#account-username").fill("member01")
        self.page.locator("#account-password").fill("MemberPass-01")
        self.page.locator("#account-submit").click()
        self.page.locator("#account-logged-in").wait_for(state="visible")
        self.page.wait_for_function(
            "() => document.querySelector('#account-storage-usage').textContent.includes('1.0 KiB')",
            timeout=10000,
        )
        self.assertIn("1.0 KiB / 50.0 MiB", self.page.locator("#account-storage-usage").text_content())
        self.page.locator('#acct-tabs [data-tab="acct-settings"]').click()
        with self.page.expect_download() as download_info:
            self.page.locator("#account-export-data").click()
        with tempfile.TemporaryDirectory() as temp:
            exported = Path(temp) / download_info.value.suggested_filename
            download_info.value.save_as(str(exported))
            self.assertEqual(json.loads(exported.read_text(encoding="utf-8"))["sketches"], export["sketches"])

        self.page.locator("#acct-settings details").nth(1).locator("summary").click()
        self.page.on("dialog", lambda dialog: dialog.accept("MemberPass-01"))
        self.page.locator("#account-delete-self").click()
        self.page.locator("#m-account").wait_for(state="hidden")
        self.page.locator("#account-open-btn").click()
        self.page.locator("#account-login-area").wait_for(state="visible")
        self.assertEqual(delete_requests, [{"password": "MemberPass-01"}])

    def test_login_detects_conflict_and_preserves_device_copy(self):
        local_sketch = {
            "id": "conflict_one",
            "name": "Device copy",
            "files": [{"name": "conflict_one.ino", "content": "void setup() { digitalWrite(13, HIGH); }"}],
            "board": "arduino:avr:uno",
            "createdAt": 1,
            "updatedAt": 2,
            "revision": 1,
        }
        remote_sketch = {
            **local_sketch,
            "name": "Server copy",
            "files": [{"name": "conflict_one.ino", "content": "void setup() { pinMode(13, OUTPUT); }"}],
            "revision": 2,
        }
        conflict_saves = []

        def api(route):
            request = route.request
            path = request.url.split(self.base, 1)[-1].split("?", 1)[0]
            if path == "/account/me":
                data = {"user": USER}
            elif path == "/account/storage":
                data = {"usedBytes": 0, "limitBytes": 50 * 1024 * 1024}
            elif path == "/account/sketches":
                data = {"sketches": [remote_sketch]}
            elif path.startswith("/account/sketches/") and request.method == "PUT":
                conflict_saves.append(request.post_data_json)
                data = {"ok": True, "revision": 1}
            elif path == "/ping":
                data = {"ok": True, "cliFound": False, "authed": True, "gen": 1}
            elif path == "/public-config":
                data = {"prefillStudentToken": False}
            else:
                return route.fulfill(status=404, content_type="application/json",
                                     body=json.dumps({"error": "not found"}))
            return route.fulfill(status=200, content_type="application/json", body=json.dumps(data))

        self.page.route("**/account/**", api)
        self.page.route("**/ping", api)
        self.page.route("**/public-config", api)
        self.page.goto(self.base + "/manifest.webmanifest")
        self.page.evaluate("sessionStorage.setItem('arduino_helper_account_session', 'browser-session')")
        self.page.evaluate("""sketch => new Promise((resolve, reject) => {
          const request = indexedDB.open('ArduinoWebIDE_account_browser-user', 1);
          request.onupgradeneeded = () => {
            const db = request.result;
            const sketches = db.createObjectStore('sketches', {keyPath: 'id'});
            sketches.createIndex('updatedAt', 'updatedAt');
            db.createObjectStore('kv', {keyPath: 'k'});
          };
          request.onerror = () => reject(request.error);
          request.onsuccess = () => {
            const transaction = request.result.transaction('sketches', 'readwrite');
            transaction.objectStore('sketches').put(sketch);
            transaction.oncomplete = () => resolve();
            transaction.onerror = () => reject(transaction.error);
          };
        })""", local_sketch)
        self.open_ide(token="ide-token")
        self.page.locator("#sketch-list").get_by_text("競合コピー", exact=False).wait_for()
        self.assertEqual(len(conflict_saves), 1)
        self.assertIn("digitalWrite(13, HIGH)", conflict_saves[0]["sketch"]["files"][0]["content"])


if __name__ == "__main__":
    unittest.main()
