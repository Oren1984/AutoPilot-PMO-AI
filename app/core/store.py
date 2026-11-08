"""Durable SQLite persistence for runs, events, approvals, executions and checkpoints."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.core.redaction import redact

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    assignment TEXT NOT NULL,
    mode TEXT NOT NULL,
    brain TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_monotonic_offset REAL DEFAULT 0,
    current_step TEXT,
    step_count INTEGER DEFAULT 0,
    pending_approval_id TEXT,
    pending_call TEXT,
    brain_state TEXT,
    plan TEXT,
    usage TEXT,
    result TEXT,
    error TEXT,
    cancel_requested INTEGER DEFAULT 0,
    pause_requested INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    type TEXT NOT NULL,
    state TEXT,
    step INTEGER,
    action TEXT,
    method TEXT,
    result TEXT,
    policy TEXT,
    duration_ms INTEGER,
    cost_usd REAL,
    message TEXT,
    detail TEXT,
    screenshot TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, id);
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    action TEXT NOT NULL,
    method TEXT NOT NULL,
    params TEXT NOT NULL,
    params_hash TEXT NOT NULL,
    record_snapshot TEXT,
    context TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    decided_at TEXT,
    decided_by TEXT,
    decision_comment TEXT,
    status_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_approvals_run ON approvals(run_id, params_hash);
CREATE TABLE IF NOT EXISTS executions (
    idem_key TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    action TEXT NOT NULL,
    method TEXT NOT NULL,
    params TEXT NOT NULL,
    financial INTEGER DEFAULT 0,
    status TEXT NOT NULL,
    result TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT
);
"""


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _dumps(value: Any) -> str:
    return json.dumps(value, default=str, sort_keys=True)


def _loads(value: str | None, default: Any = None) -> Any:
    if value is None or value == "":
        return default
    return json.loads(value)


