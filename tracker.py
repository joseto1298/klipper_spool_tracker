#!/usr/bin/env python3
"""
Klipper Spool Tracker — Rastrea consumo real de filamento por bobina
via WebSocket de Moonraker, independiente de Odoo.
"""
import asyncio
import json
import logging
import logging.handlers
import os
import sqlite3
import signal
from dataclasses import dataclass
from typing import Any, Dict, Optional

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
    pass  # log file no disponible (entorno Windows, etc.)


# ─── Config ────────────────────────────────────────────────────────────────


@dataclass
class Config:
    moonraker_url: str = "ws://localhost:7125/websocket"
    db_path: str = "spool_usage.db"
    http_host: str = "0.0.0.0"
    http_port: int = 8200

    @classmethod
    def load(cls, path: str = "config.json") -> "Config":
        cfg = cls()
        if os.path.exists(path):
            try:
                with open(path) as f:
                    data = json.load(f)
            except json.JSONDecodeError as exc:
                logger.warning("Config %s invalido: %s — usando defaults", path, exc)
                return cfg
            cfg.moonraker_url = data.get("moonraker_url", cfg.moonraker_url)
            cfg.db_path = data.get("db_path", cfg.db_path)
            http = data.get("http", {})
            if http.get("enabled", True):
                cfg.http_host = http.get("host", cfg.http_host)
                cfg.http_port = http.get("port", cfg.http_port)
        # ENV overrides
        cfg.moonraker_url = os.environ.get("MOONRAKER_URL", cfg.moonraker_url)
        cfg.db_path = os.environ.get("DB_PATH", cfg.db_path)
        cfg.http_host = os.environ.get("HTTP_HOST", cfg.http_host)
        cfg.http_port = int(os.environ.get("HTTP_PORT", str(cfg.http_port)))
        return cfg


# ─── Database ──────────────────────────────────────────────────────────────


class Database:
    def __init__(self, path: str):
        self.path = path
        self.conn: Optional[sqlite3.Connection] = None

    def open(self):
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
        # Migración: consolidar filas duplicadas (del guardado delta anterior)
        # antes de crear el índice único necesario para UPSERT
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

    def upsert_spool_usage(self, job_id: str, spool_id: int, filament_mm: float):
        self.conn.execute(
            """INSERT INTO spool_usage (job_id, spool_id, filament_mm)
               VALUES (?, ?, ?)
               ON CONFLICT(job_id, spool_id)
               DO UPDATE SET filament_mm = excluded.filament_mm""",
            (job_id, spool_id, round(filament_mm, 2)),
        )
        self.conn.commit()

    def prune(self, keep_jobs: int = 100):
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
            logger.info("Podados %s jobs antiguos", len(old))

    def query(self, job_id: str = None, spool_id: int = None) -> list:
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

    def close(self):
        if self.conn:
            self.conn.close()


# ─── Moonraker Client ─────────────────────────────────────────────────────


