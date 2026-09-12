from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class LauncherTrustTests(unittest.TestCase):
    def test_windows_launchers_prefer_system_python_and_ignore_hermes_home(self):
        for name in ("compact_ui.bat", "apply_compact.bat"):
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertNotIn("HERMES_HOME", text, name)
            self.assertLess(text.index("where python"), text.index("hermes-agent\\venv"), name)

    def test_shell_launchers_prefer_system_python_and_ignore_hermes_home(self):
        for name in ("compact_ui.sh", "apply_compact.sh"):
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertNotIn("HERMES_HOME", text, name)
            self.assertLess(text.index("command -v python3"), text.index(".hermes/hermes-agent/venv"), name)
    def test_shell_conversion_only_runs_for_msys_style_paths(self):
        for name in ("compact_ui.sh", "apply_compact.sh"):
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertIn('case "$SCRIPT" in', text, name)
            self.assertIn('/[A-Za-z]/*)', text, name)


if __name__ == "__main__":
    unittest.main()
