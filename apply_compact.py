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
    return "\n".join([
        START_MARKER,
        "# Compact tool/parameter descriptions, re-applied by hermes-lean/apply_compact.py.",
        "# Self-contained: embeds the compactor and wraps _compute_tool_definitions.",
        "# Regenerated from compact-overrides.json; edit the JSON, not this block.",
        "import copy",
        "import re",
        "",
        f"_HC_TOOL_DESCRIPTIONS = {tool_desc!r}",
        "",
        f"_HC_PARAMETER_DESCRIPTIONS = {param_desc!r}",
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
        '        _skip = _kw.get("skip_tool_search_assembly", _a[3] if len(_a) > 3 else False)',
        "        if _skip:",
        "            return _raw",
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
    return "\n".join([
        START_MARKER,
        "# Compact system-prompt strings + lean skill index, re-applied by hermes-lean/apply_compact.py.",
        "# Self-contained: does not depend on any other local edit.",
        "# Regenerated from compact-overrides.json; edit the JSON, not this block.",
        f"_HC_PROMPT_STRINGS = {globals_!r}",
        f"_HC_PLATFORM_HINTS = {platform!r}",
        f"_HC_SKILL_EXCEPTIONS = frozenset({skills!r})",
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
        '            _lines.append("  %s [names only]: %s" % (_cat, ", ".join(sorted({_n for _n, _ in _entries}))))',
        "            _names_only = True",
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
        '            "Load relevant skills with skill_view(name) before acting; use skills_list for discovery.\\n\\n"',
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

print("HC_VERIFY " + json.dumps(report))
"""


def verify(root: Path, cfg: dict) -> dict | None:
    """Import the checkout and confirm our overrides are live. None when we cannot check."""
    py = venv_python(root)
    if py is None:
        return None
    snippet = (VERIFY_SNIPPET
               .replace("ROOT", repr(str(root)))
               .replace("CFG_JSON", repr(json.dumps(cfg, ensure_ascii=False))))
    try:
        proc = subprocess.run([str(py), "-c", snippet], cwd=str(root),
                              capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return {"_error": (proc.stderr or proc.stdout or f"exit {proc.returncode}")[-800:]}
    reports = [line for line in proc.stdout.splitlines() if line.startswith("HC_VERIFY ")]
    if len(reports) != 1:
        return {"_error": f"expected one verification report, got {len(reports)}"}
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
    if not result["compute_wrapped"]:
        failures.append("_compute_tool_definitions wrap")
    if not result["gate_ok"]:
        failures.append("skill-index gate (coding_context)")
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
                          capture_output=True, text=True, timeout=180)
    if proc.returncode != 0:
        print("[x] Capture subprocess failed:")
        print((proc.stderr or proc.stdout or f"exit {proc.returncode}")[-800:])
        return 1
    reports = [line for line in proc.stdout.splitlines() if line.startswith("HC_CAPTURE ")]
    if len(reports) != 1:
        print(f"[x] Capture failed: expected one report, got {len(reports)}")
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
}
_ALLOWED_PROMPT_GLOBALS = {
    "HERMES_AGENT_HELP_GUIDANCE", "HERMES_AGENT_HELP_GUIDANCE_NO_SKILLS",
    "MEMORY_GUIDANCE", "USER_PROFILE_GUIDANCE", "SESSION_SEARCH_GUIDANCE",
    "SKILLS_GUIDANCE", "TOOL_USE_ENFORCEMENT_GUIDANCE", "TASK_COMPLETION_GUIDANCE",
    "PARALLEL_TOOL_CALL_GUIDANCE", "OPENAI_MODEL_EXECUTION_GUIDANCE",
    "GOOGLE_MODEL_OPERATIONAL_GUIDANCE", "STEER_CHANNEL_NOTE", "_WINDOWS_BASH_SHELL_HINT",
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
    print(f"[ok] Verified: {ok_count} override(s) live "
          f"({result.get('tool_ok', 0)} tool, {result.get('prompt_ok', 0)} prompt) "
          f"+ lean skill index + focus gate.")
    print("\nDone. Restart Hermes completely (fully quit + relaunch) so the new process")
    print("      loads the updated descriptions. A new chat in the same process will not.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(130)
