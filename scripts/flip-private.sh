#!/usr/bin/env bash
# Flip windows-toolkit GitHub visibility to private.
set -euo pipefail
LOG="${XDG_STATE_HOME:-$HOME/.local/state}/windows-toolkit/flip-private.log"
mkdir -p "$(dirname "$LOG")"
{
  echo "=== $(date -Is) flip start ==="
  gh repo edit ad3lre/windows-toolkit --visibility private --accept-visibility-change-consequences
  gh repo view ad3lre/windows-toolkit --json isPrivate,url
  echo "=== $(date -Is) flip done ==="
} >>"$LOG" 2>&1
