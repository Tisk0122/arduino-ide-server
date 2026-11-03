import importlib.util
import json
import textwrap
from importlib.machinery import SourceFileLoader
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WRAPPER_PATH = ROOT / "arduino-build-run"
SPEC = importlib.util.spec_from_loader(
    "arduino_build_run", SourceFileLoader("arduino_build_run", str(WRAPPER_PATH)))
wrapper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(wrapper)


class BuildWrapperAllowlistTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def assert_allowed(self, args):
        wrapper.validate_command(args, str(self.root))

    def assert_rejected(self, args):
        with self.assertRaises(SystemExit):
            wrapper.validate_command(args, str(self.root))

    def test_all_required_board_index_commands_are_allowed(self):
        self.assert_allowed(["core", "update-index"])
        self.assert_allowed(["config", "dump", "--json"])
        self.assert_allowed([
            "config", "add", "board_manager.additional_urls",
            "https://example.com/package_index.json",
        ])
        self.assert_allowed([
            "config", "remove", "board_manager.additional_urls",
            "https://example.com/package_index.json",
        ])

    def test_board_index_allowlist_does_not_open_arbitrary_config_commands(self):
        self.assert_rejected(["config", "set", "board_manager.enable_unsafe_install", "true"])
        self.assert_rejected(["config", "delete", "board_manager"])
        self.assert_rejected(["config", "add", "directories.data", "/tmp"])
        self.assert_rejected(["config", "dump"])
        self.assert_rejected(["core", "upgrade"])
        self.assert_rejected(["core", "update-index", "--config-file", "/tmp/evil"])

    def test_board_index_url_must_be_public_https_json_without_credentials(self):
        good = [
            "https://example.com/package_index.json",
            "https://downloads.vendor.example/path/package_index.JSON",
        ]
        for value in good:
            with self.subTest(value=value):
                wrapper.validate_board_index_url(value)

        bad = [
            "http://example.com/package_index.json",
            "https://localhost/package_index.json",
            "https://127.0.0.1/package_index.json",
            "https://10.0.0.2/package_index.json",
            "https://[::1]/package_index.json",
            "https://user:secret@example.com/package_index.json",
            "https://example.com/package_index.json?token=x",
            "https://example.com/package_index.json#fragment",
            "https://example.com/package_index.yaml",
            "https://device.local/package_index.json",
            "https://server.internal/package_index.json",
        ]
        for value in bad:
            with self.subTest(value=value):
                with self.assertRaises(SystemExit):
                    wrapper.validate_board_index_url(value)

    def test_wrapper_executes_only_validated_commands(self):
        fake_cli = self.root / "fake-arduino-cli.py"
        log_path = self.root / "cli-args.jsonl"
        config_path = self.root / "build-run.conf"
        fake_cli.write_text(textwrap.dedent("""\
            #!/usr/bin/env python3
            import json, os, sys
            with open(os.environ['FAKE_CLI_LOG'], 'a', encoding='utf-8') as f:
                f.write(json.dumps(sys.argv[1:]) + '\\n')
            if sys.argv[1:] == ['config', 'dump', '--json']:
                print(json.dumps({'board_manager': {'additional_urls': []}}))
            else:
                print('fake CLI success')
        """).lstrip(), encoding="utf-8")
        fake_cli.chmod(0o755)
        config_path.write_text(json.dumps({
            "cli": str(fake_cli),
            "workroot": str(self.root / "work"),
        }), encoding="utf-8")
        (self.root / "work").mkdir()

        env = os.environ.copy()
        env.pop("SUDO_UID", None)
        env["ARDUINO_BUILD_RUN_CONF"] = str(config_path)
        env["FAKE_CLI_LOG"] = str(log_path)
        commands = [
            ["core", "update-index"],
            ["config", "dump", "--json"],
            ["config", "add", "board_manager.additional_urls", "https://example.com/package_index.json"],
            ["config", "remove", "board_manager.additional_urls", "https://example.com/package_index.json"],
        ]
        for args in commands:
            result = subprocess.run(
                [sys.executable, "-I", str(WRAPPER_PATH), "--timeout", "10", "--"] + args,
                env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                timeout=15, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout)

        actual = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(actual, commands)

        before = len(actual)
        rejected = subprocess.run(
            [sys.executable, "-I", str(WRAPPER_PATH), "--timeout", "10", "--",
             "config", "set", "board_manager.enable_unsafe_install", "true"],
            env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=15, check=False,
        )
        self.assertEqual(rejected.returncode, 64)
        after = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(after), before)


if __name__ == "__main__":
    unittest.main()
