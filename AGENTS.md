# Klipper Spool Tracker

## Commands

```bash
python -m venv .venv
.venv\Scripts\activate      # Windows
# source .venv/bin/activate # Linux
pip install -r requirements.txt
python tracker.py                          # daemon (WS→SQLite + HTTP :8200)
python query.py spool_usage.db             # query local SQLite
python query.py spool_usage.db --job JOBID # filter by hex job_id
python query.py spool_usage.db --spool 1
python query.py --tracker                  # query daemon HTTP API
python query.py --tracker --job JOBID
```

Verify by running `python tracker.py` — no tests, no linter, no formatter, no CI.

## Config

- `config.json` — gitignored, read from CWD by both `tracker.py` and `query.py`
- `config.example.json` — template; copied by `install.sh` on first run
- ENV overrides (`MOONRAKER_URL`, `DB_PATH`, `HTTP_HOST`, `HTTP_PORT`) beat config.json
- `http.enabled: false` in config.json disables the HTTP server (and `--tracker` queries fail)
- DB auto-prunes to last 100 distinct jobs; WAL journal mode

## Architecture

- `tracker.py` — daemon: connects to Moonraker WebSocket, subscribes to `toolhead.position` E-axis, tracks deltas >0.01mm, saves to SQLite on job finish, checkpoint every 30s, and immediately on spool change; serves `GET /spool_usage` and `GET /health` on `:8200` via aiohttp
- Auto-reconnects to Moonraker on connection loss (backoff 1→60s, only first failure logged per cycle)
- Retries `server.spoolman.status` 7× with backoff 2→10s at job start to get initial spool_id
- Event `notify_active_spool_set` flushes current spool data to DB immediately before switching
- Signal handling (SIGINT/SIGTERM) caught on Unix; silently ignored on Windows (`NotImplementedError`)
- `query.py` — two modes: local SQLite file (default) or `--tracker` to query daemon HTTP API (reads config.json for host/port)
- SQLite: single table `spool_usage` (`id` PK, `job_id` TEXT, `spool_id` INT, `filament_mm` REAL); schema created on first run; UPSERT keeps one row per job+spool across restarts
- Only 2 deps: `websockets>=12.0`, `aiohttp>=3.9.0` (Python 3.8+)

## Deployment

- `install.sh` — 7-step auto-installer (config → venv → pip → systemd → moonraker snippet → logrotate → done)
- `klipper_spool_tracker.service` — systemd unit; `User=pi` hardcoded; paths relative to `/home/pi/klipper_spool_tracker`
- `moonraker-example.cfg` — `[update_manager]` snippet with `install_script: install.sh`
- Logs: `/var/log/spool-tracker.log` + journald (stderr)
