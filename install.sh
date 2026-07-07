#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_NAME="klipper_spool_tracker"
SERVICE_FILE="$REPO_DIR/$SERVICE_NAME.service"

echo "==> Klipper Spool Tracker — Install"
echo ""

# 1. Config
if [ ! -f "$REPO_DIR/config.json" ]; then
    echo "[1/7] Creating config.json from config.example.json..."
    cp "$REPO_DIR/config.example.json" "$REPO_DIR/config.json"
else
    echo "[1/7] config.json already exists — skipping"
fi

# 2. Create .venv and install dependencies
if [ ! -d "$REPO_DIR/.venv" ]; then
    echo "[2/7] Creating virtual environment..."
    python3 -m venv "$REPO_DIR/.venv"
fi

echo "[3/7] Installing dependencies..."
"$REPO_DIR/.venv/bin/pip" install -r "$REPO_DIR/requirements.txt" --quiet

# 3. Copy systemd service
echo "[4/7] Installing systemd service..."
sudo cp "$SERVICE_FILE" "/etc/systemd/system/$SERVICE_NAME.service"
sudo systemctl daemon-reload
sudo systemctl enable --now "$SERVICE_NAME.service"

# 4. Moonraker config snippet
ORIGINAL_USER="${SUDO_USER:-$USER}"
ORIGINAL_HOME="$(eval echo "~$ORIGINAL_USER")"
MOONRAKER_CONF=""
for candidate in \
    "$ORIGINAL_HOME/printer_data/config/moonraker.conf" \
    "$ORIGINAL_HOME/klipper_config/moonraker.conf"; do
    if [ -f "$candidate" ]; then
        MOONRAKER_CONF="$candidate"
        break
    fi
done

if [ -n "$MOONRAKER_CONF" ]; then
    echo "[5/7] Configuring update_manager in moonraker.conf..."
    python3 - "$MOONRAKER_CONF" "$REPO_DIR/moonraker-example.cfg" << 'EOF'
import re, sys
conf_path, snippet_path = sys.argv[1], sys.argv[2]
with open(conf_path) as f:
    content = f.read()
with open(snippet_path) as f:
    snippet = f.read()
pat = r'\[update_manager klipper_spool_tracker\].*?(?=\n\[|\Z)'
if re.search(pat, content, flags=re.DOTALL):
    content = re.sub(pat, snippet.strip(), content, flags=re.DOTALL)
    print("  [update_manager] updated")
else:
    content += "\n\n# Klipper Spool Tracker — added by install.sh\n" + snippet
    print("  [update_manager] added")
with open(conf_path, 'w') as f:
    f.write(content)
EOF
    echo "  IMPORTANT: Check origin URL in moonraker.conf"
else
    echo "[5/7] moonraker.conf not found — add moonraker-example.cfg manually"
fi

# 5. Logrotate
echo "[6/7] Installing logrotate..."
sudo cp "$REPO_DIR/$SERVICE_NAME.logrotate" "/etc/logrotate.d/$SERVICE_NAME"

# 6. Done
echo "[7/7] Done."
echo ""
echo "=== Next steps ==="
echo "  1. Review config.json (especially moonraker_url)"
echo "  2. If you changed anything: sudo systemctl restart $SERVICE_NAME"
echo "  3. sudo journalctl -u $SERVICE_NAME -f  (to watch logs)"
echo "  4. HTTP API will be at http://$(hostname -I | awk '{print $1}'):8200/spool_usage"
