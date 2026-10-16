#!/usr/bin/env python3
"""hermes-lean: one-shot re-apply of compact tool & prompt descriptions.

WHY THIS EXISTS
---------------
Hermes ships verbose, model-facing tool schemas and system-prompt blocks. Carefully
slimming them can reduce per-request prompt tokens while preserving intended behavior.
Hermes updates may overwrite hand-edited compaction, so this tool reapplies and verifies it.

This script keeps your compact wording in ONE data file and re-applies it with a
single command after each update. It never touches memory, the user profile, or any
secret. It only writes clearly-marked, self-contained blocks at the END of three
source files:

    <hermes-agent>/model_tools.py             (tool + parameter descriptions)
    <hermes-agent>/agent/prompt_builder.py    (system-prompt guidance + lean skill index)
    <hermes-agent>/agent/coding_context.py    (skill-index focus gate)

The block is pure additive Python appended after the existing code, so an upstream
`git pull` almost never conflicts with it (upstream edits the file body, not our tail).
It is SELF-CONTAINED: it re-asserts its own wording and its own lean skill-index
renderer, so it keeps working even if the update reverts the file body to upstream.
Re-running the script is idempotent: it replaces its own managed block in place.

USAGE
-----
    python apply_compact.py                      # apply (backs up each file first)
    python apply_compact.py --check              # dry run: report, write nothing
    python apply_compact.py --restore            # remove the managed blocks
    python apply_compact.py --capture captured.json   # snapshot live values to a config
    python apply_compact.py --hermes-root C:/path/to/.hermes

Exit codes: 0 success, 1 failure (e.g. target file missing, verification failed).

Config files are MERGED in this order (later wins; dicts merge, lists union):
    1. --config PATH            (repeatable; default ./overrides/compact-overrides.json)
    2. ./overrides.d/*.json     (drop-in additions, sorted by name -- keep PRIVATE extras here)
    3. --add-config PATH        (repeatable)

A config is a JSON object with keys:
    tool_descriptions                   {tool_name: "compact description"}
    parameter_descriptions              {tool_name: {param: "compact description"}}
    prompt_strings                      {GLOBAL_NAME: "compact text",
                                         "PLATFORM_HINTS__<platform>": "compact text"}
    skill_description_exception_skills  ["skill-a", "skill-b"]   # optional

Split PRIVATE additions (your skill allowlist, personal wording) into a file under
./overrides.d/ so the shared/base config stays publishable. Keys starting with "_" are
comments and are ignored when merging. Copy compact-overrides.example.json to start.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def _child_env() -> dict:
    """Environment for the trusted-checkout subprocesses.

    Force UTF-8 on BOTH sides: the child's stdio (so non-ASCII diagnostics like
    the tool-search emoji or Chinese descriptions never raise UnicodeEncodeError
    inside the child) and our explicit UTF-8 decode of its pipes. Without this,
    a host whose ANSI codepage is not UTF-8 (e.g. cp950) fails on
    ``subprocess(text=True)`` — it decodes with ``locale.getencoding()`` while
    our shell-exported PYTHONUTF8/PYTHONIOENCODING are absent in double-click /
    shortcut launches.
    """
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


START_MARKER = "# ==== hermes-lean: managed block (auto-generated) ===="
END_MARKER = "# ==== end hermes-lean ===="
PLATFORM_PREFIX = "PLATFORM_HINTS__"

# Older release used the name "hermes-compact" in its markers. Blocks written by that
# version are still present in existing installs, so accept those markers too — that
# way renaming the project never orphans an already-applied block (it is replaced in
# place, no duplicate is left behind).
LEGACY_START_MARKER = "# ==== hermes-compact: managed block (auto-generated) ===="
LEGACY_END_MARKER = "# ==== end hermes-compact ===="
ALL_START_MARKERS = (START_MARKER, LEGACY_START_MARKER)
ALL_END_MARKERS = (END_MARKER, LEGACY_END_MARKER)

_HC_COMPACT_FUNC = r"""def _hc_compact_tool_descriptions(tool_defs):
    # Return model-facing schemas with compact, string-only descriptions.
    compact = copy.deepcopy(tool_defs)

    def normalize(value):
        if isinstance(value, dict):
            for key, item in list(value.items()):
                if key == "description" and not isinstance(item, str):
                    if isinstance(item, (tuple, list)) and len(item) == 1:
                        value[key] = str(item[0])
                    else:
                        value[key] = str(item)
                else:
                    value[key] = normalize(item)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                value[index] = normalize(item)
        return value

    for tool in compact:
        function = tool.get("function", {})
        name = function.get("name", "")
        original_description = function.get("description", "")
        if name in _HC_TOOL_DESCRIPTIONS:
            function["description"] = _HC_TOOL_DESCRIPTIONS[name]
        if name == "browser_exec":
            if "Lightpanda" in str(original_description):
                function["description"] += " Lightpanda: no screenshots; call new_tab(url) once, then goto_url(url)."
            elif "Screenshots are attached" in str(original_description):
                function["description"] += " Screenshots attach automatically; inspect them directly, not via vision."
            elif "model cannot view images" in str(original_description):
                function["description"] += " Text-only mode: use page_info/js/fill_input and skip screenshots."
        elif name == "execute_code":
            names = None
            _m = re.search(r"Import from hermes_tools: ([^.]+)[.]", str(original_description))
            if _m:
                names = _m.group(1).strip()
            else:
                _m2 = re.search(r"from hermes_tools import ([^.\n]+)", str(original_description))
                if _m2 and _m2.group(1).strip() not in ("", "..."):
                    names = _m2.group(1).strip()
            if not names:
                _found = re.findall(r"^\s{2}([a-z_][a-z0-9_]*)\([^)]*\)\s*->", str(original_description), re.M)
                if _found:
                    names = ", ".join(_found[:3]) + ", ..."
            if names:
                function["description"] += " Available imports: " + names + "."
        elif name == "delegate_task":
            child_match = re.search(r"Children cannot call ([^.]+)[.]", str(original_description))
            if child_match:
                function["description"] += " Children cannot call " + child_match.group(1) + "."
            if "Child summaries are unverified" in str(original_description):
                function["description"] += " Child summaries are unverified; verify external side effects."
            if "Children use the parent model" in str(original_description):
                function["description"] += " Children use the parent model unless provider/model is configured."
        properties = function.get("parameters", {}).get("properties", {})
        for parameter, description in _HC_PARAMETER_DESCRIPTIONS.get(name, {}).items():
            if parameter in properties:
                properties[parameter]["description"] = description
        normalize(tool)
    return compact"""

MODEL_TOOLS_REL = "model_tools.py"
PROMPT_BUILDER_REL = "agent/prompt_builder.py"
CODING_CONTEXT_REL = "agent/coding_context.py"
SYSTEM_PROMPT_REL = "agent/system_prompt.py"

# ``compact_rewrites`` keys: JSON-driven rewordings of *dynamic* prompt blocks
# (values are built from live paths at prompt-build time, so the block re-renders
# them from a template instead of replacing a constant).
_REWRITE_KEYS = ("host_line_template", "host_note", "profile_line", "memory_block_compact")


# --------------------------------------------------------------------------- #
# Target discovery
# --------------------------------------------------------------------------- #
def default_hermes_home() -> Path:
    """$HERMES_HOME if set, else the platform default."""
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env)
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        return Path(base) / "hermes"
    return Path.home() / ".hermes"


def resolve_hermes_root(explicit: str | None) -> Path:
    """Return the hermes-agent source checkout directory (contains model_tools.py)."""
    if explicit:
        cand = Path(explicit)
        # Accept either the hermes home or the checkout itself.
        if (cand / MODEL_TOOLS_REL).is_file():
            return cand
        if (cand / "hermes-agent" / MODEL_TOOLS_REL).is_file():
            return cand / "hermes-agent"
        raise SystemExit(f"[x] --hermes-root does not contain {MODEL_TOOLS_REL}: {cand}")

    home = default_hermes_home()
    for cand in (home / "hermes-agent", home):
        if (cand / MODEL_TOOLS_REL).is_file():
            return cand
    raise SystemExit(
        f"[x] Could not find a hermes-agent checkout under {home}.\n"
        "    Pass --hermes-root <path to .hermes or hermes-agent>."
    )


def venv_python(root: Path) -> Path | None:
    """Best-effort interpreter that can import the checkout (for verification)."""
    for rel in ("venv/Scripts/python.exe", "venv/bin/python", ".venv/Scripts/python.exe", ".venv/bin/python"):
        p = root / rel
        if p.is_file():
            return p
    return None


# --------------------------------------------------------------------------- #
# Managed block construction (self-contained; no other local edit required)
# --------------------------------------------------------------------------- #
def build_model_tools_block(cfg: dict) -> str:
    tool_desc = cfg.get("tool_descriptions") or {}
    param_desc = cfg.get("parameter_descriptions") or {}
    skills_read_only = bool(cfg.get("skills_read_only"))
    return "\n".join([
        START_MARKER,
        "# Compact tool/parameter descriptions, re-applied by hermes-lean/apply_compact.py.",
        "# Self-contained: embeds the compactor and wraps _compute_tool_definitions.",
        "# Regenerated from compact-overrides.json; edit the JSON, not this block.",
        "import copy",
        "import re",
        "",
        f"_HC_SKILLS_READ_ONLY = {skills_read_only!r}",
        f"_HC_MEMORY_READ_ONLY = {bool(cfg.get('memory_read_only'))!r}",
        "",
        "if _HC_MEMORY_READ_ONLY:",
        "    _hc_orig_select_tool_names = _select_tool_names",
        "    def _hc_select_tool_names(*_a, **_kw):",
        "        _names = _hc_orig_select_tool_names(*_a, **_kw)",
        "        if isinstance(_names, (set, frozenset)):",
        "            return _names - {'memory'}",
        "        return type(_names)(_name for _name in _names if _name != 'memory')",
        "    _select_tool_names = _hc_select_tool_names",
        "    import importlib as _hc_importlib",
        "    import json as _hc_json",
        "    _hc_memory_module = _hc_importlib.import_module('tools.memory_tool')",
        "    def _hc_deny_memory_write(*_a, **_kw):",
        "        return _hc_json.dumps({'success': False, 'error':",
        "            'Persistent memory is read-only in this agent; no writes or proposals are allowed.'})",
        "    _hc_memory_module.memory_tool = _hc_deny_memory_write",
        "",
        "# --- SKILL READ-ONLY MODE -------------------------------------------------",
        "# Register a custom toolset exposing ONLY the read-side skill tools, so the",
        "# model can discover (skills_list) and load (skill_view) existing skills but",
        "# can never create, modify or delete them (skill_manage is absent).",
        "# Explicit allowlist: never includes=['skills'], so any future upstream tool",
        "# added to that toolset cannot leak into the main agent.",
        "# create_custom_toolset() only mutates the in-process TOOLSETS registry, so it",
        "# MUST run at import time here (the same process that resolves tool names).",
        "if _HC_SKILLS_READ_ONLY:",
        "    try:",
        "        from toolsets import create_custom_toolset as _hc_create_custom_toolset",
        "        _hc_create_custom_toolset(",
        '            "skills-read",',
        '            "Read existing skills without creating or modifying them",',
        '            tools=["skills_list", "skill_view"],',
        "        )",
        "        _hc_skills_read_only_installed = True",
        "    except Exception:",
        "        _hc_skills_read_only_installed = False",
        "# --------------------------------------------------------------------------",
        "",
        f"_HC_TOOL_DESCRIPTIONS = {tool_desc!r}",
        "",
        f"_HC_PARAMETER_DESCRIPTIONS = {param_desc!r}",
        "",
        f"_HC_COMPACT_BRIDGE = {bool(cfg.get('compact_tool_search_schemas'))!r}",
        _HC_COMPACT_FUNC,
        "",
        "def _hc_install_tool_overrides():",
        '    """Wrap _compute_tool_definitions so model-facing schemas get compact descriptions."""',
        "    _g = globals()",
        '    if _g.get("_hc_tool_overrides_installed"):',
        "        return",
        '    _orig = _g.get("_compute_tool_definitions")',
        "    if _orig is None:",
        "        return",
        "",
        "    def _hc_compute(*_a, **_kw):",
        "        _raw = _orig(*_a, **_kw)",
        "        if not _HC_COMPACT_BRIDGE:",
        '            _skip = _kw.get("skip_tool_search_assembly", _a[3] if len(_a) > 3 else False)',
        "            if _skip:",
        "                return _raw",
        "        return _hc_compact_tool_descriptions(_raw)",
        "",
        '    _g["_compute_tool_definitions"] = _hc_compute',
        '    _g["_hc_tool_overrides_installed"] = True',
        "",
        "",
        "_hc_install_tool_overrides()",
        END_MARKER,
    ])


def build_prompt_builder_block(cfg: dict) -> str:
    strings = cfg.get("prompt_strings") or {}
    platform = {k[len(PLATFORM_PREFIX):]: v for k, v in strings.items() if k.startswith(PLATFORM_PREFIX)}
    globals_ = {k: v for k, v in strings.items() if not k.startswith(PLATFORM_PREFIX)}
    skills = list(cfg.get("skill_description_exception_skills") or [])
    rewrites = cfg.get("compact_rewrites") or {}
    host_line = rewrites.get("host_line_template")
    host_note = rewrites.get("host_note")
    return "\n".join([
        START_MARKER,
        "# Compact system-prompt strings + lean skill index, re-applied by hermes-lean/apply_compact.py.",
        "# Self-contained: does not depend on any other local edit.",
        "# Regenerated from compact-overrides.json; edit the JSON, not this block.",
        "import re",
        "",
        f"_HC_PROMPT_STRINGS = {globals_!r}",
        f"_HC_PLATFORM_HINTS = {platform!r}",
        f"_HC_SKILL_EXCEPTIONS = frozenset({skills!r})",
        f"_HC_HOST_LINE = {host_line!r}",
        f"_HC_HOST_NOTE = {host_note!r}",
        "",
        "",
        "def _hc_build_skills_index(skills_by_category, category_descriptions, compact_categories, available_tools):",
        '    """Lean ## Skills block: names-only, with descriptions only for allowlisted skills."""',
        "    if not skills_by_category:",
        '        return ""',
        "    _demoted = frozenset(_c for _c in skills_by_category",
        "                        if _c.split('/', 1)[0] in (compact_categories or frozenset()))",
        "    _lines = []",
        "    _names_only = False",
        "    for _cat in sorted(skills_by_category):",
        "        _entries = skills_by_category[_cat]",
        "        if _cat in _demoted:",
        "            _plain = []",
        "            _exceptions = []",
        "            _exc_seen = set()",
        "            for _n, _d in sorted(_entries, key=lambda _x: _x[0]):",
        "                if _n in _HC_SKILL_EXCEPTIONS and _d:",
        "                    if _n not in _exc_seen:",
        "                        _exc_seen.add(_n)",
        "                        _exceptions.append((_n, _d))",
        "                else:",
        "                    _plain.append(_n)",
        "            if _plain:",
        '                _lines.append("  %s [names only]: %s" % (_cat, ", ".join(sorted(set(_plain)))))',
        "            if _exceptions:",
        "                if not _plain:",
        '                    _lines.append("  %s:" % _cat)',
        "                for _n, _d in _exceptions:",
        '                    _lines.append("    - %s: %s" % (_n, _d))',
        "            _names_only = bool(_plain)",
        "            continue",
        '        _lines.append("  %s:" % _cat)',
        "        _seen = set()",
        "        for _n, _d in sorted(_entries, key=lambda _x: _x[0]):",
        "            if _n in _seen:",
        "                continue",
        "            _seen.add(_n)",
        "            if _n in _HC_SKILL_EXCEPTIONS and _d:",
        '                _lines.append("    - %s: %s" % (_n, _d))',
        "            else:",
        '                _lines.append("    - %s" % _n)',
        '    _note = "\\n(Names-only entries remain available and load with skill_view(name).)" if _names_only else ""',
        '    return ("## Skills\\n"',
        '            "Skills are read-only in this agent. "',
        '            "Use skills_list for discovery and skill_view(name) to load relevant existing skills. "',
        '            "Do not create, modify, delete, or rewrite skills.\\n"',
        '            "If a needed skill contains [SKILL_PRUNED], reload it with skill_view(name) "',
        '            "before relying on it.\\n\\n"',
        '            "<available_skills>\\n"',
        '            + "\\n".join(_lines) + "\\n"',
        '            "</available_skills>" + _note)',
        "",
        "",
        "def _hc_render_skills_index(*_a, **_kw):",
        '    """Self-contained lean renderer; falls back to the original on any shape change."""',
        "    _g = globals()",
        "    try:",
        '        _skills = _kw.get("skills_by_category", _a[0] if len(_a) > 0 else None)',
        '        _cat_desc = _kw.get("category_descriptions", _a[1] if len(_a) > 1 else None)',
        '        _compact = _kw.get("compact_categories", _a[2] if len(_a) > 2 else None)',
        '        _tools = _kw.get("available_tools", _a[3] if len(_a) > 3 else None)',
        "        if not isinstance(_skills, dict):",
        "            raise TypeError('unexpected skills_by_category')",
        "        return _hc_build_skills_index(_skills, _cat_desc or {}, _compact, _tools)",
        "    except Exception:",
        '        return _g["_hc_orig_render_skills_index"](*_a, **_kw)',
        "",
        "",
        "def _hc_compact_host_block(_text):",
        '    """Merge the label-per-line host block into one line; unknown shapes pass through."""',
        "    if not isinstance(_text, str) or not _HC_HOST_LINE:",
        "        return _text",
        "    _vals = {}",
        '    for _key, _label in (("host", "Host"), ("home", "User home directory"), ("cwd", "Current working directory")):',
        '        _m = re.search("^" + re.escape(_label) + ": (.+)$", _text, re.M)',
        "        if not _m:",
        "            return _text",
        "        _vals[_key] = _m.group(1).strip()",
        "    try:",
        "        _head = _HC_HOST_LINE.format(**_vals)",
        "    except Exception:",
        "        return _text",
        '    return _head + ("\\n" + _HC_HOST_NOTE if _HC_HOST_NOTE and "\\nNote:" in _text else "")',
        "",
        "",
        "def _hc_local_host_hints(*_a, **_kw):",
        '    """Self-contained host block; falls back to the original on any shape change."""',
        "    _g = globals()",
        "    _orig = _g.get('_hc_orig_local_host_hints')",
        "    if _orig is None:",
        "        return []",
        "    try:",
        "        _hints = _orig(*_a, **_kw)",
        "    except Exception:",
        "        return _orig(*_a, **_kw)",
        "    if not isinstance(_hints, list):",
        "        return _hints",
        "    try:",
        "        return [_hc_compact_host_block(_hint) for _hint in _hints]",
        "    except Exception:",
        "        return _hints",
        "",
        "",
        "def _hc_install_prompt_overrides():",
        '    """Replace guidance strings and install the self-contained skill-index renderer."""',
        "    _g = globals()",
        '    if _g.get("_hc_prompt_overrides_installed"):',
        "        return",
        "    for _k, _v in _HC_PROMPT_STRINGS.items():",
        "        if _k in _g:",
        "            _g[_k] = _v",
        '    if isinstance(_g.get("PLATFORM_HINTS"), dict):',
        '        _g["PLATFORM_HINTS"].update(_HC_PLATFORM_HINTS)',
        '    _g["SKILL_DESCRIPTION_EXCEPTION_SKILLS"] = _HC_SKILL_EXCEPTIONS',
        '    _orig = _g.get("_render_skills_index")',
        "    if callable(_orig):",
        '        _g["_hc_orig_render_skills_index"] = _orig',
        '        _g["_render_skills_index"] = _hc_render_skills_index',
        '    _orig_host = _g.get("_local_host_hints")',
        "    if callable(_orig_host):",
        '        _g["_hc_orig_local_host_hints"] = _orig_host',
        '        _g["_local_host_hints"] = _hc_local_host_hints',
        '    _g["_hc_prompt_overrides_installed"] = True',
        "",
        "",
        "_hc_install_prompt_overrides()",
        END_MARKER,
    ])



