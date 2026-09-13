from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


class RegistryError(RuntimeError):
    """A safe bridge registry operation failed."""


class StateRegistry:
    """Small durable registry for lanes and redacted request state.

    The database deliberately has no prompt/body column. The Hermes API server
    remains the source of run output and history; this registry only lets a
    restarted MCP process reconcile a caller request safely.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        if str(self.path) == ":memory:":
            self._db_path = ":memory:"
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                self.path.parent.chmod(0o700)
            except OSError:
                pass
            self._db_path = str(self.path)
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS lanes (
                lane TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS requests (
                request_id TEXT PRIMARY KEY,
                lane TEXT NOT NULL,
                session_id TEXT,
                run_id TEXT,
                idempotency_key TEXT NOT NULL UNIQUE,
                fingerprint TEXT NOT NULL,
                status TEXT NOT NULL,
                error_code TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS requests_by_lane ON requests(lane, updated_at DESC);
            CREATE INDEX IF NOT EXISTS requests_by_run ON requests(run_id);
            """
        )
        self._conn.commit()
        self._tighten_permissions()

    def _tighten_permissions(self) -> None:
        if self._db_path == ":memory:":
            return
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(self._db_path + suffix)
            try:
                if candidate.exists():
                    candidate.chmod(0o600)
            except OSError:
                pass

    def bind_lane(self, lane: str, session_id: str) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                """INSERT INTO lanes(lane, session_id, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(lane) DO UPDATE SET session_id=excluded.session_id,
                                                   updated_at=excluded.updated_at""",
                (lane, session_id, now),
            )
            self._conn.commit()
            self._tighten_permissions()

    def session_for_lane(self, lane: str) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT session_id FROM lanes WHERE lane=?", (lane,)).fetchone()
        return str(row[0]) if row else None

    def save_request(
        self,
        *,
        request_id: str,
        lane: str,
        session_id: str | None,
        run_id: str | None,
        idempotency_key: str,
        fingerprint: str,
        status: str,
        error_code: str | None = None,
    ) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                """INSERT INTO requests(
                       request_id, lane, session_id, run_id, idempotency_key,
                       fingerprint, status, error_code, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(request_id) DO UPDATE SET
                       lane=excluded.lane, session_id=excluded.session_id,
                       run_id=excluded.run_id, idempotency_key=excluded.idempotency_key,
                       fingerprint=excluded.fingerprint, status=excluded.status,
                       error_code=excluded.error_code, updated_at=excluded.updated_at""",
                (request_id, lane, session_id, run_id, idempotency_key, fingerprint, status, error_code, now, now),
            )
            self._conn.commit()
            self._tighten_permissions()

    def update_request(self, request_id: str, **fields: Any) -> None:
        allowed = {"session_id", "run_id", "status", "error_code"}
        changes = {key: value for key, value in fields.items() if key in allowed}
        if not changes:
            return
        changes["updated_at"] = time.time()
        assignments = ", ".join(f"{key}=?" for key in changes)
        values = [changes[key] for key in changes] + [request_id]
        with self._lock:
            self._conn.execute(f"UPDATE requests SET {assignments} WHERE request_id=?", values)
            self._conn.commit()
            self._tighten_permissions()

    @staticmethod
    def _as_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row else None

    def request_by_id(self, request_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM requests WHERE request_id=?", (request_id,)).fetchone()
        return self._as_dict(row)

    def request_by_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM requests WHERE run_id=?", (run_id,)).fetchone()
        return self._as_dict(row)

    def request_by_idempotency(self, idempotency_key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM requests WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
        return self._as_dict(row)

    def latest_request_for_lane(self, lane: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM requests WHERE lane=? ORDER BY updated_at DESC LIMIT 1", (lane,)
            ).fetchone()
        return self._as_dict(row)

    def close(self) -> None:
        with self._lock:
            self._conn.close()
