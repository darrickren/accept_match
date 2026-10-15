"""任务 4：精简存储、原子重置与操作记录。

- 六张表与关键约束：预算非负、有效预留唯一、未完成任务唯一、审计只追加、合成标记
- 原子重置：失败时旧状态不变；两次重置状态哈希相同
- 初始 AM-006 预留、AM-007 拉链未核实
- 审计记录跨演示轮次保留
"""

from __future__ import annotations

import copy
import json
import sqlite3
from decimal import Decimal

import pytest

from acceptmatch.config import ALLOWED_SEED_ROOT
from acceptmatch.domain.enums import ItemStatus, Point, PointStatus
from acceptmatch.seed import SeedValidationError, load_seed, parse_seed
from acceptmatch.seed import convert
from acceptmatch.services import reset as reset_module
from acceptmatch.services.reset import ResetSelfCheckError, ensure_initialized, reset_demo
from acceptmatch.store import audit, ids, repo
from acceptmatch.store.db import TABLES, cents, connect, dumps, from_cents, state_hash, write_tx

NOW = "2026-10-15T12:00:00+00:00"


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "acceptmatch.db")
    yield c
    c.close()


@pytest.fixture()
def ready(conn):
    reset_demo(conn, now=NOW)
    return conn


def _insert_request(conn, request_id="REQ-0001"):
    conn.execute(
        "INSERT INTO buyer_requests (request_id, buyer_id, product_id, accepted_defects_json, "
        "required_conditions_json, status, created_at, synthetic) VALUES (?, 'B-DEMO-01', 'SYN-BAG-A', '[]', '[]', "
        "'SUBMITTED', ?, 1)",
        (request_id, NOW),
    )


def _insert_artifact(conn, artifact_id, kind, item_id, status, natural_key, request_id=None):
    conn.execute(
        "INSERT INTO artifacts (artifact_id, kind, request_id, item_id, condition_version, status, natural_key, "
        "content_json, created_at, synthetic) VALUES (?, ?, ?, ?, 1, ?, ?, '{}', ?, 1)",
        (artifact_id, kind, request_id, item_id, status, natural_key, NOW),
    )


def _raw_seed():
    ds = json.loads((ALLOWED_SEED_ROOT / "dataset.json").read_text(encoding="utf-8"))
    econ = json.loads((ALLOWED_SEED_ROOT / "economics.json").read_text(encoding="utf-8"))
    return ds, econ


# ------------------------------------------------------------ 表结构


def test_six_tables_and_pragmas(conn):
    names = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    assert names == sorted(TABLES) and len(names) == 6
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1


def test_reconnect_keeps_schema_and_data(tmp_path):
    path = tmp_path / "acceptmatch.db"
    first = connect(path)
    reset_demo(first, now=NOW)
    h = state_hash(first)
    first.close()
    second = connect(path)
    assert state_hash(second) == h and repo.get_meta(second).epoch == 1
    second.close()


# ------------------------------------------------------------ 初始状态


def test_reset_initial_state(ready):
    seed = load_seed()
    meta = repo.get_meta(ready)
    assert (meta.epoch, meta.parameter_set, meta.seed_hash) == (1, "default", seed.seed_hash)
    assert (meta.budget_initial, meta.budget_remaining, meta.budget_version) == (Decimal("20.00"), Decimal("20.00"), 0)
    assert meta.reset_at == NOW

    snapshots = {s.item_id: s for s in repo.list_item_snapshots(ready)}
    assert list(snapshots) == [f"AM-{n:03d}" for n in range(1, 11)]
    for item in seed.dataset.items:
        assert snapshots[item.item_id] == convert.item_snapshot(item)  # 存储往返无损

    am6 = snapshots["AM-006"]
    assert am6.item_status is ItemStatus.RESERVED
    active = repo.active_reservations(ready)
    assert [(a["artifact_id"], a["item_id"], a["content"]["buyer_id"]) for a in active] == [
        ("RSV-SEED-01", "AM-006", "B-DEMO-02")
    ]

    am7 = snapshots["AM-007"]
    assert am7.item_status is ItemStatus.AVAILABLE
    assert am7.point(Point.ZIPPER).status is PointStatus.UNKNOWN
    assert am7.point(Point.FRONT).status is PointStatus.OK
    stored = repo.condition_points(ready, "AM-007")
    assert stored["ZIPPER"]["evidence"] == [] and stored["BACK"]["evidence"][0]["photo_ref"].startswith("synthetic://")
    assert all(e["synthetic"] for p in stored.values() for e in p["evidence"])

    assert ready.execute("SELECT COUNT(*) FROM buyer_requests").fetchone()[0] == 0
    assert ready.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 1


