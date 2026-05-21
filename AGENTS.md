# Klipper Spool Tracker

## Commands

```bash
python -m venv .venv
.venv\Scripts\activate  # Windows
pip install -r requirements.txt
python tracker.py                              # run daemon (daemon + HTTP on :8200)
python query.py spool_usage.db                 # query all usage (local SQLite)
python query.py spool_usage.db --job 0004E2    # filter by job
python query.py spool_usage.db --spool 1       # filter by spool
python query.py --tracker                      # query daemon HTTP API (pull model)
python query.py --tracker --job 0004E2
```

## Config

- `config.json` / `config.example.json` — JSON config with ENV overrides: `MOONRAKER_URL`, `DB_PATH`, `HTTP_HOST`, `HTTP_PORT`
- DB auto-prunes to last 100 distinct jobs

## Architecture

- `tracker.py` — daemon: Moonraker WebSocket → E-axis deltas → SQLite (WAL), plus HTTP server on `:8200` serving `GET /spool_usage` and `GET /health`
- `query.py` — CLI: reads local SQLite (`query.py db`) or queries daemon HTTP API (`query.py --tracker`)
- SQLite DB: single table `spool_usage` (`job_id` TEXT, `spool_id` INT, `filament_mm` REAL)

## Service

- `spool-tracker.service` — systemd unit for Raspberry Pi; expects repo at `/home/pi/klipper_spool_tracker`
- `moonraker-example.cfg` — snippet to copy into `moonraker.conf` for Spoolman + update_manager

## Notes

- No tests, no linter, no formatter, no CI — run `python` to verify
- Schema created on first run via `CREATE TABLE IF NOT EXISTS`
- Python 3.7+ (asyncio.run)
