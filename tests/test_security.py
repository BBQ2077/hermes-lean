from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("apply_compact", ROOT / "apply_compact.py")
assert SPEC and SPEC.loader
ac = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ac)


class ManagedBlockTests(unittest.TestCase):
    def test_strip_block_preserves_content_after_end_marker(self):
        text = (
            "before\n"
            + ac.START_MARKER + "\nmanaged\n" + ac.END_MARKER
            + "\nafter\n"
        )
        stripped, found = ac.strip_block(text)
        self.assertTrue(found)
        self.assertEqual(stripped, "before\n\nafter\n")

    def test_strip_block_rejects_duplicate_managed_blocks(self):
        block = ac.START_MARKER + "\nx\n" + ac.END_MARKER
        with self.assertRaises(SystemExit):
            ac.strip_block(block + "\n" + block)
    def test_strip_block_ignores_marker_text_not_on_its_own_line(self):
        text = f'x = {ac.START_MARKER!r}\ny = {ac.END_MARKER!r}\n'
        stripped, found = ac.strip_block(text)
        self.assertFalse(found)
        self.assertEqual(stripped, text)

    def test_legacy_markers_are_still_recognised(self):
        """重新命名專案後，舊版寫入的區塊必須就地被取代，不能變成孤兒區塊。"""
        text = (
            "before\n"
            + ac.LEGACY_START_MARKER + "\nold managed content\n" + ac.LEGACY_END_MARKER
            + "\nafter\n"
        )
        stripped, found = ac.strip_block(text)
        self.assertTrue(found)
        self.assertEqual(stripped, "before\n\nafter\n")

    def test_upsert_replaces_legacy_block_without_duplicating(self):
        legacy = (
            "before\n"
            + ac.LEGACY_START_MARKER + "\nold managed content\n" + ac.LEGACY_END_MARKER + "\n"
        )
        out = ac.upsert_block(legacy, ac.START_MARKER + "\nnew\n" + ac.END_MARKER)
        self.assertEqual(out.count("managed block (auto-generated)"), 1)
        self.assertNotIn("hermes-compact", out)
        self.assertIn("before", out)
        self.assertIn(ac.START_MARKER, out)


