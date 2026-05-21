#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_NAME="spool-tracker"
SERVICE_FILE="$REPO_DIR/$SERVICE_NAME.service"

echo "==> Klipper Spool Tracker — Instalacion"
echo ""

# 1. Config
if [ ! -f "$REPO_DIR/config.json" ]; then
    echo "[1/7] Creando config.json desde config.example.json..."
    cp "$REPO_DIR/config.example.json" "$REPO_DIR/config.json"
else
    echo "[1/7] config.json ya existe — omitiendo"
fi

# 2. Crear .venv e instalar dependencias
if [ ! -d "$REPO_DIR/.venv" ]; then
    echo "[2/7] Creando entorno virtual..."
    python3 -m venv "$REPO_DIR/.venv"
fi

echo "[3/7] Instalando dependencias..."
"$REPO_DIR/.venv/bin/pip" install -r "$REPO_DIR/requirements.txt" --quiet

# 3. Copiar systemd service
echo "[4/7] Instalando systemd service..."
sudo cp "$SERVICE_FILE" "/etc/systemd/system/$SERVICE_NAME.service"
sudo systemctl daemon-reload
sudo systemctl enable --now "$SERVICE_NAME"

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
    if grep -q "\[update_manager klipper_spool_tracker\]" "$MOONRAKER_CONF" 2>/dev/null; then
        echo "[5/7] moonraker.conf ya configurado — omitiendo"
    else
        echo "[5/7] Anadiendo snippet a $MOONRAKER_CONF..."
        {
            echo ""
            echo "# Klipper Spool Tracker — anadido por install.sh"
            cat "$REPO_DIR/moonraker-example.cfg"
        } >> "$MOONRAKER_CONF"
        echo "  IMPORTANTE: Revisa y edita origin URL en moonraker.conf antes de reiniciar"
    fi
else
    echo "[5/7] moonraker.conf no encontrado — anade moonraker-example.cfg manualmente"
fi

# 5. Logrotate
echo "[6/7] Instalando logrotate..."
sudo cp "$REPO_DIR/$SERVICE_NAME.logrotate" "/etc/logrotate.d/$SERVICE_NAME"

# 6. Resumen final
echo "[7/7] Hecho."
echo ""
echo "=== Proximos pasos ==="
echo "  1. Revisa config.json (sobre todo moonraker_url)"
echo "  2. Si cambiaste algo: sudo systemctl restart $SERVICE_NAME"
echo "  3. sudo journalctl -u $SERVICE_NAME -f  (para ver logs)"
echo "  4. La API HTTP estara en http://$(hostname -I | awk '{print $1}'):8200/spool_usage"
