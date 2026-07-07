#!/usr/bin/env python3
"""
Klipper Spool Tracker — tracks real filament consumption per spool
via Moonraker WebSocket, independent of Odoo.
"""
import asyncio
import json
import logging
import logging.handlers
import os
import sqlite3
import signal
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import aiohttp
from aiohttp import web
import websockets

_LOG_FILE = "/var/log/spool-tracker.log"
_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"

logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT)
logger = logging.getLogger("spool_tracker")

try:
    _fh = logging.handlers.WatchedFileHandler(_LOG_FILE)
    _fh.setFormatter(logging.Formatter(_LOG_FORMAT))
    logger.addHandler(_fh)
except OSError:
    pass  # log file not available (Windows, etc.)

_DEFAULT_HTTP_PORT = 8200
_CHECKPOINT_INTERVAL = 30  # seconds between DB checkpoint writes (power-loss safety)


# ─── Config ────────────────────────────────────────────────────────────────


@dataclass
class Config:
    moonraker_url: str = "ws://localhost:7125/websocket"
    db_path: str = "spool_usage.db"
    http_host: str = "0.0.0.0"
    http_port: int = _DEFAULT_HTTP_PORT

    @classmethod
    def load(cls, path: str = "config.json") -> "Config":
        cfg = cls()
        if os.path.exists(path):
            try:
                with open(path) as f:
                    data = json.load(f)
            except json.JSONDecodeError as exc:
                logger.warning("Config %s is invalid: %s — using defaults", path, exc)
                return cfg
            raw_url = data.get("moonraker_url")
            if raw_url is not None and not isinstance(raw_url, str):
                logger.warning("moonraker_url must be a string, got %s — using default", type(raw_url).__name__)
            else:
                cfg.moonraker_url = raw_url if raw_url is not None else cfg.moonraker_url

            raw_db = data.get("db_path")
            if raw_db is not None and not isinstance(raw_db, str):
                logger.warning("db_path must be a string, got %s — using default", type(raw_db).__name__)
            else:
                cfg.db_path = raw_db if raw_db is not None else cfg.db_path

            http = data.get("http", {})
            if http.get("enabled", True):
                raw_host = http.get("host")
                if raw_host is not None and not isinstance(raw_host, str):
                    logger.warning("http.host must be a string, got %s — using default", type(raw_host).__name__)
                else:
                    cfg.http_host = raw_host if raw_host is not None else cfg.http_host

                raw_port = http.get("port")
                if raw_port is not None and not isinstance(raw_port, (int, float)):
                    logger.warning("http.port must be a number, got %s — using default", type(raw_port).__name__)
                else:
                    cfg.http_port = int(raw_port) if raw_port is not None else cfg.http_port
        # ENV overrides
        cfg.moonraker_url = os.environ.get("MOONRAKER_URL", cfg.moonraker_url)
        cfg.db_path = os.environ.get("DB_PATH", cfg.db_path)
        cfg.http_host = os.environ.get("HTTP_HOST", cfg.http_host)
        cfg.http_port = int(os.environ.get("HTTP_PORT", str(cfg.http_port)))
        return cfg