class BackupTests(unittest.TestCase):
    def test_two_backups_never_reuse_the_same_path(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "sample.py"
            target.write_text("first", encoding="utf-8")
            first = ac.backup(target)
            target.write_text("second", encoding="utf-8")
            second = ac.backup(target)
            self.assertNotEqual(first, second)
            self.assertEqual(first.read_text(encoding="utf-8"), "first")
            self.assertEqual(second.read_text(encoding="utf-8"), "second")

    def test_backup_rejects_symlink_target(self):
        if os.name == "nt":
            self.skipTest("Creating symlinks is not reliably permitted on Windows")
        with tempfile.TemporaryDirectory() as td:
            real = Path(td) / "real.py"
            link = Path(td) / "link.py"
            real.write_text("x", encoding="utf-8")
            link.symlink_to(real)
            with self.assertRaises(SystemExit):
                ac.backup(link)


class ConfigValidationTests(unittest.TestCase):
    def test_valid_example_config_is_accepted(self):
        data = json.loads((ROOT / "compact-overrides.example.json").read_text(encoding="utf-8"))
        ac.validate_config(data, source="example")

    def test_unknown_top_level_key_is_rejected(self):
        with self.assertRaises(SystemExit):
            ac.validate_config({"unknown": "value"}, source="bad")

    def test_non_string_description_is_rejected(self):
        with self.assertRaises(SystemExit):
            ac.validate_config({"tool_descriptions": {"terminal": 123}}, source="bad")

    def test_unknown_prompt_global_is_rejected(self):
        with self.assertRaises(SystemExit):
            ac.validate_config({"prompt_strings": {"os": "replace module"}}, source="bad")

    def test_non_finite_json_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / "bad.json"
            bad.write_text('{"tool_descriptions":{"x":NaN}}', encoding="utf-8")
            with self.assertRaises(SystemExit):
                ac.load_configs([bad])
    def test_duplicate_json_members_are_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / "bad.json"
            bad.write_text('{"skill_index_gate":"focus","skill_index_gate":"focus_and_coding"}', encoding="utf-8")
            with self.assertRaises(SystemExit):
                ac.load_configs([bad])


class VerificationContractTests(unittest.TestCase):
    def test_missing_report_fields_fail_closed(self):
        result = {"tool_ok": 0, "tool_bad": [], "prompt_ok": 0, "prompt_bad": [], "skills_ok": True}
        self.assertTrue(ac._verification_failures(result, {}))

    def test_report_counts_must_match_config(self):
        cfg = {"tool_descriptions": {"terminal": "x"}, "prompt_strings": {"TASK_COMPLETION_GUIDANCE": "y"}}
        result = {"tool_ok": 0, "tool_bad": [], "prompt_ok": 1, "prompt_bad": [],
                  "skills_ok": True, "compute_wrapped": True, "gate_ok": True}
        self.assertTrue(ac._verification_failures(result, cfg))

    def test_configured_name_absent_upstream_is_a_failure(self):
        cfg = {"tool_descriptions": {"__bogus__": "x"}, "prompt_strings": {}}
        result = {"tool_ok": 1, "tool_bad": [], "prompt_ok": 0, "prompt_bad": [],
                  "skills_ok": True, "compute_wrapped": True, "gate_ok": True,
                  "unknown_tools": ["__bogus__"], "unknown_parameters": []}
        failures = ac._verification_failures(result, cfg)
        self.assertTrue(any("not present upstream" in f for f in failures))

    def test_clean_report_with_matching_counts_passes(self):
        cfg = {"tool_descriptions": {"terminal": "x"}, "prompt_strings": {"MEMORY_GUIDANCE": "y"}}
        result = {"tool_ok": 1, "tool_bad": [], "prompt_ok": 1, "prompt_bad": [],
                  "skills_ok": True, "compute_wrapped": True, "gate_ok": True,
                  "unknown_tools": [], "unknown_parameters": []}
        self.assertEqual(ac._verification_failures(result, cfg), [])


class CaptureTests(unittest.TestCase):
    def test_capture_reads_effective_lazy_tool_definitions(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "agent").mkdir()
            (root / "agent" / "__init__.py").write_text("", encoding="utf-8")
            (root / "agent" / "prompt_builder.py").write_text(
                "TASK_COMPLETION_GUIDANCE = 'done'\n"
                "SKILL_DESCRIPTION_EXCEPTION_SKILLS = []\n"
                "PLATFORM_HINTS = {}\n", encoding="utf-8")
            (root / "model_tools.py").write_text(
                "def get_all_tool_definitions():\n"
                " return [{'function': {'name': 'terminal', 'description': 'Run safely', "
                "'parameters': {'properties': {'command': {'description': 'Command text'}}}}}]\n",
                encoding="utf-8")
            out = root / "capture.json"
            with mock.patch.object(ac, "venv_python", return_value=Path(sys.executable)):
                self.assertEqual(ac.capture(root, out), 0)
            data = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(data["tool_descriptions"]["terminal"], "Run safely")
            self.assertEqual(data["parameter_descriptions"]["terminal"]["command"], "Command text")


class ApplyTransactionTests(unittest.TestCase):
    def make_checkout(self, td: str) -> tuple[Path, list[Path]]:
        root = Path(td)
        (root / "agent").mkdir()
        paths = [root / "model_tools.py", root / "agent/prompt_builder.py", root / "agent/coding_context.py"]
        for index, p in enumerate(paths):
            p.write_text(f"ORIGINAL_{index}\n", encoding="utf-8")
        return root, paths

    def test_failed_verification_rolls_back_all_targets(self):
        with tempfile.TemporaryDirectory() as td:
            root, paths = self.make_checkout(td)
            originals = {p: p.read_bytes() for p in paths}
            planned = [(p, p.read_text(encoding="utf-8") + "# changed\n") for p in paths]
            with mock.patch.object(ac, "verify", return_value=None):
                result, backups = ac.apply_transaction(planned, root, {})
            self.assertFalse(result)
            self.assertEqual({p: p.read_bytes() for p in paths}, originals)
            self.assertEqual(len(backups), 3)

    def test_preflight_syntax_error_writes_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            root, paths = self.make_checkout(td)
            originals = {p: p.read_bytes() for p in paths}
            planned = [(paths[0], "def broken(:\n"), *[(p, p.read_text(encoding="utf-8")) for p in paths[1:]]]
            with self.assertRaises(SystemExit):
                ac.apply_transaction(planned, root, {})
            self.assertEqual({p: p.read_bytes() for p in paths}, originals)

    def test_symlink_target_is_rejected(self):
        if os.name == "nt":
            self.skipTest("Creating symlinks is not reliably permitted on Windows")
        with tempfile.TemporaryDirectory() as td:
            root, paths = self.make_checkout(td)
            outside = Path(td).parent / "outside-hc-test.py"
            outside.write_text("outside", encoding="utf-8")
            paths[0].unlink()
            paths[0].symlink_to(outside)
            try:
                with self.assertRaises(SystemExit):
                    ac.apply_transaction([(p, "x\n") for p in paths], root, {})
                self.assertEqual(outside.read_text(encoding="utf-8"), "outside")
            finally:
                outside.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
