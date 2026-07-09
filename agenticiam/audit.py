"""Append-only audit trail. Every auth decision and admin mutation is logged
here, mirroring Active Directory's Security event log."""

import json
import sqlite3
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(
    conn: sqlite3.Connection,
    action: str,
    result: str,
    actor_id: str = None,
    actor_name: str = None,
    resource: str = None,
    detail: dict = None,
) -> None:
    conn.execute(
        "INSERT INTO audit_log (ts, actor_id, actor_name, action, resource, result, detail) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            _now(),
            actor_id,
            actor_name,
            action,
            resource,
            result,
            json.dumps(detail) if detail is not None else None,
        ),
    )
    conn.commit()


def tail(conn: sqlite3.Connection, limit: int = 50) -> list:
    rows = conn.execute(
        "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(row) for row in rows]