# ─── Database ──────────────────────────────────────────────────────────────


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self.conn: Optional[sqlite3.Connection] = None

    def open(self) -> None:
        db_dir = os.path.dirname(self.path) or "."
        if not os.path.exists(db_dir):
            raise OSError(f"Directory {db_dir} does not exist")
        if not os.access(db_dir, os.W_OK):
            raise OSError(f"Directory {db_dir} is not writable")
        self.conn = sqlite3.connect(self.path, timeout=5)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS spool_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                spool_id INTEGER NOT NULL,
                filament_mm REAL NOT NULL
            )
        """)
        # Migration: consolidate duplicate rows from earlier delta-only saves
        # before creating the unique index needed for UPSERT
        self.conn.execute("""
            UPDATE spool_usage SET filament_mm = (
                SELECT ROUND(SUM(s2.filament_mm), 2) FROM spool_usage s2
                WHERE s2.job_id = spool_usage.job_id
                AND s2.spool_id = spool_usage.spool_id
            )
            WHERE id IN (
                SELECT MIN(id) FROM spool_usage
                GROUP BY job_id, spool_id HAVING COUNT(*) > 1
            )
        """)
        self.conn.execute("""
            DELETE FROM spool_usage WHERE id NOT IN (
                SELECT MIN(id) FROM spool_usage GROUP BY job_id, spool_id
            )
        """)
        self.conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_spool_usage_job_spool
            ON spool_usage(job_id, spool_id)
        """)
        self.conn.commit()

    def upsert_spool_usage(self, job_id: str, spool_id: int, filament_mm: float) -> None:
        self.conn.execute(
            """INSERT INTO spool_usage (job_id, spool_id, filament_mm)
               VALUES (?, ?, ?)
               ON CONFLICT(job_id, spool_id)
               DO UPDATE SET filament_mm = excluded.filament_mm""",
            (job_id, spool_id, round(filament_mm, 2)),
        )
        self.conn.commit()

    def prune(self, keep_jobs: int = 100) -> None:
        cur = self.conn.execute("""
            SELECT job_id FROM spool_usage
            GROUP BY job_id
            ORDER BY MAX(id) DESC
        """)
        jobs = [row[0] for row in cur.fetchall()]
        if len(jobs) > keep_jobs:
            old = jobs[keep_jobs:]
            placeholders = ",".join("?" for _ in old)
            self.conn.execute(
                f"DELETE FROM spool_usage WHERE job_id IN ({placeholders})",
                old,
            )
            self.conn.commit()
            logger.info("Pruned %s old jobs", len(old))

    def query(self, job_id: Optional[str] = None, spool_id: Optional[int] = None) -> List[Dict[str, Any]]:
        cur = self.conn.execute(
            "SELECT job_id, spool_id, SUM(filament_mm) "
            "FROM spool_usage "
            "WHERE (? IS NULL OR job_id = ?) "
            "AND (? IS NULL OR spool_id = ?) "
            "GROUP BY job_id, spool_id "
            "ORDER BY job_id",
            (job_id, job_id, spool_id, spool_id),
        )
        return [
            {"job_id": r[0], "spool_id": r[1], "filament_mm": round(r[2], 2)}
            for r in cur.fetchall()
        ]

    def close(self) -> None:
        if self.conn:
            self.conn.close()


# ─── Moonraker Client ─────────────────────────────────────────────────────


