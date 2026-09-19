from __future__ import annotations

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
            CREATE TABLE IF NOT EXISTS live_requests (
                request_id TEXT PRIMARY KEY,
                lane TEXT NOT NULL,
                session_id TEXT NOT NULL,
                prompt_sha256 TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                status TEXT NOT NULL,
                runtime_session_id TEXT,
                start_seq INTEGER,
                error_code TEXT,
                attribution TEXT,
                proof_seq INTEGER,
                proof_epoch TEXT,
                proof_generation INTEGER,
                inflight_sha256 TEXT,
                boundary_row_id INTEGER,
                boundary_count INTEGER,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS live_requests_by_lane ON live_requests(lane, updated_at DESC);
            """
        )
        # Additive-only upgrade for databases created before the attribution
        # columns existed. Legacy rows keep NULL in every new column and must
        # degrade conservatively (never false-green) in live wait/reconcile.
        for column in (
            "attribution TEXT",
            "proof_seq INTEGER",
            "proof_epoch TEXT",
            "proof_generation INTEGER",
            "inflight_sha256 TEXT",
            "boundary_row_id INTEGER",
            "boundary_count INTEGER",
        ):
            try:
                self._conn.execute(f"ALTER TABLE live_requests ADD COLUMN {column}")
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise
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

    # ----- live request state --------------------------------------------

    def save_live_request(
        self,
        *,
        request_id: str,
        lane: str,
        session_id: str,
        prompt_sha256: str,
        fingerprint: str,
        status: str,
        runtime_session_id: str | None = None,
        start_seq: int | None = None,
        error_code: str | None = None,
        attribution: str | None = None,
        proof_seq: int | None = None,
        proof_epoch: str | None = None,
        proof_generation: int | None = None,
        inflight_sha256: str | None = None,
        boundary_row_id: int | None = None,
        boundary_count: int | None = None,
    ) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                """INSERT INTO live_requests(
                       request_id, lane, session_id, prompt_sha256, fingerprint,
                       status, runtime_session_id, start_seq, error_code,
                       attribution, proof_seq, proof_epoch, proof_generation, inflight_sha256,
                       boundary_row_id, boundary_count,
                       created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(request_id) DO UPDATE SET
                       lane=excluded.lane, session_id=excluded.session_id,
                       prompt_sha256=excluded.prompt_sha256, fingerprint=excluded.fingerprint,
                       status=excluded.status, runtime_session_id=excluded.runtime_session_id,
                       start_seq=excluded.start_seq, error_code=excluded.error_code,
                       attribution=excluded.attribution, proof_seq=excluded.proof_seq,
                       proof_epoch=excluded.proof_epoch, proof_generation=excluded.proof_generation,
                       inflight_sha256=excluded.inflight_sha256,
                       boundary_row_id=excluded.boundary_row_id,
                       boundary_count=excluded.boundary_count,
                       updated_at=excluded.updated_at""",
                (request_id, lane, session_id, prompt_sha256, fingerprint,
                 status, runtime_session_id, start_seq, error_code,
                 attribution, proof_seq, proof_epoch, proof_generation, inflight_sha256,
                 boundary_row_id, boundary_count, now, now),
            )
            self._conn.commit()
            self._tighten_permissions()

    def reserve_live_request(self, record: dict[str, Any]) -> bool:
        """Atomically reserve a request_id for first submission.

        The whole live submit path must guarantee at most one gateway mutation
        per request_id, including when two callers race with the same ID. A
        plain INSERT that fails on conflict provides that guarantee; the losing
        caller then reads the winner's record and replays or conflicts.
        """
        columns = (
            "request_id", "lane", "session_id", "prompt_sha256", "fingerprint",
            "status", "runtime_session_id", "start_seq", "error_code",
            "attribution", "proof_seq", "proof_epoch", "proof_generation", "inflight_sha256",
            "boundary_row_id", "boundary_count", "created_at", "updated_at",
        )
        values = (
            record.get("request_id"), record.get("lane"), record.get("session_id"),
            record.get("prompt_sha256"), record.get("fingerprint"), record.get("status"),
            record.get("runtime_session_id"), record.get("start_seq"), record.get("error_code"),
            record.get("attribution"), record.get("proof_seq"), record.get("proof_epoch"),
            record.get("proof_generation"), record.get("inflight_sha256"),
            record.get("boundary_row_id"), record.get("boundary_count"),
            record.get("created_at"), record.get("updated_at"),
        )
        placeholders = ", ".join("?" for _ in columns)
        with self._lock:
            try:
                self._conn.execute(
                    f"INSERT INTO live_requests({', '.join(columns)}) VALUES ({placeholders})",
                    values,
                )
            except sqlite3.IntegrityError:
                return False
            self._conn.commit()
            self._tighten_permissions()
        return True

    def update_live_request(self, request_id: str, **fields: Any) -> None:
        allowed = {
            "status", "runtime_session_id", "start_seq", "error_code", "session_id", "lane",
            "attribution", "proof_seq", "proof_epoch", "proof_generation",
        }
        changes = {key: value for key, value in fields.items() if key in allowed}
        if not changes:
            return
        changes["updated_at"] = time.time()
        assignments = ", ".join(f"{key}=?" for key in changes)
        values = [changes[key] for key in changes] + [request_id]
        with self._lock:
            self._conn.execute(f"UPDATE live_requests SET {assignments} WHERE request_id=?", values)
            self._conn.commit()
            self._tighten_permissions()

    def mark_live_request_awaiting_recovery(self, request_id: str, error_code: str | None) -> bool:
        """Compare-and-set a live request into the conservative recovery state.

        Terminal outcomes (completed/failed/interrupted/reconciled) are never
        overwritten: a stale concurrent waiter that observed a conservative
        condition must not erase an already-delivered terminal result. Returns
        True when the row now carries the recovery state, False when a
        terminal state was already committed.
        """
        with self._lock:
            cursor = self._conn.execute(
                """UPDATE live_requests SET status='unknown', error_code=?, updated_at=?
                   WHERE request_id=?
                     AND status NOT IN ('completed','failed','interrupted','reconciled')""",
                (error_code, time.time(), request_id),
            )
            self._conn.commit()
            self._tighten_permissions()
            return cursor.rowcount > 0

    def live_request_by_id(self, request_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM live_requests WHERE request_id=?", (request_id,)).fetchone()
        return self._as_dict(row)

    def latest_live_request_for_lane(self, lane: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM live_requests WHERE lane=? ORDER BY updated_at DESC LIMIT 1", (lane,)
            ).fetchone()
        return self._as_dict(row)

    def live_requests_for_lane(self, lane: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM live_requests WHERE lane=? ORDER BY updated_at DESC", (lane,)
            ).fetchall()
        return [dict(row) for row in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
