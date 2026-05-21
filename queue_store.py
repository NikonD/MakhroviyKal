from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from syslog import log


Status = Literal["pending_approval", "approved", "sent", "error"]


@dataclass(frozen=True)
class RequestRow:
    id: int
    drive_file_id: str
    filename: str
    status: Status
    created_at: int
    updated_at: int
    analyze_json: dict[str, Any]
    edit_json: dict[str, Any] | None
    output_drive_file_id: str | None
    error: str | None


class QueueStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    drive_file_id TEXT NOT NULL UNIQUE,
                    filename TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    analyze_json TEXT NOT NULL,
                    edit_json TEXT NULL,
                    output_drive_file_id TEXT NULL,
                    error TEXT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_requests_status ON requests(status)")
        log(f"Очередь: БД {self.db_path}")

    def _row_to_request(self, r: sqlite3.Row) -> RequestRow:
        return RequestRow(
            id=int(r["id"]),
            drive_file_id=str(r["drive_file_id"]),
            filename=str(r["filename"]),
            status=str(r["status"]),  # type: ignore[assignment]
            created_at=int(r["created_at"]),
            updated_at=int(r["updated_at"]),
            analyze_json=json.loads(r["analyze_json"]),
            edit_json=json.loads(r["edit_json"]) if r["edit_json"] else None,
            output_drive_file_id=str(r["output_drive_file_id"]) if r["output_drive_file_id"] else None,
            error=str(r["error"]) if r["error"] else None,
        )

    def upsert_pending(self, *, drive_file_id: str, filename: str, analyze_json: dict[str, Any]) -> RequestRow:
        now = int(time.time())
        payload = json.dumps(analyze_json, ensure_ascii=False)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO requests(drive_file_id, filename, status, created_at, updated_at, analyze_json)
                VALUES (?, ?, 'pending_approval', ?, ?, ?)
                ON CONFLICT(drive_file_id) DO NOTHING
                """,
                (drive_file_id, filename, now, now, payload),
            )
            row = conn.execute(
                "SELECT * FROM requests WHERE drive_file_id = ?",
                (drive_file_id,),
            ).fetchone()
            assert row is not None
            req = self._row_to_request(row)
            log(f"Очередь: заявка #{req.id} «{filename}» → pending_approval")
            return req

    def list(self, *, status: Status | None = None, limit: int = 200) -> list[RequestRow]:
        q = "SELECT * FROM requests"
        params: tuple[Any, ...] = ()
        if status:
            q += " WHERE status = ?"
            params = (status,)
        q += " ORDER BY updated_at DESC LIMIT ?"
        params = (*params, int(limit))
        with self._connect() as conn:
            rows = conn.execute(q, params).fetchall()
        return [self._row_to_request(r) for r in rows]

    def get_by_drive_file_id(self, drive_file_id: str) -> RequestRow | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM requests WHERE drive_file_id = ?",
                (str(drive_file_id),),
            ).fetchone()
        return self._row_to_request(row) if row else None

    def get(self, request_id: int) -> RequestRow:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM requests WHERE id = ?", (int(request_id),)).fetchone()
        if not row:
            raise KeyError(f"request {request_id} not found")
        return self._row_to_request(row)

    def update_analyze(self, request_id: int, analyze_json: dict[str, Any]) -> RequestRow:
        now = int(time.time())
        with self._connect() as conn:
            conn.execute(
                "UPDATE requests SET analyze_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(analyze_json, ensure_ascii=False), now, int(request_id)),
            )
            row = conn.execute("SELECT * FROM requests WHERE id = ?", (int(request_id),)).fetchone()
            assert row is not None
            req = self._row_to_request(row)
            name = (analyze_json.get("student_name") or "").strip() or "—"
            log(f"Очередь: #{request_id} распознано ({name}), дисциплин={len(analyze_json.get('disciplines') or [])}, сертификатов={len(analyze_json.get('certificates') or [])}")
            return req

    def save_edit(self, request_id: int, edit_json: dict[str, Any]) -> RequestRow:
        now = int(time.time())
        with self._connect() as conn:
            conn.execute(
                "UPDATE requests SET edit_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(edit_json, ensure_ascii=False), now, int(request_id)),
            )
            row = conn.execute("SELECT * FROM requests WHERE id = ?", (int(request_id),)).fetchone()
            assert row is not None
            log(f"Очередь: #{request_id} черновик сохранён")
            return self._row_to_request(row)

    def mark_error(self, request_id: int, error: str) -> RequestRow:
        now = int(time.time())
        with self._connect() as conn:
            conn.execute(
                "UPDATE requests SET status = 'error', error = ?, updated_at = ? WHERE id = ?",
                (error[:2000], now, int(request_id)),
            )
            row = conn.execute("SELECT * FROM requests WHERE id = ?", (int(request_id),)).fetchone()
            assert row is not None
            log(f"Очередь: #{request_id} → error: {error[:120]}")
            return self._row_to_request(row)

    def reset_pending(self, request_id: int) -> RequestRow:
        """После повторного распознавания — снова ждёт апрува."""
        now = int(time.time())
        with self._connect() as conn:
            conn.execute(
                "UPDATE requests SET status = 'pending_approval', error = NULL, updated_at = ? WHERE id = ?",
                (now, int(request_id)),
            )
            row = conn.execute("SELECT * FROM requests WHERE id = ?", (int(request_id),)).fetchone()
            assert row is not None
            log(f"Очередь: #{request_id} → pending_approval (повторное распознавание)")
            return self._row_to_request(row)

    def mark_approved(self, request_id: int) -> RequestRow:
        now = int(time.time())
        with self._connect() as conn:
            conn.execute(
                "UPDATE requests SET status = 'approved', updated_at = ? WHERE id = ?",
                (now, int(request_id)),
            )
            row = conn.execute("SELECT * FROM requests WHERE id = ?", (int(request_id),)).fetchone()
            assert row is not None
            log(f"Очередь: #{request_id} → approved")
            return self._row_to_request(row)

    def mark_sent(self, request_id: int, *, output_drive_file_id: str | None) -> RequestRow:
        now = int(time.time())
        with self._connect() as conn:
            conn.execute(
                "UPDATE requests SET status = 'sent', output_drive_file_id = ?, updated_at = ? WHERE id = ?",
                (output_drive_file_id, now, int(request_id)),
            )
            row = conn.execute("SELECT * FROM requests WHERE id = ?", (int(request_id),)).fetchone()
            assert row is not None
            log(f"Очередь: #{request_id} → sent (drive_out={output_drive_file_id})")
            return self._row_to_request(row)