class MoonrakerClient:
    def __init__(self, config: Config, db: Database) -> None:
        self.config = config
        self.db = db
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._pending: Dict[int, asyncio.Future] = {}
        self._req_id = 0
        self._running = True
        self.connected: bool = False

        # Current job state
        self.current_job_id: Optional[str] = None
        self.current_spool_id: Optional[int] = None
        self.current_filename: str = ""
        self.last_e_pos: Optional[float] = None
        self.job_spool_usage: Dict[int, float] = {}

    # ── Connection ─────────────────────────────────────────────────────────

    async def run(self) -> None:
        self._session = aiohttp.ClientSession()
        delay = 1
        first_fail = True
        try:
            while self._running:
                try:
                    async with websockets.connect(
                        self.config.moonraker_url,
                        ping_interval=30,
                        ping_timeout=15,
                    ) as ws:
                        self.ws = ws
                        await self._identify()
                        await self._subscribe_toolhead()
                        logger.info("Connected to %s", self.config.moonraker_url)
                        self.connected = True
                        delay = 1
                        first_fail = True
                        checkpoint_task = asyncio.create_task(self._periodic_checkpoint())
                        try:
                            await self._message_loop()
                        finally:
                            checkpoint_task.cancel()
                            try:
                                await checkpoint_task
                            except asyncio.CancelledError:
                                pass
                except websockets.ConnectionClosed:
                    self.connected = False
                    if first_fail:
                        logger.warning("Connection lost — reconnecting...")
                        first_fail = False
                except asyncio.CancelledError:
                    break
                except Exception as exc:
                    self.connected = False
                    if first_fail:
                        logger.warning("Connection error: %s — reconnecting...", exc)
                        first_fail = False
                    else:
                        logger.debug("Connection error: %s (retry in %ds)", exc, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)
        finally:
            if self._session:
                await self._session.close()

    async def _identify(self) -> None:
        await self._send_json({
            "jsonrpc": "2.0",
            "method": "connection.identify",
            "params": {
                "client_name": "klipper_spool_tracker",
                "type": "agent",
            },
        })

    async def _subscribe_toolhead(self) -> None:
        await self._send_json({
            "jsonrpc": "2.0",
            "method": "printer.objects.subscribe",
            "params": {
                "objects": {
                    "toolhead": ["position"],
                },
            },
            "id": self._next_id(),
        })

    # ── Messages ───────────────────────────────────────────────────────────

    async def _message_loop(self) -> None:
        async for raw in self.ws:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue

            # Response to our own request
            if "id" in data:
                fut = self._pending.pop(data["id"], None)
                if fut and not fut.done():
                    fut.set_result(data.get("result"))
                continue

            method = data.get("method")

            if method == "notify_status_update":
                self._on_status_update(data.get("params", [None])[0])
            elif method == "notify_active_spool_set":
                await self._on_spool_changed(data.get("params", [{}])[0])
            elif method == "notify_history_changed":
                await self._on_history_changed(data.get("params", [{}])[0])

    # ── Events ─────────────────────────────────────────────────────────────

    def _on_status_update(self, status: Optional[Dict[str, Any]]) -> None:
        """Receives Klipper status updates every ~250ms."""
        if not status or self.current_job_id is None:
            return
        if self.current_spool_id is None:
            return

        toolhead = status.get("toolhead")
        if not toolhead:
            return
        pos = toolhead.get("position")
        if not pos or len(pos) < 4:
            return

        e_pos = pos[3]
        if self.last_e_pos is not None:
            delta = e_pos - self.last_e_pos
            if delta > 0.01:  # ignore sub-micron noise
                self.job_spool_usage[self.current_spool_id] = \
                    self.job_spool_usage.get(self.current_spool_id, 0) + delta
        self.last_e_pos = e_pos

    async def _on_spool_changed(self, params: Optional[Dict[str, Any]]) -> None:
        """Active spool change detected by Moonraker."""
        if not params:
            return
        new_spool_id = params.get("spool_id")
        if new_spool_id == self.current_spool_id:
            return

        # Flush previous spool data immediately before switching
        if self.current_spool_id is not None:
            logger.info(
                "  Spool %s so far: %.2f mm — flushing",
                self.current_spool_id,
                self.job_spool_usage.get(self.current_spool_id, 0),
            )
            self._flush_current_usage()

        self.current_spool_id = new_spool_id
        if new_spool_id is not None and new_spool_id not in self.job_spool_usage:
            self.job_spool_usage[new_spool_id] = 0
        logger.info("Active spool: %s", new_spool_id)

    # ── Checkpoint ─────────────────────────────────────────────────────────

    def _flush_current_usage(self) -> None:
        """Saves accumulated usage to SQLite (UPSERT keeps one row per
        job+spool even after power loss)."""
        job_id = self.current_job_id
        if job_id is None:
            return
        for spool_id, total_mm in list(self.job_spool_usage.items()):
            if total_mm > 0:
                self.db.upsert_spool_usage(job_id, spool_id, total_mm)
                logger.debug("Checkpoint: spool %s %.2f mm", spool_id, total_mm)

    async def _periodic_checkpoint(self) -> None:
        """Runs in background: every 30s writes a checkpoint and retries
        spool_id if still None (ASSERT_ACTIVE_FILAMENT may take minutes
        after START_PRINT)."""
        while self._running:
            await asyncio.sleep(_CHECKPOINT_INTERVAL)
            if self.current_job_id is None:
                continue
            if self.current_spool_id is None and self.ws is not None:
                status = await self._request("server.spoolman.status")
                if status and status.get("spool_id") is not None:
                    self.current_spool_id = status.get("spool_id")
                    logger.info(
                        "Spool recovered during job: %s",
                        self.current_spool_id,
                    )
                    self.last_e_pos = None
            if self.current_spool_id is not None and self.job_spool_usage:
                self._flush_current_usage()

    async def _on_history_changed(self, params: Dict[str, Any]) -> None:
        """Job start/finish events."""
        action = params.get("action")
        job = params.get("job", {})

        if action == "added" and job.get("status") == "in_progress":
            await self._start_job(job)
        elif action == "finished":
            await self._finish_job(job)

    async def _start_job(self, job: Dict[str, Any]) -> None:
        job_id = job.get("job_id")
        if job_id is None:
            logger.warning("'added' event without job_id — ignored")
            return
        self.current_job_id = job_id
        self.current_filename = job.get("filename", "")
        self.last_e_pos = None
        self.job_spool_usage = {}

        # Retry spool lookup with backoff (~30s total)
        # Spoolman may take time to validate the spool after job start
        retry_delay = 2
        for attempt in range(7):
            status = await self._request("server.spoolman.status")
            if status and status.get("spool_id") is not None:
                self.current_spool_id = status.get("spool_id")
                break
            if attempt < 6:
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 1.5, 10)  # 2→3→4.5→6.75→10→10s

        logger.info(
            "Job started: %s | %s | Initial spool: %s",
            self.current_job_id, self.current_filename, self.current_spool_id,
        )

    async def _finish_job(self, job: Dict[str, Any]) -> None:
        job_id = job.get("job_id")
        if job_id is None or job_id != self.current_job_id:
            return

        self._flush_current_usage()

        logger.info(
            "Job finished: %s | Spools: %s",
            job_id, dict(self.job_spool_usage),
        )

        for spool_id, mm in self.job_spool_usage.items():
            if mm > 0:
                logger.info("  Spool %s: %.2f mm", spool_id, mm)

        self.db.prune()

        self.current_job_id = None
        self.current_spool_id = None
        self.last_e_pos = None
        self.job_spool_usage = {}

    # ── Utilities ──────────────────────────────────────────────────────────

    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    async def _request(self, method: str, params: Optional[Dict[str, Any]] = None
                       ) -> Optional[Any]:
        req_id = self._next_id()
        fut = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        await self._send_json({
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
            "id": req_id,
        })
        try:
            return await asyncio.wait_for(fut, timeout=10)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            return None

    async def _send_json(self, data: Dict[str, Any]) -> None:
        if self.ws is None:
            return
        await self.ws.send(json.dumps(data))

    async def stop(self) -> None:
        self._running = False
        if self.ws:
            await self.ws.close()


