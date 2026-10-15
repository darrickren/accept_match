"""人工输入之后继续推进流程。

Agent 运行在"等待检查员 / 等待买家"时结束；页面上的人工操作成功（或确认被拒且需要重新评估）后，
调用 continue_after() 为相关请求启动新的运行。人工操作本身不经过 Agent。
"""

from __future__ import annotations

from acceptmatch.agent.orchestrator import Orchestrator, RunOutcome
from acceptmatch.tools import human_ops as ho
from acceptmatch.tools import state as st
from acceptmatch.tools.envelope import Rejection, ToolResult

TRIGGERS = {
    ho.SUBMIT_REQUEST: "REQUEST_SUBMITTED",
    ho.SUBMIT_INSPECTION: "INSPECTION_RESULT_RECORDED",
    ho.CONFIRM: "REEVALUATION_REQUIRED",
}


def continue_after(orch: Orchestrator, result: ToolResult) -> list[RunOutcome]:
    trigger = TRIGGERS.get(result.tool)
    if trigger is None:
        return []
    if result.tool == ho.SUBMIT_INSPECTION:
        request_ids = list((result.data or {}).get("requests_updated", [])) if result.ok else []
    else:
        request_ids = [result.request_id] if result.request_id else []
    runs = []
    for request_id in request_ids:
        try:
            plan = st.plan(st.load_state(orch.rt.conn, request_id))
        except Rejection:
            continue
        if plan.actions:  # 只有存在可由 Agent 执行的下一步时才启动运行
            runs.append(orch.run(request_id, trigger))
    return runs