def build_coding_context_block(cfg: dict) -> str:
    """Block that re-asserts the skill-index gate (the names-only switch).

    ``skill_index_gate``:
      "focus"            -> demote whenever agent.coding_context == focus  (your behaviour)
      "focus_and_coding" -> upstream gate: only in a code workspace AND focus
    ``skill_index_categories`` optionally overrides the demoted category list
    (default: the module's own CODING_PROFILE.compact_skill_categories).
    """
    gate = cfg.get("skill_index_gate") or "focus"
    cats = cfg.get("skill_index_categories")
    cats_literal = "None" if cats is None else repr(list(cats))
    return "\n".join([
        START_MARKER,
        "# Skill-index gate (names-only demotion), re-applied by hermes-lean/apply_compact.py.",
        "# Self-contained: patches RuntimeMode.compact_skill_categories; falls back safely.",
        "# Regenerated from compact-overrides.json; edit the JSON, not this block.",
        f"_HC_SKILL_GATE = {gate!r}",
        f"_HC_SKILL_CATEGORIES = {cats_literal}",
        "",
        "",
        "def _hc_compact_skill_categories(self):",
        '    """Categories to demote to names-only. Never hides or disables skills."""',
        "    try:",
        '        if _HC_SKILL_GATE == "focus_and_coding":',
        '            if not self.is_coding or self.config_mode != "focus":',
        "                return frozenset()",
        "        else:",
        '            if self.config_mode != "focus":',
        "                return frozenset()",
        "        _cats = _HC_SKILL_CATEGORIES",
        "        if _cats is None:",
        '            _cats = getattr(CODING_PROFILE, "compact_skill_categories", ())',
        "        return frozenset(_cats)",
        "    except Exception:",
        "        return frozenset()",
        "",
        "",
        "def _hc_install_coding_context_override():",
        '    """Replace the gate method on RuntimeMode; no-op if the class moved."""',
        "    _g = globals()",
        '    if _g.get("_hc_coding_context_installed"):',
        "        return",
        '    _cls = _g.get("RuntimeMode")',
        "    if _cls is not None:",
        "        _cls.compact_skill_categories = _hc_compact_skill_categories",
        '        _g["_hc_coding_context_installed"] = True',
        "",
        "",
        "_hc_install_coding_context_override()",
        END_MARKER,
    ])


