#!/usr/bin/env bash
# WA Radar installer for macOS. Everything goes into one folder (default ~/WaRadar).
# Nothing is installed inside WhatsApp, and WhatsApp's own files are never changed.
set -euo pipefail

HOME_DIR="${WA_RADAR_HOME:-$HOME/WaRadar}"
SKILL="$(cd "$(dirname "$0")/.." && pwd)"
export PATH="$HOME/.local/bin:$PATH"

echo "[1/4] Python manager (uv)"
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi

echo "[2/4] Folder: $HOME_DIR"
mkdir -p "$HOME_DIR/packs" "$HOME_DIR/reports" "$HOME_DIR/imports" "$HOME_DIR/voice"

echo "[3/4] Python"
if [ ! -x "$HOME_DIR/.venv/bin/python" ]; then
  uv venv --python 3.11 "$HOME_DIR/.venv"
fi

cat > "$HOME_DIR/wr" <<WRAP
#!/usr/bin/env bash
export WA_RADAR_HOME="$HOME_DIR"
exec "$HOME_DIR/.venv/bin/python" "$SKILL/scripts/wr.py" "\$@"
WRAP
chmod +x "$HOME_DIR/wr"

echo "[4/4] Check"
"$HOME_DIR/wr" check
