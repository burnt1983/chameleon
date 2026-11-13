#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
SHARE="${HOME}/.local/share/chameleon"
BIN="${HOME}/.local/bin"
APPS="${HOME}/.local/share/applications"
mkdir -p "$SHARE" "$BIN" "$APPS"
python3 - "$ROOT" "$SHARE" <<'PY'
import base64
import sys
from pathlib import Path

root = Path(sys.argv[1])
share = Path(sys.argv[2])
for part in sorted((root / "assets").glob("*.png.b64")):
    name = part.name[:-4]
    (share / name).write_bytes(base64.b64decode(part.read_text()))
(share / "layout.json").write_bytes((root / "assets" / "layout.json").read_bytes())
PY
install -m 0755 "$ROOT/chameleon.py" "$SHARE/chameleon.py"
ln -sfn "$SHARE/chameleon.py" "$BIN/chameleon"
cat > "$APPS/chameleon.desktop" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=Chameleon
Comment=Sits on top of a window or widget and changes colour to match it.
Exec=${BIN}/chameleon
Icon=${SHARE}/icon.png
Terminal=false
Categories=Utility;
StartupNotify=false
StartupWMClass=chameleon
Keywords=chameleon;lizard;window;widget;desktop;
EOF
echo "Installed."
echo "  Run: chameleon"
echo "  It does not start at login."
