"""按演示轮次生成的序号 ID（例如 REQ-0001、DEC-0001）。

运行时表在重置时清空，所以每轮演示的编号都从 0001 开始，便于演示和测试比对。
必须在 write_tx() 内调用（BEGIN IMMEDIATE 保证不会并发取到同一个号）。
种子记录使用非数字后缀（例如 RSV-SEED-01），不参与编号。
"""

from __future__ import annotations

import sqlite3

ARTIFACT_PREFIX = {
    "EVALUATION": "MEV",
    "DECISION": "DEC",
    "INSPECTION_TASK": "TASK",
    "EVIDENCE_PACKAGE": "EVP",
    "QUOTE": "QUO",
    "RESERVATION": "RSV",
}

# 表名和列名只来自这里的固定映射，不接受外部输入
_TARGETS = {
    "REQ": ("buyer_requests", "request_id"),
    **{prefix: ("artifacts", "artifact_id") for prefix in ARTIFACT_PREFIX.values()},
}


def _next(conn: sqlite3.Connection, prefix: str) -> str:
    table, column = _TARGETS[prefix]
    rows = conn.execute(
        f"SELECT {column} AS id FROM {table} WHERE {column} GLOB ?",  # noqa: S608 - 固定映射
        (f"{prefix}-[0-9]*",),
    ).fetchall()
    numbers = [int(r["id"][len(prefix) + 1:]) for r in rows if r["id"][len(prefix) + 1:].isdigit()]
    return f"{prefix}-{max(numbers, default=0) + 1:04d}"


def next_request_id(conn: sqlite3.Connection) -> str:
    return _next(conn, "REQ")


def next_artifact_id(conn: sqlite3.Connection, kind: str) -> str:
    return _next(conn, ARTIFACT_PREFIX[kind])
