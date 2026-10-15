"""任务 6：Agent 编排（需求 12、13.3、14）。

- scripted 模式和 Strands（真实 Agent 循环 + 模型替身）都能跑通检查通过 / 检查失败两条路径
- 遇到等待检查员或等待买家时结束运行，人工输入后由新运行继续
- Agent 只能调用 allowed_next_actions 中的工具；每次调用记录理由和结果
- 运行失败后不再发起新的写操作，直到恢复
- 模型编号与区域只来自环境变量
"""

from __future__ import annotations

import os
from decimal import Decimal

import pytest
from botocore.exceptions import ClientError, ReadTimeoutError

from acceptmatch.agent import orchestrator as orc
from acceptmatch.agent.flow import continue_after
from acceptmatch.agent.guards import HIDDEN_TEXT, NumberGuard, numbers_in
from acceptmatch.agent.orchestrator import Orchestrator, ScriptedDriver
from acceptmatch.agent.strands_driver import StrandsDriver, build_bedrock_model
from acceptmatch.config import ConfigError, load_settings
from acceptmatch.services.reset import reset_demo
from acceptmatch.store import audit, repo
from acceptmatch.store.db import connect, state_hash
from acceptmatch.tools import human_ops as ho
from acceptmatch.tools import state as st
from acceptmatch.tools.runtime import Runtime
from tests.fakes import FakeModel, follow_allowed

NOW = "2026-10-15T12:00:00+00:00"
DEMO = {
    "buyer_id": "B-DEMO-01",
    "product_id": "SYN-BAG-A",
    "accepted_defects": [{"point": "BACK", "defect_type": "SCRATCH", "max_severity": "MINOR"}],
    "required_conditions": ["FRONT", "ZIPPER"],
}
ZIP_OK = [{"point": "ZIPPER", "conclusion": "OK", "evidence_refs": ["synthetic://inspection/AM-007/zipper-1"]}]
ZIP_JAM = [{"point": "ZIPPER", "conclusion": "DEFECT", "defect": {"type": "JAM", "severity": "MODERATE"},
            "evidence_refs": ["synthetic://inspection/AM-007/zipper-1"]}]

FIRST_RUN = ["get_request_status", "evaluate_request", "decide_inspection", "create_inspection_task"]
PASS_RUN = ["get_request_status", "evaluate_request", "build_evidence_package", "generate_quote"]
FAIL_RUN = ["get_request_status", "evaluate_request", "finalize_no_match"]


@pytest.fixture()
def rt(tmp_path):
    conn = connect(tmp_path / "acceptmatch.db")
    runtime = Runtime(conn, reset_demo(conn, now=NOW).bundle, clock=lambda: NOW)
    yield runtime
    conn.close()


def settings(**env):
    return load_settings({"AGENT_MODE": "scripted", **{k: str(v) for k, v in env.items()}})


def scripted(rt, **env) -> Orchestrator:
    return Orchestrator(rt, settings(**env))


def strands(rt, model: FakeModel, **env) -> Orchestrator:
    return Orchestrator(rt, settings(**env), driver_factory=lambda o: StrandsDriver(o, model))


def submit(rt, orch):
    res = ho.submit_buyer_request(rt, **DEMO)
    assert res.ok
    runs = continue_after(orch, res)
    assert len(runs) == 1
    return res.request_id, runs[0]


def inspect(rt, orch, findings, tag="SYN-T-Q5ZT"):
    task = [t for t in st.artifacts(rt.conn, "INSPECTION_TASK") if t.status == "OPEN"][0]
    res = ho.submit_inspection_result(rt, task.artifact_id, "SYN-INSPECTOR-02", tag, "2026-10-15T14:00:00-04:00",
                                      findings)
    return res, continue_after(orch, res)


def tools_of(run) -> list[str]:
    return [c.tool for c in run.calls]


