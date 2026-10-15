"""只读查询：meta、商品快照和有效预留。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from acceptmatch.domain.enums import DefectType, ItemStatus, Point, PointStatus, Severity
from acceptmatch.domain.models import DefectRecord, ItemSnapshot, PointState
from acceptmatch.store.db import from_cents, loads


@dataclass(frozen=True)
class Meta:
    epoch: int
    parameter_set: str
    seed_hash: str
    budget_initial: Decimal
    budget_remaining: Decimal
    budget_version: int
    reset_at: str


def get_meta(conn: sqlite3.Connection) -> Meta | None:
    row = conn.execute("SELECT * FROM meta WHERE id = 1").fetchone()
    if row is None:
        return None
    return Meta(
        epoch=row["epoch"],
        parameter_set=row["parameter_set"],
        seed_hash=row["seed_hash"],
        budget_initial=from_cents(row["budget_initial_cents"]),
        budget_remaining=from_cents(row["budget_remaining_cents"]),
        budget_version=row["budget_version"],
        reset_at=row["reset_at"],
    )


def snapshot_from_points(row: sqlite3.Row, points_json: str) -> ItemSnapshot:
    data = loads(points_json)
    points = {}
    for name, rec in data.items():
        defects = tuple(
            DefectRecord(d["defect_id"], DefectType(d["type"]), Severity(d["severity"])) for d in rec["defects"]
        )
        evidence_ids = tuple(rec["evidence_ids"]) + tuple(e for d in rec["defects"] for e in d["evidence_ids"])
        points[Point(name)] = PointState(PointStatus(rec["status"]), defects, evidence_ids)
    return ItemSnapshot(
        item_id=row["item_id"],
        product_id=row["product_id"],
        tag_code=row["tag_code"],
        item_status=ItemStatus(row["item_status"]),
        condition_version=row["condition_version"],
        points=points,
    )


_SNAPSHOT_SQL = (
    "SELECT i.*, cv.points_json FROM items i "
    "JOIN condition_versions cv ON cv.item_id = i.item_id AND cv.version = i.condition_version"
)


def item_snapshot(conn: sqlite3.Connection, item_id: str) -> ItemSnapshot:
    row = conn.execute(f"{_SNAPSHOT_SQL} WHERE i.item_id = ?", (item_id,)).fetchone()
    if row is None:
        raise KeyError(item_id)
    return snapshot_from_points(row, row["points_json"])


def list_item_snapshots(conn: sqlite3.Connection) -> list[ItemSnapshot]:
    rows = conn.execute(f"{_SNAPSHOT_SQL} ORDER BY i.item_id").fetchall()
    return [snapshot_from_points(r, r["points_json"]) for r in rows]


def condition_points(conn: sqlite3.Connection, item_id: str, version: int | None = None) -> dict[str, Any]:
    """某个版本（默认当前版本）的完整存储内容，包括 Evidence 记录。"""
    if version is None:
        version = conn.execute("SELECT condition_version FROM items WHERE item_id = ?", (item_id,)).fetchone()[0]
    row = conn.execute(
        "SELECT points_json FROM condition_versions WHERE item_id = ? AND version = ?", (item_id, version)
    ).fetchone()
    if row is None:
        raise KeyError((item_id, version))
    return loads(row["points_json"])


def active_reservations(conn: sqlite3.Connection, item_id: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM artifacts WHERE kind = 'RESERVATION' AND status = 'ACTIVE'"
    params: tuple = ()
    if item_id is not None:
        sql += " AND item_id = ?"
        params = (item_id,)
    rows = conn.execute(sql + " ORDER BY artifact_id", params).fetchall()
    return [{**dict(r), "content": loads(r["content_json"])} for r in rows]
