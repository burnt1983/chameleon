#!/usr/bin/env bash
set -euo pipefail
rm -f \
  "${HOME}/.local/bin/chameleon" \
  "${HOME}/.local/share/applications/chameleon.desktop" \
  "${HOME}/.cache/chameleon.pid"
rm -rf "${HOME}/.local/share/chameleon"
echo "Removed the chameleon."