def tool_call_events(rt, run_id):
    return [e for e in audit.list_events(rt.conn, event_type="AGENT_TOOL_CALL") if e.payload["run_id"] == run_id]


# ===================================================== 两条完整路径


@pytest.mark.parametrize("driver", ["scripted", "strands"])
def test_pass_path_stops_for_humans_and_continues(rt, driver):
    model = FakeModel()
    orch = scripted(rt) if driver == "scripted" else strands(rt, model)

    rid, run1 = submit(rt, orch)
    assert run1.status == "COMPLETED" and tools_of(run1) == FIRST_RUN
    assert (run1.request_status, run1.awaiting_human, run1.allowed_next_actions) == (
        "WAITING_FOR_INSPECTION", "INSPECTOR", ())
    assert repo.get_meta(rt.conn).budget_remaining == Decimal("12.00")

    res, runs = inspect(rt, orch, ZIP_OK)
    assert res.ok and len(runs) == 1
    run2 = runs[0]
    assert run2.status == "COMPLETED" and tools_of(run2) == PASS_RUN
    assert (run2.request_status, run2.awaiting_human) == ("OFFERED", "BUYER")
    quote = st.load_state(rt.conn, rid).quote.content
    assert quote["amount"] == "108.00"

    confirmed = ho.confirm_reservation(rt, rid, "B-DEMO-01", "AM-007", quote["quote_id"],
                                       quote["verified_defect_ids"], quote["amount"])
    assert confirmed.ok and continue_after(orch, confirmed) == []  # 预留后没有 Agent 可做的事
    assert repo.item_snapshot(rt.conn, "AM-007").item_status.value == "RESERVED"

    if driver == "strands":
        assert model.calls == 8  # 没有可做的动作时直接结束循环，不再多发一次模型调用
    for run in (run1, run2):
        events = tool_call_events(rt, run.run_id)
        assert [e.payload["tool"] for e in events] == tools_of(run)
        assert all(e.payload["rationale"] and e.payload["result"]["ok"] for e in events)
        assert all(e.actor_type == "agent" and e.actor_id == run.run_id for e in events)
    ends = audit.list_events(rt.conn, event_type="AGENT_RUN_ENDED")
    assert [(e.payload["run_id"], e.outcome) for e in ends] == [("RUN-0001", "COMPLETED"), ("RUN-0002", "COMPLETED")]


@pytest.mark.parametrize("driver", ["scripted", "strands"])
def test_fail_path_ends_in_no_match(rt, driver):
    orch = scripted(rt) if driver == "scripted" else strands(rt, FakeModel())
    rid, run1 = submit(rt, orch)
    assert tools_of(run1) == FIRST_RUN
    _, runs = inspect(rt, orch, ZIP_JAM)
    run2 = runs[0]
    assert run2.status == "COMPLETED" and tools_of(run2) == FAIL_RUN
    assert run2.request_status == "NO_MATCH" and run2.awaiting_human is None
    no_match = st.load_request(rt.conn, rid).no_match
    assert len(no_match["items"]) == 10
    am7 = {e["item_id"]: e for e in no_match["items"]}["AM-007"]
    assert am7["buyer_messages"] == ["拉链不是完好状态，不符合您的要求", "人工检查结论：拉链卡滞（中等）"]
    assert repo.item_snapshot(rt.conn, "AM-007").item_status.value == "AVAILABLE"


def test_rejected_inspection_does_not_start_a_run(rt):
    orch = scripted(rt)
    submit(rt, orch)
    res, runs = inspect(rt, orch, ZIP_OK, tag="SYN-T-9CFL")
    assert res.reason_codes == ("TAG_MISMATCH",) and runs == []


