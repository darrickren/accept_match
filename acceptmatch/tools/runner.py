"""工具执行框架：一个调用 = 一个写事务。

- 正常完成：业务写入和审计在同一事务内提交。
- 抛出 Rejection：回滚全部业务写入（状态不变），再在单独事务中追加一条 OPERATION_REJECTED。
- 未预期异常：同样回滚，返回 INTERNAL_ERROR。工具永远不向调用方抛异常。
- 返回值中的 request_status / allowed_next_actions 在操作之后重新读取并由 plan() 计算。
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Any, Callable

from acceptmatch.store import audit
from acceptmatch.store.db import dumps, loads, write_tx
from acceptmatch.tools import state as st
from acceptmatch.tools.envelope import Actor, Rejection, ToolCode, ToolResult
from acceptmatch.tools.runtime import Runtime

log = logging.getLogger(__name__)


@dataclass
class Outcome:
    ok: bool = True
    data: dict[str, Any] | None = None
    recorded: tuple[str, ...] = ()
    replayed: bool = False
    reason_codes: tuple[str, ...] = ()
    message: str | None = None
    request_id: str | None = None  # 新建请求或由任务推出的请求


def run(
    rt: Runtime,
    tool: str,
    actor: Actor,
    args: dict[str, Any],
    request_id: str | None,
    body: Callable[[], Outcome],
) -> ToolResult:
    try:
        with write_tx(rt.conn):
            outcome = body()
            return envelope(rt.conn, tool, outcome.request_id or request_id, outcome)
    except Rejection as r:
        _audit_rejection(rt, tool, actor, args, request_id, r.codes, r.message)
        return envelope(rt.conn, tool, request_id,
                         Outcome(ok=False, reason_codes=r.codes, message=r.message))
    except Exception as exc:  # noqa: BLE001 - 工具不能把异常抛给 Agent
        log.exception("工具 %s 执行失败", tool)
        message = f"{type(exc).__name__}: {exc}"
        _audit_rejection(rt, tool, actor, args, request_id, (ToolCode.INTERNAL_ERROR,), message)
        return envelope(rt.conn, tool, request_id,
                         Outcome(ok=False, reason_codes=(ToolCode.INTERNAL_ERROR,), message=message))


def envelope(conn: sqlite3.Connection, tool: str, request_id: str | None, outcome: Outcome) -> ToolResult:
    status, actions, awaiting = None, (), None
    if request_id is not None:
        try:
            state = st.load_state(conn, request_id)
        except Rejection:
            state = None
        if state is not None:
            plan = st.plan(state)
            status, actions, awaiting = state.request.status.value, plan.actions, plan.awaiting
    return ToolResult(
        ok=outcome.ok,
        tool=tool,
        request_id=request_id,
        reason_codes=tuple(str(c) for c in outcome.reason_codes),
        # 统一成 JSON 形式（金额为两位小数字符串），首次调用和重放的返回完全一致
        data=None if outcome.data is None else loads(dumps(outcome.data)),
        recorded=outcome.recorded,
        replayed=outcome.replayed,
        request_status=status,
        allowed_next_actions=actions,
        awaiting_human=str(awaiting) if awaiting else None,
        message=outcome.message,
    )


def _audit_rejection(
    rt: Runtime,
    tool: str,
    actor: Actor,
    args: dict[str, Any],
    request_id: str | None,
    codes: tuple[str, ...],
    message: str | None,
) -> None:
    try:
        with write_tx(rt.conn):
            # 请求不存在时不写 request_id，避免把审计挂到不存在的请求上
            known = request_id is not None and rt.conn.execute(
                "SELECT 1 FROM buyer_requests WHERE request_id = ?", (request_id,)).fetchone() is not None
            audit.append(
                rt.conn,
                actor_type=actor.actor_type,
                actor_id=actor.actor_id,
                event_type="OPERATION_REJECTED",
                outcome=str(codes[0]),
                item_id=args.get("item_id"),
                request_id=request_id if known else None,
                payload={"tool": tool, "args": args, "reason_codes": list(codes), "message": message},
                ts=rt.now(),
            )
    except sqlite3.Error:
        log.exception("记录拒绝审计失败")


def require_actor(actor: Actor, *allowed_types: str) -> None:
    if actor.actor_type not in allowed_types:
        raise Rejection(ToolCode.ACTOR_NOT_ALLOWED,
                        message=f"该操作只能由 {'/'.join(allowed_types)} 发起，当前为 {actor.actor_type}")
