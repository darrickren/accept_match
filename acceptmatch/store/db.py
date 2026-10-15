"""SQLite 连接、事务与状态哈希（design §5.1）。

- 每个操作打开一个短生命周期连接：WAL、foreign_keys=ON、busy_timeout=5000。
- 写操作用 write_tx()：BEGIN IMMEDIATE，异常时回滚。
- state_hash() 对业务表做规范化摘要（不含时间戳、epoch 和审计），用于验证
  "重置结果可复现"和"被拒操作不改变状态"。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Iterator

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
SCHEMA_VERSION = 1
BUSY_TIMEOUT_MS = 5000

TABLES = ("meta", "items", "condition_versions", "buyer_requests", "artifacts", "audit_events")


def connect(db_path: Path | str) -> sqlite3.Connection:
    """打开连接（自动提交模式，事务由 write_tx 显式控制），并确保表结构存在。"""
    if str(db_path) != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    # Strands 在工作线程中执行工具，所以允许跨线程使用同一连接；
    # 一次 Agent 运行内工具按顺序执行（SequentialToolExecutor），不会并发使用同一连接
    conn = sqlite3.connect(str(db_path), timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    if str(db_path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    init_schema(conn)
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version == SCHEMA_VERSION:
        return
    if version not in (0, SCHEMA_VERSION):
        raise RuntimeError(f"数据库结构版本 {version} 与当前代码（{SCHEMA_VERSION}）不一致，请删除 var/ 后重新启动")
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))


@contextmanager
def write_tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


# ------------------------------------------------------------------ JSON


def _default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    raise TypeError(f"无法序列化 {type(value).__name__}")


def dumps(value: Any) -> str:
    """规范化 JSON：键排序、保留中文、金额 Decimal 序列化为字符串。"""
    return json.dumps(value, default=_default, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def loads(text: str | None) -> Any:
    return None if text is None else json.loads(text)


def cents(amount: Decimal) -> int:
    value = amount * 100
    if value != value.to_integral_value():
        raise ValueError(f"金额 {amount} 超过两位小数")
    return int(value)


def from_cents(value: int) -> Decimal:
    return (Decimal(value) / 100).quantize(Decimal("0.01"))


# ------------------------------------------------------------- 状态哈希

_HASH_QUERIES = {
    "meta": "SELECT parameter_set, seed_hash, budget_initial_cents, budget_remaining_cents, budget_version FROM meta",
    "items": "SELECT * FROM items ORDER BY item_id",
    "condition_versions": "SELECT item_id, version, points_json, source_ref, synthetic "
                          "FROM condition_versions ORDER BY item_id, version",
    "buyer_requests": "SELECT request_id, buyer_id, product_id, accepted_defects_json, required_conditions_json, "
                      "status, exclusions_json, no_match_json, synthetic FROM buyer_requests ORDER BY request_id",
    "artifacts": "SELECT artifact_id, kind, request_id, item_id, condition_version, status, natural_key, "
                 "content_json, synthetic FROM artifacts ORDER BY artifact_id",
}


def state_hash(conn: sqlite3.Connection) -> str:
    """业务状态摘要。不含 epoch、时间戳和 audit_events。"""
    snapshot = {name: [dict(row) for row in conn.execute(sql)] for name, sql in _HASH_QUERIES.items()}
    return hashlib.sha256(dumps(snapshot).encode()).hexdigest()