def build_system_prompt_block(cfg: dict) -> str:
    """Block that compacts two *dynamic* pieces of agent/system_prompt.py.

    ``compact_rewrites.profile_line``         reworded ambient "Active Hermes profile" line
                                              ({root} = profile root, filled at render time)
    ``compact_rewrites.memory_block_compact`` drop the ═ rule lines memory_tool_store wraps
                                              around every MEMORY / USER PROFILE block
    """
    rewrites = cfg.get("compact_rewrites") or {}
    profile_line = rewrites.get("profile_line")
    memory_compact = rewrites.get("memory_block_compact")
    return "\n".join([
        START_MARKER,
        "# Compact profile line + memory/USER block framing, re-applied by hermes-lean/apply_compact.py.",
        "# Self-contained: wraps agent.system_prompt._active_profile_line/_memory_parts and falls",
        "# back to the upstream text whenever the shape it parses changes.",
        "# Regenerated from compact-overrides.json; edit the JSON, not this block.",
        "import re as _hc_re",
        "",
        f"_HC_PROFILE_LINE = {profile_line!r}",
        f"_HC_MEMORY_COMPACT = {memory_compact!r}",
        f"_HC_MEMORY_READ_ONLY = {bool(cfg.get('memory_read_only'))!r}",
        "",
        "def _hc_readonly_memory_parts(_agent):",
        '    """Read existing files only; freeze a separate snapshot for each agent/profile."""',
        "    from pathlib import Path as _Path",
        "    _home = _agent_home(_agent)",
        "    if _home is None:",
        "        from hermes_constants import get_hermes_home as _get_home",
        "        _home = _get_home()",
        "    _home = _Path(_home)",
        "    _key = str(_home.resolve())",
        "    _snapshots = getattr(_agent, '_hc_readonly_memory_snapshots', None)",
        "    if _snapshots is None:",
        "        _snapshots = {}",
        "        _agent._hc_readonly_memory_snapshots = _snapshots",
        "    if _key not in _snapshots:",
        "        _blocks = []",
        "        for _name, _title in (('MEMORY.md', 'MEMORY (your personal notes)'),",
        "                              ('USER.md', 'USER PROFILE (who the user is)')):",
        "            _path = _home / 'memories' / _name",
        "            try:",
        "                _content = _path.read_bytes().decode('utf-8-sig')",
        "            except FileNotFoundError:",
        "                continue",
        "            if _content.strip(' \\t\\r\\n§'):",
        "                _blocks.append(_title + ' [read-only]\\n' + _content)",
        "        if _blocks:",
        "            _blocks.insert(0, 'Persistent memory is read-only. Use it as context. '",
        "                           'Do not add, replace, delete, or rewrite MEMORY.md or USER.md '",
        "                           'with memory, file, terminal, or other tools.')",
        "        _snapshots[_key] = tuple(_blocks)",
        "    return list(_snapshots[_key])",
        "",
        "",
        "def _hc_compact_memory_part(_part):",
        '    """Drop the ═ rule lines around a memory block; the header line is kept verbatim."""',
        "    if not _HC_MEMORY_COMPACT or not isinstance(_part, str):",
        "        return _part",
        "    try:",
        '        _kept = [_line for _line in _part.split("\\n")',
        '                 if not (len(_line) >= 8 and set(_line) == {"═"})]',
        '        _out = "\\n".join(_kept).strip("\\n")',
        "        return _out or _part",
        "    except Exception:",
        "        return _part",
        "",
        "",
        "def _hc_compact_profile_line(_text):",
        '    """Reword the ambient profile line from the template; unknown shapes pass through."""',
        "    if not isinstance(_text, str) or not _HC_PROFILE_LINE:",
        "        return _text",
        '    _flat = _text.replace("\\n", " ")',
        '    _m = _hc_re.search(r"live\\s+under\\s+(\\S+?)/profiles/", _flat)',
        "    if not _m:",
        "        return _text",
        "    try:",
        "        return _HC_PROFILE_LINE.format(root=_m.group(1))",
        "    except Exception:",
        "        return _text",
        "",
        "",
        "def _hc_active_profile_line(*_a, **_kw):",
        '    """Self-contained compact profile line; falls back to the original on any change."""',
        "    _g = globals()",
        "    _orig = _g.get('_hc_orig_active_profile_line')",
        "    if _orig is None:",
        '        return ""',
        "    try:",
        "        _text = _orig(*_a, **_kw)",
        "    except Exception:",
        "        return _orig(*_a, **_kw)",
        "    try:",
        "        return _hc_compact_profile_line(_text)",
        "    except Exception:",
        "        return _text",
        "",
        "",
        "def _hc_memory_parts(*_a, **_kw):",
        '    """Self-contained memory/USER blocks without the ═ framing; falls back on any change."""',
        "    _g = globals()",
        "    _orig = _g.get('_hc_orig_memory_parts')",
        "    if _orig is None:",
        "        return []",
        "    if _HC_MEMORY_READ_ONLY:",
        "        import copy as _hc_copy",
        "        _agent = _a[0] if _a else _kw['agent']",
        "        _snapshot = _hc_readonly_memory_parts(_agent)",
        "        _proxy = _hc_copy.copy(_agent)",
        "        _proxy._memory_store = None",
        "        return _snapshot + [_hc_compact_memory_part(_part) for _part in _orig(_proxy)]",
        "    else:",
        "        _parts = _orig(*_a, **_kw)",
        "    if not isinstance(_parts, list):",
        "        return _parts",
        "    try:",
        "        return [_hc_compact_memory_part(_part) for _part in _parts]",
        "    except Exception:",
        "        return _parts",
        "",
        "",
        "def _hc_install_system_prompt_overrides():",
        '    """Swap in the wrappers; no-op (never raises) if upstream renamed anything."""',
        "    _g = globals()",
        '    if _g.get("_hc_system_prompt_installed"):',
        "        return",
        '    _orig_profile = _g.get("_active_profile_line")',
        "    if callable(_orig_profile):",
        '        _g["_hc_orig_active_profile_line"] = _orig_profile',
        '        _g["_active_profile_line"] = _hc_active_profile_line',
        '    _orig_memory = _g.get("_memory_parts")',
        "    if callable(_orig_memory):",
        '        _g["_hc_orig_memory_parts"] = _orig_memory',
        '        _g["_memory_parts"] = _hc_memory_parts',
        '    _g["_hc_system_prompt_installed"] = True',
        "",
        "",
        "_hc_install_system_prompt_overrides()",
        END_MARKER,
    ])


