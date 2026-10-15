"""演示重置（需求 1.1、1.6，tasks.md 任务 4.3）。

流程：
1. 先加载并校验种子；失败时数据库保持不变，只追加一条拒绝审计。
2. 在一个 BEGIN IMMEDIATE 事务内：epoch 加一；清空 audit_events 以外的表；写入十件商品、
   版本 1 的 Condition_Record、AM-006 的种子预留和预算；运行自检；写审计 DEMO_RESET。
3. 自检或任何一步失败都会回滚，旧状态保持不变。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable

from acceptmatch.domain.enums import ItemStatus, Point, PointStatus
from acceptmatch.seed import ResolvedEconomics, SeedBundle, SeedValidationError, load_seed
from acceptmatch.seed import convert
from acceptmatch.store import audit, repo
from acceptmatch.store.audit import utc_now
from acceptmatch.store.db import cents, dumps, write_tx

DEMO_OPERATOR = "SYN-OPERATOR-01"
SeedLoader = Callable[[], SeedBundle]


class ResetError(Exception):
    code = "RESET_FAILED"


class ResetSelfCheckError(ResetError):
    code = "SELF_CHECK_FAILED"

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__(f"{self.code}: " + "；".join(problems))


@dataclass(frozen=True)
class ResetResult:
    epoch: int
    parameter_set: str
    seed_hash: str
    budget_initial: Decimal
    checks: tuple[str, ...]
    bundle: SeedBundle


@dataclass(frozen=True)
class InitStatus:
    did_reset: bool
    epoch: int
    seed_hash_matches: bool
    bundle: SeedBundle


def reset_demo(
    conn: sqlite3.Connection,
    parameter_set: str = "default",
    operator_id: str = DEMO_OPERATOR,
    *,
    loader: SeedLoader = load_seed,
    now: str | None = None,
) -> ResetResult:
    result = _reset(conn, parameter_set, operator_id, loader=loader, now=now, only_if_uninitialized=False)
    assert result is not None
    return result


def ensure_initialized(conn: sqlite3.Connection, *, loader: SeedLoader = load_seed) -> InitStatus:
    """首次启动（还没有 meta）时按默认参数组重置；否则只核对种子是否在上次重置后被修改。"""
    meta = repo.get_meta(conn)
    if meta is None:
        result = _reset(conn, "default", DEMO_OPERATOR, loader=loader, now=None, only_if_uninitialized=True)
        if result is not None:
            return InitStatus(did_reset=True, epoch=result.epoch, seed_hash_matches=True, bundle=result.bundle)
        meta = repo.get_meta(conn)  # 另一个会话刚刚完成了初始化
        assert meta is not None
    bundle = loader()
    return InitStatus(
        did_reset=False,
        epoch=meta.epoch,
        seed_hash_matches=bundle.seed_hash == meta.seed_hash,
        bundle=bundle,
    )


# ---------------------------------------------------------------- 内部


def _reset(
    conn: sqlite3.Connection,
    parameter_set: str,
    operator_id: str,
    *,
    loader: SeedLoader,
    now: str | None,
    only_if_uninitialized: bool,
) -> ResetResult | None:
    ts = now or utc_now()
    try:
        bundle = loader()
    except SeedValidationError as exc:
        _record_rejection(conn, operator_id, parameter_set, exc.code,
                          {"problem_count": len(exc.problems), "problems": exc.problems[:20]}, ts)
        raise
    try:
        econ = bundle.parameter_set(parameter_set)
    except ValueError:
        _record_rejection(conn, operator_id, parameter_set, "UNKNOWN_PARAMETER_SET",
                          {"available": list(bundle.parameter_sets)}, ts)
        raise

    try:
        with write_tx(conn):
            if only_if_uninitialized and repo.get_meta(conn) is not None:
                return None
            epoch = _next_epoch(conn)
            _clear_runtime(conn)
            _insert_items(conn, bundle, ts)
            reservation_ids = _insert_reservations(conn, bundle, ts)
            _insert_meta(conn, epoch, parameter_set, bundle.seed_hash, econ.budget_initial, ts)
            checks = _self_check(conn, bundle, econ)
            audit.append(
                conn,
                actor_type="operator",
                actor_id=operator_id,
                event_type="DEMO_RESET",
                related_ids=[*reservation_ids, *(r.item_id for r in bundle.dataset.initial_reservations)],
                payload={
                    "epoch": epoch,
                    "parameter_set": parameter_set,
                    "seed_hash": bundle.seed_hash,
                    "item_count": len(bundle.dataset.items),
                    "budget_initial": econ.budget_initial,
                    "checks": checks,
                    "synthetic": True,
                },
                epoch=epoch,
                ts=ts,
            )
    except ResetSelfCheckError as exc:
        _record_rejection(conn, operator_id, parameter_set, exc.code, {"problems": exc.problems}, ts)
        raise
    except Exception as exc:
        _record_rejection(conn, operator_id, parameter_set, "INTERNAL_ERROR", {"error": type(exc).__name__}, ts)
        raise

    return ResetResult(
        epoch=epoch,
        parameter_set=parameter_set,
        seed_hash=bundle.seed_hash,
        budget_initial=econ.budget_initial,
        checks=tuple(checks),
        bundle=bundle,
    )


def _record_rejection(
    conn: sqlite3.Connection, operator_id: str, parameter_set: str, outcome: str, payload: dict, ts: str
) -> None:
    """在单独的事务中记录被拒绝的重置；记录失败不掩盖原始错误。"""
    try:
        with write_tx(conn):
            audit.append(
                conn,
                actor_type="operator",
                actor_id=operator_id,
                event_type="DEMO_RESET_REJECTED",
                outcome=outcome,
                payload={"parameter_set": parameter_set, **payload},
                ts=ts,
            )
    except sqlite3.Error:
        pass


def _next_epoch(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT MAX(COALESCE((SELECT epoch FROM meta WHERE id = 1), 0), "
        "COALESCE((SELECT MAX(epoch) FROM audit_events), 0))"
    ).fetchone()
    return int(row[0]) + 1


def _clear_runtime(conn: sqlite3.Connection) -> None:
    # 按外键依赖顺序删除；audit_events 不清空
    for table in ("artifacts", "buyer_requests", "condition_versions", "items", "meta"):
        conn.execute(f"DELETE FROM {table}")  # noqa: S608 - 固定表名


def _insert_items(conn: sqlite3.Connection, bundle: SeedBundle, ts: str) -> None:
    meta = bundle.dataset.meta
    source_ref = f"seed:{meta.dataset_id}@v{meta.version}"
    for item in bundle.dataset.items:
        conn.execute(
            "INSERT INTO items (item_id, product_id, tag_code, item_status, condition_version, synthetic) "
            "VALUES (?, ?, ?, ?, 1, 1)",
            (item.item_id, item.product_id, item.tag_code, item.item_status.value),
        )
        conn.execute(
            "INSERT INTO condition_versions (item_id, version, points_json, source_ref, created_at, synthetic) "
            "VALUES (?, 1, ?, ?, ?, 1)",
            (item.item_id, dumps(convert.condition_payload(item)), source_ref, ts),
        )


def _insert_reservations(conn: sqlite3.Connection, bundle: SeedBundle, ts: str) -> list[str]:
    ids = []
    for r in bundle.dataset.initial_reservations:
        conn.execute(
            "INSERT INTO artifacts (artifact_id, kind, request_id, item_id, condition_version, status, natural_key, "
            "content_json, created_at, synthetic) VALUES (?, 'RESERVATION', NULL, ?, 1, 'ACTIVE', ?, ?, ?, 1)",
            (
                r.reservation_id,
                r.item_id,
                f"seed:{r.reservation_id}",
                dumps({"buyer_id": r.buyer_id, "source": "SEED", "quote_id": None, "synthetic": True}),
                ts,
            ),
        )
        ids.append(r.reservation_id)
    return ids


def _insert_meta(
    conn: sqlite3.Connection, epoch: int, parameter_set: str, seed_hash: str, budget: Decimal, ts: str
) -> None:
    amount = cents(budget)
    conn.execute(
        "INSERT INTO meta (id, epoch, parameter_set, seed_hash, budget_initial_cents, budget_remaining_cents, "
        "budget_version, reset_at) VALUES (1, ?, ?, ?, ?, ?, 0, ?)",
        (epoch, parameter_set, seed_hash, amount, amount, ts),
    )


def _self_check(conn: sqlite3.Connection, bundle: SeedBundle, econ: ResolvedEconomics) -> list[str]:
    problems: list[str] = []
    checks: list[str] = []

    snapshots = {s.item_id: s for s in repo.list_item_snapshots(conn)}
    expected_ids = [i.item_id for i in bundle.dataset.items]
    if sorted(snapshots) != sorted(expected_ids) or len(snapshots) != 10:
        problems.append(f"商品数量或编号不对：{sorted(snapshots)}")
    for item in bundle.dataset.items:
        stored = snapshots.get(item.item_id)
        if stored is None or stored != convert.item_snapshot(item):
            problems.append(f"{item.item_id} 写入后的快照与种子不一致")
    checks.append(f"{len(snapshots)} 件合成商品，状态版本均为 1")

    for r in bundle.dataset.initial_reservations:
        active = repo.active_reservations(conn, r.item_id)
        stored = snapshots.get(r.item_id)
        if stored is None or stored.item_status is not ItemStatus.RESERVED:
            problems.append(f"{r.item_id} 不是 RESERVED")
        if len(active) != 1 or active[0]["content"]["buyer_id"] != r.buyer_id:
            problems.append(f"{r.item_id} 应有且只有一个属于 {r.buyer_id} 的有效预留")

    am6 = snapshots.get("AM-006")
    am6_active = repo.active_reservations(conn, "AM-006")
    if am6 is None or am6.item_status is not ItemStatus.RESERVED or [
        a["content"]["buyer_id"] for a in am6_active
    ] != ["B-DEMO-02"]:
        problems.append("AM-006 应为 RESERVED，并且只有一个属于 B-DEMO-02 的有效预留")
    else:
        checks.append("AM-006 已预留给 B-DEMO-02")

    am7 = snapshots.get("AM-007")
    if am7 is None or am7.item_status is not ItemStatus.AVAILABLE or am7.point(Point.ZIPPER).status is not PointStatus.UNKNOWN:
        problems.append("AM-007 应为 AVAILABLE，且拉链为 UNKNOWN")
    else:
        checks.append("AM-007 可售，拉链未核实")

    meta = repo.get_meta(conn)
    if meta is None or meta.budget_remaining != econ.budget_initial or meta.budget_initial != econ.budget_initial \
            or meta.budget_version != 0:
        problems.append("预算没有按参数组初始化")
    else:
        checks.append(f"检查预算 {econ.budget_initial}（参数组 {econ.parameter_set}）")

    leftovers = conn.execute(
        "SELECT (SELECT COUNT(*) FROM buyer_requests), "
        "(SELECT COUNT(*) FROM artifacts WHERE NOT (kind = 'RESERVATION' AND natural_key LIKE 'seed:%'))"
    ).fetchone()
    if tuple(leftovers) != (0, 0):
        problems.append("重置后仍有运行时记录")

    if problems:
        raise ResetSelfCheckError(problems)
    return checks