class MoonrakerClient:
    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._pending: Dict[int, asyncio.Future] = {}
        self._req_id = 0
        self._running = True

        # Estado del trabajo actual
        self.current_job_id: Optional[str] = None
        self.current_spool_id: Optional[int] = None
        self.current_filename: str = ""
        self.last_e_pos: Optional[float] = None
        self.job_spool_usage: Dict[int, float] = {}

    # ── Conexion ──────────────────────────────────────────────────────────

    async def run(self):
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
                        logger.info("Conectado a %s", self.config.moonraker_url)
                        delay = 1
                        first_fail = True
                        flush_task = asyncio.create_task(self._periodic_flush())
                        try:
                            await self._message_loop()
                        finally:
                            flush_task.cancel()
                            try:
                                await flush_task
                            except asyncio.CancelledError:
                                pass
                except websockets.ConnectionClosed:
                    if first_fail:
                        logger.warning("Conexion perdida — reintentando...")
                        first_fail = False
                except asyncio.CancelledError:
                    break
                except Exception as exc:
                    if first_fail:
                        logger.warning("Error de conexion: %s — reintentando...", exc)
                        first_fail = False
                    else:
                        logger.debug("Error de conexion: %s (reintento en %ds)", exc, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)
        finally:
            if self._session:
                await self._session.close()

    async def _identify(self):
        await self._send_json({
            "jsonrpc": "2.0",
            "method": "connection.identify",
            "params": {
                "client_name": "klipper_spool_tracker",
                "type": "agent",
            },
        })

    async def _subscribe_toolhead(self):
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

    # ── Mensajes ──────────────────────────────────────────────────────────

    async def _message_loop(self):
        async for raw in self.ws:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue

            # Respuesta a request nuestra
            if "id" in data:
                fut = self._pending.pop(data["id"], None)
                if fut and not fut.done():
                    fut.set_result(data.get("result"))
                continue

            method = data.get("method")

            if method == "notify_status_update":
                self._on_status_update(data.get("params", [None])[0])
            elif method == "notify_active_spool_set":
                self._on_spool_changed(data.get("params", [{}])[0])
            elif method == "notify_history_changed":
                await self._on_history_changed(data.get("params", [{}])[0])

    # ── Eventos ──────────────────────────────────────────────────────────

    def _on_status_update(self, status: Optional[Dict]):
        """Recibe actualizacion de estado de Klipper cada ~250ms."""
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
            if delta > 0.01:  # ignorar ruido sub-micron
                self.job_spool_usage[self.current_spool_id] = \
                    self.job_spool_usage.get(self.current_spool_id, 0) + delta
        self.last_e_pos = e_pos

    def _on_spool_changed(self, params: Optional[Dict]):
        """Cambio de bobina activa detectado por Moonraker."""
        if not params:
            return
        new_spool_id = params.get("spool_id")
        if new_spool_id == self.current_spool_id:
            return
        if self.current_spool_id is not None:
            logger.info(
                "  Bobina %s hasta ahora: %.2f mm",
                self.current_spool_id,
                self.job_spool_usage.get(self.current_spool_id, 0),
            )
        self.current_spool_id = new_spool_id
        if new_spool_id is not None and new_spool_id not in self.job_spool_usage:
            self.job_spool_usage[new_spool_id] = 0
        logger.info("Bobina activa: %s", new_spool_id)

    # ── Flujo periódico ──────────────────────────────────────────────────

    def _flush_current_usage(self):
        """Guarda en SQLite el total acumulado (UPSERT para mantener
        una sola fila por job+spool aunque haya corte de luz)."""
        job_id = self.current_job_id
        if job_id is None:
            return
        for spool_id, total_mm in list(self.job_spool_usage.items()):
            if total_mm > 0:
                self.db.upsert_spool_usage(job_id, spool_id, total_mm)
                logger.debug("Flujo parcial: bobina %s %.2f mm", spool_id, total_mm)

    async def _periodic_flush(self):
        """Corre en background: cada 30s hace flush parcial y reintenta
        obtener spool_id si aún es None (ASSERT_ACTIVE_FILAMENT puede
        tardar minutos en ejecutarse tras START_PRINT)."""
        while self._running:
            await asyncio.sleep(30)
            if self.current_job_id is None:
                continue
            if self.current_spool_id is None and self.ws is not None:
                status = await self._request("server.spoolman.status")
                if status and status.get("spool_id") is not None:
                    self.current_spool_id = status.get("spool_id")
                    logger.info(
                        "Bobina recuperada durante trabajo: %s",
                        self.current_spool_id,
                    )
                    self.last_e_pos = None
            if self.current_spool_id is not None and self.job_spool_usage:
                self._flush_current_usage()

    async def _on_history_changed(self, params: Dict):
        """Eventos de inicio/fin de trabajo."""
        action = params.get("action")
        job = params.get("job", {})

        if action == "added" and job.get("status") == "in_progress":
            await self._start_job(job)
        elif action == "finished":
            await self._finish_job(job)

    async def _start_job(self, job: Dict):
        job_id = job.get("job_id")
        if job_id is None:
            logger.warning("Evento 'added' sin job_id — ignorado")
            return
        self.current_job_id = job_id
        self.current_filename = job.get("filename", "")
        self.last_e_pos = None
        self.job_spool_usage = {}

        # Obtener spool inicial con reintento (backoff hasta ~30s)
        # Spoolman puede tardar en validar la bobina tras iniciar el trabajo
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
            "Trabajo iniciado: %s | %s | Bobina inicial: %s",
            self.current_job_id, self.current_filename, self.current_spool_id,
        )

    async def _finish_job(self, job: Dict):
        job_id = job.get("job_id")
        if job_id is None or job_id != self.current_job_id:
            return

        self._flush_current_usage()

        logger.info(
            "Trabajo finalizado: %s | Bobinas: %s",
            job_id, dict(self.job_spool_usage),
        )

        for spool_id, mm in self.job_spool_usage.items():
            if mm > 0:
                logger.info("  Bobina %s: %.2f mm", spool_id, mm)

        self.db.prune()

        self.current_job_id = None
        self.current_spool_id = None
        self.last_e_pos = None
        self.job_spool_usage = {}

    # ── Utilidades ────────────────────────────────────────────────────────

    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    async def _request(self, method: str, params: Optional[Dict] = None
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

    async def _send_json(self, data: Dict):
        if self.ws is None:
            return
        await self.ws.send(json.dumps(data))

    async def stop(self):
        self._running = False
        if self.ws:
            await self.ws.close()


# ─── HTTP Server ────────────────────────────────────────────────────────────


class SpoolHTTPServer:
    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        self.app = web.Application()
        self.app.router.add_get("/spool_usage", self.handle_query)
        self.app.router.add_get("/health", self.handle_health)
        self._runner: Optional[web.AppRunner] = None

    async def start(self):
        self._runner = web.AppRunner(self.app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.config.http_host, self.config.http_port)
        await site.start()
        logger.info(
            "Servidor HTTP escuchando en %s:%s",
            self.config.http_host, self.config.http_port,
        )

    async def stop(self):
        if self._runner:
            await self._runner.cleanup()
            self._runner = None

    async def handle_query(self, request):
        job_id = request.query.get("job_id")
        spool_id = request.query.get("spool_id")
        if spool_id is not None:
            try:
                spool_id = int(spool_id)
            except (ValueError, TypeError):
                return web.json_response(
                    {"error": "spool_id debe ser entero"}, status=400,
                )
        rows = self.db.query(job_id=job_id, spool_id=spool_id)
        return web.json_response(rows)

    async def handle_health(self, request):
        return web.json_response({"status": "ok"})


# ─── Main ──────────────────────────────────────────────────────────────────


async def main():
    config = Config.load()
    db = Database(config.db_path)
    db.open()

    client = MoonrakerClient(config, db)
    httpd = SpoolHTTPServer(config, db)

    loop = asyncio.get_running_loop()

    async def stop_all():
        await client.stop()
        await httpd.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.create_task(stop_all()))
        except NotImplementedError:
            pass  # Windows no soporta add_signal_handler

    await httpd.start()
    logger.info("Klipper Spool Tracker iniciado")
    try:
        await client.run()
    finally:
        await httpd.stop()
        db.close()
        logger.info("Detenido")


if __name__ == "__main__":
    asyncio.run(main())