# --------------------------------------------------------------------------- #
# Marker-based block write
# --------------------------------------------------------------------------- #
def _marker_matches(text: str, markers: tuple[str, ...]):
    """Find every marker that occupies a whole line, for any accepted marker spelling."""
    pattern_suffix = "\r?$"
    found = []
    for marker in markers:
        found.extend(re.finditer("(?m)^" + re.escape(marker) + pattern_suffix, text))
    return sorted(found, key=lambda m: m.start())


def strip_block(text: str) -> tuple[str, bool]:
    """Remove exactly one managed block while preserving all surrounding text.

    Accepts both the current markers and the legacy "hermes-compact" markers, so a
    block applied by an older release is replaced in place instead of being left
    orphaned (which would duplicate it on the next apply).
    """
    start_matches = _marker_matches(text, ALL_START_MARKERS)
    end_matches = _marker_matches(text, ALL_END_MARKERS)
    if not start_matches and not end_matches:
        return text, False
    if len(start_matches) != 1 or len(end_matches) != 1 or end_matches[0].start() < start_matches[0].end():
        raise SystemExit(
            f"[x] Expected exactly one managed block; found "
            f"{len(start_matches)} start marker(s) and {len(end_matches)} end marker(s)."
        )
    start = start_matches[0].start()
    end = end_matches[0].end()
    return text[:start] + text[end:], True

def upsert_block(text: str, block: str) -> str:
    """Insert or replace the managed block at the end of the file (idempotent)."""
    base, found = strip_block(text)
    if found:
        base = base.rstrip("\r\n")
    else:
        base = text.rstrip("\r\n")
    return base + "\n\n" + block + "\n"

def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _is_link_or_reparse(path: Path) -> bool:
    """True for symlinks and Windows reparse points (junction-like objects)."""
    if path.is_symlink():
        return True
    try:
        attrs = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return False
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attrs & flag)


def ensure_safe_target(root: Path, path: Path) -> None:
    """Reject targets outside *root* or reached through a link/reparse point."""
    root_real = root.resolve(strict=True)
    try:
        path.resolve(strict=True).relative_to(root_real)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"[x] Refusing target outside Hermes checkout: {path}") from exc
    current = root_real
    relative = path.resolve(strict=False).relative_to(root_real)
    for part in relative.parts:
        current = current / part
        if current.exists() and _is_link_or_reparse(current):
            raise SystemExit(f"[x] Refusing symlink/reparse-point target: {current}")
    if not path.is_file():
        raise SystemExit(f"[x] Expected regular source file: {path}")


def backup(path: Path) -> Path:
    """Create a collision-free backup without following a destination symlink."""
    if _is_link_or_reparse(path):
        raise SystemExit(f"[x] Refusing to back up a symlink/reparse point: {path}")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    prefix = path.name + f".bak-hermes-lean-{stamp}-"
    fd, raw = tempfile.mkstemp(prefix=prefix, dir=str(path.parent))
    os.close(fd)
    dst = Path(raw)
    try:
        with path.open("rb") as src, dst.open("wb") as out:
            shutil.copyfileobj(src, out)
        shutil.copystat(path, dst, follow_symlinks=False)
        return dst
    except Exception:
        dst.unlink(missing_ok=True)
        raise


