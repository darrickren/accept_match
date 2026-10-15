"""Agent 运行管理与工具守卫（design §7.4–§7.7，需求 12、13.3）。

一次运行 = 一个 Buyer_Request 从当前状态推进到"没有可做的 Agent 动作"为止：
- 遇到等待检查员 / 等待买家 / 终态时结束，收到人工输入后由新的运行继续（见 flow.py）。
- 驱动可以是 Strands + Bedrock，也可以是离线脚本（scripted，非 Bedrock）。两者都只能通过
  Orchestrator.invoke() 调用工具。

invoke() 的守卫顺序：
1. 运行已停止 → RUN_NOT_ACTIVE（失败后不再发起任何新的写操作）
2. 超过运行总时长 → RUN_TIMEOUT；超过工具调用次数 → RUN_STEP_LIMIT（两者都会停止本次运行）
3. 未知工具（含人工操作）或参数不合法 → INVALID_ARGUMENTS；请求编号不是本次运行的 → ACTION_NOT_ALLOWED
4. 同一运行内以相同参数重复被拒绝的调用 → REPEATED_REJECTED_CALL
5. 不在当前 allowed_next_actions 中 → 用与工具相同的前置条件给出原因码，不执行
6. 执行工具（工具在事务内再次校验）；记录 AGENT_TOOL_CALL（理由与完整结果）

运行失败（Bedrock 异常、超时、守卫停止）后，同一请求的新运行会被拒绝（RUN_HALTED），
直到演示负责人以 resume=True 恢复。已提交的工具操作全部保留。
"""

from __future__ import annotations

import json
import logging
import socket
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from acceptmatch.agent.guards import NumberGuard, clean_rationale, validate_args
from acceptmatch.config import AgentMode, ConfigError, Settings
from acceptmatch.domain.enums import RequestStatus
from acceptmatch.store import audit
from acceptmatch.store.db import dumps, write_tx
from acceptmatch.tools import agent_tools as at
from acceptmatch.tools import state as st
from acceptmatch.tools.envelope import Actor, NextAction, Rejection, ToolCode, ToolResult
from acceptmatch.tools.runner import Outcome, envelope
from acceptmatch.tools.runtime import Runtime

log = logging.getLogger(__name__)

# 运行状态
COMPLETED = "COMPLETED"
INCOMPLETE = "INCOMPLETE"
FAILED_BEDROCK = "FAILED_BEDROCK"
TIMEOUT = "TIMEOUT"
GUARD_STOPPED = "GUARD_STOPPED"
REFUSED_HALTED = "REFUSED_HALTED"
CONFIG_ERROR = "CONFIG_ERROR"
FAILED_STATUSES = frozenset({FAILED_BEDROCK, TIMEOUT, GUARD_STOPPED})

# 守卫原因码
RUN_NOT_ACTIVE = "RUN_NOT_ACTIVE"
RUN_TIMEOUT = "RUN_TIMEOUT"
RUN_STEP_LIMIT = "RUN_STEP_LIMIT"
REPEATED_REJECTED_CALL = "REPEATED_REJECTED_CALL"
ACTION_NOT_ALLOWED = "ACTION_NOT_ALLOWED"
RUN_HALTED = "RUN_HALTED"

MODE_LABELS = {
    AgentMode.BEDROCK: "Strands + Amazon Bedrock",
    AgentMode.SCRIPTED: "离线脚本编排（非 Bedrock）",
}

SCRIPTED_RATIONALE = "脚本编排：执行 allowed_next_actions 中的第一项"


@dataclass(frozen=True)
class CallRecord:
    seq: int
    tool: str
    args: dict[str, Any]
    rationale: str | None
    rationale_display: str
    rationale_flagged: bool
    ok: bool
    replayed: bool
    reason_codes: tuple[str, ...]
    guard_rejected: bool
    request_status: str | None
    allowed_next_actions: tuple[dict[str, Any], ...]