def test_confirmation_rejection_triggers_reevaluation_run(rt):
    orch = scripted(rt)
    rid, _ = submit(rt, orch)
    inspect(rt, orch, ZIP_OK)
    quote = st.load_state(rt.conn, rid).quote.content
    from tests.test_tools import bump_condition_version  # noqa: PLC0415

    bump_condition_version(rt, "AM-007")  # 报价后商品状态被更新：确认会被拒，需要重新评估
    res = ho.confirm_reservation(rt, rid, "B-DEMO-01", "AM-007", quote["quote_id"], quote["verified_defect_ids"],
                                 quote["amount"])
    assert res.reason_codes == ("CONDITION_VERSION_CHANGED",)
    runs = continue_after(orch, res)
    assert tools_of(runs[0]) == PASS_RUN and runs[0].request_status == "OFFERED"
    assert st.load_state(rt.conn, rid).quote.content["condition_version"] == 3


# ===================================================== 只能调用允许的下一步


def test_disallowed_and_repeated_calls_are_rejected(rt):
    script = iter([
        ("get_request_status", {"request_id": "REQ-0001"}, "读取状态"),
        ("finalize_no_match", {"request_id": "REQ-0001"}, "试图直接宣布无匹配"),
        ("finalize_no_match", {"request_id": "REQ-0001"}, "再试一次"),
    ])

    def decide(messages, n):
        return next(script, None) or follow_allowed(messages)

    orch = strands(rt, FakeModel(decide))
    rid, run = submit(rt, orch)
    assert [(c.tool, c.ok, c.reason_codes) for c in run.calls[:3]] == [
        ("get_request_status", True, ()),
        ("finalize_no_match", False, ("INVALID_STATE",)),
        ("finalize_no_match", False, ("REPEATED_REJECTED_CALL",)),
    ]
    assert all(c.guard_rejected for c in run.calls[1:3])
    assert tools_of(run)[3:] == FIRST_RUN[1:] and run.status == "COMPLETED"  # 两次被拒后仍按允许的下一步完成
    assert st.load_request(rt.conn, rid).status.value == "WAITING_FOR_INSPECTION"


@pytest.mark.parametrize(
    "tool,args,code",
    [
        ("confirm_reservation", {"request_id": "REQ-0001"}, "INVALID_ARGUMENTS"),         # 人工操作不对 Agent 开放
        ("submit_inspection_result", {"request_id": "REQ-0001"}, "INVALID_ARGUMENTS"),
        ("evaluate_request", {"request_id": "REQ-0001", "force": True}, "INVALID_ARGUMENTS"),
        ("decide_inspection", {"request_id": "REQ-0001", "item_id": "../AM-007"}, "INVALID_ARGUMENTS"),
        ("evaluate_request", {"request_id": "REQ-0002"}, "ACTION_NOT_ALLOWED"),          # 不是本次运行的请求
        ("decide_inspection", {"request_id": "REQ-0001", "item_id": "AM-007"}, "INVALID_STATE"),  # 还没评估
    ],
)
def test_guard_rejects_without_touching_state(rt, tool, args, code):
    rid = ho.submit_buyer_request(rt, **DEMO).request_id
    ho.submit_buyer_request(rt, **DEMO)  # REQ-0002 存在，但不属于本次运行
    before = state_hash(rt.conn)
    outcomes = []

    def driver_factory(o):
        class OneShot:
            def run(self, ctx):
                outcomes.append(o.invoke(ctx, tool, args, "测试守卫"))
                return None
        return OneShot()

    run = Orchestrator(rt, settings(), driver_factory=driver_factory).run(rid, "TEST")
    assert outcomes[0]["ok"] is False and outcomes[0]["reason_codes"] == [code] and outcomes[0]["guard_rejected"]
    assert state_hash(rt.conn) == before
    assert run.status == "INCOMPLETE"  # 仍有允许的下一步没做
    event = tool_call_events(rt, run.run_id)[0]
    assert event.outcome == code and event.payload["rationale"] == "测试守卫"