def test_reset_audit_event(ready):
    events = audit.list_events(ready)
    assert [(e.epoch, e.event_id, e.event_type, e.outcome, e.actor_type) for e in events] == [
        (1, "EVT-0001", "DEMO_RESET", "OK", "operator")
    ]
    payload = events[0].payload
    assert payload["parameter_set"] == "default" and payload["budget_initial"] == "20.00"
    assert payload["item_count"] == 10 and payload["synthetic"] is True
    assert "AM-006 已预留给 B-DEMO-02" in payload["checks"]
    assert [e.event_id for e in audit.list_events(ready, item_id="AM-006")] == ["EVT-0001"]


@pytest.mark.parametrize("parameter_set,budget", [("default", "20.00"), ("budget_low", "5.00"), ("low_pass", "20.00")])
def test_parameter_set_budget(conn, parameter_set, budget):
    result = reset_demo(conn, parameter_set, now=NOW)
    meta = repo.get_meta(conn)
    assert meta.parameter_set == parameter_set and result.parameter_set == parameter_set
    assert meta.budget_initial == meta.budget_remaining == Decimal(budget)


# ------------------------------------------------- 重复重置与审计保留


def test_two_resets_same_state_and_audit_kept(ready):
    h1 = state_hash(ready)
    reset_demo(ready, now="2026-10-15T13:00:00+00:00")
    assert state_hash(ready) == h1
    assert repo.get_meta(ready).epoch == 2
    events = audit.list_events(ready, event_type="DEMO_RESET")
    assert [(e.epoch, e.event_id) for e in events] == [(1, "EVT-0001"), (2, "EVT-0001")]


def test_reset_clears_runtime_data_but_keeps_audit(ready):
    with write_tx(ready):
        _insert_request(ready)
        _insert_artifact(ready, "QUO-0001", "QUOTE", "AM-007", "ACTIVE", "q1", request_id="REQ-0001")
        _insert_artifact(ready, "TASK-0001", "INSPECTION_TASK", "AM-007", "OPEN", "t1", request_id="REQ-0001")
        ready.execute(
            "INSERT INTO condition_versions (item_id, version, points_json, source_ref, created_at, synthetic) "
            "SELECT item_id, 2, points_json, 'test', ?, 1 FROM condition_versions WHERE item_id = 'AM-007'", (NOW,))
        ready.execute("UPDATE items SET condition_version = 2 WHERE item_id = 'AM-007'")
        ready.execute("UPDATE meta SET budget_remaining_cents = 1200, budget_version = 1")
        audit.append(ready, actor_type="agent", actor_id="RUN-0001", event_type="TEST_EVENT", item_id="AM-007")

    reset_demo(ready, now=NOW)
    assert ready.execute("SELECT COUNT(*) FROM buyer_requests").fetchone()[0] == 0
    assert [r[0] for r in ready.execute("SELECT artifact_id FROM artifacts")] == ["RSV-SEED-01"]
    assert repo.item_snapshot(ready, "AM-007").condition_version == 1
    meta = repo.get_meta(ready)
    assert (meta.budget_remaining, meta.budget_version, meta.epoch) == (Decimal("20.00"), 0, 2)
    kept = audit.list_events(ready, epoch=1)
    assert [e.event_type for e in kept] == ["DEMO_RESET", "TEST_EVENT"]


def test_ids_restart_each_epoch(ready):
    with write_tx(ready):
        assert ids.next_request_id(ready) == "REQ-0001"
        _insert_request(ready, "REQ-0001")
        assert ids.next_request_id(ready) == "REQ-0002"
        assert ids.next_artifact_id(ready, "RESERVATION") == "RSV-0001"  # 种子编号 RSV-SEED-01 不参与
        assert ids.next_artifact_id(ready, "DECISION") == "DEC-0001"
    reset_demo(ready, now=NOW)
    with write_tx(ready):
        assert ids.next_request_id(ready) == "REQ-0001"
        assert audit.append(ready, actor_type="system", actor_id="test", event_type="X") == "EVT-0002"


# ------------------------------------------------------------ 原子性


def test_unknown_parameter_set_leaves_state_unchanged(ready):
    h = state_hash(ready)
    with pytest.raises(ValueError):
        reset_demo(ready, "nope", now=NOW)
    assert state_hash(ready) == h and repo.get_meta(ready).epoch == 1
    rejected = audit.list_events(ready, event_type="DEMO_RESET_REJECTED")
    assert [(e.epoch, e.outcome) for e in rejected] == [(1, "UNKNOWN_PARAMETER_SET")]