# --------------------------------------------------------------------------- #
# Verification (import in the checkout's venv)
# --------------------------------------------------------------------------- #
VERIFY_SNIPPET = r"""
import json, sys
sys.path.insert(0, ROOT)
cfg = json.loads(CFG_JSON)
report = {"tool_ok": 0, "tool_bad": [], "prompt_ok": 0, "prompt_bad": [], "skills_ok": False}

import model_tools
# Verify our own tail-block compactor directly (present even if the body was reverted).
for name, want in (cfg.get("tool_descriptions") or {}).items():
    sample = [{"type": "function", "function": {
        "name": name, "description": "LONG ORIGINAL",
        "parameters": {"type": "object", "properties": {}}}}]
    out = model_tools._hc_compact_tool_descriptions(sample)
    got = out[0]["function"]["description"]
    if got == want:
        report["tool_ok"] += 1
    else:
        report["tool_bad"].append(name)
report["compute_wrapped"] = getattr(model_tools._compute_tool_definitions, "__name__", "") == "_hc_compute"

# Read-only skill mode: the "skills-read" toolset must exist in-process AND resolve to
# exactly the read-side tools. A configured-but-missing registration must fail here.
report["skills_read_only_ok"] = True
report["skills_read_toolset"] = None
if cfg.get("skills_read_only"):
    report["skills_read_only_ok"] = False
    try:
        from toolsets import resolve_toolset as _hc_resolve_toolset
        _resolved = set(_hc_resolve_toolset("skills-read"))
        report["skills_read_toolset"] = sorted(_resolved)
        report["skills_read_only_ok"] = (
            _resolved == {"skills_list", "skill_view"}
            and "skill_manage" not in _resolved
        )
    except Exception as _e:
        report["skills_read_error"] = str(_e)[:200]

# Read-only memory: exercise the real wrapper against isolated synthetic files only.
report["memory_read_only_ok"] = True
if cfg.get("memory_read_only"):
    report["memory_read_only_ok"] = False
    try:
        import agent.system_prompt as _sp_ro
        import tools.memory_tool as _mt_ro
        from pathlib import Path as _RoPath
        from types import SimpleNamespace as _RoAgent
        from tempfile import TemporaryDirectory as _RoTemp
        with _RoTemp(prefix="hc_memory_ro_") as _tmp:
            _home = _RoPath(_tmp)
            _mdir = _home / "memories"
            _mdir.mkdir()
            _user = ("Synthetic profile\n" + "x" * 600 + "\n" + "═" * 20 + "\nEND\n").encode("utf-8")
            _note = b"Synthetic notes\n"
            (_mdir / "USER.md").write_bytes(_user)
            (_mdir / "MEMORY.md").write_bytes(_note)
            def _new_agent():
                return _RoAgent(_session_db=_RoAgent(db_path=str(_home / "state.db")),
                                _memory_store=None, _memory_enabled=False,
                                _user_profile_enabled=False, _memory_manager=None)
            _agent = _new_agent()
            _first = _sp_ro._memory_parts(_agent)
            _rendered = "\n".join(_first)
            _unchanged = ((_mdir / "USER.md").read_bytes() == _user
                          and (_mdir / "MEMORY.md").read_bytes() == _note)
            _ro_results = [json.loads(_mt_ro.memory_tool(action=_op, target="user", content="NO", store=object()))
                           for _op in ("add", "replace", "remove", "batch")]
            _denied = all(_r.get("success") is False and "read-only" in _r.get("error", "")
                          for _r in _ro_results)
            _unchanged = (_unchanged and (_mdir / "USER.md").read_bytes() == _user
                          and (_mdir / "MEMORY.md").read_bytes() == _note)
            (_mdir / "USER.md").write_bytes(b"human edit")
            _frozen = _sp_ro._memory_parts(_agent) == _first
            _fresh = "human edit" in "\n".join(_sp_ro._memory_parts(_new_agent()))
            _names = model_tools._select_tool_names(enabled_toolsets=["memory"], disabled_toolsets=None, quiet_mode=True)
            _hidden = "memory" not in _names
            for _bridge in (False, True):
                _defs_ro = model_tools.get_tool_definitions(enabled_toolsets=["memory"], quiet_mode=True,
                                                           skip_tool_search_assembly=_bridge)
                _hidden = _hidden and all(_d["function"]["name"] != "memory" for _d in _defs_ro)
            report["memory_read_only_ok"] = bool(
                _user.decode("utf-8") in _rendered and _note.decode("utf-8") in _rendered
                and "read-only" in _rendered and _denied and _unchanged and _frozen and _fresh and _hidden)
    except Exception as _e:
        report["memory_read_only_error"] = str(_e)[:200]

# ``compact_tool_search_schemas``: the bridge/CLI path (skip_tool_search_assembly=True)
# feeds tool_describe/tool_call replies, the failed-call schema dump, `hermes tools` and
# the desktop tool inspector. Probe it on a configured tool so "on but not applied" fails.
report["bridge_compact_ok"] = True
if cfg.get("compact_tool_search_schemas"):
    report["bridge_compact_ok"] = False
    _want = cfg.get("tool_descriptions") or {}
    for _t in (model_tools.get_tool_definitions(skip_tool_search_assembly=True) or []):
        _fn = (_t or {}).get("function") or {}
        _w = _want.get(_fn.get("name"))
        if isinstance(_w, str) and (_fn.get("description") or "").startswith(_w):
            report["bridge_compact_ok"] = True
            break

# Report configured names/parameters that do not exist upstream: such an override is a
# silent no-op, so it must fail verification instead of passing quietly.
# Names come from the FULL registry (the session-gated selection hides tools whose
# check_fn is false right now); parameter shapes come from the ungated definitions.
report["unknown_tools"] = []
report["unknown_parameters"] = []
try:
    _all_names = set()
    try:
        _all_names = set(model_tools.get_all_tool_names())
    except Exception:
        pass
    _defs = []
    for _kw in ({}, {"skip_tool_search_assembly": True}):
        try:
            _defs.extend(model_tools.get_tool_definitions(**_kw))
        except Exception:
            pass
    _props = {}
    for _t in _defs or []:
        _fn = (_t or {}).get("function") or {}
        _nm = _fn.get("name")
        _keys = (((_fn.get("parameters") or {}).get("properties")) or {}).keys()
        _props.setdefault(_nm, set()).update(_keys)
        _all_names.add(_nm)
    if not _all_names:
        report["unknown_tools"].append("could not enumerate live tools")
    for _name in (cfg.get("tool_descriptions") or {}):
        if _name not in _all_names:
            report["unknown_tools"].append(_name)
    for _name, _params in (cfg.get("parameter_descriptions") or {}).items():
        if _name not in _all_names:
            report["unknown_parameters"].append(_name + ".*")
        elif _name in _props:            # parameter shape known -> check each key
            for _p in _params:
                if _p not in _props[_name]:
                    report["unknown_parameters"].append(f"{_name}.{_p}")
except Exception as _e:
    report["unknown_tools"].append("could not enumerate live tools: %s" % _e)

import agent.prompt_builder as pb
for key, want in (cfg.get("prompt_strings") or {}).items():
    if key.startswith("PLATFORM_HINTS__"):
        got = (getattr(pb, "PLATFORM_HINTS", {}) or {}).get(key[len("PLATFORM_HINTS__"):])
    else:
        got = getattr(pb, key, None)
    if got == want:
        report["prompt_ok"] += 1
    else:
        report["prompt_bad"].append(key)

_exc = cfg.get("skill_description_exception_skills") or []
_keep = _exc[0] if _exc else "no-allowlisted-skill"
_sk = {"alpha": [(_keep, "KEEP DESC"), ("zzz-other", "DROP DESC")]}
try:
    _rendered = pb._render_skills_index(_sk, {}, frozenset(), None)
    report["skills_ok"] = ("KEEP DESC" in _rendered) and ("DROP DESC" not in _rendered)
except Exception as _e:
    report["skills_ok"] = False

# (B) read-only preamble must be present in the rendered skill index.
# (C) an allowlisted skill must keep its description even when its CATEGORY is demoted
#     to names-only, while the non-allowlisted sibling stays a bare name.
report["skills_readonly_banner_ok"] = False
report["skills_demote_escape_ok"] = False
try:
    _banner = pb._render_skills_index(_sk, {}, frozenset(), None)
    report["skills_readonly_banner_ok"] = ("read-only" in _banner.lower())
except Exception:
    pass
try:
    _dem = pb._render_skills_index({"alpha": [(_keep, "KEEP DESC"), ("zzz-other", "DROP DESC")]},
                                   {}, frozenset({"alpha"}), None)
    report["skills_demote_escape_ok"] = (
        "KEEP DESC" in _dem and "DROP DESC" not in _dem and "[names only]" in _dem
    )
except Exception as _e:
    report["skills_demote_escape_error"] = str(_e)[:200]

# (C2) a demoted category whose entries are ALL allowlisted has no names-only tail;
#      the category header must still be emitted or the exception lines become orphans
#      (they would visually attach to the previous category).
report["skills_demote_all_exceptions_ok"] = False
try:
    _all = pb._render_skills_index({"beta": [(_keep, "ONLY DESC")]}, {}, frozenset({"beta"}), None)
    _lines_a = [ln for ln in _all.splitlines() if ln.strip()]
    _hdr = [ln for ln in _lines_a if ln.startswith("  beta")]
    _orphan = [ln for ln in _lines_a if ln.startswith("    - ")]
    report["skills_demote_all_exceptions_ok"] = bool(_hdr) and bool(_orphan)
except Exception as _e:
    report["skills_demote_all_exceptions_error"] = str(_e)[:200]

# Skill-index gate (agent/coding_context.py): under "focus" it must demote even
# outside a code workspace; under "focus_and_coding" it must not.
report["gate_ok"] = False
report["gate_is_coding"] = None
try:
    import agent.coding_context as _cc
    import tempfile as _tf, os as _os
    # Use a NON-code workspace so "focus" and "focus_and_coding" actually differ.
    _probe = _tf.mkdtemp(prefix="hc_gate_")
    _rm = _cc.resolve_runtime_mode(platform="desktop", cwd=_probe,
                                   config={"agent": {"coding_context": "focus"}})
    report["gate_is_coding"] = bool(_rm.is_coding)
    _got = _rm.compact_skill_categories()
    _gate = cfg.get("skill_index_gate") or "focus"
    if _gate == "focus_and_coding":
        report["gate_ok"] = len(_got) == 0          # non-code cwd -> must NOT demote
    else:
        report["gate_ok"] = len(_got) > 0           # focus alone -> must demote
except Exception as _e:
    report["gate_ok"] = False

# Prompt-shape rewrites: ambient profile line (agent/system_prompt.py) and the local host
# block (agent/prompt_builder.py). Both are checked on the PRODUCTION path — a constant that
# nothing reads must fail here instead of passing quietly.
_rw = cfg.get("compact_rewrites") or {}
report["rewrites_ok"] = True
report["memory_ok"] = True
report["host_ok"] = True
report["profile_chars"] = 0
try:
    import agent.system_prompt as _sp
    if _rw.get("profile_line"):
        _get_root = getattr(_sp, "get_default_hermes_root", None)
        _want = _rw["profile_line"].format(root=str(_get_root() if callable(_get_root) else ""))
        report["rewrites_ok"] = (_sp._active_profile_line(None) == _want)
        report["profile_chars"] = len(_want)
    if _rw.get("memory_block_compact"):
        _probe = ("═" * 46) + "\nMEMORY (your personal notes) [0% — 1/0 chars]\n" + ("═" * 46) + "\nbody entry"
        _out = _sp._hc_compact_memory_part(_probe)
        _wrapped = getattr(getattr(_sp, "_memory_parts", None), "__name__", "") == "_hc_memory_parts"
        report["memory_ok"] = ("═" not in _out and "MEMORY (your personal notes)" in _out
                               and "body entry" in _out and _wrapped)
except Exception as _e:
    report["rewrites_ok"] = False
    report["memory_ok"] = False
    report["rewrite_error"] = str(_e)[:200]
try:
    import agent.prompt_builder as _pbh
    if _rw.get("host_line_template") and callable(getattr(_pbh, "_hc_orig_local_host_hints", None)):
        _env = _pbh.build_environment_hints()
        report["host_ok"] = ("User home directory:" not in _env) and ("Home:" in _env)
except Exception as _e:
    report["host_ok"] = False
    report["host_error"] = str(_e)[:200]

print("HC_VERIFY " + json.dumps(report))
"""


