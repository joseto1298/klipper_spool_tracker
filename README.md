# Klipper Spool Tracker

Tracks real filament consumption per spool by connecting to a Moonraker (Klipper) WebSocket.
Serves data via HTTP GET on port 8200 for Odoo to consume (pull model).

## Setup (desarrollo)

```bash
python -m venv .venv
.venv\Scripts\activate  # Windows
# source .venv/bin/activate  # Linux
pip install -r requirements.txt
```

## Usage

```bash
python tracker.py                              # inicia daemon (HTTP en :8200)
python query.py spool_usage.db                 # consulta toda la DB local
python query.py spool_usage.db --job 0004E2    # filtrar por trabajo
python query.py spool_usage.db --spool 1       # filtrar por bobina
python query.py --tracker                      # consultar via HTTP API del daemon
```

## Deployment (Raspberry Pi / Linux)

```bash
cd ~
git clone <repo-url> klipper_spool_tracker
cd klipper_spool_tracker
```

### Opcion A — automatica (recomendada)

```bash
chmod +x install.sh && ./install.sh
```

Esto hace todo automaticamente:
1. Crea `config.json` desde `config.example.json` (si no existe)
2. Crea `.venv`
3. Instala dependencias (`pip install`)
4. Instala, habilita **y arranca** el systemd service (`enable --now`)
5. Agrega el snippet de Moonraker a `moonraker.conf`
6. Instala logrotate para `/var/log/spool-tracker.log`

### Opcion B — manual

```bash
# 1. Config
cp config.example.json config.json

# 2. Entorno virtual
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 3. Systemd service — instala, habilita para boot y arranca
sudo cp klipper_spool_tracker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now klipper_spool_tracker.service

# 4. Logrotate
sudo cp klipper_spool_tracker.logrotate /etc/logrotate.d/klipper_spool_tracker

# 5. Moonraker — copia el snippet a tu moonraker.conf
cat moonraker-example.cfg >> ~/printer_data/config/moonraker.conf
```

### Post-instalacion

1. **Edita `config.json`** — pon la IP real de tu Moonraker (`moonraker_url`) y Spoolman si aplica.  
   `config.json` esta en `.gitignore` asi que `git pull` nunca lo sobrescribe.
2. **Edita `moonraker.conf`** — revisa la URL `origin` del repo.
3. **Si cambiaste config, reinicia:**
   ```bash
   sudo systemctl restart klipper_spool_tracker
   sudo journalctl -u klipper_spool_tracker -f
   ```

## Config

Edita `config.json` (se crea desde `config.example.json` si no existe):

| Variable         | Descripcion                     | Default                        |
|------------------|---------------------------------|--------------------------------|
| `MOONRAKER_URL`  | WebSocket de Moonraker          | `ws://localhost:7125/websocket`|
| `DB_PATH`        | Ruta a la DB SQLite             | `spool_usage.db`               |
| `HTTP_HOST`      | Bind address del servidor HTTP  | `0.0.0.0`                      |
| `HTTP_PORT`      | Puerto del servidor HTTP        | `8200`                         |

Las variables de entorno tienen prioridad sobre `config.json`.

La DB se poda automaticamente a los ultimos 100 jobs distintos.  
Los logs van a `/var/log/spool-tracker.log` (rotacion diaria via `spool-tracker.logrotate`) y a journald (stderr).

## HTTP Endpoints

- `GET /health` — health check (`{"status": "ok"}`)
- `GET /spool_usage` — todos los registros
- `GET /spool_usage?job_id=0004E2` — filtrar por job
- `GET /spool_usage?spool_id=1` — filtrar por bobina

## Database

SQLite con WAL mode. Una sola tabla:

```sql
spool_usage (id INTEGER PK, job_id TEXT, spool_id INTEGER, filament_mm REAL)
```

El schema se crea automaticamente en el primer arranque.
