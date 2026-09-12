from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("compact_ui", ROOT / "compact_ui.py")
ui = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(ui)


class UiBoundaryTests(unittest.TestCase):
    def test_find_python_never_uses_checkout_venv(self):
        fake_hc = mock.Mock()
        fake_hc.venv_python.return_value = Path("C:/untrusted/venv/python.exe")
        with mock.patch.object(ui, "hc", fake_hc):
            self.assertEqual(ui.find_python(Path("C:/untrusted")), sys.executable)
        fake_hc.venv_python.assert_not_called()

    def test_private_target_never_overwrites_selected_external_config(self):
        app = object.__new__(ui.App)
        app.extra_configs = ["C:/important/settings.json"]
        self.assertEqual(app._private_target(), ui.DEFAULT_PRIVATE)

    def test_strict_json_read_does_not_turn_corruption_into_empty_config(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "broken.json"
            path.write_text("{broken", encoding="utf-8")
            with self.assertRaises(ValueError):
                ui.read_json(path, strict=True)

    def test_config_open_guard_accepts_only_existing_regular_json(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            good = root / "good.json"
            bad = root / "script.py"
            good.write_text("{}", encoding="utf-8")
            bad.write_text("print('x')", encoding="utf-8")
            self.assertTrue(ui.safe_config_path(good))
            self.assertFalse(ui.safe_config_path(bad))
            self.assertFalse(ui.safe_config_path(root / "missing.json"))
    def test_editor_command_uses_notepad_and_never_shell_association(self):
        with tempfile.TemporaryDirectory() as td:
            good = Path(td) / "config.JSON"
            good.write_text("{}", encoding="utf-8")
            cmd = ui.editor_command(good)
            self.assertTrue(cmd[0] in ("notepad.exe", "open", "xdg-open"))
            self.assertEqual(len(cmd), 2)

    def test_editor_command_rejects_non_json_and_missing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            exe = root / "payload.exe"
            exe.write_bytes(b"MZ")
            with self.assertRaises(ValueError):
                ui.editor_command(exe)
            with self.assertRaises(ValueError):
                ui.editor_command(root / "missing.json")

    def test_capture_warning_does_not_claim_completeness(self):
        self.assertIn("不代表完整", ui.CAPTURE_WARNING)
        self.assertIn("私密", ui.CAPTURE_WARNING)

    def test_longest_banner_detail_is_a_superset_of_every_state_message(self):
        """主視窗高度是靠「最長橫幅」預留的；任何狀態訊息若更長就會被裁掉。"""
        longest = ui.LONGEST_BANNER_DETAIL
        longest_lines = longest.count("\n") + 1
        for detail in (ui.SHORT_BANNER_DETAIL, longest):
            self.assertLessEqual(detail.count("\n") + 1, longest_lines, detail)
        # 「需要重新套用」的三行訊息必須塞得進最長版。
        self.assertIn("file(s) are back to upstream", longest)
        self.assertIn("Press the button above", longest)
        self.assertGreaterEqual(longest_lines, 3)

    def test_dynamic_heights_are_locked_before_layout(self):
        """換狀態文字不得改變版面，否則大按鈕會被推出視窗下緣。"""
        src = (ROOT / "compact_ui.py").read_text(encoding="utf-8")
        self.assertIn("_lock_dynamic_heights()", src)
        self.assertIn("pack_propagate(False)", src)
        # 主視窗最小尺寸必須等於貼合後的大小。
        self.assertIn("root.minsize(w, h)", src)
    def test_restart_guidance_requires_a_process_restart(self):
        """Hermes 在程序啟動時載入這些檔案，文件必須要求完整重開。"""
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("完整關閉並重新開啟", readme)
        self.assertIn("fully quit and relaunch", readme)

        core = (ROOT / "apply_compact.py").read_text(encoding="utf-8")
        self.assertIn("fully quit + relaunch", core)

        ui = (ROOT / "compact_ui.py").read_text(encoding="utf-8")
        self.assertIn("完整重開 Hermes", ui)


if __name__ == "__main__":
    unittest.main()
