from __future__ import annotations

import importlib.util
import json
import sys
from types import ModuleType
from unittest import mock
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("lean_memory_tests", ROOT / "apply_compact.py")
ac = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ac)


def prompt_namespace(cfg):
    ns = {
        "_memory_parts": lambda agent: ["external provider"] if agent._memory_manager else [],
        "_agent_home": lambda agent: Path(agent._session_db.db_path).parent,
    }
    exec(compile(ac.build_system_prompt_block(cfg), "<managed prompt>", "exec"), ns)
    return ns


def agent(home):
    return SimpleNamespace(
        _session_db=SimpleNamespace(db_path=str(home / "state.db")),
        _memory_store=None, _memory_enabled=False, _user_profile_enabled=False,
        _memory_manager=None,
    )


class ReadOnlyMemoryTests(unittest.TestCase):
    def test_readonly_config_is_boolean_and_verification_is_required(self):
        ac.validate_config({"memory_read_only": True})
        ac.validate_config({"memory_read_only": False})
        with self.assertRaises(SystemExit):
            ac.validate_config({"memory_read_only": "true"})
        report = {"tool_ok": 0, "tool_bad": [], "prompt_ok": 0, "prompt_bad": [],
                  "skills_ok": True, "compute_wrapped": True, "gate_ok": True,
                  "rewrites_ok": True, "memory_ok": True, "host_ok": True,
                  "skills_readonly_banner_ok": True, "skills_demote_escape_ok": True,
                  "skills_demote_all_exceptions_ok": True}
        self.assertTrue(ac._verification_failures(report, {"memory_read_only": True}))
        report["memory_read_only_ok"] = True
        self.assertEqual(ac._verification_failures(report, {"memory_read_only": True}), [])

    def test_direct_memory_call_cannot_write_or_stage(self):
        tools = ModuleType("tools")
        tools.__path__ = []
        memory = ModuleType("tools.memory_tool")
        calls = []
        def original(*args, **kwargs):
            calls.append((args, kwargs))
            return json.dumps({"success": True})
        memory.memory_tool = original
        ns = {"_select_tool_names": lambda *a, **kw: ["memory"]}
        with mock.patch.dict(sys.modules, {"tools": tools, "tools.memory_tool": memory}):
            exec(compile(ac.build_model_tools_block({"memory_read_only": True}), "<managed tools>", "exec"), ns)
            for action in ("add", "replace", "remove", "batch"):
                result = json.loads(memory.memory_tool(action=action, target="user", content="new fact"))
                self.assertFalse(result["success"])
                self.assertIn("read-only", result["error"])
            self.assertEqual(calls, [])

    def test_selection_preserves_upstream_collection_type(self):
        for kind in (set, frozenset, list, tuple):
            with self.subTest(kind=kind):
                original = kind(("memory", "read_file"))
                ns = {"_select_tool_names": lambda *a, **kw: original}
                tools = ModuleType("tools")
                tools.__path__ = []
                memory = ModuleType("tools.memory_tool")
                with mock.patch.dict(sys.modules, {"tools": tools, "tools.memory_tool": memory}):
                    exec(compile(ac.build_model_tools_block({"memory_read_only": True}), "<managed tools>", "exec"), ns)
                filtered = ns["_select_tool_names"]()
                self.assertIs(type(filtered), kind)
                self.assertEqual(set(filtered), {"read_file"})
                self.assertIn("memory", original)

    def test_memory_tool_is_removed_before_main_and_bridge_assembly(self):
        ns = {}
        selected = []
        def select(*args, **kwargs):
            return ["read_file", "memory"]
        def compute(*args, **kwargs):
            names = ns["_select_tool_names"](*args, **kwargs)
            selected.append(names)
            return [{"type": "function", "function": {"name": name, "description": "original"}}
                    for name in names]
        ns.update(_select_tool_names=select, _compute_tool_definitions=compute)
        tools = ModuleType("tools")
        tools.__path__ = []
        memory = ModuleType("tools.memory_tool")
        with mock.patch.dict(sys.modules, {"tools": tools, "tools.memory_tool": memory}):
            exec(compile(ac.build_model_tools_block({"memory_read_only": True}), "<managed tools>", "exec"), ns)
        for bridge in (False, True):
            defs = ns["_compute_tool_definitions"](skip_tool_search_assembly=bridge)
            self.assertEqual([d["function"]["name"] for d in defs], ["read_file"])
            self.assertEqual(selected[-1], ["read_file"])

    def test_profile_snapshots_do_not_bleed_and_provider_is_preserved_once(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as td:
            home = Path(td)
            other = home / "profiles" / "other"
            for folder, content in ((home, "default user"), (other, "other user")):
                (folder / "memories").mkdir(parents=True)
                (folder / "memories" / "USER.md").write_text(content, encoding="utf-8")
            ns = prompt_namespace({"memory_read_only": True, "compact_rewrites": {"memory_block_compact": True}})
            a = agent(home)
            a._memory_manager = True
            first = "\n".join(ns["_memory_parts"](a))
            self.assertIn("default user", first)
            self.assertNotIn("other user", first)
            self.assertEqual(first.count("external provider"), 1)
            a._session_db.db_path = str(other / "state.db")
            second = "\n".join(ns["_memory_parts"](a))
            self.assertIn("other user", second)
            self.assertNotIn("default user", second)

    def test_mode_off_and_missing_files_are_nonmutating(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as td:
            home = Path(td)
            ns = prompt_namespace({"memory_read_only": True, "compact_rewrites": {"memory_block_compact": True}})
            self.assertEqual(ns["_memory_parts"](agent(home)), [])
            self.assertFalse((home / "memories").exists())
            ns = prompt_namespace({"memory_read_only": False})
            a = agent(home)
            a._memory_manager = True
            self.assertEqual(ns["_memory_parts"](a), ["external provider"])
            self.assertFalse(hasattr(a, "_hc_readonly_memory_snapshots"))

    def test_disabled_memory_is_visible_as_full_frozen_readonly_snapshot(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as td:
            home = Path(td)
            memories = home / "memories"
            memories.mkdir()
            text = "偏好完整保留\n" + "甲" * 500 + "\n" + "═" * 20 + "\n末尾不可遺失\n"
            user = memories / "USER.md"
            user.write_bytes(text.encode("utf-8"))
            original = user.read_bytes()
            ns = prompt_namespace({"memory_read_only": True, "compact_rewrites": {"memory_block_compact": True}})
            a = agent(home)
            first = ns["_memory_parts"](a)
            rendered = "\n".join(first)
            self.assertIn(text, rendered)
            self.assertIn("read-only", rendered)
            self.assertIn("Do not", rendered)
            self.assertEqual(user.read_bytes(), original)
            self.assertEqual(sorted(p.name for p in memories.iterdir()), ["USER.md"])
            user.write_text("updated by human", encoding="utf-8")
            self.assertEqual(ns["_memory_parts"](a), first)
            self.assertIn("updated by human", "\n".join(ns["_memory_parts"](agent(home))))


if __name__ == "__main__":
    unittest.main()