@dataclass
class RunContext:
    run_id: str
    request_id: str
    trigger: str
    mode: AgentMode
    started: float
    calls: list[CallRecord] = field(default_factory=list)
    rejections: int = 0
    rejected_keys: set[str] = field(default_factory=set)
    stopped: bool = False
    stop_status: str | None = None
    stop_loop: bool = False  # 通知驱动结束循环（没有可做的动作或已停止）
    guard: NumberGuard = field(default_factory=NumberGuard)

    def stop(self, status: str) -> None:
        if not self.stopped:
            self.stopped, self.stop_status = True, status
        self.stop_loop = True


@dataclass(frozen=True)
class RunOutcome:
    run_id: str | None
    request_id: str
    trigger: str
    mode: str
    status: str
    calls: tuple[CallRecord, ...]
    error_code: str | None
    error_detail: str | None
    summary: str
    summary_flagged: bool
    request_status: str | None
    awaiting_human: str | None
    allowed_next_actions: tuple[NextAction, ...]


class Driver(Protocol):
    def run(self, ctx: RunContext) -> str | None: ...


DriverFactory = Callable[["Orchestrator"], Driver]


# ------------------------------------------------------------------ 驱动


class ScriptedDriver:
    """离线脚本编排（非 Bedrock）：总是执行 allowed_next_actions 的第一项。

    fail_before_call / error 用于测试：在第 N 次工具调用之前抛出异常，模拟模型调用失败。
    """

    def __init__(self, orch: Orchestrator, *, fail_before_call: int | None = None,
                 error: BaseException | None = None):
        self.orch = orch
        self.fail_before_call = fail_before_call
        self.error = error

    def _invoke(self, ctx: RunContext, tool: str, args: dict[str, Any], rationale: str) -> dict[str, Any]:
        if self.fail_before_call is not None and len(ctx.calls) + 1 >= self.fail_before_call:
            raise self.error or RuntimeError("模拟的模型调用失败")
        return self.orch.invoke(ctx, tool, args, rationale)

    def run(self, ctx: RunContext) -> str | None:
        result = self._invoke(ctx, st.GET_STATUS, {"request_id": ctx.request_id}, "读取请求的当前状态")
        while not ctx.stop_loop and result["allowed_next_actions"]:
            action = result["allowed_next_actions"][0]
            result = self._invoke(ctx, action["tool"], action["args"], SCRIPTED_RATIONALE)
        return None


def _default_driver_factory(orch: Orchestrator) -> Driver:
    if orch.settings.agent_mode is AgentMode.SCRIPTED:
        return ScriptedDriver(orch)
    from acceptmatch.agent.strands_driver import StrandsDriver, build_bedrock_model  # noqa: PLC0415

    return StrandsDriver(orch, build_bedrock_model(orch.settings))


def classify_failure(exc: BaseException) -> tuple[str, str]:
    """把驱动抛出的异常归类为 TIMEOUT 或 FAILED_BEDROCK。"""
    try:
        from botocore.exceptions import ConnectTimeoutError, ReadTimeoutError  # noqa: PLC0415

        timeout_types: tuple[type[BaseException], ...] = (ReadTimeoutError, ConnectTimeoutError, socket.timeout,
                                                          TimeoutError)
    except ImportError:  # pragma: no cover
        timeout_types = (socket.timeout, TimeoutError)
    seen, current = set(), exc
    while current is not None and id(current) not in seen:
        if isinstance(current, timeout_types):
            return TIMEOUT, "BEDROCK_TIMEOUT"
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return FAILED_BEDROCK, "BEDROCK_ERROR"


# ------------------------------------------------------------ Orchestrator


