#!/usr/bin/env bash
# One-click re-apply of compact Hermes tool & prompt descriptions (macOS/Linux/Git-Bash).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Prefer a trusted system interpreter. Standard Hermes installs are fallback only.
PY=""
for cand in \
  "$(command -v python3 || true)" \
  "$(command -v python || true)" \
  "$HOME/.hermes/hermes-agent/venv/bin/python" \
  "$HOME/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe"; do
  [ -n "$cand" ] && [ -x "$cand" ] && { PY="$cand"; break; }
done
[ -n "$PY" ] || { echo "No Python interpreter found. Install Hermes, or Python 3.10+." >&2; exit 1; }

# A native-Windows interpreter cannot read MSYS paths like /c/Users/...; convert.
SCRIPT="$HERE/apply_compact.py"
case "$SCRIPT" in
  /[A-Za-z]/*) if command -v cygpath >/dev/null 2>&1; then SCRIPT="$(cygpath -w "$SCRIPT")"; fi ;;
esac

exec "$PY" "$SCRIPT" --yes "$@"