# ─── HTTP Server ────────────────────────────────────────────────────────────


class SpoolHTTPServer:
    def __init__(self, config: Config, db: Database, client: MoonrakerClient) -> None:
        self.config = config
        self.db = db
        self.client = client
        self.app = web.Application()
        self.app.router.add_get("/spool_usage", self.handle_query)
        self.app.router.add_get("/health", self.handle_health)
        self._runner: Optional[web.AppRunner] = None

    async def start(self) -> None:
        self._runner = web.AppRunner(self.app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.config.http_host, self.config.http_port)
        try:
            await site.start()
        except OSError as exc:
            logger.error(
                "Failed to bind HTTP server on %s:%s — %s",
                self.config.http_host, self.config.http_port, exc,
            )
            raise
        logger.info(
            "HTTP server listening on %s:%s",
            self.config.http_host, self.config.http_port,
        )

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            self._runner = None

    async def handle_query(self, request: web.Request) -> web.Response:
        job_id = request.query.get("job_id")
        spool_id = request.query.get("spool_id")
        if spool_id is not None:
            try:
                spool_id = int(spool_id)
            except (ValueError, TypeError):
                return web.json_response(
                    {"error": "spool_id must be an integer"}, status=400,
                )
        rows = self.db.query(job_id=job_id, spool_id=spool_id)
        return web.json_response(rows)

    async def handle_health(self, request: web.Request) -> web.Response:
        if self.client.connected:
            return web.json_response({"status": "ok", "moonraker": "connected"})
        return web.json_response(
            {"status": "degraded", "moonraker": "disconnected"},
            status=503,
        )


# ─── Main ──────────────────────────────────────────────────────────────────


async def main() -> None:
    config = Config.load()
    db = Database(config.db_path)
    db.open()

    client = MoonrakerClient(config, db)
    httpd = SpoolHTTPServer(config, db, client)

    loop = asyncio.get_running_loop()

    async def stop_all() -> None:
        await client.stop()
        await httpd.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.create_task(stop_all()))
        except NotImplementedError:
            pass  # Windows does not support add_signal_handler

    await httpd.start()
    logger.info("Klipper Spool Tracker started")
    try:
        await client.run()
    finally:
        await httpd.stop()
        db.close()
        logger.info("Stopped")


if __name__ == "__main__":
    asyncio.run(main())
