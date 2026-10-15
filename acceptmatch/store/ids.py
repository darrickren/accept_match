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


def _next_in_conditions(conn: sqlite3.Connection, key: str, prefix: str, count: int) -> list[str]:
    """在所有 Condition_Record 快照中查找已用编号（Evidence、缺陷保存在 points_json 里）。"""
    rows = conn.execute(
        "SELECT j.value AS id FROM condition_versions cv, json_tree(cv.points_json) j "
        "WHERE j.key = ? AND j.value GLOB ?",
        (key, f"{prefix}-[0-9]*"),
    ).fetchall()
    numbers = [int(r["id"][len(prefix) + 1:]) for r in rows if r["id"][len(prefix) + 1:].isdigit()]
    start = max(numbers, default=0) + 1
    return [f"{prefix}-{n:04d}" for n in range(start, start + count)]


def next_evidence_ids(conn: sqlite3.Connection, count: int) -> list[str]:
    """运行时 Evidence 编号（EV-0001 起）；种子编号如 EV-AM007-BACK-1 不参与。"""
    return _next_in_conditions(conn, "evidence_id", "EV", count)


def next_defect_ids(conn: sqlite3.Connection, count: int) -> list[str]:
    return _next_in_conditions(conn, "defect_id", "DF", count)


def next_delivery_check_id(conn: sqlite3.Connection) -> str:
    """交付核验编号（DLV-0001 起）。核验记录保存在预留记录的 content_json 中。"""
    rows = conn.execute(
        "SELECT j.value AS id FROM artifacts a, json_tree(a.content_json) j "
        "WHERE a.kind = 'RESERVATION' AND j.key = 'check_id' AND j.value GLOB 'DLV-[0-9]*'"
    ).fetchall()
    numbers = [int(r["id"][4:]) for r in rows if r["id"][4:].isdigit()]
    return f"DLV-{max(numbers, default=0) + 1:04d}"
