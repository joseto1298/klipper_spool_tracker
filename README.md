# Klipper Spool Tracker

Tracks real filament consumption per spool by connecting to a Moonraker (Klipper) WebSocket.
Serves data via HTTP GET on port 8200 for external systems to consume (pull model).

## How it works

```
┌─────────────┐    WebSocket     ┌────────────────┐    SQLite    ┌──────────┐
│  Moonraker   │ ──────────────→ │  tracker.py    │ ──────────→ │  .db     │
│  (Klipper)   │                 │  (daemon)      │             │          │
└─────────────┘                  └────────┬───────┘             └──────────┘
                                          │ HTTP :8200
                                          ▼
                                   ┌──────────────┐
                                   │  query.py     │
                                   │  (CLI o API)  │
                                   └──────────────┘
```

### Daemon (`tracker.py`)

Runs as a systemd service and does 3 things simultaneously:

1. **Moonraker WebSocket client** — subscribes to `toolhead.position` (E-axis updates every ~250ms) and listens for job/spool events.
2. **Filament calculator** — each E-axis update computes `delta = E_now - E_prev`; if `delta > 0.01mm` it's real extrusion (sub-micron noise is ignored). Accumulated mm per spool is tracked in memory.
3. **HTTP server** — serves `GET /spool_usage` and `GET /health` on port 8200.

### Job lifecycle

```
1. notify_history_changed(action="added")
   → saves job_id, retries spool_id from Spoolman (7 attempts, backoff 2→10s)

2. notify_status_update (every ~250ms)
   → delta E → accumulate mm per active spool

3. Every 30s
   → checkpoint to SQLite (power-loss safety)

4. notify_active_spool_set (spool change)
   → flush previous spool data, switch to new spool

5. notify_history_changed(action="finished")
   → final flush + prune old jobs (>100)
```

### Crash safety

- **Checkpoints every 30s** — max data loss on power failure is 30 seconds.
- **UPSERT** — if the daemon restarts mid-job, the accumulated value replaces the old row.
- **Auto-reconnect** — exponential backoff (1s→60s) on Moonraker connection loss.
- **Flush on spool change** — previous spool data is saved before switching.

## Development Setup

```bash
python -m venv .venv
.venv\Scripts\activate  # Windows
# source .venv/bin/activate  # Linux
pip install -r requirements.txt
```

## Usage

```bash
python tracker.py                              # start daemon (HTTP on :8200)
python query.py spool_usage.db                 # query local SQLite DB
python query.py spool_usage.db --job 0004E2    # filter by job
python query.py spool_usage.db --spool 1       # filter by spool
python query.py --tracker                      # query via daemon HTTP API
```

## Deployment (Raspberry Pi / Linux)

```bash
cd ~
git clone <repo-url> klipper_spool_tracker
cd klipper_spool_tracker
```

### Option A — automatic (recommended)

```bash
chmod +x install.sh && ./install.sh
```

This does everything automatically:
1. Creates `config.json` from `config.example.json` (if it doesn't exist)
2. Creates `.venv`
3. Installs dependencies (`pip install`)
4. Installs, enables **and starts** the systemd service (`enable --now`)
5. Adds the Moonraker snippet to `moonraker.conf`
6. Installs logrotate for `/var/log/spool-tracker.log`

### Option B — manual

```bash
# 1. Config
cp config.example.json config.json

# 2. Virtual environment
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 3. Systemd service — install, enable on boot, and start
sudo cp klipper_spool_tracker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now klipper_spool_tracker.service

# 4. Logrotate
sudo cp klipper_spool_tracker.logrotate /etc/logrotate.d/klipper_spool_tracker

# 5. Moonraker — append the snippet to your moonraker.conf
cat moonraker-example.cfg >> ~/printer_data/config/moonraker.conf
```

### Post-installation

1. **Edit `config.json`** — set your Moonraker IP (`moonraker_url`) and Spoolman if applicable.  
   `config.json` is in `.gitignore` so `git pull` never overwrites it.
2. **Edit `moonraker.conf`** — check the `origin` URL of the repo.
3. **If you changed config, restart:**
   ```bash
   sudo systemctl restart klipper_spool_tracker
   sudo journalctl -u klipper_spool_tracker -f
   ```

## Config

Edit `config.json` (created from `config.example.json` if it doesn't exist):

| Variable        | Description                    | Default                        |
|-----------------|--------------------------------|--------------------------------|
| `MOONRAKER_URL` | Moonraker WebSocket URL        | `ws://localhost:7125/websocket`|
| `DB_PATH`       | SQLite database path           | `spool_usage.db`               |
| `HTTP_HOST`     | HTTP server bind address       | `0.0.0.0`                      |
| `HTTP_PORT`     | HTTP server port               | `8200`                         |

Environment variables take precedence over `config.json`.

The DB auto-prunes to the last 100 distinct jobs.  
A checkpoint is written to SQLite every 30s during active jobs (power-loss safety) and immediately on spool change.  
Logs go to `/var/log/spool-tracker.log` (daily rotation via `spool-tracker.logrotate`) and journald (stderr).

## HTTP Endpoints

- `GET /health` — health check (`{"status": "ok"}`)
- `GET /spool_usage` — all records
- `GET /spool_usage?job_id=0004E2` — filter by job
- `GET /spool_usage?spool_id=1` — filter by spool

## Database

SQLite with WAL mode. A single table:

```sql
spool_usage (id INTEGER PK, job_id TEXT, spool_id INTEGER, filament_mm REAL)
```

The schema is created automatically on first run.
