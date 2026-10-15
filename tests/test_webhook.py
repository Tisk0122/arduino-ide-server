import hashlib
import hmac
import importlib.util
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
TEMP_HOME = tempfile.TemporaryDirectory()
os.environ["HELPER_HOME"] = TEMP_HOME.name
SPEC = importlib.util.spec_from_file_location("arduino_helper_webhook", ROOT / "arduino-helper.py")
server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(server)


class GitHubWebhookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        server.HOME = Path(self.temp.name)
        server.WEBHOOK_LAST = 0
        server.GITHUB_WEBHOOK_SECRET = "test-webhook-secret"
        server.GITHUB_WEBHOOK_REPO = "example/arduino-helper"
        server.GITHUB_WEBHOOK_BRANCH = "main"
        self.httpd = server.Server(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def _stop_server(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)

    def post_push(self, ref="refs/heads/main", signature_secret=None):
        body = json.dumps({
            "repository": {"full_name": server.GITHUB_WEBHOOK_REPO},
            "ref": ref,
        }).encode("utf-8")
        secret = server.GITHUB_WEBHOOK_SECRET if signature_secret is None else signature_secret
        signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        req = urllib.request.Request(
            self.base + "/webhook/github",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-GitHub-Event": "push",
                "X-Hub-Signature-256": signature,
            },
            method="POST",
        )
        try:
            response = urllib.request.urlopen(req, timeout=5)
        except urllib.error.HTTPError as exc:
            response = exc
        payload = response.read()
        return response.code, json.loads(payload.decode("utf-8")) if payload else {}

    def test_valid_push_schedules_systemd_update(self):
        with patch.object(server.subprocess, "run", return_value=SimpleNamespace(
                returncode=0, stderr="")) as run:
            status, result = self.post_push()
        self.assertEqual(status, 202)
        self.assertTrue(result["scheduled"])
        self.assertEqual(result["trigger"], "systemd")
        self.assertEqual(run.call_args.args[0][-2:], ["start", "arduino-helper-update.service"])

    def test_systemd_failure_falls_back_to_legacy_updater(self):
        with patch.object(server.subprocess, "run", return_value=SimpleNamespace(
                returncode=1, stderr="systemd unavailable")), \
                patch.object(server.subprocess, "Popen") as popen:
            status, result = self.post_push()
        self.assertEqual(status, 202)
        self.assertEqual(result["trigger"], "legacy")
        popen.assert_called_once()

    def test_failed_fallback_returns_service_error_and_allows_retry(self):
        with patch.object(server.subprocess, "run", return_value=SimpleNamespace(
                returncode=1, stderr="systemd unavailable")), \
                patch.object(server.subprocess, "Popen", side_effect=OSError("cannot start")):
            status, _ = self.post_push()
        self.assertEqual(status, 503)
        self.assertEqual(server.WEBHOOK_LAST, 0)

    def test_invalid_signature_does_not_schedule_update(self):
        with patch.object(server.subprocess, "run") as run:
            status, _ = self.post_push(signature_secret="wrong-secret")
        self.assertEqual(status, 401)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
