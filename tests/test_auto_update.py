import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class AutoUpdateContractTests(unittest.TestCase):
    def test_updater_deploys_itself_and_all_ui_pages(self):
        updater = (ROOT / "github-update.sh").read_text(encoding="utf-8")
        match = re.search(r"^FILES=\(\n(.*?)^\)", updater, re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(match)
        files = set(re.findall(r'"([^"]+)"', match.group(1)))
        self.assertTrue({
            "arduino-helper.py",
            "arduino-build-run",
            "www/index.html",
            "www/admin.html",
            "www/docs.html",
            "www/teacher.html",
            "configure-github-webhook.sh",
            "github-update.sh",
            "arduino-helper-update.service",
            "arduino-helper-update.timer",
        }.issubset(files))
        self.assertIn('github-update.sh) printf', updater)
        self.assertIn("configure-github-webhook.sh) printf", updater)

    def test_periodic_update_checks_remote_commit_and_records_success(self):
        updater = (ROOT / "github-update.sh").read_text(encoding="utf-8")
        timer = (ROOT / "arduino-helper-update.timer").read_text(encoding="utf-8")
        service = (ROOT / "arduino-helper-update.service").read_text(encoding="utf-8")
        self.assertIn("git ls-remote", updater)
        self.assertIn('"$UPDATE_DIR/applied-commit"', updater)
        self.assertIn("OnCalendar=*-*-* *:0/5:00", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("arduino-helper-update --check", service)

    def test_installer_and_webhook_setup_enable_the_update_timer(self):
        installer = (ROOT / "install.sh").read_text(encoding="utf-8")
        configure = (ROOT / "configure-github-webhook.sh").read_text(encoding="utf-8")
        self.assertIn('"$HERE/arduino-helper-update.service"', installer)
        self.assertIn('"$HERE/arduino-helper-update.timer"', installer)
        self.assertIn("systemctl enable --now arduino-helper-update.timer", installer)
        self.assertIn("systemctl enable --now arduino-helper-update.timer", configure)
        self.assertIn("arduino-helper-update.service", configure)
        self.assertIn("/usr/bin/systemctl --no-block start arduino-helper-update.service", configure)
        self.assertIn("/usr/bin/systemctl --no-block start arduino-helper-update.service", installer)


if __name__ == "__main__":
    unittest.main()
