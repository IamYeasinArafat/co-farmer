"""SQLite database on the farm computer (Hydro Monitor plan 2.3-2.5, 3.4).

    log        one row per reading or actuator switch, in the plan's log format (2.4), tagged with the farm
    plans      every agent plan, versioned: the ranges file the master downloads, plus the reports behind it
    runs       every agent run, including failed ones (a failed run never changes the ranges)
    approvals  the farmer's Approve / Correct decisions: the future fine-tuning data (3.5)
    notes      the farmer's notes per field
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).parent
DB_PATH = ROOT / "data" / "hydro.db"
LOG_COLUMNS = ["field_id", "module_id", "device_id", "type", "kind", "value", "unit", "timestamp"]
_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS log (
    id INTEGER PRIMARY KEY, field_id TEXT, module_id TEXT, device_id TEXT, type TEXT, kind TEXT,
    value TEXT, unit TEXT, timestamp TEXT, ts REAL, received_at TEXT, farm_id TEXT);
CREATE INDEX IF NOT EXISTS log_field_ts ON log(field_id, ts);
CREATE TABLE IF NOT EXISTS plans (
    version INTEGER PRIMARY KEY, farm_id TEXT, created_at TEXT, valid_from TEXT, ranges TEXT,
    proposed TEXT, flags TEXT, message_en TEXT, message_ar TEXT, todos TEXT, todos_ar TEXT,
    reports TEXT, summaries TEXT, advice TEXT, made_by TEXT, trigger TEXT);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY, farm_id TEXT, trigger TEXT, started_at TEXT, finished_at TEXT,
    status TEXT, error TEXT, version INTEGER, detail TEXT);
CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY, version INTEGER, item TEXT, decision TEXT, correction TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY, field_id TEXT, text TEXT, created_at TEXT, farm_id TEXT);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=30, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    for table in ("log", "notes"):          # databases made before farms were tagged
        if "farm_id" not in [r[1] for r in con.execute(f"PRAGMA table_info({table})")]:
            con.execute(f"ALTER TABLE {table} ADD COLUMN farm_id TEXT")
    con.execute("CREATE INDEX IF NOT EXISTS log_farm_ts ON log(farm_id, ts)")
    return con


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ── log rows ─────────────────────────────────────────────────────────────
def parse_rows(body: str | list | dict) -> list[dict[str, Any]]:
    """CSV text (with or without the header line) or JSON (one object or a list) -> row dicts."""
    if isinstance(body, dict):
        body = [body]
    if isinstance(body, list):
        rows = [{c: r.get(c, "") for c in LOG_COLUMNS} for r in body]
    else:
        lines = [l for l in body.strip().splitlines() if l.strip() and not l.startswith("field_id")]
        rows = [dict(zip(LOG_COLUMNS, rec)) for rec in csv.reader(io.StringIO("\n".join(lines)))]
    out = []
    for r in rows:
        if r.get("type") not in ("reading", "action") or not r.get("timestamp") or not r.get("field_id"):
            raise ValueError(f"bad row: {r}")
        datetime.fromisoformat(str(r["timestamp"]))   # must be ISO 8601 with the offset
        out.append({c: ("" if r.get(c) is None else str(r[c])) for c in LOG_COLUMNS})
    return out


def insert_rows(con: sqlite3.Connection, rows: Iterable[dict[str, Any]], farm_id: str | None = None) -> int:
    """farm_id: the farm the master belongs to (the server stamps the active farm; the master doesn't send it)."""
    now = _now()
    data = [(*[r[c] for c in LOG_COLUMNS], datetime.fromisoformat(r["timestamp"]).timestamp(), now, farm_id) for r in rows]
    with _lock, con:
        con.executemany(f"INSERT INTO log ({','.join(LOG_COLUMNS)}, ts, received_at, farm_id) VALUES ({','.join('?' * 11)})", data)
    return len(data)


def read_log(con: sqlite3.Connection, since_ts: float, field_id: str | None = None,
             farm_id: str | None = None) -> list[dict[str, Any]]:
    sql, args = "SELECT * FROM log WHERE ts >= ?", [since_ts]
    if field_id:
        sql, args = sql + " AND field_id = ?", args + [field_id]
    if farm_id:
        sql, args = sql + " AND farm_id = ?", args + [farm_id]
    return [dict(r) for r in con.execute(sql + " ORDER BY ts", args)]


def latest_rows(con: sqlite3.Connection, limit: int = 30, farm_id: str | None = None) -> list[dict[str, Any]]:
    where, args = ("WHERE farm_id = ?", [farm_id]) if farm_id else ("", [])
    return [dict(r) for r in con.execute(f"SELECT {','.join(LOG_COLUMNS)} FROM log {where} ORDER BY ts DESC, id DESC LIMIT ?",
                                         [*args, limit])]


# ── plans (versioned ranges) ─────────────────────────────────────────────
JSON_COLS = ("ranges", "proposed", "flags", "todos", "todos_ar", "reports", "summaries", "advice")


def _plan(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    plan = dict(row)
    for col in JSON_COLS:
        plan[col] = json.loads(plan[col]) if plan[col] else None
    return plan


def latest_plan(con: sqlite3.Connection, farm_id: str | None = None) -> dict[str, Any] | None:
    sql, args = "SELECT * FROM plans", []
    if farm_id:
        sql, args = sql + " WHERE farm_id = ?", [farm_id]
    return _plan(con.execute(sql + " ORDER BY version DESC LIMIT 1", args).fetchone())


def plan_history(con: sqlite3.Connection, farm_id: str, limit: int = 20) -> list[dict[str, Any]]:
    rows = con.execute("SELECT * FROM plans WHERE farm_id = ? ORDER BY version DESC LIMIT ?", (farm_id, limit))
    return [_plan(r) for r in rows]


def save_plan(con: sqlite3.Connection, farm_id: str, valid_from: str, ranges_by_field: dict[str, Any], **extra: Any) -> dict[str, Any]:
    """Store a new plan with the next version number; the ranges file carries that number."""
    with _lock, con:
        version = (con.execute("SELECT COALESCE(MAX(version), 0) FROM plans").fetchone()[0] or 0) + 1
        ranges = {"version": version, "valid_from": valid_from, **ranges_by_field}
        cols = {"version": version, "farm_id": farm_id, "created_at": _now(), "valid_from": valid_from,
                "ranges": json.dumps(ranges)}
        for key, value in extra.items():
            cols[key] = json.dumps(value, ensure_ascii=False) if key in JSON_COLS else value
        con.execute(f"INSERT INTO plans ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", list(cols.values()))
    return latest_plan(con, farm_id)


# ── runs, approvals, notes ───────────────────────────────────────────────
def start_run(con: sqlite3.Connection, farm_id: str, trigger: str) -> int:
    with _lock, con:
        cur = con.execute("INSERT INTO runs (farm_id, trigger, started_at, status) VALUES (?,?,?, 'running')",
                          (farm_id, trigger, _now()))
    return cur.lastrowid


def finish_run(con: sqlite3.Connection, run_id: int, status: str, version: int | None = None,
               error: str | None = None, detail: Any = None) -> None:
    with _lock, con:
        con.execute("UPDATE runs SET finished_at=?, status=?, version=?, error=?, detail=? WHERE id=?",
                    (_now(), status, version, error, json.dumps(detail, ensure_ascii=False) if detail is not None else None, run_id))


def runs(con: sqlite3.Connection, limit: int = 10) -> list[dict[str, Any]]:
    return [dict(r) for r in con.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))]


