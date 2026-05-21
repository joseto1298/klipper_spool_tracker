#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_NAME="spool-tracker"
SERVICE_FILE="$REPO_DIR/$SERVICE_NAME.service"

echo "==> Klipper Spool Tracker — Instalacion"
echo ""

# 1. Crear .venv e instalar dependencias
if [ ! -d "$REPO_DIR/.venv" ]; then
    echo "[1/4] Creando entorno virtual..."
    python3 -m venv "$REPO_DIR/.venv"
fi

echo "[2/4] Instalando dependencias..."
"$REPO_DIR/.venv/bin/pip" install -r "$REPO_DIR/requirements.txt" --quiet

# 2. Copiar systemd service
echo "[3/4] Instalando systemd service..."
sudo cp "$SERVICE_FILE" "/etc/systemd/system/$SERVICE_NAME.service"
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"

# 3. Resumen final
echo "[4/4] Hecho."
echo ""
echo "=== Proximos pasos ==="
echo "  1. Edita config.json con tu Moonraker WS y Spoolman"
echo "  2. sudo systemctl start $SERVICE_NAME"
echo "  3. sudo journalctl -u $SERVICE_NAME -f  (para ver logs)"
echo "  4. Anade moonraker-example.cfg a tu moonraker.conf"
echo "  5. La API HTTP estara en http://$(hostname -I | awk '{print $1}'):8200/spool_usage"
