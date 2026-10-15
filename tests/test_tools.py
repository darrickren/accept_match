"""任务 5：工具层与服务（需求 2–10、12.2–12.5、13）。

重点：
- 每个写操作在事务内重新校验前置条件；不在 allowed_next_actions 中的调用不会改变业务状态
- 重复调用只重放，不重复扣预算、不重复建任务
- 检查结果必须核对标签码
- 买家确认只接受买家本人输入；旧版本报价、重复预留被拒绝
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from acceptmatch.domain.enums import ItemStatus, Point, PointStatus
from acceptmatch.seed import parse_seed
from acceptmatch.services.reset import reset_demo
from acceptmatch.store import audit, repo
from acceptmatch.store.db import connect, state_hash, write_tx
from acceptmatch.tools import agent_tools as at
from acceptmatch.tools import human_ops as ho
from acceptmatch.tools import state as st
from acceptmatch.tools.envelope import Actor, NextAction
from acceptmatch.tools.runtime import Runtime
from acceptmatch.ui import messages

NOW = "2026-10-15T12:00:00+00:00"
INSPECTED_AT = "2026-10-15T14:00:00-04:00"
DEMO = {
    "buyer_id": "B-DEMO-01",
    "product_id": "SYN-BAG-A",
    "accepted_defects": [{"point": "BACK", "defect_type": "SCRATCH", "max_severity": "MINOR"}],
    "required_conditions": ["FRONT", "ZIPPER"],
}
ZIP_OK = [{"point": "ZIPPER", "conclusion": "OK", "evidence_refs": ["synthetic://inspection/AM-007/zipper-1"]}]
ZIP_JAM = [{"point": "ZIPPER", "conclusion": "DEFECT", "defect": {"type": "JAM", "severity": "MODERATE"},
            "evidence_refs": ["synthetic://inspection/AM-007/zipper-1"]}]
ZIP_UNSURE = [{"point": "ZIPPER", "conclusion": "INCONCLUSIVE", "evidence_refs": ["synthetic://inspection/AM-007/zipper-1"]}]


def make_runtime(tmp_path, parameter_set="default") -> Runtime:
    conn = connect(tmp_path / f"{parameter_set}.db")
    result = reset_demo(conn, parameter_set, now=NOW)
    return Runtime(conn, result.bundle, clock=lambda: NOW)


@pytest.fixture()
def rt(tmp_path):
    runtime = make_runtime(tmp_path)
    yield runtime
    runtime.conn.close()


def submit(rt: Runtime, **overrides) -> str:
    res = ho.submit_buyer_request(rt, **{**DEMO, **overrides})
    assert res.ok, res
    return res.request_id


def to_inspection(rt: Runtime, **overrides) -> tuple[str, dict, dict]:
    rid = submit(rt, **overrides)
    assert at.evaluate_request(rt, rid).ok
    dec = at.decide_inspection(rt, rid, "AM-007")
    assert dec.ok and dec.data["verdict"] == "INSPECT"
    task = at.create_inspection_task(rt, rid, "AM-007", dec.data["decision_id"])
    assert task.ok
    return rid, dec.data, task.data


def inspect(rt: Runtime, task_id: str, findings, tag="SYN-T-Q5ZT", inspector="SYN-INSPECTOR-02", **kwargs):
    return ho.submit_inspection_result(rt, task_id, inspector, tag, INSPECTED_AT, findings, **kwargs)


def to_offer(rt: Runtime) -> tuple[str, dict]:
    rid, _, task = to_inspection(rt)
    assert inspect(rt, task["task_id"], ZIP_OK).ok
    assert at.evaluate_request(rt, rid).ok
    pkg = at.build_evidence_package(rt, rid, "AM-007")
    quote = at.generate_quote(rt, rid, "AM-007", pkg.data["evidence_package_id"])
    assert quote.ok
    return rid, quote.data


def confirm(rt: Runtime, rid: str, quote: dict, **overrides):
    args = {"request_id": rid, "buyer_id": "B-DEMO-01", "item_id": "AM-007", "quote_id": quote["quote_id"],
            "acknowledged_defect_ids": quote["verified_defect_ids"], "acknowledged_amount": quote["amount"]}
    return ho.confirm_reservation(rt, **{**args, **overrides})


def event_types(rt: Runtime, rid: str) -> list[str]:
    return [e.event_type for e in audit.list_events(rt.conn, request_id=rid)]


def unchanged(rt: Runtime, fn):
    """调用 fn 前后业务状态哈希不变（审计记录除外）。"""
    before = state_hash(rt.conn)
    result = fn()
    assert state_hash(rt.conn) == before, result
    return result


def bump_condition_version(rt: Runtime, item_id: str) -> None:
    with write_tx(rt.conn):
        rt.conn.execute(
            "INSERT INTO condition_versions (item_id, version, points_json, source_ref, created_at, synthetic) "
            "SELECT item_id, version + 1, points_json, 'test:recheck', ?, 1 FROM condition_versions "
            "WHERE item_id = ? AND version = (SELECT condition_version FROM items WHERE item_id = ?)",
            (NOW, item_id, item_id))
        rt.conn.execute("UPDATE items SET condition_version = condition_version + 1 WHERE item_id = ?", (item_id,))


# ============================================================ 5.1 返回结构


def test_envelope_shape(rt):
    res = ho.submit_buyer_request(rt, **DEMO).to_dict()
    assert set(res) == {"ok", "tool", "request_id", "replayed", "reason_codes", "data", "recorded", "request_status",
                        "allowed_next_actions", "awaiting_human", "message", "synthetic"}
    assert res["synthetic"] is True and res["request_status"] == "SUBMITTED"
    assert res["allowed_next_actions"] == [{"tool": "evaluate_request", "args": {"request_id": "REQ-0001"}}]


# ============================================================ 通过路径


def test_am007_pass_chain(rt):
    req = ho.submit_buyer_request(rt, **DEMO)
    rid = req.request_id
    assert req.data["echo"] == "您接受的缺陷：背面划痕（最高轻微）。要求完好的部位：正面、拉链。未列出的缺陷一律视为不接受。"

    ev = at.evaluate_request(rt, rid)
    results = {r["item_id"]: r for r in ev.data["results"]}
    assert len(results) == 10 and ev.data["ranking"] == ["AM-007"]
    assert results["AM-007"]["result"] == "NEEDS_INSPECTION" and results["AM-007"]["unknown_points"] == ["ZIPPER"]
    assert results["AM-010"]["details"] == [{"code": "UNACCEPTED_DEFECT", "point": "BACK", "defect_id": "DF-AM010-BACK-2",
                                             "defect_type": "STAIN", "severity": "MINOR"}]
    assert ev.allowed_next_actions == (NextAction("decide_inspection", {"request_id": rid, "item_id": "AM-007"}),)

    dec = at.decide_inspection(rt, rid, "AM-007")
    assert dec.data["verdict"] == "INSPECT" and dec.data["reason_codes"] == []
    assert {s["name"]: s["value"] for s in dec.data["steps"]} == {
        "R_pass": "98.00", "s1": "78.40", "s2": "8.00", "EV_inspect": "78.40", "V_none": "45.00", "ΔEV": "33.40"}
    assert dec.data["inputs"]["pass_quote"] == "108.00" and dec.data["inputs"]["B"] == "20.00"
    assert dec.data["inputs"]["synthetic"] is True
    assert dec.allowed_next_actions[0] == NextAction(
        "create_inspection_task", {"request_id": rid, "item_id": "AM-007", "decision_id": "DEC-0001"})

    task = at.create_inspection_task(rt, rid, "AM-007", "DEC-0001")
    assert task.data["task_id"] == "TASK-0001" and task.data["tag_code"] == "SYN-T-Q5ZT"
    assert task.data["points"] == [{"point": "ZIPPER", "label": "拉链", "method": "完整开合一次，检查是否卡滞"}]
    assert task.data["budget"] == {"before": "20.00", "after": "12.00"}
    assert task.request_status == "WAITING_FOR_INSPECTION" and task.awaiting_human == "INSPECTOR"
    assert task.allowed_next_actions == ()
    assert repo.get_meta(rt.conn).budget_remaining == Decimal("12.00")

    res = inspect(rt, "TASK-0001", ZIP_OK)
    assert res.ok and res.data["new_condition_version"] == 2 and res.data["evidence_ids"] == ["EV-0001"]
    assert res.request_status == "INSPECTION_RESULT_RECORDED" and res.data["item_status"] == "AVAILABLE"

    ev2 = at.evaluate_request(rt, rid)
    assert ev2.data["inspection_outcome"] == {"task_id": "TASK-0001", "item_id": "AM-007", "outcome": "PASS"}
    assert {r["item_id"]: r["result"] for r in ev2.data["results"]}["AM-007"] == "QUALIFIED"
    assert ev2.allowed_next_actions[0].tool == "build_evidence_package"

    pkg = at.build_evidence_package(rt, rid, "AM-007")
    points = {p["point"]: p for p in pkg.data["points"]}
    assert pkg.data["condition_version"] == 2 and pkg.data["tag_code"] == "SYN-T-Q5ZT"
    assert points["BACK"]["defects"][0]["accepted_defect_index"] == 0
    assert points["ZIPPER"]["evidence"][0]["source"] == "INSPECTION"
    assert points["ZIPPER"]["evidence"][0]["inspection_task_id"] == "TASK-0001"
    assert pkg.data["required_conditions_proof"] == [
        {"point": "FRONT", "evidence_ids": ["EV-AM007-FRONT-1"]}, {"point": "ZIPPER", "evidence_ids": ["EV-0001"]}]

    quote = at.generate_quote(rt, rid, "AM-007", pkg.data["evidence_package_id"])
    assert quote.data["amount"] == "108.00" and quote.data["condition_version"] == 2
    assert quote.data["discounts"] == [{"defect_id": "DF-AM007-BACK-1", "point": "BACK", "type": "SCRATCH",
                                        "severity": "MINOR", "rate": "0.10", "amount": "12.00"}]
    assert quote.awaiting_human == "BUYER" and quote.allowed_next_actions == ()

    res = confirm(rt, rid, quote.data)
    assert res.ok and res.data["reservation_id"] == "RSV-0001" and res.request_status == "RESERVED"
    assert repo.item_snapshot(rt.conn, "AM-007").item_status is ItemStatus.RESERVED
    assert [(a["artifact_id"], a["item_id"]) for a in repo.active_reservations(rt.conn)] == [
        ("RSV-0001", "AM-007"), ("RSV-SEED-01", "AM-006")]
    assert st.get_artifact(rt.conn, quote.data["quote_id"]).status == "ACCEPTED"

    assert event_types(rt, rid) == [
        "REQUEST_SUBMITTED", "MATCH_EVALUATED", "INSPECTION_DECIDED", "BUDGET_DEDUCTED", "INSPECTION_TASK_CREATED",
        "INSPECTION_RESULT_RECORDED", "MATCH_EVALUATED", "EVIDENCE_PACKAGE_BUILT", "QUOTE_GENERATED",
        "RESERVATION_CREATED"]


# ============================================================ 失败路径


def test_am007_fail_chain_no_match(rt):
    rid, _, task = to_inspection(rt)
    res = inspect(rt, task["task_id"], ZIP_JAM)
    assert res.ok and res.data["defect_ids"] == ["DF-0001"]
    am7 = repo.item_snapshot(rt.conn, "AM-007")
    assert am7.item_status is ItemStatus.AVAILABLE and am7.condition_version == 2
    assert am7.point(Point.ZIPPER).status is PointStatus.DEFECT  # 缺陷保留，可供接受该缺陷的买家匹配

    ev = at.evaluate_request(rt, rid)
    am7_result = {r["item_id"]: r for r in ev.data["results"]}["AM-007"]
    assert am7_result["result"] == "EXCLUDED" and am7_result["reason_codes"] == ["REQUIREMENT_NOT_MET"]
    assert ev.data["inspection_outcome"]["outcome"] == "FAIL" and ev.data["ranking"] == []
    assert ev.allowed_next_actions == (NextAction("finalize_no_match", {"request_id": rid}),)

    nm = at.finalize_no_match(rt, rid)
    assert nm.request_status == "NO_MATCH" and len(nm.data["items"]) == 10
    entry = {e["item_id"]: e for e in nm.data["items"]}["AM-007"]
    assert entry["inspection_conclusion"]["findings"] == [
        {"point": "ZIPPER", "conclusion": "DEFECT", "defect_type": "JAM", "severity": "MODERATE"}]
    assert entry["buyer_messages"] == ["拉链不是完好状态，不符合您的要求", "人工检查结论：拉链卡滞（中等）"]
    assert all(e["buyer_messages"] for e in nm.data["items"])
    assert nm.allowed_next_actions == () and repo.get_meta(rt.conn).budget_remaining == Decimal("12.00")
    assert unchanged(rt, lambda: at.finalize_no_match(rt, rid)).replayed


def test_inconclusive_inspection_excludes_and_keeps_unknown(rt):
    rid, _, task = to_inspection(rt)
    assert inspect(rt, task["task_id"], ZIP_UNSURE).ok
    am7 = repo.item_snapshot(rt.conn, "AM-007")
    assert am7.condition_version == 2 and am7.point(Point.ZIPPER).status is PointStatus.UNKNOWN
    ev = at.evaluate_request(rt, rid)
    assert ev.data["inspection_outcome"]["outcome"] == "INCONCLUSIVE"
    nm = at.finalize_no_match(rt, rid)
    entry = {e["item_id"]: e for e in nm.data["items"]}["AM-007"]
    assert entry["reason_codes"] == ["INSPECTION_INCONCLUSIVE"]
    assert entry["buyer_messages"] == ["人工检查无法确定拉链的状态", "人工检查结论：拉链无法确定"]


# ============================================================ 经济判断不安排检查


@pytest.mark.parametrize(
    "parameter_set,code,ev_value,delta,budget",
    [("budget_low", "INSPECTION_OVER_BUDGET", "78.40", "33.40", "5.00"),
     ("low_pass", "INSPECTION_NOT_WORTHWHILE", "37.80", "-7.20", "20.00")],
)
def test_economics_skip_leads_to_no_match(tmp_path, parameter_set, code, ev_value, delta, budget):
    rt = make_runtime(tmp_path, parameter_set)
    rid = submit(rt)
    at.evaluate_request(rt, rid)
    dec = at.decide_inspection(rt, rid, "AM-007")
    assert dec.data["verdict"] == "SKIP" and dec.data["reason_codes"] == [code]
    assert (dec.data["ev_inspect"], dec.data["delta_ev"]) == (ev_value, delta)
    assert dec.data["recommended_disposition"] == {"kind": "LIQUIDATE_AS_IS", "label": "按现状清仓（拉链未核实）",
                                                   "net": "45.00"}
    assert dec.allowed_next_actions == (NextAction("finalize_no_match", {"request_id": rid}),)

    rejected = unchanged(rt, lambda: at.create_inspection_task(rt, rid, "AM-007", dec.data["decision_id"]))
    assert rejected.reason_codes == ("DECISION_NOT_INSPECT",)
    nm = at.finalize_no_match(rt, rid)
    entry = {e["item_id"]: e for e in nm.data["items"]}["AM-007"]
    assert entry["reason_codes"] == [code] and entry["buyer_messages"] == ["拉链未核实，本次不安排检查"]
    assert repo.get_meta(rt.conn).budget_remaining == Decimal(budget)
    assert st.artifacts(rt.conn, "INSPECTION_TASK") == []
    rt.conn.close()


def test_missing_economics_params_skip_as_invalid(rt):
    # AM-008 正面未核实、背面凹陷被接受，成为候选；但它没有配置经济参数
    rid = submit(rt, accepted_defects=[{"point": "BACK", "defect_type": "DENT", "max_severity": "MODERATE"}],
                 required_conditions=["ZIPPER"])
    ev = at.evaluate_request(rt, rid)
    assert ev.data["ranking"] == ["AM-008"]
    dec = at.decide_inspection(rt, rid, "AM-008")
    assert dec.data["verdict"] == "SKIP" and dec.data["reason_codes"] == ["ECONOMICS_INPUT_INVALID"]
    assert "未配置该商品的检查经济参数" in dec.data["invalid_inputs"]
    nm = at.finalize_no_match(rt, rid)
    assert {e["item_id"]: e for e in nm.data["items"]}["AM-008"]["buyer_messages"] == ["正面未核实，本次不安排检查"]


def test_qualified_item_skips_economics_and_inspection(rt):
    # 接受背面轻微污渍：AM-003 全部已核实，可以直接进入证据和报价（需求 5.1）
    rid = submit(rt, accepted_defects=[{"point": "BACK", "defect_type": "STAIN", "max_severity": "MINOR"}])
    ev = at.evaluate_request(rt, rid)
    assert ev.data["ranking"][0] == "AM-003"
    assert ev.allowed_next_actions == (NextAction("build_evidence_package", {"request_id": rid, "item_id": "AM-003"}),)
    assert unchanged(rt, lambda: at.decide_inspection(rt, rid, "AM-007")).reason_codes == ("INVALID_STATE",)
    assert unchanged(rt, lambda: at.finalize_no_match(rt, rid)).reason_codes == ("INVALID_STATE",)
    assert unchanged(rt, lambda: at.build_evidence_package(rt, rid, "AM-007")).reason_codes == ("NOT_TOP_QUALIFIED",)
    pkg = at.build_evidence_package(rt, rid, "AM-003")
    assert at.generate_quote(rt, rid, "AM-003", pkg.data["evidence_package_id"]).data["amount"] == "108.00"


# ============================================================ 幂等


def test_repeated_calls_replay_without_double_deduction(rt):
    rid = submit(rt)
    first = at.evaluate_request(rt, rid)
    again = unchanged(rt, lambda: at.evaluate_request(rt, rid))
    assert again.replayed and again.data == first.data

    dec = at.decide_inspection(rt, rid, "AM-007")
    dec2 = unchanged(rt, lambda: at.decide_inspection(rt, rid, "AM-007"))
    assert dec2.replayed and dec2.data == dec.data

    task = at.create_inspection_task(rt, rid, "AM-007", "DEC-0001")
    for _ in range(3):
        replay = unchanged(rt, lambda: at.create_inspection_task(rt, rid, "AM-007", "DEC-0001"))
        assert replay.ok and replay.replayed and replay.data["task_id"] == task.data["task_id"]
    assert len(st.artifacts(rt.conn, "INSPECTION_TASK")) == 1
    meta = repo.get_meta(rt.conn)
    assert (meta.budget_remaining, meta.budget_version) == (Decimal("12.00"), 1)
    assert len(audit.list_events(rt.conn, event_type="BUDGET_DEDUCTED")) == 1

    inspect(rt, "TASK-0001", ZIP_OK)
    at.evaluate_request(rt, rid)
    pkg = at.build_evidence_package(rt, rid, "AM-007")
    assert unchanged(rt, lambda: at.build_evidence_package(rt, rid, "AM-007")).data == pkg.data
    quote = at.generate_quote(rt, rid, "AM-007", pkg.data["evidence_package_id"])
    replay = unchanged(rt, lambda: at.generate_quote(rt, rid, "AM-007", pkg.data["evidence_package_id"]))
    assert replay.replayed and replay.data == quote.data
    assert len(st.artifacts(rt.conn, "QUOTE")) == 1


def test_second_request_links_open_task_without_deduction(rt):
    rid1, _, task1 = to_inspection(rt)
    rid2 = submit(rt)
    at.evaluate_request(rt, rid2)
    dec2 = at.decide_inspection(rt, rid2, "AM-007")
    assert dec2.data["inputs"]["B"] == "12.00" and dec2.data["verdict"] == "INSPECT"
    link = at.create_inspection_task(rt, rid2, "AM-007", dec2.data["decision_id"])
    assert link.data["task_id"] == task1["task_id"] and link.data["linked_existing"] is True
    assert link.data["budget"] == {"before": "12.00", "after": "12.00"}
    assert repo.get_meta(rt.conn).budget_remaining == Decimal("12.00")
    assert len(st.artifacts(rt.conn, "INSPECTION_TASK")) == 1

    res = inspect(rt, task1["task_id"], ZIP_OK)
    assert sorted(res.data["requests_updated"]) == [rid1, rid2]
    for rid in (rid1, rid2):  # 一条检查结果记录同时关联两个请求
        assert "INSPECTION_RESULT_RECORDED" in event_types(rt, rid)

    quotes = {}
    for rid in (rid1, rid2):
        at.evaluate_request(rt, rid)
        pkg = at.build_evidence_package(rt, rid, "AM-007")
        quotes[rid] = at.generate_quote(rt, rid, "AM-007", pkg.data["evidence_package_id"]).data
    assert confirm(rt, rid1, quotes[rid1]).ok

    # 同一件商品不能被第二次预留
    second = confirm(rt, rid2, quotes[rid2])
    assert not second.ok and second.reason_codes == ("ITEM_NOT_AVAILABLE",)
    assert second.request_status == "NEEDS_REEVALUATION" and second.recorded
    assert st.get_artifact(rt.conn, quotes[rid2]["quote_id"]).status == "SUPERSEDED"
    assert len(repo.active_reservations(rt.conn, "AM-007")) == 1
    ev = at.evaluate_request(rt, rid2)
    assert {r["item_id"]: r for r in ev.data["results"]}["AM-007"]["reason_codes"] == ["NOT_AVAILABLE"]
    assert at.finalize_no_match(rt, rid2).request_status == "NO_MATCH"


# ============================================================ 拒绝时状态不变


def test_rejections_leave_state_unchanged_and_are_audited(rt):
    rid = submit(rt)
    res = unchanged(rt, lambda: at.generate_quote(rt, rid, "AM-007", "EVP-0001"))
    assert res.reason_codes == ("INVALID_STATE",) and res.allowed_next_actions[0].tool == "evaluate_request"
    at.evaluate_request(rt, rid)
    assert unchanged(rt, lambda: at.decide_inspection(rt, rid, "AM-008")).reason_codes == ("OUT_OF_ORDER",)
    assert unchanged(rt, lambda: at.finalize_no_match(rt, rid)).reason_codes == ("CANDIDATES_REMAIN",)
    assert unchanged(rt, lambda: at.create_inspection_task(rt, rid, "AM-007", "DEC-0009")).reason_codes == (
        "DECISION_NOT_FOUND",)
    assert unchanged(rt, lambda: at.evaluate_request(rt, "REQ-0404")).reason_codes == ("REQUEST_NOT_FOUND",)

    rejected = audit.list_events(rt.conn, event_type="OPERATION_REJECTED")
    assert [e.payload["tool"] for e in rejected] == [
        "generate_quote", "decide_inspection", "finalize_no_match", "create_inspection_task", "evaluate_request"]
    assert rejected[1].payload["reason_codes"] == ["OUT_OF_ORDER"] and rejected[1].actor_type == "agent"


def test_decision_goes_stale_when_budget_changes(rt):
    rid = submit(rt)
    at.evaluate_request(rt, rid)
    at.decide_inspection(rt, rid, "AM-007")
    with write_tx(rt.conn):  # 模拟其他操作改变了预算
        rt.conn.execute("UPDATE meta SET budget_remaining_cents = 700, budget_version = budget_version + 1")
    stale = unchanged(rt, lambda: at.create_inspection_task(rt, rid, "AM-007", "DEC-0001"))
    assert stale.reason_codes == ("DECISION_STALE",)
    assert stale.allowed_next_actions == (NextAction("decide_inspection", {"request_id": rid, "item_id": "AM-007"}),)
    redo = at.decide_inspection(rt, rid, "AM-007")
    assert redo.data["decision_id"] == "DEC-0002" and redo.data["reason_codes"] == ["INSPECTION_OVER_BUDGET"]


def test_quote_rejected_when_item_changes_after_evidence(rt):
    rid, _, task = to_inspection(rt)
    inspect(rt, task["task_id"], ZIP_OK)
    at.evaluate_request(rt, rid)
    pkg = at.build_evidence_package(rt, rid, "AM-007")
    bump_condition_version(rt, "AM-007")
    res = unchanged(rt, lambda: at.generate_quote(rt, rid, "AM-007", pkg.data["evidence_package_id"]))
    assert res.reason_codes == ("EVALUATION_STALE",) and res.allowed_next_actions[0].tool == "evaluate_request"


def test_internal_error_rolls_back(rt, monkeypatch):
    rid = submit(rt)

    def boom(*a, **k):
        raise RuntimeError("模拟故障")

    monkeypatch.setattr(at.matching, "evaluate", boom)
    res = unchanged(rt, lambda: at.evaluate_request(rt, rid))
    assert res.reason_codes == ("INTERNAL_ERROR",) and res.request_status == "SUBMITTED"


def test_seed_changed_blocks_tools(rt):
    import json
    from acceptmatch.config import ALLOWED_SEED_ROOT

    ds = json.loads((ALLOWED_SEED_ROOT / "dataset.json").read_text(encoding="utf-8"))
    ds["meta"]["notice"] += "（已修改）"
    changed = parse_seed(json.dumps(ds).encode(), (ALLOWED_SEED_ROOT / "economics.json").read_bytes())
    other = Runtime(rt.conn, changed, clock=lambda: NOW)
    res = unchanged(rt, lambda: ho.submit_buyer_request(other, **DEMO))
    assert res.reason_codes == ("SEED_CHANGED",)


def test_unknown_tool_name():
    res = at.call(None, "confirm_reservation", {"request_id": "REQ-0001"})  # 人工操作不在 Agent 工具表中
    assert not res.ok and res.reason_codes == ("INVALID_ARGUMENTS",)


# ============================================================ 前置条件共用：全量扫描


def _candidate_calls(rt: Runtime, rid: str) -> list[tuple[str, dict]]:
    items = [f"AM-{n:03d}" for n in range(1, 11)]
    decisions = [d.artifact_id for d in st.artifacts(rt.conn, "DECISION")] + ["DEC-9999"]
    packages = [p.artifact_id for p in st.artifacts(rt.conn, "EVIDENCE_PACKAGE")] + ["EVP-9999"]
    calls = [("evaluate_request", {}), ("finalize_no_match", {})]
    for item in items:
        calls += [("decide_inspection", {"item_id": item}), ("build_evidence_package", {"item_id": item})]
        calls += [("create_inspection_task", {"item_id": item, "decision_id": d}) for d in decisions]
        calls += [("generate_quote", {"item_id": item, "evidence_package_id": p}) for p in packages]
    return [(tool, {"request_id": rid, **args}) for tool, args in calls]


@pytest.mark.parametrize("findings,final_status", [(ZIP_OK, "RESERVED"), (ZIP_JAM, "NO_MATCH")])
def test_only_allowed_actions_change_state(rt, findings, final_status):
    """每一步：不在 allowed_next_actions 中的调用要么被拒，要么只是重放，业务状态都不变；允许的动作一定成功。"""
    rid = submit(rt)
    for _ in range(20):
        s = st.load_state(rt.conn, rid)
        plan = st.plan(s)
        for tool, args in _candidate_calls(rt, rid):
            if NextAction(tool, args) in plan.actions:
                continue
            res = unchanged(rt, lambda: at.call(rt, tool, args))
            assert (not res.ok) or res.replayed, (tool, args, res)
        if plan.actions:
            action = plan.actions[0]
            res = at.call(rt, action.tool, action.args)
            assert res.ok and not res.replayed, res
        elif plan.awaiting == "INSPECTOR":
            task = [t for t in s.tasks if t.status == "OPEN"][0]
            assert inspect(rt, task.artifact_id, findings).ok
        elif plan.awaiting == "BUYER":
            assert confirm(rt, rid, s.quote.content).ok
        else:
            break
    assert st.load_request(rt.conn, rid).status.value == final_status


# ============================================================ 检查结果校验


@pytest.mark.parametrize(
    "kwargs,code",
    [
        ({"tag": "SYN-T-9CFL"}, "TAG_MISMATCH"),                      # 扫到 AM-006 的标签
        ({"findings": []}, "FINDING_INCOMPLETE"),
        ({"findings": [{"point": "ZIPPER", "conclusion": "OK", "evidence_refs": []}]}, "EVIDENCE_REQUIRED"),
        ({"findings": [{"point": "ZIPPER", "conclusion": "INCONCLUSIVE", "evidence_refs": []}]}, "EVIDENCE_REQUIRED"),
        ({"findings": [{"point": "ZIPPER", "conclusion": "DEFECT", "evidence_refs": ["synthetic://x"]}]},
         "FINDING_INCOMPLETE"),
        ({"findings": [{"point": "ZIPPER", "conclusion": "OK", "evidence_refs": ["/Users/x/photo.jpg"]}]},
         "INVALID_ARGUMENTS"),
        ({"findings": [*ZIP_OK, {"point": "FRONT", "conclusion": "OK", "evidence_refs": ["synthetic://x"]}]},
         "INVALID_ARGUMENTS"),
        ({"inspector": "inspector-bob"}, "ACTOR_NOT_ALLOWED"),
        ({"actor": Actor("agent", "RUN-0001")}, "ACTOR_NOT_ALLOWED"),
    ],
)
def test_inspection_result_validation(rt, kwargs, code):
    _, _, task = to_inspection(rt)
    findings = kwargs.pop("findings", ZIP_OK)
    res = unchanged(rt, lambda: inspect(rt, task["task_id"], findings, **kwargs))
    assert res.reason_codes == (code,), res
    assert st.get_artifact(rt.conn, task["task_id"]).status == "OPEN"
    assert repo.item_snapshot(rt.conn, "AM-007").condition_version == 1
    assert res.request_status == "WAITING_FOR_INSPECTION"


def test_inspection_timestamp_and_task_checks(rt):
    rid, _, task = to_inspection(rt)
    naive = unchanged(rt, lambda: ho.submit_inspection_result(
        rt, task["task_id"], "SYN-INSPECTOR-02", "SYN-T-Q5ZT", "2026-10-15T14:00:00", ZIP_OK))
    assert naive.reason_codes == ("INVALID_ARGUMENTS",)
    assert unchanged(rt, lambda: inspect(rt, "TASK-0404", ZIP_OK)).reason_codes == ("TASK_NOT_FOUND",)
    assert inspect(rt, task["task_id"], ZIP_OK).ok
    again = unchanged(rt, lambda: inspect(rt, task["task_id"], ZIP_JAM))
    assert again.reason_codes == ("TASK_NOT_OPEN",)
    assert repo.item_snapshot(rt.conn, "AM-007").condition_version == 2


# ============================================================ 买家确认


def test_confirmation_must_come_from_the_buyer(rt):
    rid, quote = to_offer(rt)
    cases = [
        ({"actor": Actor("agent", "RUN-0001")}, "CONFIRMATION_NOT_FROM_BUYER"),
        ({"actor": Actor("operator", "SYN-OPERATOR-01")}, "CONFIRMATION_NOT_FROM_BUYER"),
        ({"buyer_id": "B-DEMO-02"}, "QUOTE_NOT_OWNED"),
        ({"item_id": "AM-006"}, "QUOTE_ITEM_MISMATCH"),
        ({"quote_id": "QUO-0404"}, "QUOTE_NOT_FOUND"),
        ({"acknowledged_defect_ids": []}, "ACKNOWLEDGEMENT_INCOMPLETE"),
        ({"acknowledged_amount": "100.00"}, "ACKNOWLEDGEMENT_INCOMPLETE"),
        ({"acknowledged_amount": "abc"}, "ACKNOWLEDGEMENT_INCOMPLETE"),
    ]
    for overrides, code in cases:
        res = unchanged(rt, lambda: confirm(rt, rid, quote, **overrides))
        assert res.reason_codes == (code,), (overrides, res)
    assert st.load_request(rt.conn, rid).status.value == "OFFERED"
    assert confirm(rt, rid, quote).ok
    duplicate = unchanged(rt, lambda: confirm(rt, rid, quote))
    assert duplicate.reason_codes == ("INVALID_STATE",)
    assert len(repo.active_reservations(rt.conn, "AM-007")) == 1


def test_old_version_quote_rejected_then_requoted(rt):
    rid, quote = to_offer(rt)
    bump_condition_version(rt, "AM-007")  # 报价后商品状态被更新到版本 3
    res = confirm(rt, rid, quote)
    assert not res.ok and res.reason_codes == ("CONDITION_VERSION_CHANGED",)
    assert res.request_status == "NEEDS_REEVALUATION" and res.allowed_next_actions[0].tool == "evaluate_request"
    assert st.get_artifact(rt.conn, quote["quote_id"]).status == "SUPERSEDED"
    assert repo.item_snapshot(rt.conn, "AM-007").item_status is ItemStatus.AVAILABLE

    at.evaluate_request(rt, rid)
    pkg = at.build_evidence_package(rt, rid, "AM-007")
    new_quote = at.generate_quote(rt, rid, "AM-007", pkg.data["evidence_package_id"]).data
    assert new_quote["quote_id"] == "QUO-0002" and new_quote["condition_version"] == 3
    stale = unchanged(rt, lambda: confirm(rt, rid, quote))  # 旧报价不能再用
    assert stale.reason_codes == ("QUOTE_NOT_ACTIVE",)
    assert confirm(rt, rid, new_quote).ok


def test_no_quote_or_reservation_while_waiting(rt):
    rid, _, _ = to_inspection(rt)
    assert unchanged(rt, lambda: at.generate_quote(rt, rid, "AM-007", "EVP-0001")).reason_codes == ("INVALID_STATE",)
    assert unchanged(rt, lambda: at.build_evidence_package(rt, rid, "AM-007")).reason_codes == ("INVALID_STATE",)


def test_decline_keeps_item_available(rt):
    rid, quote = to_offer(rt)
    assert unchanged(rt, lambda: ho.decline_quote(rt, rid, "B-DEMO-02", quote["quote_id"])).reason_codes == (
        "QUOTE_NOT_OWNED",)
    res = ho.decline_quote(rt, rid, "B-DEMO-01", quote["quote_id"])
    assert res.ok and res.request_status == "DECLINED" and res.data["item_status"] == "AVAILABLE"
    assert st.get_artifact(rt.conn, quote["quote_id"]).status == "DECLINED"
    assert repo.active_reservations(rt.conn, "AM-007") == []
    assert unchanged(rt, lambda: confirm(rt, rid, quote)).reason_codes == ("INVALID_STATE",)


# ============================================================ 买家需求


@pytest.mark.parametrize(
    "overrides,code",
    [
        ({"required_conditions": ["FRONT", "ZIPPER", "BACK"]}, "REQUEST_CONFLICT_POINT"),
        ({"product_id": "SYN-BAG-Z"}, "UNKNOWN_PRODUCT"),
        ({"buyer_id": "B-NOPE"}, "UNKNOWN_BUYER"),
        ({"required_conditions": ["FRONT", "FRONT"]}, "INVALID_ARGUMENTS"),
        ({"accepted_defects": [{"point": "BACK", "defect_type": "RIP", "max_severity": "MINOR"}]}, "INVALID_ARGUMENTS"),
        ({"accepted_defects": [{"point": "BACK"}]}, "INVALID_ARGUMENTS"),
        ({"actor": Actor("agent", "RUN-0001")}, "ACTOR_NOT_ALLOWED"),
        ({"actor": Actor("buyer", "B-DEMO-02")}, "ACTOR_NOT_ALLOWED"),
    ],
)
def test_buyer_request_validation(rt, overrides, code):
    res = unchanged(rt, lambda: ho.submit_buyer_request(rt, **{**DEMO, **overrides}))
    assert res.reason_codes == (code,), res
    assert rt.conn.execute("SELECT COUNT(*) FROM buyer_requests").fetchone()[0] == 0
    assert audit.list_events(rt.conn, event_type="OPERATION_REJECTED")[-1].payload["tool"] == "submit_buyer_request"


# ============================================================ 5.4 买家消息


_FORBIDDEN = ("成本", "概率", "预算", "预期价值", "ΔEV", "EV", "8.00", "0.80", "45.00", "20.00", "清仓")


def test_buyer_messages_never_reveal_economics():
    base = {"details": [], "unknown_points": ["ZIPPER"], "inspection_conclusion": None}
    for code in ("PRODUCT_MISMATCH", "NOT_AVAILABLE", "INSPECTION_OVER_BUDGET", "INSPECTION_NOT_WORTHWHILE",
                 "ECONOMICS_INPUT_INVALID", "INSPECTION_INCONCLUSIVE"):
        lines = messages.item_reason_lines({**base, "reason_codes": [code]})
        assert lines and not any(word in line for line in lines for word in _FORBIDDEN), (code, lines)
    lines = messages.item_reason_lines({
        "reason_codes": ["REQUIREMENT_NOT_MET", "UNACCEPTED_DEFECT"],
        "details": [{"code": "REQUIREMENT_NOT_MET", "point": "FRONT"},
                    {"code": "UNACCEPTED_DEFECT", "point": "BACK", "defect_type": "DENT", "severity": "MODERATE"}],
        "unknown_points": [], "inspection_conclusion": None})
    assert lines == ["正面不是完好状态，不符合您的要求", "有您未接受的缺陷：背面凹陷（中等）"]


def test_no_match_messages_from_tools_have_no_economics(tmp_path):
    rt = make_runtime(tmp_path, "budget_low")
    rid = submit(rt)
    at.evaluate_request(rt, rid)
    at.decide_inspection(rt, rid, "AM-007")
    nm = at.finalize_no_match(rt, rid)
    text = nm.data["summary"] + "".join(line for e in nm.data["items"] for line in e["buyer_messages"])
    assert not any(word in text for word in _FORBIDDEN), text
    rt.conn.close()