class Store:
    """Thread-safe wrapper around one SQLite connection (WAL mode)."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def _one(self, sql: str, params: tuple = ()) -> dict | None:
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
            return dict(row) if row else None

    def _all(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    # ---------------- runs ----------------
    def create_run(self, assignment: str, mode: str, brain: str) -> dict:
        run_id = "RUN-" + uuid.uuid4().hex[:10].upper()
        ts = now_iso()
        self._exec(
            "INSERT INTO runs (id, assignment, mode, brain, state, created_at, updated_at, usage, plan) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (run_id, assignment, mode, brain, "CREATED", ts, ts, _dumps({}), _dumps([])),
        )
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> dict | None:
        row = self._one("SELECT * FROM runs WHERE id=?", (run_id,))
        if not row:
            return None
        for key, default in (
            ("pending_call", None),
            ("brain_state", None),
            ("plan", []),
            ("usage", {}),
            ("result", None),
        ):
            row[key] = _loads(row[key], default)
        row["cancel_requested"] = bool(row["cancel_requested"])
        row["pause_requested"] = bool(row["pause_requested"])
        return row

    def list_runs(self, limit: int = 50) -> list[dict]:
        rows = self._all(
            "SELECT id, assignment, mode, brain, state, created_at, updated_at, step_count, current_step "
            "FROM runs ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        return rows

    def runs_in_states(self, states: list[str]) -> list[dict]:
        marks = ",".join("?" for _ in states)
        return [self.get_run(r["id"]) for r in self._all(f"SELECT id FROM runs WHERE state IN ({marks})", tuple(states))]  # noqa: S608

    def update_run(self, run_id: str, **fields: Any) -> None:
        if not fields:
            return
        json_fields = {"pending_call", "brain_state", "plan", "usage", "result"}
        cols, vals = [], []
        for k, v in fields.items():
            cols.append(f"{k}=?")
            if k in json_fields:
                vals.append(None if v is None else _dumps(v))
            elif isinstance(v, bool):
                vals.append(int(v))
            else:
                vals.append(v)
        cols.append("updated_at=?")
        vals.append(now_iso())
        vals.append(run_id)
        self._exec(f"UPDATE runs SET {', '.join(cols)} WHERE id=?", tuple(vals))  # noqa: S608

    # ---------------- events ----------------
    def add_event(self, run_id: str, type_: str, **kw: Any) -> dict:
        detail = redact(kw.get("detail") or {})
        message = redact(kw.get("message") or "")
        cur = self._exec(
            "INSERT INTO events (run_id, ts, type, state, step, action, method, result, policy, duration_ms, "
            "cost_usd, message, detail, screenshot) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                now_iso(),
                type_,
                kw.get("state"),
                kw.get("step"),
                kw.get("action"),
                kw.get("method"),
                kw.get("result"),
                kw.get("policy"),
                kw.get("duration_ms"),
                kw.get("cost_usd"),
                message,
                _dumps(detail),
                kw.get("screenshot"),
            ),
        )
        return {"id": cur.lastrowid}

    def list_events(self, run_id: str, after_id: int = 0) -> list[dict]:
        rows = self._all("SELECT * FROM events WHERE run_id=? AND id>? ORDER BY id", (run_id, after_id))
        for r in rows:
            r["detail"] = _loads(r["detail"], {})
        return rows

    def recent_events(self, limit: int = 100) -> list[dict]:
        rows = self._all("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
        for r in rows:
            r["detail"] = _loads(r["detail"], {})
        return rows

    # ---------------- approvals ----------------
    def create_approval(
        self,
        run_id: str,
        action: str,
        method: str,
        params: dict,
        params_hash: str,
        snapshot: dict,
        context: dict,
        ttl_minutes: int,
    ) -> dict:
        approval_id = "APR-" + uuid.uuid4().hex[:10].upper()
        created = datetime.now(UTC)
        self._exec(
            "INSERT INTO approvals (id, run_id, action, method, params, params_hash, record_snapshot, context, "
            "status, created_at, expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                approval_id,
                run_id,
                action,
                method,
                _dumps(params),
                params_hash,
                _dumps(snapshot),
                _dumps(context),
                "PENDING",
                created.isoformat(timespec="milliseconds"),
                (created + timedelta(minutes=ttl_minutes)).isoformat(timespec="milliseconds"),
            ),
        )
        return self.get_approval(approval_id)

    def _approval_row(self, row: dict | None) -> dict | None:
        if not row:
            return None
        row["params"] = _loads(row["params"], {})
        row["record_snapshot"] = _loads(row["record_snapshot"], {})
        row["context"] = _loads(row["context"], {})
        return row

    def get_approval(self, approval_id: str) -> dict | None:
        return self._approval_row(self._one("SELECT * FROM approvals WHERE id=?", (approval_id,)))

    def find_approval(self, run_id: str, params_hash: str) -> dict | None:
        return self._approval_row(
            self._one(
                "SELECT * FROM approvals WHERE run_id=? AND params_hash=? ORDER BY created_at DESC LIMIT 1",
                (run_id, params_hash),
            )
        )

    def list_approvals(self, status: str | None = None, run_id: str | None = None) -> list[dict]:
        sql, params = "SELECT * FROM approvals WHERE 1=1", []
        if status:
            sql += " AND status=?"
            params.append(status)
        if run_id:
            sql += " AND run_id=?"
            params.append(run_id)
        sql += " ORDER BY created_at DESC"
        return [self._approval_row(r) for r in self._all(sql, tuple(params))]

    def transition_approval(self, approval_id: str, expected: str, new: str, **fields: Any) -> bool:
        """Compare-and-set status change. Returns False if the approval was not in `expected` state."""
        cols, vals = ["status=?"], [new]
        for k, v in fields.items():
            cols.append(f"{k}=?")
            vals.append(v)
        vals += [approval_id, expected]
        cur = self._exec(f"UPDATE approvals SET {', '.join(cols)} WHERE id=? AND status=?", tuple(vals))  # noqa: S608
        return cur.rowcount == 1

    # ---------------- executions (idempotency ledger) ----------------
    def get_execution(self, idem_key: str) -> dict | None:
        row = self._one("SELECT * FROM executions WHERE idem_key=?", (idem_key,))
        if row:
            row["params"] = _loads(row["params"], {})
            row["result"] = _loads(row["result"], None)
        return row

    def begin_execution(self, idem_key: str, run_id: str, action: str, method: str, params: dict, financial: bool) -> bool:
        """Insert an in-flight execution marker. Returns False if the key already exists."""
        try:
            self._exec(
                "INSERT INTO executions (idem_key, run_id, action, method, params, financial, status, started_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (idem_key, run_id, action, method, _dumps(params), int(financial), "STARTED", now_iso()),
            )
            return True
        except sqlite3.IntegrityError:
            return False

    def finish_execution(self, idem_key: str, status: str, result: Any) -> None:
        self._exec(
            "UPDATE executions SET status=?, result=?, finished_at=? WHERE idem_key=?",
            (status, _dumps(result), now_iso(), idem_key),
        )

    def reset_execution(self, idem_key: str) -> None:
        self._exec("DELETE FROM executions WHERE idem_key=? AND financial=0", (idem_key,))

    def list_executions(self, run_id: str | None = None, status: str | None = None) -> list[dict]:
        sql, params = "SELECT * FROM executions WHERE 1=1", []
        if run_id:
            sql += " AND run_id=?"
            params.append(run_id)
        if status:
            sql += " AND status=?"
            params.append(status)
        rows = self._all(sql + " ORDER BY started_at", tuple(params))
        for r in rows:
            r["params"] = _loads(r["params"], {})
            r["result"] = _loads(r["result"], None)
        return rows

    # ---------------- retention ----------------
    def purge_older_than(self, days: int) -> int:
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="milliseconds")
        old = [
            r["id"]
            for r in self._all(
                "SELECT id FROM runs WHERE updated_at < ? AND state IN ('COMPLETED','REJECTED','BLOCKED','FAILED','CANCELLED')",
                (cutoff,),
            )
        ]
        for rid in old:
            for table in ("events", "approvals", "executions"):
                self._exec(f"DELETE FROM {table} WHERE run_id=?", (rid,))  # noqa: S608
            self._exec("DELETE FROM runs WHERE id=?", (rid,))
        return len(old)