def verify(root: Path, cfg: dict) -> dict | None:
    """Import the checkout and confirm our overrides are live. None when we cannot check."""
    py = venv_python(root)
    if py is None:
        return None
    # Stage the config in a temp file: embedding it in "-c" overflows the Windows
    # command line (~32k chars) once the config grows past a few hundred tools.
    with tempfile.TemporaryDirectory(prefix="hc-verify-") as tmpd:
        cfg_path = Path(tmpd) / "cfg.json"
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        snippet = (VERIFY_SNIPPET
                   .replace("ROOT", repr(str(root)))
                   .replace("CFG_JSON", f"open({str(cfg_path)!r}, encoding='utf-8').read()"))
        try:
            proc = subprocess.run([str(py), "-c", snippet], cwd=str(root),
                                  capture_output=True, encoding="utf-8",
                                  errors="replace", timeout=180, env=_child_env())
        except (OSError, subprocess.SubprocessError):
            return None
    if proc.returncode != 0:
        return {"_error": (proc.stderr or proc.stdout or f"exit {proc.returncode}")[-800:]}
    _out = proc.stdout or ""
    reports = [line for line in _out.splitlines() if line.startswith("HC_VERIFY ")]
    if len(reports) != 1:
        # A decode/import failure leaves stdout empty; surface the child's own
        # diagnostics instead of a bare count (this masked a cp950/UTF-8 mismatch).
        _tail = ((proc.stderr or "") + "\n" + _out)[-500:].strip()
        return {"_error": f"expected one verification report, got {len(reports)}; tail: {_tail}"}
    try:
        return _loads_strict_json(reports[0][len("HC_VERIFY "):])
    except (json.JSONDecodeError, ValueError) as exc:
        return {"_error": f"invalid verification report: {exc}"}


def _verification_failures(result: dict | None, cfg: dict) -> list[str]:
    if result is None:
        return ["verification could not run"]
    if not isinstance(result, dict):
        return ["verification report is not an object"]
    if "_error" in result:
        return [f"verification import failed: {result['_error']}"]
    required = {
        "tool_ok": int, "tool_bad": list, "prompt_ok": int, "prompt_bad": list,
        "skills_ok": bool, "compute_wrapped": bool, "gate_ok": bool,
        "rewrites_ok": bool, "memory_ok": bool, "host_ok": bool,
    }
    failures = []
    for key, kind in required.items():
        value = result.get(key)
        if key not in result or type(value) is not kind:
            failures.append(f"invalid/missing report field: {key}")
    if failures:
        return failures
    if any(not isinstance(v, str) for v in result["tool_bad"] + result["prompt_bad"]):
        failures.append("invalid bad-item list")
    if result["tool_ok"] < 0 or result["prompt_ok"] < 0:
        failures.append("negative verification count")
    expected_tools = len(cfg.get("tool_descriptions") or {})
    expected_prompts = len(cfg.get("prompt_strings") or {})
    if result["tool_ok"] != expected_tools or result["tool_bad"]:
        failures.append(f"tool overrides ({result['tool_ok']}/{expected_tools}; bad={result['tool_bad']})")
    if result["prompt_ok"] != expected_prompts or result["prompt_bad"]:
        failures.append(f"prompt overrides ({result['prompt_ok']}/{expected_prompts}; bad={result['prompt_bad']})")
    if not result["skills_ok"]:
        failures.append("skill-index renderer")
    if not result.get("skills_readonly_banner_ok"):
        failures.append("skill-index read-only preamble (B) not live")
    if not result.get("skills_demote_escape_ok"):
        failures.append("allowlisted skill cannot escape names-only demotion (C)")
    if not result.get("skills_demote_all_exceptions_ok"):
        failures.append("demoted category with only allowlisted skills loses its header (C2)")
    if not result["compute_wrapped"]:
        failures.append("_compute_tool_definitions wrap")
    if not result["gate_ok"]:
        failures.append("skill-index gate (coding_context)")
    rewrites = cfg.get("compact_rewrites") or {}
    if rewrites.get("profile_line") and not result["rewrites_ok"]:
        failures.append("compact_rewrites.profile_line (not live on the prompt path)")
    if rewrites.get("memory_block_compact") and not result["memory_ok"]:
        failures.append("compact_rewrites.memory_block_compact (not live on the prompt path)")
    if rewrites.get("host_line_template") and not result["host_ok"]:
        failures.append("compact_rewrites.host_line_template (host block unchanged)")
    if cfg.get("compact_tool_search_schemas") and not result.get("bridge_compact_ok"):
        failures.append("compact_tool_search_schemas (bridge/tool_describe path still raw)")
    if cfg.get("skills_read_only") and not result.get("skills_read_only_ok"):
        failures.append(
            "skills_read_only (skills-read toolset missing or not exactly "
            "skills_list+skill_view; resolved=%s)" % (result.get("skills_read_toolset"),)
        )
    if cfg.get("memory_read_only") and result.get("memory_read_only_ok") is not True:
        failures.append("memory_read_only (snapshot injection or write denial not live)")
    stray = list(result.get("unknown_tools") or []) + list(result.get("unknown_parameters") or [])
    if stray:
        failures.append(f"configured but not present upstream: {stray}")
    return failures


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write beside the target, fsync, preserve mode, then atomically replace it."""
    fd, raw = tempfile.mkstemp(prefix=path.name + ".hc-tmp-", dir=str(path.parent))
    tmp = Path(raw)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        shutil.copystat(path, tmp, follow_symlinks=False)
        os.replace(tmp, path)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        tmp.unlink(missing_ok=True)
        raise


def apply_transaction(
    planned: list[tuple[Path, str]], root: Path, cfg: dict,
    *, require_verification: bool = True,
) -> tuple[bool, list[Path]]:
    """Preflight, back up, atomically write, verify, and roll back on failure."""
    originals: dict[Path, bytes] = {}
    for path, new_text in planned:
        ensure_safe_target(root, path)
        try:
            compile(new_text, str(path), "exec")
        except SyntaxError as exc:
            raise SystemExit(f"[x] Generated Python failed syntax preflight for {path}: {exc}") from exc
        originals[path] = path.read_bytes()

    backups: list[Path] = []
    try:
        for path, _ in planned:
            backups.append(backup(path))
        for path, new_text in planned:
            _atomic_write_bytes(path, new_text.encode("utf-8"))

        if require_verification:
            result = verify(root, cfg)
            failures = _verification_failures(result, cfg)
            if failures:
                print(f"[x] Verification failed: {failures}")
                raise RuntimeError("post-write verification failed")
            apply_transaction.last_result = result
        else:
            apply_transaction.last_result = None
        return True, backups
    except Exception as exc:
        rollback_errors = []
        for path, original in originals.items():
            try:
                _atomic_write_bytes(path, original)
            except Exception as rollback_exc:
                rollback_errors.append(f"{path}: {rollback_exc}")
        print(f"[x] Write failed; restored original files: {exc}")
        if rollback_errors:
            print(f"[x] ROLLBACK ERROR: {rollback_errors}")
        return False, backups


apply_transaction.last_result = None


# --------------------------------------------------------------------------- #
# Capture (snapshot live values into a fresh config)
# --------------------------------------------------------------------------- #
CAPTURE_SNIPPET = r"""
import json, sys
sys.path.insert(0, ROOT)
import model_tools as mt
import agent.prompt_builder as pb
out = {
    "tool_descriptions": {},
    "parameter_descriptions": {},
    "prompt_strings": {},
    "skill_description_exception_skills": sorted(getattr(pb, "SKILL_DESCRIPTION_EXCEPTION_SKILLS", [])),
}
# Current Hermes computes tool schemas lazily; capture the effective definitions, not obsolete globals.
defs = []
getter = getattr(mt, "get_all_tool_definitions", None)
if callable(getter):
    defs = getter()