class Orchestrator:
    def __init__(self, rt: Runtime, settings: Settings, *, driver_factory: DriverFactory | None = None,
                 monotonic: Callable[[], float] = time.monotonic):
        self.rt = rt
        self.settings = settings
        self.driver_factory = driver_factory or _default_driver_factory
        self.monotonic = monotonic

    # ---------------------------------------------------------------- 运行

    def run(self, request_id: str, trigger: str, *, resume: bool = False) -> RunOutcome:
        mode = self.settings.agent_mode
        last = self.last_run_end(request_id)
        if last is not None and last.outcome in FAILED_STATUSES and not resume:
            detail = f"上一次运行 {last.payload.get('run_id')} 以 {last.outcome} 结束，需要演示负责人恢复后才能继续"
            self._audit_refused(request_id, trigger, RUN_HALTED, detail)
            return self._outcome(None, request_id, trigger, mode, REFUSED_HALTED, (), RUN_HALTED, detail,
                                 detail, False)

        try:
            driver = self.driver_factory(self)
        except ConfigError as exc:
            self._audit_refused(request_id, trigger, CONFIG_ERROR, str(exc))
            return self._outcome(None, request_id, trigger, mode, CONFIG_ERROR, (), CONFIG_ERROR, str(exc),
                                 str(exc), False)

        label = getattr(driver, "mode_label", MODE_LABELS[mode])
        ctx = self._start(request_id, trigger, mode, label,
                          resume_of=last.payload.get("run_id") if resume and last else None)
        error_code = error_detail = None
        text: str | None = None
        try:
            text = driver.run(ctx)
        except Exception as exc:  # noqa: BLE001 - 任何驱动异常都结束运行，不再发起写操作
            status, error_code = classify_failure(exc)
            error_detail = f"{type(exc).__name__}: {str(exc)[:300]}"
            log.warning("Agent 运行 %s 失败：%s", ctx.run_id, error_detail)
            ctx.stop(ctx.stop_status or status)
        finally:
            ctx.stopped = True  # 运行结束后，这个上下文中的任何调用都会被拒绝

        if ctx.stop_status is None:
            plan = self._plan(request_id)
            ctx.stop_status = COMPLETED if plan is None or not plan.actions else INCOMPLETE
        guarded = ctx.guard.check(text) if text else None
        summary = guarded.display if guarded else self._default_summary(ctx)
        self._end(ctx, error_code, error_detail, summary, bool(guarded and guarded.flagged),
                  text if guarded else None)
        return self._outcome(ctx.run_id, request_id, trigger, mode, ctx.stop_status, tuple(ctx.calls), error_code,
                             error_detail, summary, bool(guarded and guarded.flagged), label=label)

    # ------------------------------------------------------------- 工具调用

    def invoke(self, ctx: RunContext, tool: str, args: Any, rationale: Any) -> dict[str, Any]:
        """守卫后的工具调用。返回 ToolResult.to_dict()；从不抛异常。"""
        seq = len(ctx.calls) + 1
        reason, truncated = clean_rationale(rationale)
        raw_args = args if isinstance(args, dict) else {"_raw": repr(args)}
        guard_code: str | None = None
        guard_message: str | None = None
        result: ToolResult | None = None
        clean: dict[str, Any] | None = None

        if ctx.stopped:
            guard_code, guard_message = RUN_NOT_ACTIVE, "本次运行已停止，不能再调用工具"
        elif self.monotonic() - ctx.started > self.settings.agent_run_timeout_s:
            ctx.stop(TIMEOUT)
            guard_code, guard_message = RUN_TIMEOUT, "超过本次运行的总时长上限"
        elif seq > self.settings.agent_max_tool_calls:
            ctx.stop(GUARD_STOPPED)
            guard_code, guard_message = RUN_STEP_LIMIT, "超过本次运行的工具调用次数上限"
        else:
            clean, problem = validate_args(tool, args)
            if clean is None:
                guard_code, guard_message = ToolCode.INVALID_ARGUMENTS.value, problem
            elif reason is None:
                guard_code, guard_message = ToolCode.INVALID_ARGUMENTS.value, "rationale 不能为空"
            elif clean["request_id"] != ctx.request_id:
                guard_code, guard_message = ACTION_NOT_ALLOWED, f"本次运行只处理请求 {ctx.request_id}"
            elif self._key(tool, clean) in ctx.rejected_keys:
                guard_code, guard_message = REPEATED_REJECTED_CALL, "这个调用已被拒绝过，不能以相同参数重复提交"
            elif tool != st.GET_STATUS:
                guard_code, guard_message = self._precheck(tool, clean)

        actor = Actor("agent", ctx.run_id)
        if guard_code is None:
            assert clean is not None
            result = at.call(self.rt, tool, clean, actor=actor)
        else:
            result = envelope(self.rt.conn, tool, ctx.request_id,
                              Outcome(ok=False, reason_codes=(guard_code,), message=guard_message))
        out = result.to_dict()
        out["guard_rejected"] = guard_code is not None

        display = ctx.guard.check(reason)  # 理由只能引用之前的工具结果中出现过的数值
        ctx.guard.learn(json.dumps(out, ensure_ascii=False))
        record = CallRecord(
            seq=seq, tool=tool, args=clean or raw_args, rationale=reason, rationale_display=display.display,
            rationale_flagged=display.flagged, ok=out["ok"], replayed=out["replayed"],
            reason_codes=tuple(out["reason_codes"]), guard_rejected=guard_code is not None,
            request_status=out["request_status"], allowed_next_actions=tuple(out["allowed_next_actions"]),
        )
        ctx.calls.append(record)
        self._audit_call(ctx, record, out, truncated, display.unverified)

        if not out["ok"] and guard_code != RUN_NOT_ACTIVE:
            ctx.rejections += 1
            if clean is not None:
                ctx.rejected_keys.add(self._key(tool, clean))
            if ctx.rejections >= self.settings.agent_max_rejections:
                ctx.stop(GUARD_STOPPED)
        if ctx.stopped or not out["allowed_next_actions"]:
            ctx.stop_loop = True
        return out

    # ------------------------------------------------------------- 内部辅助

    @staticmethod
    def _key(tool: str, args: dict[str, Any]) -> str:
        return tool + ":" + dumps(args)

    def _plan(self, request_id: str) -> st.Plan | None:
        try:
            return st.plan(st.load_state(self.rt.conn, request_id))
        except Rejection:
            return None

    def _precheck(self, tool: str, args: dict[str, Any]) -> tuple[str | None, str | None]:
        """用与工具相同的前置条件判断调用是否在当前 allowed_next_actions 中。"""
        try:
            state = st.load_state(self.rt.conn, args["request_id"])
            st.require_allowed(state, tool, **{k: v for k, v in args.items() if k != "request_id"})
        except Rejection as r:
            return r.codes[0], r.message
        return None, None

    def last_run_end(self, request_id: str) -> audit.AuditEvent | None:
        epoch = audit.current_epoch(self.rt.conn)
        events = audit.list_events(self.rt.conn, epoch=epoch, request_id=request_id, event_type="AGENT_RUN_ENDED")
        return events[-1] if events else None

    def _start(self, request_id: str, trigger: str, mode: AgentMode, label: str, *,
               resume_of: str | None) -> RunContext:
        conn = self.rt.conn
        with write_tx(conn):
            epoch = audit.current_epoch(conn)
            count = conn.execute(
                "SELECT COUNT(*) FROM audit_events WHERE epoch = ? AND event_type = 'AGENT_RUN_STARTED'", (epoch,)
            ).fetchone()[0]
            run_id = f"RUN-{count + 1:04d}"
            payload: dict[str, Any] = {
                "run_id": run_id, "trigger": trigger, "mode": mode.value, "mode_label": label,
                "resume_of": resume_of,
                "limits": {"max_tool_calls": self.settings.agent_max_tool_calls,
                           "max_rejections": self.settings.agent_max_rejections,
                           "run_timeout_s": self.settings.agent_run_timeout_s},
            }
            if mode is AgentMode.BEDROCK:
                payload.update(model_id=self.settings.bedrock_model_id, region=self.settings.aws_region)
            audit.append(conn, actor_type="agent", actor_id=run_id, event_type="AGENT_RUN_STARTED",
                         request_id=request_id, related_ids=[run_id], payload=payload, ts=self.rt.now())
        ctx = RunContext(run_id=run_id, request_id=request_id, trigger=trigger, mode=mode, started=self.monotonic())
        ctx.guard.learn(json.dumps({"run_id": run_id, "request_id": request_id}))
        return ctx

    def _audit_call(self, ctx: RunContext, record: CallRecord, result: dict[str, Any], truncated: bool,
                    unverified: tuple[str, ...]) -> None:
        try:
            with write_tx(self.rt.conn):
                audit.append(
                    self.rt.conn, actor_type="agent", actor_id=ctx.run_id, event_type="AGENT_TOOL_CALL",
                    outcome="OK" if record.ok else (record.reason_codes[0] if record.reason_codes else "REJECTED"),
                    request_id=ctx.request_id, item_id=record.args.get("item_id"),
                    related_ids=[ctx.run_id, *result.get("recorded", [])],
                    payload={
                        "run_id": ctx.run_id, "seq": record.seq, "tool": record.tool, "args": record.args,
                        "rationale": record.rationale, "rationale_display": record.rationale_display,
                        "rationale_flagged": record.rationale_flagged, "rationale_unverified": list(unverified),
                        "rationale_truncated": truncated, "guard_rejected": record.guard_rejected,
                        "result": result,
                    },
                    ts=self.rt.now(),
                )
        except Exception:  # noqa: BLE001
            log.exception("记录 AGENT_TOOL_CALL 失败")

    def _end(self, ctx: RunContext, error_code: str | None, error_detail: str | None, summary: str,
             flagged: bool, raw_text: str | None) -> None:
        with write_tx(self.rt.conn):
            audit.append(
                self.rt.conn, actor_type="agent", actor_id=ctx.run_id, event_type="AGENT_RUN_ENDED",
                outcome=ctx.stop_status or COMPLETED, request_id=ctx.request_id, related_ids=[ctx.run_id],
                payload={"run_id": ctx.run_id, "status": ctx.stop_status, "tool_calls": len(ctx.calls),
                         "rejections": ctx.rejections, "error_code": error_code, "error_detail": error_detail,
                         "summary": summary, "summary_flagged": flagged, "agent_text": raw_text},
                ts=self.rt.now(),
            )

    def _audit_refused(self, request_id: str, trigger: str, code: str, detail: str) -> None:
        with write_tx(self.rt.conn):
            audit.append(self.rt.conn, actor_type="system", actor_id="orchestrator", event_type="AGENT_RUN_REFUSED",
                         outcome=code, request_id=request_id,
                         payload={"trigger": trigger, "reason": detail}, ts=self.rt.now())

    def _default_summary(self, ctx: RunContext) -> str:
        plan = self._plan(ctx.request_id)
        try:
            status = RequestStatus(st.load_request(self.rt.conn, ctx.request_id).status).label
        except Rejection:
            status = "未知"
        waiting = {"INSPECTOR": "等待检查员提交结果", "BUYER": "等待买家确认报价", "DELIVERY": "等待交付",
                   "OPERATOR": "需要演示负责人处理"}.get(str(plan.awaiting) if plan and plan.awaiting else "", "")
        tools = "、".join(c.tool for c in ctx.calls) or "无"
        tail = f"，{waiting}" if waiting else ""
        return f"本次运行依次调用：{tools}。请求当前状态：{status}{tail}。"

    def _outcome(self, run_id: str | None, request_id: str, trigger: str, mode: AgentMode, status: str,
                 calls: tuple[CallRecord, ...], error_code: str | None, error_detail: str | None, summary: str,
                 flagged: bool, *, label: str | None = None) -> RunOutcome:
        plan = self._plan(request_id)
        try:
            request_status = st.load_request(self.rt.conn, request_id).status.value
        except Rejection:
            request_status = None
        return RunOutcome(
            run_id=run_id, request_id=request_id, trigger=trigger, mode=label or MODE_LABELS[mode], status=status,
            calls=calls, error_code=error_code, error_detail=error_detail, summary=summary, summary_flagged=flagged,
            request_status=request_status,
            awaiting_human=str(plan.awaiting) if plan and plan.awaiting else None,
            allowed_next_actions=plan.actions if plan else (),
        )
