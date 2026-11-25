#!/usr/bin/env bash
# Pack Windows agent files for copy to a Windows box (USB / scp / RDP).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-$ROOT/dist/wintoolkit-agent.tar.gz}"
mkdir -p "$(dirname "$OUT")"
rm -f "$OUT"
tar -C "$ROOT/agent" -czf "$OUT" \
  WinToolkitAgent.ps1 \
  Install-WinToolkit.ps1 \
  Uninstall-WinToolkit.ps1 \
  agent.config.example.json
echo "packed $OUT"