elif callable(getattr(mt, "_compute_tool_definitions", None)):
    defs = mt._compute_tool_definitions()
for tool in defs or []:
    function = tool.get("function", {}) if isinstance(tool, dict) else {}
    name = function.get("name")
    description = function.get("description")
    if isinstance(name, str) and isinstance(description, str):
        out["tool_descriptions"][name] = description
    params = (function.get("parameters") or {}).get("properties", {})
    if isinstance(name, str) and isinstance(params, dict):
        captured = {key: value.get("description") for key, value in params.items()
                    if isinstance(key, str) and isinstance(value, dict)
                    and isinstance(value.get("description"), str)}
        if captured:
            out["parameter_descriptions"][name] = captured
# Compatibility fallback for older installations that expose only compact maps.
if not out["tool_descriptions"]:
    out["tool_descriptions"] = dict(getattr(mt, "_HC_TOOL_DESCRIPTIONS",
                                            getattr(mt, "_COMPACT_TOOL_DESCRIPTIONS", {})))
if not out["parameter_descriptions"]:
    raw_params = getattr(mt, "_HC_PARAMETER_DESCRIPTIONS",
                         getattr(mt, "_COMPACT_PARAMETER_DESCRIPTIONS", {}))
    out["parameter_descriptions"] = {k: dict(v) for k, v in raw_params.items()}
for key in ("HERMES_AGENT_HELP_GUIDANCE", "HERMES_AGENT_HELP_GUIDANCE_NO_SKILLS",
            "MEMORY_GUIDANCE", "USER_PROFILE_GUIDANCE", "SESSION_SEARCH_GUIDANCE",
            "SKILLS_GUIDANCE", "TOOL_USE_ENFORCEMENT_GUIDANCE", "TASK_COMPLETION_GUIDANCE",
            "PARALLEL_TOOL_CALL_GUIDANCE", "OPENAI_MODEL_EXECUTION_GUIDANCE",
            "GOOGLE_MODEL_OPERATIONAL_GUIDANCE", "STEER_CHANNEL_NOTE", "_WINDOWS_BASH_SHELL_HINT"):
    val = getattr(pb, key, None)
    if isinstance(val, str):
        out["prompt_strings"][key] = val
for plat in ("desktop", "cli", "tui", "telegram"):
    val = (getattr(pb, "PLATFORM_HINTS", {}) or {}).get(plat)
    if isinstance(val, str):
        out["prompt_strings"]["PLATFORM_HINTS__" + plat] = val
