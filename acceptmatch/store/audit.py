"""只追加的操作记录（需求 13，design §5.2）。

- 事件编号按演示轮次（epoch）从 EVT-0001 开始。
- 表上有触发器禁止 UPDATE/DELETE；重置不会清空审计记录。
- 应在业务写入所在的同一事务中调用，这样操作完成就一定有记录。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from acceptmatch.store.db import dumps, loads

ACTOR_TYPES = frozenset({"operator", "buyer", "inspector", "delivery", "agent", "system"})


@dataclass(frozen=True)
class AuditEvent:
    epoch: int
    seq: int
    event_id: str
    ts: str
    actor_type: str
    actor_id: str
    event_type: str
    item_id: str | None
    request_id: str | None
    related_ids: list[str]
    payload: dict[str, Any]
    outcome: str


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def current_epoch(conn: sqlite3.Connection) -> int:
    """当前演示轮次；尚未初始化时为 0。"""
    row = conn.execute("SELECT epoch FROM meta WHERE id = 1").fetchone()
    return int(row["epoch"]) if row else 0


def append(
    conn: sqlite3.Connection,
    *,
    actor_type: str,
    actor_id: str,
    event_type: str,
    outcome: str = "OK",
    item_id: str | None = None,
    request_id: str | None = None,
    related_ids: Iterable[str] = (),
    payload: dict[str, Any] | None = None,
    epoch: int | None = None,
    ts: str | None = None,
) -> str:
    if actor_type not in ACTOR_TYPES:
        raise ValueError(f"未知的操作者类型：{actor_type}")
    epoch = current_epoch(conn) if epoch is None else epoch
    seq = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 FROM audit_events WHERE epoch = ?", (epoch,)
    ).fetchone()[0]
    event_id = f"EVT-{seq:04d}"
    conn.execute(
        "INSERT INTO audit_events (epoch, seq, event_id, ts, actor_type, actor_id, event_type, item_id, "
        "request_id, related_ids_json, payload_json, outcome) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            epoch, seq, event_id, ts or utc_now(), actor_type, actor_id, event_type, item_id, request_id,
            dumps(list(related_ids)), dumps(payload or {}), outcome,
        ),
    )
    return event_id


def _row_to_event(row: sqlite3.Row) -> AuditEvent:
    return AuditEvent(
        epoch=row["epoch"],
        seq=row["seq"],
        event_id=row["event_id"],
        ts=row["ts"],
        actor_type=row["actor_type"],
        actor_id=row["actor_id"],
        event_type=row["event_type"],
        item_id=row["item_id"],
        request_id=row["request_id"],
        related_ids=loads(row["related_ids_json"]),
        payload=loads(row["payload_json"]),
        outcome=row["outcome"],
    )


def list_events(
    conn: sqlite3.Connection,
    *,
    epoch: int | None = None,
    item_id: str | None = None,
    request_id: str | None = None,
    event_type: str | None = None,
) -> list[AuditEvent]:
    """按时间顺序（epoch、seq）列出事件。item_id、request_id 同时匹配 related_ids 中出现的编号。"""
    clauses, params = [], []
    if epoch is not None:
        clauses.append("epoch = ?")
        params.append(epoch)
    if item_id is not None:
        clauses.append("(item_id = ? OR EXISTS (SELECT 1 FROM json_each(related_ids_json) WHERE value = ?))")
        params.extend([item_id, item_id])
    if request_id is not None:
        clauses.append("(request_id = ? OR EXISTS (SELECT 1 FROM json_each(related_ids_json) WHERE value = ?))")
        params.extend([request_id, request_id])
    if event_type is not None:
        clauses.append("event_type = ?")
        params.append(event_type)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(f"SELECT * FROM audit_events {where} ORDER BY epoch, seq", params).fetchall()
    return [_row_to_event(r) for r in rows]
