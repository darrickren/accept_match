"""任务 9：交付时核验是同一件商品（需求 11、14.4、14.6；design §11.1 属性 11）。"""

from __future__ import annotations

import pytest

from acceptmatch.store import audit, repo
from acceptmatch.tools import delivery_ops as do
from acceptmatch.tools import state as st
from acceptmatch.tools.envelope import Actor
from tests.test_tools import confirm, make_runtime, to_offer, unchanged

OPERATOR = "SYN-DELIVERY-01"
ALL_MATCH = [{"point": p, "matches": True} for p in ("FRONT", "BACK", "ZIPPER")]


@pytest.fixture()
def reserved(tmp_path):
    rt = make_runtime(tmp_path)
    rid, quote = to_offer(rt)
    res = confirm(rt, rid, quote)
    assert res.ok
    yield rt, rid, res.data["reservation_id"]
    rt.conn.close()


def scan(rt, reservation_id, tag, **kwargs):
    return do.start_delivery_check(rt, reservation_id, OPERATOR, tag, **kwargs)


def finish(rt, reservation_id, check_id, confirmations=ALL_MATCH, **kwargs):
    return do.complete_delivery_check(rt, reservation_id, check_id, OPERATOR, confirmations, **kwargs)


def statuses(rt, rid, reservation_id):
    return (repo.item_snapshot(rt.conn, "AM-007").item_status.value, st.get_artifact(rt.conn, reservation_id).status,
            st.load_request(rt.conn, rid).status.value)


def test_correct_tag_and_matching_evidence_delivers(reserved):
    rt, rid, rsv = reserved
    started = scan(rt, rsv, "SYN-T-Q5ZT")
    assert started.ok and started.data["check_id"] == "DLV-0001" and started.data["condition_version"] == 2
    points = {p["point"]: p for p in started.data["points"]}
    assert points["BACK"]["defects"][0]["defect_id"] == "DF-AM007-BACK-1"
    assert points["ZIPPER"]["evidence"][0]["inspection_task_id"] == "TASK-0001"  # 证据来自买家确认时的版本
    assert statuses(rt, rid, rsv) == ("RESERVED", "ACTIVE", "RESERVED")  # 扫码本身不改变交付状态

    done = finish(rt, rsv, "DLV-0001")
    assert done.ok and done.data["item_status"] == "DELIVERED"
    assert statuses(rt, rid, rsv) == ("DELIVERED", "FULFILLED", "DELIVERED")
    assert repo.active_reservations(rt.conn, "AM-007") == []
    assert done.allowed_next_actions == () and done.request_status == "DELIVERED"
    events = [e.event_type for e in audit.list_events(rt.conn, item_id="AM-007")][-2:]
    assert events == ["DELIVERY_CHECK_STARTED", "DELIVERY_COMPLETED"]


def test_wrong_tag_blocks_delivery_and_records_mismatch(reserved):
    rt, rid, rsv = reserved
    res = scan(rt, rsv, "SYN-T-9CFL")  # AM-006 的标签
    assert not res.ok and res.reason_codes == ("DELIVERY_MISMATCH",) and res.recorded == ("DLV-0001",)
    assert "SYN-T-9CFL（这是 AM-006 的标签）不是预留商品 AM-007 的标签" in res.message
    assert statuses(rt, rid, rsv) == ("RESERVED", "ACTIVE", "RESERVED")  # 预留保持不变
    event = audit.list_events(rt.conn, event_type="DELIVERY_MISMATCH")[-1]
    assert (event.item_id, event.payload["reserved_item_id"], event.payload["scanned_tag_code"],
            event.payload["scanned_item_id"]) == ("AM-007", "AM-007", "SYN-T-9CFL", "AM-006")
    assert "AM-006" in event.related_ids
    assert repo.item_snapshot(rt.conn, "AM-006").item_status.value == "RESERVED"  # 别的商品也不受影响

    # 换成正确的商品后可以正常交付
    retry = scan(rt, rsv, "SYN-T-Q5ZT")
    assert retry.ok and retry.data["check_id"] == "DLV-0002"
    assert finish(rt, rsv, "DLV-0002").ok
    checks = st.get_artifact(rt.conn, rsv).content["delivery_checks"]
    assert [(c["check_id"], c["outcome"]) for c in checks] == [("DLV-0001", "DELIVERY_MISMATCH"), ("DLV-0002", "PASSED")]


def test_unknown_tag_is_also_blocked(reserved):
    rt, _, rsv = reserved
    res = scan(rt, rsv, "SYN-T-ZZZZ")
    assert res.reason_codes == ("DELIVERY_MISMATCH",) and res.data["scanned_item_id"] is None
    assert "不属于任何合成商品" in res.message