def test_invalid_seed_leaves_state_unchanged(ready):
    with write_tx(ready):
        _insert_request(ready)
    h = state_hash(ready)

    def bad_loader():
        raise SeedValidationError(["dataset.json: items[AM-002]: tag_code 重复：SYN-T-K3P9"])

    with pytest.raises(SeedValidationError):
        reset_demo(ready, loader=bad_loader, now=NOW)
    assert state_hash(ready) == h and repo.get_meta(ready).epoch == 1
    rejected = audit.list_events(ready, event_type="DEMO_RESET_REJECTED")
    assert rejected[-1].outcome == "SEED_INVALID" and rejected[-1].payload["problem_count"] == 1


def test_failure_midway_rolls_back(ready, monkeypatch):
    with write_tx(ready):
        _insert_request(ready)
        ready.execute("UPDATE meta SET budget_remaining_cents = 1200, budget_version = 1")
    h = state_hash(ready)

    def boom(*args, **kwargs):
        raise RuntimeError("模拟写入失败")

    monkeypatch.setattr(reset_module, "_insert_reservations", boom)  # 此时商品已被清空并重新写入
    with pytest.raises(RuntimeError):
        reset_demo(ready, now=NOW)
    assert state_hash(ready) == h
    assert repo.get_meta(ready).epoch == 1 and repo.get_meta(ready).budget_remaining == Decimal("12.00")
    assert ready.execute("SELECT COUNT(*) FROM buyer_requests").fetchone()[0] == 1
    assert audit.list_events(ready, event_type="DEMO_RESET_REJECTED")[-1].outcome == "INTERNAL_ERROR"


def test_self_check_failure_rolls_back(ready):
    ds, econ = _raw_seed()
    ds = copy.deepcopy(ds)
    ds["initial_reservations"][0]["buyer_id"] = "B-DEMO-01"  # 种子校验能通过，但不符合演示设定
    bundle = parse_seed(json.dumps(ds).encode(), json.dumps(econ).encode())
    h = state_hash(ready)
    with pytest.raises(ResetSelfCheckError) as exc:
        reset_demo(ready, loader=lambda: bundle, now=NOW)
    assert any("B-DEMO-02" in p for p in exc.value.problems)
    assert state_hash(ready) == h and repo.get_meta(ready).epoch == 1
    assert audit.list_events(ready, event_type="DEMO_RESET_REJECTED")[-1].outcome == "SELF_CHECK_FAILED"


def test_rejection_before_first_reset_is_epoch_zero(conn):
    with pytest.raises(ValueError):
        reset_demo(conn, "nope", now=NOW)
    assert repo.get_meta(conn) is None
    assert [(e.epoch, e.event_id) for e in audit.list_events(conn)] == [(0, "EVT-0001")]
    reset_demo(conn, now=NOW)
    assert repo.get_meta(conn).epoch == 1  # 拒绝记录在第 0 轮，不影响后续编号


# ------------------------------------------------------------ 首次启动


def test_ensure_initialized(conn):
    first = ensure_initialized(conn)
    assert (first.did_reset, first.epoch, first.seed_hash_matches) == (True, 1, True)
    second = ensure_initialized(conn)
    assert (second.did_reset, second.epoch, second.seed_hash_matches) == (False, 1, True)

    ds, econ = _raw_seed()
    ds["meta"]["notice"] += "（已修改）"
    changed = parse_seed(json.dumps(ds).encode(), json.dumps(econ).encode())
    third = ensure_initialized(conn, loader=lambda: changed)
    assert (third.did_reset, third.seed_hash_matches) == (False, False)
    assert len(audit.list_events(conn, event_type="DEMO_RESET")) == 1


# ------------------------------------------------------------ 约束


def test_budget_cannot_go_negative_or_above_initial(ready):
    with pytest.raises(sqlite3.IntegrityError):
        with write_tx(ready):
            ready.execute("UPDATE meta SET budget_remaining_cents = budget_remaining_cents - 2001")
    with pytest.raises(sqlite3.IntegrityError):
        with write_tx(ready):
            ready.execute("UPDATE meta SET budget_remaining_cents = 2001")
    with write_tx(ready):
        ready.execute("UPDATE meta SET budget_remaining_cents = 0")  # 正好用完是允许的
    assert repo.get_meta(ready).budget_remaining == Decimal("0.00")


