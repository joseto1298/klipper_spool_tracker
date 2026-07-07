#!/usr/bin/env python3
"""
Usage: python query.py [db_path] [--job <id>] [--spool <id>]
       python query.py --tracker [--job <id>] [--spool <id>]
"""
import json
import sqlite3
import sys
import urllib.request
import urllib.parse
from typing import Any, Dict, List, Optional

CONFIG_PATH = "config.json"
_DEFAULT_HTTP_PORT = 8200


def load_config() -> Dict[str, Any]:
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def query_tracker(job_id: Optional[str] = None, spool_id: Optional[int] = None) -> None:
    cfg = load_config()
    http = cfg.get("http", {})
    host = http.get("host", "localhost")
    port = http.get("port", _DEFAULT_HTTP_PORT)
    if http.get("enabled", True) is False:
        print("Error: HTTP server disabled in config.json")
        return

    url = f"http://{host}:{port}/spool_usage"
    params: Dict[str, str] = {}
    if job_id:
        params["job_id"] = job_id
    if spool_id is not None:
        params["spool_id"] = str(spool_id)
    if params:
        url += "?" + urllib.parse.urlencode(params)

    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
    except Exception as exc:
        print(f"Error querying tracker: {exc}")
        return

    rows = data if isinstance(data, list) else []
    if not rows:
        print("No results")
        return

    for r in rows:
        print(f"Job {r['job_id']:>6} | Spool {r['spool_id']:>3} | {r['filament_mm']:>8.2f} mm")

    if not job_id and spool_id is None:
        total = sum(r['filament_mm'] for r in rows)
        print(f"\nTotal global (tracker): {total:.2f} mm")


def query_local(db_path: str, job_id: Optional[str] = None, spool_id: Optional[int] = None) -> None:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    if job_id and spool_id:
        cur.execute(
            "SELECT * FROM spool_usage WHERE job_id=? AND spool_id=?",
            (job_id, spool_id),
        )
        rows = cur.fetchall()
        for r in rows:
            print(f"Job {r[1]:>6} | Spool {r[2]:>3} | {r[3]:>8.2f} mm")
    elif job_id:
        cur.execute(
            "SELECT * FROM spool_usage WHERE job_id=? ORDER BY spool_id",
            (job_id,),
        )
        rows = cur.fetchall()
        for r in rows:
            print(f"Job {r[1]:>6} | Spool {r[2]:>3} | {r[3]:>8.2f} mm")
    elif spool_id:
        cur.execute(
            "SELECT * FROM spool_usage WHERE spool_id=? ORDER BY job_id",
            (spool_id,),
        )
        rows = cur.fetchall()
        for r in rows:
            print(f"Job {r[1]:>6} | Spool {r[2]:>3} | {r[3]:>8.2f} mm")
    else:
        cur.execute(
            "SELECT job_id, spool_id, SUM(filament_mm) FROM spool_usage "
            "GROUP BY job_id, spool_id ORDER BY job_id"
        )
        rows = cur.fetchall()
        for r in rows:
            print(f"Job {r[0]:>6} | Spool {r[1]:>3} | {r[2]:>8.2f} mm")

    if not rows:
        conn.close()
        print("No results")
        return

    if not job_id and not spool_id:
        cur.execute("SELECT SUM(filament_mm) FROM spool_usage")
        total = cur.fetchone()[0] or 0
        print(f"\nTotal global: {total:.2f} mm")

    conn.close()


if __name__ == "__main__":
    args = sys.argv[1:]
    kwargs: Dict[str, Any] = {}
    use_tracker = "--tracker" in args
    args = [a for a in args if a != "--tracker"]

    db: str = args[0] if args and args[0][0] != "-" else "spool_usage.db"
    for i, arg in enumerate(args):
        if arg == "--job" and i + 1 < len(args):
            kwargs["job_id"] = args[i + 1]
        elif arg == "--spool" and i + 1 < len(args):
            kwargs["spool_id"] = int(args[i + 1])

    if use_tracker:
        query_tracker(**kwargs)
    else:
        query_local(db, **kwargs)