def test_condition_mismatch_blocks_and_requires_manual_review(reserved):
    rt, rid, rsv = reserved
    scan(rt, rsv, "SYN-T-Q5ZT")
    res = finish(rt, rsv, "DLV-0001", [{"point": "FRONT", "matches": True}, {"point": "BACK", "matches": True},
                                        {"point": "ZIPPER", "matches": False}])
    assert not res.ok and res.reason_codes == ("CONDITION_MISMATCH",) and res.recorded == ("DLV-0001",)
    assert res.data["mismatched_points"] == ["ZIPPER"] and "拉链的实物与证据不一致" in res.message
    assert statuses(rt, rid, rsv) == ("RESERVED", "ACTIVE", "RESERVED")
    assert audit.list_events(rt.conn, event_type="CONDITION_MISMATCH")[-1].payload["needs_manual_review"] is True
    blocked = unchanged(rt, lambda: scan(rt, rsv, "SYN-T-Q5ZT"))
    assert blocked.reason_codes == ("MANUAL_REVIEW_REQUIRED",)


@pytest.mark.parametrize(
    "confirmations,code",
    [
        ([], "FINDING_INCOMPLETE"),
        ([{"point": "FRONT", "matches": True}, {"point": "BACK", "matches": True}], "FINDING_INCOMPLETE"),
        ([{"point": "FRONT", "matches": True}, {"point": "BACK", "matches": True},
          {"point": "ZIPPER", "matches": None}], "FINDING_INCOMPLETE"),
        ([{"point": "FRONT", "matches": "yes"}, {"point": "BACK", "matches": True},
          {"point": "ZIPPER", "matches": True}], "INVALID_ARGUMENTS"),
        ([*ALL_MATCH, {"point": "FRONT", "matches": True}], "INVALID_ARGUMENTS"),
        ([*ALL_MATCH[:2], {"point": "HANDLE", "matches": True}], "INVALID_ARGUMENTS"),
    ],
)
def test_every_point_must_be_confirmed(reserved, confirmations, code):
    rt, rid, rsv = reserved
    scan(rt, rsv, "SYN-T-Q5ZT")
    res = unchanged(rt, lambda: finish(rt, rsv, "DLV-0001", confirmations))
    assert res.reason_codes == (code,)
    assert statuses(rt, rid, rsv) == ("RESERVED", "ACTIVE", "RESERVED")


def test_guards(reserved):
    rt, rid, rsv = reserved
    assert unchanged(rt, lambda: scan(rt, rsv, "SYN-T-Q5ZT", actor=Actor("agent", "RUN-0001"))).reason_codes == (
        "ACTOR_NOT_ALLOWED",)
    assert unchanged(rt, lambda: do.start_delivery_check(rt, rsv, "bob", "SYN-T-Q5ZT")).reason_codes == (
        "ACTOR_NOT_ALLOWED",)
    assert unchanged(rt, lambda: scan(rt, "RSV-0404", "SYN-T-Q5ZT")).reason_codes == ("RESERVATION_NOT_ACTIVE",)
    assert unchanged(rt, lambda: scan(rt, rsv, "  ")).reason_codes == ("INVALID_ARGUMENTS",)
    assert unchanged(rt, lambda: finish(rt, rsv, "DLV-0009")).reason_codes == ("DELIVERY_CHECK_NOT_PENDING",)

    scan(rt, rsv, "SYN-T-Q5ZT")
    assert unchanged(rt, lambda: scan(rt, rsv, "SYN-T-Q5ZT")).reason_codes == ("DELIVERY_CHECK_PENDING",)
    assert finish(rt, rsv, "DLV-0001").ok
    assert unchanged(rt, lambda: finish(rt, rsv, "DLV-0001")).reason_codes == ("RESERVATION_NOT_ACTIVE",)
    assert unchanged(rt, lambda: scan(rt, rsv, "SYN-T-Q5ZT")).reason_codes == ("RESERVATION_NOT_ACTIVE",)
    rejected = audit.list_events(rt.conn, event_type="OPERATION_REJECTED")
    assert {e.payload["tool"] for e in rejected} == {"start_delivery_check", "complete_delivery_check"}


def test_seed_reservation_can_be_delivered(tmp_path):
    rt = make_runtime(tmp_path)
    started = scan(rt, "RSV-SEED-01", "SYN-T-9CFL")
    assert started.ok and started.request_id is None and started.data["condition_version"] == 1
    assert finish(rt, "RSV-SEED-01", started.data["check_id"]).ok
    assert repo.item_snapshot(rt.conn, "AM-006").item_status.value == "DELIVERED"
    rt.conn.close()


def test_delivered_only_with_matching_tag_and_all_points_confirmed(reserved):
    """属性 11：只要被标为 DELIVERED，就一定是标签一致且三个部位都确认一致。"""
    rt, _, rsv = reserved
    scan(rt, rsv, "SYN-T-9CFL")
    scan(rt, rsv, "SYN-T-Q5ZT")
    finish(rt, rsv, "DLV-0002")
    assert repo.item_snapshot(rt.conn, "AM-007").item_status.value == "DELIVERED"
    passed = [c for c in st.get_artifact(rt.conn, rsv).content["delivery_checks"] if c["outcome"] == "PASSED"]
    assert len(passed) == 1 and passed[0]["tag_match"] is True and all(passed[0]["confirmations"].values())
    assert passed[0]["scanned_tag_code"] == repo.item_snapshot(rt.conn, "AM-007").tag_code
