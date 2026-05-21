# Klipper Spool Tracker

Tracks real filament consumption per spool by connecting to a Moonraker (Klipper) WebSocket.
Serves data via HTTP GET on port 8200 for Odoo to consume (pull model).

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate  # Windows
# source .venv/bin/activate  # Linux
pip install -r requirements.txt
```

## Usage

```bash
python tracker.py                              # start daemon
python query.py spool_usage.db                 # query all usage
python query.py spool_usage.db --job 0004E2    # filter by job
python query.py spool_usage.db --spool 1       # filter by spool
python query.py --tracker                      # query tracker HTTP instead of local DB
```

## Deployment (Raspberry Pi / Linux)

Clone to `/home/pi/klipper_spool_tracker`:

```bash
cd /home/pi
git clone <repo-url> klipper_spool_tracker
cd klipper_spool_tracker
```

Option A — automatic (recommended):

```bash
chmod +x install.sh && ./install.sh
```

Option B — manual:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
sudo cp spool-tracker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable spool-tracker
```

Edit `config.json` to point to your Moonraker WebSocket before starting. See `config.example.json` for the default structure.

Add `moonraker-example.cfg` to your `moonraker.conf` to enable Spoolman integration and automatic updates.

## Config

Edit `config.json` (or copy `config.example.json`):

| Variable         | Description                  |
|------------------|------------------------------|
| `MOONRAKER_URL`  | WebSocket URL (default `ws://localhost:7125/websocket`) |
| `DB_PATH`        | SQLite database path         |
| `HTTP_HOST`      | HTTP server bind address     |
| `HTTP_PORT`      | HTTP server port             |

The DB auto-prunes to the last 100 distinct jobs to keep the file small. Logs go to `/var/log/spool-tracker.log` (rotated daily via `spool-tracker.logrotate`).

## HTTP Endpoints

- `GET /spool_usage` — returns JSON array of spool usage records
- `GET /spool_usage?job_id=0004E2` — filter by job
- `GET /spool_usage?spool_id=1` — filter by spool
- `GET /health` — health check

## Database

SQLite with WAL mode. Single table `spool_usage` (`job_id` TEXT, `spool_id` INT, `filament_mm` REAL). Schema auto-created on first start.