def test_only_one_active_reservation_per_item(ready):
    with pytest.raises(sqlite3.IntegrityError):
        with write_tx(ready):
            _insert_artifact(ready, "RSV-0001", "RESERVATION", "AM-006", "ACTIVE", "r1")
    with write_tx(ready):
        _insert_artifact(ready, "RSV-0001", "RESERVATION", "AM-007", "ACTIVE", "r1")  # 其他商品不受影响
        ready.execute("UPDATE artifacts SET status = 'FULFILLED' WHERE artifact_id = 'RSV-SEED-01'")
        _insert_artifact(ready, "RSV-0002", "RESERVATION", "AM-006", "ACTIVE", "r2")  # 旧预留完成后可以再预留
    assert [a["artifact_id"] for a in repo.active_reservations(ready)] == ["RSV-0001", "RSV-0002"]


def test_only_one_open_inspection_task_per_item(ready):
    with write_tx(ready):
        _insert_artifact(ready, "TASK-0001", "INSPECTION_TASK", "AM-007", "OPEN", "t1")
    with pytest.raises(sqlite3.IntegrityError):
        with write_tx(ready):
            _insert_artifact(ready, "TASK-0002", "INSPECTION_TASK", "AM-007", "OPEN", "t2")
    with write_tx(ready):
        ready.execute("UPDATE artifacts SET status = 'COMPLETED' WHERE artifact_id = 'TASK-0001'")
        _insert_artifact(ready, "TASK-0002", "INSPECTION_TASK", "AM-007", "OPEN", "t2")


def test_natural_key_unique_per_kind(ready):
    with pytest.raises(sqlite3.IntegrityError):
        with write_tx(ready):
            _insert_artifact(ready, "DEC-0001", "DECISION", "AM-007", "RECORDED", "same")
            _insert_artifact(ready, "DEC-0002", "DECISION", "AM-007", "RECORDED", "same")


def test_audit_events_are_append_only(ready):
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        ready.execute("UPDATE audit_events SET outcome = 'X'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        ready.execute("DELETE FROM audit_events")
    assert len(audit.list_events(ready)) == 1


def test_condition_versions_are_immutable(ready):
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        ready.execute("UPDATE condition_versions SET points_json = '{}' WHERE item_id = 'AM-007'")


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO items VALUES ('AM-099', 'SYN-BAG-A', 'SYN-T-ZZZZ', 'AVAILABLE', 1, 0)",       # 合成标记
        "INSERT INTO items VALUES ('AM-099', 'SYN-BAG-A', 'SYN-T-ZZZZ', 'LOST', 1, 1)",           # 非法状态
        "INSERT INTO items VALUES ('AM-099', 'SYN-BAG-A', 'SYN-T-K3P9', 'AVAILABLE', 1, 1)",      # 标签码重复
        "INSERT INTO meta VALUES (2, 1, 'default', 'h', 0, 0, 0, 'now')",                         # meta 只有一行
        "INSERT INTO artifacts VALUES ('RSV-0009', 'RESERVATION', NULL, 'AM-007', 1, 'OPEN', 'k', '{}', 'now', 1)",
        "INSERT INTO artifacts VALUES ('QUO-0009', 'QUOTE', NULL, 'AM-404', 1, 'ACTIVE', 'k', '{}', 'now', 1)",  # 外键
        "INSERT INTO artifacts VALUES ('QUO-0009', 'QUOTE', NULL, NULL, 1, 'ACTIVE', 'k', '{}', 'now', 1)",     # 必须有商品
        "INSERT INTO artifacts VALUES ('DEC-0009', 'DECISION', NULL, 'AM-007', 1, 'RECORDED', 'k', 'not json', 'now', 1)",
        "INSERT INTO buyer_requests (request_id, buyer_id, product_id, accepted_defects_json, required_conditions_json,"
        " status, created_at, synthetic) VALUES ('REQ-0009', 'B', 'P', '[]', '[]', 'UNKNOWN', 'now', 1)",
    ],
)
def test_constraints_reject_invalid_rows(ready, sql):
    with pytest.raises(sqlite3.IntegrityError):
        with write_tx(ready):
            ready.execute(sql)


def test_cents_helpers():
    assert cents(Decimal("20.00")) == 2000 and from_cents(1200) == Decimal("12.00")
    with pytest.raises(ValueError):
        cents(Decimal("8.005"))
    assert dumps({"b": Decimal("1.50"), "a": Point.ZIPPER}) == '{"a":"ZIPPER","b":"1.50"}'