def test_missing_rationale_is_rejected(rt):
    rid = ho.submit_buyer_request(rt, **DEMO).request_id
    seen = []

    def driver_factory(o):
        class NoReason:
            def run(self, ctx):
                seen.append(o.invoke(ctx, "evaluate_request", {"request_id": rid}, "  "))
                return None
        return NoReason()

    Orchestrator(rt, settings(), driver_factory=driver_factory).run(rid, "TEST")
    assert seen[0]["reason_codes"] == ["INVALID_ARGUMENTS"] and "rationale" in seen[0]["message"]
    assert st.load_request(rt.conn, rid).status.value == "SUBMITTED"


def test_too_many_rejections_stop_the_run(rt):
    bad = ("finalize_no_match", {"request_id": "REQ-0001"}, "一直调用不允许的工具")
    calls = iter([("get_request_status", {"request_id": "REQ-0001"}, "读取状态"),
                  bad, ("generate_quote", {"request_id": "REQ-0001", "item_id": "AM-007",
                                           "evidence_package_id": "EVP-0001"}, "乱试"),
                  ("decide_inspection", {"request_id": "REQ-0001", "item_id": "AM-001"}, "再乱试")])
    model = FakeModel(lambda messages, n: next(calls, bad))
    orch = strands(rt, model, AGENT_MAX_REJECTIONS=3)
    rid = ho.submit_buyer_request(rt, **DEMO).request_id
    before = state_hash(rt.conn)
    run = orch.run(rid, "REQUEST_SUBMITTED")
    assert run.status == "GUARD_STOPPED" and run.calls[-1].reason_codes == ("INVALID_STATE",)
    assert model.calls == 4  # 第三次被拒后结束循环
    assert state_hash(rt.conn) == before


def test_step_limit_stops_the_run(rt):
    orch = scripted(rt, AGENT_MAX_TOOL_CALLS=2)
    rid, run = submit(rt, orch)
    assert run.status == "GUARD_STOPPED"
    assert [(c.tool, c.ok) for c in run.calls] == [("get_request_status", True), ("evaluate_request", True),
                                                   ("decide_inspection", False)]
    assert run.calls[-1].reason_codes == ("RUN_STEP_LIMIT",)
    assert st.artifacts(rt.conn, "DECISION") == []  # 被拦下的调用没有执行


def test_run_timeout_stops_the_run(rt):
    clock = iter([0.0, 1.0, 2.0, 500.0, 501.0, 502.0])
    orch = Orchestrator(rt, settings(AGENT_RUN_TIMEOUT_S=120), monotonic=lambda: next(clock))
    rid, run = submit(rt, orch)
    assert run.status == "TIMEOUT" and run.calls[-1].reason_codes == ("RUN_TIMEOUT",)
    assert tools_of(run) == ["get_request_status", "evaluate_request", "decide_inspection"]
    assert st.artifacts(rt.conn, "DECISION") == []


# ===================================================== 失败后不再写入，恢复后继续


