from __future__ import annotations

"""Publish-safety regression tests.

Guards the two things that must never reach the public repository:
  1. text that leaks a real machine (absolute home paths, drive paths into personal folders);
  2. local scratch files that carry such text (UI state, saved rendered prompts, tool backups).

Everything is checked against the set of files Git would actually commit, so keeping dev scratch
files in your working copy is fine as long as .gitignore covers them.
"""

import fnmatch
import importlib.util
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("apply_compact", ROOT / "apply_compact.py")
assert SPEC and SPEC.loader
ac = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ac)

BINARY_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".ico", ".zip")

# Absolute paths that identify one real machine / one real person.
LEAK_PATTERNS = (
    re.compile(r"[A-Za-z]:\\+Users\\+[^\\\s]+"),
    re.compile(r"[A-Za-z]:\\+Documents and Settings\\+[^\\\s]+"),
    re.compile(r"/(?:Users|home)/[A-Za-z0-9._-]+/"),
    re.compile(r"[A-Za-z]:\\+[^\\\s]*[\u4e00-\u9fff]"),
)

# A file whose name matches any of these carries local state and must be git-ignored.
SCRATCH_SUFFIXES = (".bak", ".disabled")
SCRATCH_NAMES = ("ui_state.json", "captured.json")
SCRATCH_PREFIXES = ("tests/_",)


def _entries():
    for path in sorted(ROOT.rglob("*")):
        if path.is_file():
            yield path.relative_to(ROOT), path


def _gitignore_lines():
    lines = []
    for raw in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            lines.append(line)
    return lines


def _matches(rel_posix: str, pattern: str) -> bool:
    if pattern.endswith("/"):
        base = pattern.rstrip("/")
        return rel_posix == base or rel_posix.startswith(base + "/")
    if "/" in pattern:
        return fnmatch.fnmatch(rel_posix, pattern)
    return any(fnmatch.fnmatch(part, pattern) for part in rel_posix.split("/"))


def is_ignored(rel_posix: str) -> bool:
    """Minimal gitignore evaluation: last matching pattern wins, '!' negates."""
    ignored = False
    for pattern in _gitignore_lines():
        negate = pattern.startswith("!")
        body = pattern[1:] if negate else pattern
        if _matches(rel_posix, body):
            ignored = not negate
    return ignored


def is_scratch(rel: Path) -> bool:
    name = rel.name
    return (
        name.endswith(SCRATCH_SUFFIXES)
        or name in SCRATCH_NAMES
        or any(rel.as_posix().startswith(prefix) for prefix in SCRATCH_PREFIXES)
    )


class PublishSafetyTests(unittest.TestCase):
    def test_no_committable_file_leaks_a_local_path(self):
        leaks = []
        for rel, path in _entries():
            if is_ignored(rel.as_posix()):
                continue
            if path.suffix.lower() in BINARY_SUFFIXES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for pattern in LEAK_PATTERNS:
                found = pattern.search(text)
                if found:
                    leaks.append(rel.as_posix() + ": " + repr(found.group(0)))
        self.assertEqual(leaks, [], "absolute local paths must not be committed: " + "; ".join(leaks))

    def test_local_scratch_state_and_backups_are_git_ignored(self):
        unignored = [rel.as_posix() for rel, _ in _entries()
                     if is_scratch(rel) and not is_ignored(rel.as_posix())]
        self.assertEqual(unignored, [], "these files carry local state and must be git-ignored: " + "; ".join(unignored))

    def test_gitignore_keeps_the_privacy_guards(self):
        text = (ROOT / ".gitignore").read_text(encoding="utf-8")
        for needle in ("overrides/", "overrides.d/*.json", "ui_state.json",
                       "*.bak", "tests/_*", "_*.png", "__pycache__/"):
            self.assertIn(needle, text, ".gitignore must keep the " + repr(needle) + " guard")

    def test_every_example_config_is_publishable(self):
        """An example that .gitignore swallows never reaches the repo - the 90-private negation
        must stay general enough to cover every *.example.json drop-in."""
        examples = [p.relative_to(ROOT).as_posix()
                    for p in sorted(ROOT.glob("*.example.json")) + sorted((ROOT / "overrides.d").glob("*.example.json"))]
        self.assertTrue(examples)
        self.assertEqual([e for e in examples if is_ignored(e)], [],
                         "these examples would be silently excluded from the repo")

    def test_every_example_config_passes_the_shared_schema(self):
        examples = sorted(ROOT.glob("*.example.json")) + sorted((ROOT / "overrides.d").glob("*.example.json"))
        self.assertTrue(examples)
        for path in examples:
            with self.subTest(example=path.name):
                ac.validate_config(json.loads(path.read_text(encoding="utf-8")), source=str(path))

    def test_slim_example_only_overrides_known_prompt_globals(self):
        path = ROOT / "overrides.d" / "95-prompt-slim.example.json"
        if not path.is_file():
            self.skipTest("optional slim example not present")
        data = json.loads(path.read_text(encoding="utf-8"))
        strings = data.get("prompt_strings") or {}
        self.assertTrue(strings)
        for key in strings:
            self.assertIn(key, ac._ALLOWED_PROMPT_GLOBALS, key)


if __name__ == "__main__":
    unittest.main()
