import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
TEMP_HOME = tempfile.TemporaryDirectory()
os.environ["HELPER_HOME"] = TEMP_HOME.name
SPEC = importlib.util.spec_from_file_location("arduino_helper", ROOT / "arduino-helper.py")
server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(server)


class ServerLimitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        server.HOME = Path(self.temp.name)
        server.WORK_DIR = server.HOME / "work"
        server.WORK_DIR.mkdir()
        server.HISTORY_FILE = server.HOME / "history.jsonl"
        server.HISTORY.clear()
        server.HISTORY_LINES = 0
        server.JOBS.clear()
        server.QUEUE.clear()
        server.ADMIN_WAITING = 0
        server.TOKEN = "test-token"
        server.ADMIN_TOKEN = "admin-test-token"
        server.WRAPPER = "test-wrapper"
        server.READ_CACHE.clear()
        server.GEN = 1
        server.ADMIN_MAX_QUEUE = 10
        server.AUTH_FAILS.clear()
        server.AUTH_BLOCK.clear()
        server.AUTH_STATE_LIMIT = 5000
        server.CORS_ORIGINS = ("*",)
        server.TRUST_CF_CONNECTING_IP = False
        for name in ("ARDUINO_DIRECTORIES_DATA", "ARDUINO_DIRECTORIES_USER",
                     "ARDUINO_DIRECTORIES_DOWNLOADS"):
            os.environ.pop(name, None)

    def test_history_statistics_cover_all_retained_records(self):
        records = [
            {"t": 1_700_000_000 + i, "id": str(i), "kind": "compile",
             "state": "done", "success": True, "time": 1}
            for i in range(1500)
        ]
        server.HISTORY_FILE.write_text(
            "".join(json.dumps(record) + "\n" for record in records),
            encoding="utf-8",
        )

        server.load_history()

        self.assertEqual(len(server.HISTORY), 1500)
        self.assertEqual(server.HISTORY_LINES, 1500)
        self.assertEqual(server.history_stats(list(server.HISTORY))["total"], 1500)

    def test_history_trim_synchronizes_file_and_memory(self):
        records = [
            {"t": 1_700_000_000 + i, "id": str(i), "kind": "compile",
             "state": "done", "success": True}
            for i in range(server.HISTORY_TRIM + 1)
        ]
        server.HISTORY_FILE.write_text(
            "".join(json.dumps(record) + "\n" for record in records),
            encoding="utf-8",
        )

        server.load_history()

        self.assertEqual(server.HISTORY_LINES, server.HISTORY_KEEP // 2)
        self.assertEqual(len(server.HISTORY), server.HISTORY_KEEP // 2)
        self.assertEqual(server.HISTORY[0]["id"], str(server.HISTORY_TRIM + 1 - server.HISTORY_KEEP // 2))

    def test_stale_compile_job_is_failed_and_recorded_not_silently_removed(self):
        job = server.new_job("compile", "network-a", False)
        job["created"] -= 4 * 3600
        server.QUEUE.append(job["id"])

        server.purge_jobs()

        self.assertIn(job["id"], server.JOBS)
        self.assertEqual(job["state"], "error")
        self.assertNotIn(job["id"], server.QUEUE)
        self.assertIn("待機時間が上限を超えた", job["error"])
        self.assertEqual(server.HISTORY[-1]["id"], job["id"])

    def test_waiting_admin_job_is_not_purged_while_worker_owns_it(self):
        job = server.new_job("admin", "admin", True)
        job["created"] -= 4 * 3600

        server.purge_jobs()

        self.assertIn(job["id"], server.JOBS)
        self.assertEqual(job["state"], "queued")

    def test_client_identity_ignores_forged_header(self):
        handler = object.__new__(server.Handler)
        handler.headers = {"X-Client-Id": "victim-id"}
        handler._client_ip = lambda: "203.0.113.10"
        first = handler._client_id()
        handler.headers = {"X-Client-Id": "another-forged-id"}

        self.assertEqual(first, handler._client_id())
        handler._client_ip = lambda: "203.0.113.11"
        self.assertNotEqual(first, handler._client_id())
        self.assertNotIn("203.0.113.10", first)

    def test_cf_forwarded_ip_requires_trust_setting_and_valid_ip(self):
        handler = object.__new__(server.Handler)
        handler.client_address = ("127.0.0.1", 1234)
        handler.headers = {"CF-Connecting-IP": "203.0.113.20"}

        self.assertEqual(handler._client_ip(), "127.0.0.1")
        server.TRUST_CF_CONNECTING_IP = True
        self.assertEqual(handler._client_ip(), "203.0.113.20")
        handler.headers = {"CF-Connecting-IP": "spoofed, 203.0.113.20"}
        self.assertEqual(handler._client_ip(), "127.0.0.1")

    def test_cors_allowlist_only_echoes_configured_origins(self):
        handler = object.__new__(server.Handler)
        sent = []
        handler.send_header = lambda key, value: sent.append((key, value))
        server.CORS_ORIGINS = ("https://ide.example.org",)
        handler.headers = {"Origin": "https://IDE.example.org"}

        handler._cors()

        self.assertIn(("Access-Control-Allow-Origin", "https://ide.example.org"), sent)
        handler.headers = {"Origin": "https://attacker.example"}
        sent.clear()
        handler._cors()
        self.assertFalse(any(key == "Access-Control-Allow-Origin" for key, _ in sent))

    def test_auth_failure_state_is_bounded_without_global_reset(self):
        server.AUTH_STATE_LIMIT = 2
        server.AUTH_FAILS.update({"old-a": [1], "old-b": [2]})

        with patch.object(server.time, "time", return_value=10):
            server.auth_failed("new")

        self.assertEqual(len(server.AUTH_FAILS), 2)
        self.assertIn("new", server.AUTH_FAILS)
        self.assertIn("old-b", server.AUTH_FAILS)
        self.assertNotIn("old-a", server.AUTH_FAILS)

    @unittest.skipUnless(os.name == "posix", "POSIX process groups are unavailable")
    @patch.object(server.os, "name", "posix")
    @patch.object(server.os, "killpg")
    def test_timeout_kills_entire_cli_process_group(self, killpg):
        process = type("Process", (), {"pid": 12345})()

        server.kill_process_tree(process)

        killpg.assert_called_once_with(12345, server.signal.SIGKILL)

    @patch.object(server.os, "name", "nt")
    def test_timeout_kills_process_on_non_posix_platforms(self):
        process = type("Process", (), {"kill": unittest.mock.Mock()})()

        server.kill_process_tree(process)

        process.kill.assert_called_once_with()

    def test_search_endpoints_propagate_cli_errors_and_do_not_cache(self):
        endpoints = (
            ("/cores/search?q=esp32", "cs:esp32"),
            ("/cores/list", "cl"),
            ("/libs/search?q=servo", "ls:servo"),
            ("/libs/list", "ll"),
        )
        for path, cache_key in endpoints:
            with self.subTest(path=path):
                handler = object.__new__(server.Handler)
                handler.path = path
                handler.headers = {"Authorization": "Bearer admin-test-token"}
                handler.client_address = ("127.0.0.1", 1234)
                handler._send = lambda *args, **kwargs: self.fail("unexpected success response")
                with patch.object(server, "run_cli_json", side_effect=server.ApiError(502, "upstream error")):
                    with self.assertRaises(server.ApiError) as error:
                        handler._dispatch("GET")
                self.assertEqual(error.exception.code, 502)
                self.assertNotIn(cache_key, server.READ_CACHE)

    def test_search_endpoints_reject_unexpected_cli_json_shapes(self):
        endpoints = (
            "/cores/search?q=esp32",
            "/cores/list",
            "/libs/search?q=servo",
            "/libs/list",
        )
        for path in endpoints:
            with self.subTest(path=path):
                handler = object.__new__(server.Handler)
                handler.path = path
                handler.headers = {"Authorization": "Bearer admin-test-token"}
                handler.client_address = ("127.0.0.1", 1234)
                handler._send = lambda *args, **kwargs: self.fail("unexpected success response")
                with patch.object(server, "run_cli_json", return_value={}):
                    with self.assertRaises(server.ApiError) as error:
                        handler._dispatch("GET")
                self.assertEqual(error.exception.code, 502)

    def test_library_search_retries_only_unsupported_option(self):
        with patch.object(server, "run_cli", side_effect=[
            (2, "Error: unknown flag: --omit-releases-details"),
            (0, '{"libraries": []}'),
        ]) as run_cli:
            self.assertEqual(
                server.run_cli_json(["lib", "search", "servo", "--omit-releases-details"]),
                {"libraries": []},
            )

        self.assertEqual(run_cli.call_count, 2)
        self.assertEqual(run_cli.call_args_list[1].args[0],
                         ["lib", "search", "servo", "--format", "json"])

    def test_library_search_does_not_retry_transient_cli_failure(self):
        with patch.object(server, "run_cli", return_value=(1, "network unavailable")) as run_cli:
            with self.assertRaises(server.ApiError) as error:
                server.run_cli_json(["lib", "search", "servo", "--omit-releases-details"])

        self.assertEqual(error.exception.code, 502)
        run_cli.assert_called_once()

    def test_cli_timeout_and_cancellation_never_accept_partial_json(self):
        for return_code, expected_status in ((124, 504), (130, 503), (-9, 503)):
            with self.subTest(return_code=return_code):
                with patch.object(server, "run_cli", return_value=(return_code, '{"libraries": []}')):
                    with self.assertRaises(server.ApiError) as error:
                        server.run_cli_json(["lib", "search", "servo"])
                self.assertEqual(error.exception.code, expected_status)

    def test_cli_exit_code_one_is_accepted_only_with_valid_json(self):
        with patch.object(server, "run_cli", return_value=(1, '{"libraries": []}')):
            self.assertEqual(server.run_cli_json(["lib", "search", "servo"]), {"libraries": []})

    def test_unexpected_nonzero_cli_exit_code_rejects_even_valid_json(self):
        with patch.object(server, "run_cli", return_value=(2, '{"libraries": []}')):
            with self.assertRaises(server.ApiError) as error:
                server.run_cli_json(["lib", "search", "servo"])

        self.assertEqual(error.exception.code, 502)

    def test_exit_code_one_is_not_allowed_for_cli_list(self):
        with patch.object(server, "run_cli", return_value=(1, '{"libraries": []}')):
            with self.assertRaises(server.ApiError) as error:
                server.run_cli_json(["lib", "list"])

        self.assertEqual(error.exception.code, 502)

    def test_admin_queue_limit_rejects_and_cleans_upload(self):
        server.ADMIN_MAX_QUEUE = 1
        server.new_job("admin", "network-a", True)
        cleaned = []

        with self.assertRaises(server.ApiError) as error:
            server.start_admin_job("upload", ["lib", "install", "example"],
                                   cleanup=lambda: cleaned.append(True))

        self.assertEqual(error.exception.code, 429)
        self.assertEqual(cleaned, [True])

    def test_unknown_disk_space_blocks_compile(self):
        job = {"plan": {"fqbn": "arduino:avr:uno", "sketch": "Example", "main": "Example.ino"}}
        with patch.object(server.shutil, "disk_usage", side_effect=OSError("unavailable")):
            self.assertIsNone(server.free_mb())
            with self.assertRaises(server.ApiError) as error:
                server.run_compile(job)

        self.assertEqual(error.exception.code, 503)

    def test_build_storage_with_less_space_is_used_for_compile_guard(self):
        data_dir = Path(self.temp.name) / "arduino-data"
        user_dir = Path(self.temp.name) / "arduino-user"
        downloads_dir = Path(self.temp.name) / "downloads"
        for directory in (data_dir, data_dir / "build-cache", user_dir, downloads_dir):
            directory.mkdir()
        os.environ["ARDUINO_DIRECTORIES_DATA"] = str(data_dir)
        os.environ["ARDUINO_DIRECTORIES_USER"] = str(user_dir)
        os.environ["ARDUINO_DIRECTORIES_DOWNLOADS"] = str(downloads_dir)
        server.MIN_FREE_MB = 1024

        checked_paths = []

        def disk_usage(path):
            checked_paths.append(path)
            free = 512 * 1024 * 1024 if path == str(user_dir) else 2 * 1024 * 1024 * 1024
            return type("DiskUsage", (), {"free": free})()

        job = {"plan": {"fqbn": "arduino:avr:uno", "sketch": "Example", "main": "Example.ino"}}
        with patch.object(server.shutil, "disk_usage", side_effect=disk_usage), \
                patch.object(server, "MIN_FREE_MB", 1024):
            self.assertEqual(server.free_mb(), 512)
            with self.assertRaises(server.ApiError) as error:
                server.run_compile(job)

        self.assertEqual(error.exception.code, 507)
        self.assertTrue({
            str(server.HOME), str(server.WORK_DIR), str(data_dir),
            str(data_dir / "build-cache"), str(user_dir), str(downloads_dir),
        }.issubset(set(checked_paths)))


if __name__ == "__main__":
    unittest.main()