@pytest.mark.parametrize(
    "error,status,code",
    [
        (ClientError({"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "Converse"),
         "FAILED_BEDROCK", "BEDROCK_ERROR"),
        (ReadTimeoutError(endpoint_url="https://bedrock-runtime.example"), "TIMEOUT", "BEDROCK_TIMEOUT"),
    ],
)
def test_bedrock_failure_keeps_committed_work_and_halts(rt, error, status, code):
    model = FakeModel(fail_on_call=3, error=error)  # 第 3 次模型调用失败：此时已完成状态读取和匹配评估
    orch = strands(rt, model)
    rid, run = submit(rt, orch)
    assert (run.status, run.error_code) == (status, code)
    assert tools_of(run) == ["get_request_status", "evaluate_request"]
    assert st.load_request(rt.conn, rid).status.value == "EVALUATED"  # 已提交的评估保留
    assert len(st.artifacts(rt.conn, "EVALUATION")) == 1 and st.artifacts(rt.conn, "DECISION") == []
    end = audit.list_events(rt.conn, event_type="AGENT_RUN_ENDED")[-1]
    assert end.outcome == status and end.payload["error_code"] == code

    # 失败后：新的运行被拒绝，不发起任何写操作
    before = state_hash(rt.conn)
    refused = orch.run(rid, "RETRY")
    assert refused.status == "REFUSED_HALTED" and refused.run_id is None and state_hash(rt.conn) == before
    assert model.calls == 3

    # 恢复：从数据库状态继续，不重复已完成的步骤
    resumed = Orchestrator(rt, settings()).run(rid, "RESUME", resume=True)
    assert resumed.status == "COMPLETED"
    assert tools_of(resumed) == ["get_request_status", "decide_inspection", "create_inspection_task"]
    assert len(st.artifacts(rt.conn, "EVALUATION")) == 1
    assert repo.get_meta(rt.conn).budget_remaining == Decimal("12.00")
    started = audit.list_events(rt.conn, event_type="AGENT_RUN_STARTED")[-1]
    assert started.payload["resume_of"] == run.run_id


def test_scripted_failure_injection(rt):
    orch = Orchestrator(rt, settings(), driver_factory=lambda o: ScriptedDriver(
        o, fail_before_call=4, error=ReadTimeoutError(endpoint_url="x")))
    rid, run = submit(rt, orch)
    assert run.status == "TIMEOUT" and tools_of(run) == ["get_request_status", "evaluate_request", "decide_inspection"]
    assert st.artifacts(rt.conn, "INSPECTION_TASK") == [] and repo.get_meta(rt.conn).budget_remaining == Decimal("20.00")


def test_calls_after_run_end_are_rejected(rt):
    rid = ho.submit_buyer_request(rt, **DEMO).request_id
    kept = {}

    def driver_factory(o):
        class Keep:
            def run(self, ctx):
                kept["ctx"] = ctx
                return None
        return Keep()

    orch = Orchestrator(rt, settings(), driver_factory=driver_factory)
    orch.run(rid, "TEST")
    before = state_hash(rt.conn)
    late = orch.invoke(kept["ctx"], "evaluate_request", {"request_id": rid}, "运行结束后才到达的调用")
    assert late["reason_codes"] == ["RUN_NOT_ACTIVE"] and state_hash(rt.conn) == before


# ===================================================== 理由与数值守卫


def test_rationale_numbers_must_come_from_tool_results(rt):
    script = iter([
        ("get_request_status", {"request_id": "REQ-0001"}, "读取 REQ-0001 的状态"),
        ("evaluate_request", {"request_id": "REQ-0001"}, "评估后预计能卖 999.99"),
    ])

    def decide(messages, n):
        step = next(script, None)
        if step:
            return step
        result = follow_allowed(messages)
        return result if isinstance(result, str) else (result[0], result[1], "ΔEV 为 33.40，值得检查")

    orch = strands(rt, FakeModel(decide))
    _, run = submit(rt, orch)
    flagged = {c.tool: (c.rationale_display, c.rationale_flagged) for c in run.calls}
    assert flagged["get_request_status"] == ("读取 REQ-0001 的状态", False)
    assert flagged["evaluate_request"] == (HIDDEN_TEXT, True)
    assert flagged["create_inspection_task"] == ("ΔEV 为 33.40，值得检查", False)  # 33.40 来自经济判断结果
    event = [e for e in tool_call_events(rt, run.run_id) if e.payload["tool"] == "evaluate_request"][0]
    assert event.payload["rationale"] == "评估后预计能卖 999.99" and event.payload["rationale_unverified"] == ["999.99"]


def test_agent_final_text_is_guarded(rt):
    # 模型不调用工具、直接给出含有编造数值的文字：文字被隐藏；仍有下一步没做，所以是 INCOMPLETE
    orch = strands(rt, FakeModel(lambda m, n: "我先不调用工具，预计报价 150.00"))
    rid = ho.submit_buyer_request(rt, **DEMO).request_id
    run = orch.run(rid, "REQUEST_SUBMITTED")
    assert run.status == "INCOMPLETE" and run.calls == ()
    assert run.summary == HIDDEN_TEXT and run.summary_flagged
    end = audit.list_events(rt.conn, event_type="AGENT_RUN_ENDED")[-1]
    assert end.payload["agent_text"] == "我先不调用工具，预计报价 150.00"  # 原文保留在审计中


def test_number_guard_ignores_ids():
    assert numbers_in("AM-007 的 REQ-0001 与 SYN-T-Q5ZT，EV-AM007-FRONT-1，B-DEMO-01") == set()
    guard = NumberGuard()
    guard.learn('{"delta_ev": "-7.20", "amount": "108.00"}')
    assert not guard.check("ΔEV 为 −7.20，报价 108").flagged
    assert guard.check("报价 109").flagged


# ===================================================== Bedrock 配置


def test_bedrock_mode_without_env_refuses_before_any_write(rt):
    rid = ho.submit_buyer_request(rt, **DEMO).request_id
    before = state_hash(rt.conn)
    run = Orchestrator(rt, load_settings({"AGENT_MODE": "bedrock"})).run(rid, "REQUEST_SUBMITTED")
    assert run.status == "CONFIG_ERROR" and "BEDROCK_MODEL_ID" in run.error_detail
    assert state_hash(rt.conn) == before
    assert audit.list_events(rt.conn, event_type="AGENT_RUN_STARTED") == []
    assert audit.list_events(rt.conn, event_type="AGENT_RUN_REFUSED")[-1].outcome == "CONFIG_ERROR"


def test_bedrock_model_uses_env_values_only():
    s = load_settings({"AGENT_MODE": "bedrock", "BEDROCK_MODEL_ID": "model-id-from-env",
                       "AWS_DEFAULT_REGION": "region-from-env", "BEDROCK_READ_TIMEOUT_S": "33"})
    model = build_bedrock_model(s)  # 只构建客户端，不发起网络请求
    config = model.get_config()
    assert config["model_id"] == "model-id-from-env"
    assert config["temperature"] == 0.0 and config["streaming"] is False
    assert model.client.meta.region_name == "region-from-env"
    assert model.client.meta.config.read_timeout == 33
    assert model.client.meta.config.retries["total_max_attempts"] == 2


def test_run_start_records_mode_model_and_region(rt):
    s = load_settings({"AGENT_MODE": "bedrock", "BEDROCK_MODEL_ID": "model-id-from-env", "AWS_REGION": "r-env"})
    rid = ho.submit_buyer_request(rt, **DEMO).request_id
    Orchestrator(rt, s, driver_factory=lambda o: StrandsDriver(o, FakeModel())).run(rid, "REQUEST_SUBMITTED")
    payload = audit.list_events(rt.conn, event_type="AGENT_RUN_STARTED")[-1].payload
    assert (payload["mode"], payload["model_id"], payload["region"]) == ("bedrock", "model-id-from-env", "r-env")
    assert payload["mode_label"] == "Strands + FakeModel（非 Bedrock）"  # 如实标注：替身模型不是 Bedrock
    scripted_run = Orchestrator(rt, settings()).run(ho.submit_buyer_request(rt, **DEMO).request_id, "X")
    assert scripted_run.mode == "离线脚本编排（非 Bedrock）"


# ===================================================== 真实 Bedrock（默认跳过）


@pytest.mark.bedrock
def test_live_bedrock_pass_path(rt):
    s = load_settings(os.environ)
    s.require_bedrock()
    orch = Orchestrator(rt, s)
    rid, run1 = submit(rt, orch)
    assert run1.status == "COMPLETED" and run1.request_status == "WAITING_FOR_INSPECTION", run1
    _, runs = inspect(rt, orch, ZIP_OK)
    assert runs[0].request_status == "OFFERED", runs[0]
    assert st.load_state(rt.conn, rid).quote.content["amount"] == "108.00"