def add_approval(con: sqlite3.Connection, version: int, item: str, decision: str, correction: str = "") -> None:
    with _lock, con:
        con.execute("INSERT INTO approvals (version, item, decision, correction, created_at) VALUES (?,?,?,?,?)",
                    (version, item, decision, correction, _now()))


def approvals(con: sqlite3.Connection, version: int) -> dict[str, dict[str, Any]]:
    """Latest decision per item for one plan version."""
    out: dict[str, dict[str, Any]] = {}
    for r in con.execute("SELECT * FROM approvals WHERE version = ? ORDER BY id", (version,)):
        out[r["item"]] = dict(r)
    return out


def add_note(con: sqlite3.Connection, field_id: str, text: str, farm_id: str | None = None) -> None:
    with _lock, con:
        con.execute("INSERT INTO notes (field_id, text, created_at, farm_id) VALUES (?,?,?,?)", (field_id, text, _now(), farm_id))


def notes(con: sqlite3.Connection, field_id: str | None = None, limit: int = 5,
          farm_id: str | None = None) -> list[dict[str, Any]]:
    clauses, args = [], []
    if field_id:
        clauses, args = clauses + ["field_id = ?"], args + [field_id]
    if farm_id:
        clauses, args = clauses + ["farm_id = ?"], args + [farm_id]
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return [dict(r) for r in con.execute("SELECT * FROM notes" + where + " ORDER BY id DESC LIMIT ?", args + [limit])]