print("HC_CAPTURE " + json.dumps(out, ensure_ascii=False))
"""


def capture(root: Path, out_path: Path) -> int:
    py = venv_python(root)
    if py is None:
        print("[x] No venv interpreter found under the checkout; cannot capture.")
        return 1
    snippet = CAPTURE_SNIPPET.replace("ROOT", repr(str(root)))
    proc = subprocess.run([str(py), "-c", snippet], cwd=str(root),
                          capture_output=True, encoding="utf-8",
                          errors="replace", timeout=180, env=_child_env())
    if proc.returncode != 0:
        print("[x] Capture subprocess failed:")
        print((proc.stderr or proc.stdout or f"exit {proc.returncode}")[-800:])
        return 1
    _out = proc.stdout or ""
    reports = [line for line in _out.splitlines() if line.startswith("HC_CAPTURE ")]
    if len(reports) != 1:
        _tail = ((proc.stderr or "") + "\n" + _out)[-500:].strip()
        print(f"[x] Capture failed: expected one report, got {len(reports)}; tail: {_tail}")
        return 1
    try:
        data = _loads_strict_json(reports[0][len("HC_CAPTURE "):])
        validate_config(data, source="captured values")
    except (json.JSONDecodeError, ValueError, SystemExit) as exc:
        print(f"[x] Capture returned invalid data: {exc}")
        return 1
    if out_path.suffix.lower() != ".json":
        print(f"[x] Capture output must use a .json extension: {out_path}")
        return 1
    if out_path.exists() and _is_link_or_reparse(out_path):
        print(f"[x] Refusing symlink/reparse-point capture output: {out_path}")
        return 1
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        backup(out_path)
    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    if out_path.exists():
        _atomic_write_bytes(out_path, payload.encode("utf-8"))
    else:
        fd, raw = tempfile.mkstemp(prefix=out_path.name + ".hc-tmp-", dir=str(out_path.parent))
        tmp = Path(raw)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload.encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, out_path)
        finally:
            tmp.unlink(missing_ok=True)
    print(f"[ok] Captured live values -> {out_path}")
    print(f"     tools={len(data['tool_descriptions'])} "
          f"params={len(data['parameter_descriptions'])} "
          f"prompts={len(data['prompt_strings'])}")
    return 0


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def deep_merge(base: dict, extra: dict) -> dict:
    """Recursively merge *extra* onto *base*. Dicts merge; lists union; scalars replace.

    Keys beginning with "_" are comments/metadata and are never merged.
    """
    out = dict(base)
    for key, val in extra.items():
        if isinstance(key, str) and key.startswith("_"):
            continue
        cur = out.get(key)
        if isinstance(cur, dict) and isinstance(val, dict):
            out[key] = deep_merge(cur, val)
        elif isinstance(cur, list) and isinstance(val, list):
            merged = list(cur)
            for item in val:
                if item not in merged:
                    merged.append(item)
            out[key] = merged
        else:
            out[key] = val
    return out



_ALLOWED_CONFIG_KEYS = {
    "tool_descriptions", "parameter_descriptions", "prompt_strings",
    "skill_description_exception_skills", "skill_index_gate", "skill_index_categories",
    "compact_rewrites", "compact_tool_search_schemas", "skills_read_only", "memory_read_only",
}
_ALLOWED_PROMPT_GLOBALS = {
    "HERMES_AGENT_HELP_GUIDANCE", "HERMES_AGENT_HELP_GUIDANCE_NO_SKILLS",
    "MEMORY_GUIDANCE", "USER_PROFILE_GUIDANCE", "SESSION_SEARCH_GUIDANCE",
    "SKILLS_GUIDANCE", "TOOL_USE_ENFORCEMENT_GUIDANCE", "TASK_COMPLETION_GUIDANCE",
    "PARALLEL_TOOL_CALL_GUIDANCE", "OPENAI_MODEL_EXECUTION_GUIDANCE",
    "GOOGLE_MODEL_OPERATIONAL_GUIDANCE", "STEER_CHANNEL_NOTE", "_WINDOWS_BASH_SHELL_HINT",
    "STEER_MARKER_OPEN", "KANBAN_GUIDANCE",
}


def _config_error(source: str, message: str) -> None:
    raise SystemExit(f"[x] Invalid config {source}: {message}")


def validate_config(data: dict, source: str = "<config>") -> None:
    """Validate the complete config schema before generating Python code."""
    if not isinstance(data, dict):
        _config_error(source, "root must be a JSON object")
    unknown = [k for k in data if not (isinstance(k, str) and (k.startswith("_") or k in _ALLOWED_CONFIG_KEYS))]
    if unknown:
        _config_error(source, f"unknown top-level key(s): {unknown}")

    tools = data.get("tool_descriptions", {})
    if not isinstance(tools, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in tools.items()):
        _config_error(source, "tool_descriptions must be an object of string -> string")

    params = data.get("parameter_descriptions", {})
    if not isinstance(params, dict):
        _config_error(source, "parameter_descriptions must be an object")
    for tool, mapping in params.items():
        if not isinstance(tool, str) or not isinstance(mapping, dict):
            _config_error(source, "each parameter_descriptions entry must be an object")
        if any(not isinstance(k, str) or not isinstance(v, str) for k, v in mapping.items()):
            _config_error(source, f"parameter_descriptions[{tool!r}] must be string -> string")

    prompts = data.get("prompt_strings", {})
    if not isinstance(prompts, dict):
        _config_error(source, "prompt_strings must be an object")
    for key, value in prompts.items():
        allowed = isinstance(key, str) and (
            key in _ALLOWED_PROMPT_GLOBALS
            or (key.startswith(PLATFORM_PREFIX) and len(key) > len(PLATFORM_PREFIX))
        )
        if not allowed or not isinstance(value, str):
            _config_error(source, f"unsupported prompt string entry: {key!r}")

    skills = data.get("skill_description_exception_skills", [])
    if not isinstance(skills, list) or any(not isinstance(v, str) for v in skills):
        _config_error(source, "skill_description_exception_skills must be a list of strings")
    gate = data.get("skill_index_gate", "focus")
    if gate not in ("focus", "focus_and_coding"):
        _config_error(source, "skill_index_gate must be 'focus' or 'focus_and_coding'")
    cats = data.get("skill_index_categories")
    if cats is not None and (not isinstance(cats, list) or any(not isinstance(v, str) for v in cats)):
        _config_error(source, "skill_index_categories must be null or a list of strings")
    bridge = data.get("compact_tool_search_schemas")
    if bridge is not None and not isinstance(bridge, bool):
        _config_error(source, "compact_tool_search_schemas must be true or false")
    read_only = data.get("skills_read_only")
    if read_only is not None and not isinstance(read_only, bool):
        _config_error(source, "skills_read_only must be true or false")

    memory_read_only = data.get("memory_read_only")
    if "memory_read_only" in data and type(memory_read_only) is not bool:
        _config_error(source, "memory_read_only must be true or false")

    rewrites = data.get("compact_rewrites")
    if rewrites is not None:
        if not isinstance(rewrites, dict):
            _config_error(source, "compact_rewrites must be an object")
        unknown_rewrites = [k for k in rewrites
                            if not (isinstance(k, str) and (k.startswith("_") or k in _REWRITE_KEYS))]
        if unknown_rewrites:
            _config_error(source, f"unknown compact_rewrites key(s): {unknown_rewrites}")
        for key in ("host_line_template", "host_note", "profile_line"):
            value = rewrites.get(key)
            if value is not None and not isinstance(value, str):
                _config_error(source, f"compact_rewrites.{key} must be a string or null")
        template = rewrites.get("profile_line")
        if isinstance(template, str) and "{root}" not in template:
            _config_error(source, "compact_rewrites.profile_line must contain the {root} placeholder")
        compact = rewrites.get("memory_block_compact")
        if compact is not None and not isinstance(compact, bool):
            _config_error(source, "compact_rewrites.memory_block_compact must be true/false")


def _reject_json_constant(value: str):
    raise ValueError(f"non-finite JSON constant {value!r} is not allowed")


def _reject_duplicate_object_pairs(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate JSON member {key!r}")
        out[key] = value
    return out


def _loads_strict_json(text: str):
    return json.loads(text, parse_constant=_reject_json_constant,
                      object_pairs_hook=_reject_duplicate_object_pairs)

def discover_config_paths(explicit: list[str] | None, extra: list[str] | None, here: Path) -> list[Path]:
    """Ordered config files: base (--config or default), then overrides.d/*.json, then --add-config."""
    paths: list[Path] = []
    if explicit:
        paths.extend(Path(p).expanduser() for p in explicit)
    else:
        base = here / "overrides" / "compact-overrides.json"
        if not base.is_file():
            # Fresh clone: no local config yet. Fall back to the shipped example
            # so the tool runs out of the box (the user can copy + edit it later).
            example = here / "compact-overrides.example.json"
            if example.is_file():
                print(f"[i] No local base config; using shipped example: {example.name}")
                print("    Copy it to overrides/compact-overrides.json to customise.")
                base = example
        paths.append(base)
    dropin = here / "overrides.d"
    if dropin.is_dir():
        paths.extend(sorted(p for p in dropin.glob("*.json") if not p.name.endswith(".example.json")))
    if extra:
        paths.extend(Path(p).expanduser() for p in extra)
    return paths


def load_configs(paths: list[Path]) -> tuple[dict, list[Path]]:
    """Merge every existing config file; the first path is required, later ones are optional."""
    if not paths:
        raise SystemExit("[x] No config paths resolved.")
    merged: dict = {}
    used: list[Path] = []
    for index, path in enumerate(paths):
        if not path.is_file():
            if index == 0:
                raise SystemExit(
                    f"[x] Config not found: {path}\n"
                    "    Copy compact-overrides.example.json to overrides/compact-overrides.json.")
            print(f"[warn] Config not found (skipped): {path}")
            continue
        try:
            data = _loads_strict_json(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError) as e:
            raise SystemExit(f"[x] Config is not valid strict JSON: {path}: {e}")
        validate_config(data, source=str(path))
        merged = deep_merge(merged, data)
        used.append(path)
    validate_config(merged, source="merged config")
    return merged, used


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Re-apply compact Hermes tool & prompt descriptions.")
    ap.add_argument("--hermes-root", default=None, help="Path to .hermes or the hermes-agent checkout.")
    ap.add_argument("--config", action="append", default=None,
                    help="Base config (repeatable; default overrides/compact-overrides.json).")
    ap.add_argument("--add-config", action="append", default=None,
                    help="Extra private config layered on top (repeatable).")
    ap.add_argument("--check", action="store_true", help="Report changes; write nothing.")
    ap.add_argument("--restore", action="store_true", help="Remove the managed blocks.")
    ap.add_argument("--capture", default=None, metavar="OUT.json",
                    help="Snapshot live values from the checkout into OUT.json and exit.")
    ap.add_argument("--yes", action="store_true", help="Skip the confirmation prompt.")
    args = ap.parse_args(argv)

    here = Path(__file__).resolve().parent
    cfg_paths = discover_config_paths(args.config, args.add_config, here)

    root = resolve_hermes_root(args.hermes_root)

    if args.capture:
        return capture(root, Path(args.capture))

    targets = {
        root / MODEL_TOOLS_REL: build_model_tools_block,
        root / PROMPT_BUILDER_REL: build_prompt_builder_block,
        root / CODING_CONTEXT_REL: build_coding_context_block,
        root / SYSTEM_PROMPT_REL: build_system_prompt_block,
    }
    for path in targets:
        ensure_safe_target(root, path)

    if args.restore:
        planned_restore: list[tuple[Path, str]] = []
        for path in targets:
            text = read_text(path)
            stripped, found = strip_block(text)
            if found:
                planned_restore.append((path, stripped))
                print(f"[{'check' if args.check else 'change'}] remove managed block: {path}")
            else:
                print(f"[skip] no managed block: {path}")
        if args.check or not planned_restore:
            return 0
        ok, backups = apply_transaction(planned_restore, root, {}, require_verification=False)
        if not ok:
            return 1
        for path, bak in zip((p for p, _ in planned_restore), backups):
            print(f"[ok] restored upstream source: {path}\n     backup: {bak.name}")
        return 0

    cfg, used = load_configs(cfg_paths)
    print(f"[i] Hermes checkout : {root}")
    print(f"[i] Configs ({len(used)}, merged in order):")
    for _p in used:
        print(f"      - {_p}")
    print(f"[i] Interpreter     : {venv_python(root) or '(none found)'}")
    print()

    planned: list[tuple[Path, str]] = []
    for path, builder in targets.items():
        old_text = read_text(path)
        new_text = upsert_block(old_text, builder(cfg))
        planned.append((path, new_text))
        print(f"[{'change' if new_text != old_text else 'same  '}] {path}")

    if args.check:
        print("\n[check] Dry run only — nothing written.")
        return 0

    if not args.yes:
        try:
            if input("\nApply these changes? [y/N] ").strip().lower() not in ("y", "yes"):
                print("[abort] Nothing written.")
                return 0
        except EOFError:
            print("[abort] No input; nothing written. Re-run with --yes to apply.")
            return 0

    print("\n[i] Writing atomically, then verifying in the trusted checkout interpreter...")
    success, backups = apply_transaction(planned, root, cfg)
    for (path, _), bak in zip(planned, backups):
        print(f"[backup] {path}\n         {bak.name}")
    if not success:
        print("[x] Apply failed; original source files were restored.")
        return 1

    result = apply_transaction.last_result or {}
    ok_count = result.get("tool_ok", 0) + result.get("prompt_ok", 0)
    extras = "+ lean skill index + focus gate"
    if cfg.get("memory_read_only"):
        extras += " + read-only memory (snapshot + write denial)"
    _rewrites = cfg.get("compact_rewrites") or {}
    _live = [label for label, key in (("profile line", "profile_line"), ("memory framing", "memory_block_compact"),
                                      ("host block", "host_line_template")) if _rewrites.get(key)]
    if _live:
        extras += " + " + ", ".join(_live)
    print(f"[ok] Verified: {ok_count} override(s) live "
          f"({result.get('tool_ok', 0)} tool, {result.get('prompt_ok', 0)} prompt) {extras}.")
    print("\nDone. Restart Hermes completely (fully quit + relaunch) so the new process")
    print("      loads the updated descriptions. A new chat in the same process will not.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(130)
