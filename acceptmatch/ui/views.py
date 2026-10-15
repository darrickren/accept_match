"""页面用的只读视图（不写数据库）。

所有数据都来自现有的只读查询：store.repo、store.audit、tools.state。
面向买家的内容不包含经济参数；经济判断只出现在控制台视图中。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from acceptmatch.config import AgentMode, Settings
from acceptmatch.domain.enums import (
    DefectType,
    EvidenceSource,
    ItemStatus,
    NoInspectionOptionKind,
    Point,
    PointStatus,
    RequestStatus,
    Severity,
)
from acceptmatch.store import audit, repo
from acceptmatch.tools import state as st
from acceptmatch.tools.envelope import Rejection
from acceptmatch.ui import messages


# ------------------------------------------------------------- 编排模式


@dataclass(frozen=True)
class ModeInfo:
    kind: str  # scripted / stub / bedrock
    label: str
    description: str
    ready: bool
    problem: str | None


def mode_info(settings: Settings) -> ModeInfo:
    mode = settings.agent_mode
    if mode is AgentMode.SCRIPTED:
        return ModeInfo("scripted", "离线脚本编排（非 Bedrock）",
                        "按 allowed_next_actions 的第一项依次执行，不调用任何模型。", True, None)
    if mode is AgentMode.STUB:
        return ModeInfo("stub", "Strands + 替身模型（非 Bedrock）",
                        "在真实的 Strands Agent 循环中运行，由本地替身模型选择下一步，不访问网络。", True, None)
    try:
        model_id, region = settings.require_bedrock()
    except Exception as exc:  # noqa: BLE001 - ConfigError
        return ModeInfo("bedrock", "Strands + Amazon Bedrock", "需要先设置环境变量。", False, str(exc))
    return ModeInfo("bedrock", "Strands + Amazon Bedrock",
                    f"模型：{model_id}｜区域：{region}（来自环境变量）", True, None)


# ------------------------------------------------------------- 通用标签


def point_label(p: str) -> str:
    return Point(p).label


def status_label(status: str) -> str:
    return PointStatus(status).label


def defect_text(defect_type: str, severity: str) -> str:
    return f"{DefectType(defect_type).label}（{Severity(severity).label}）"


def request_status_label(status: str) -> str:
    return RequestStatus(status).label


def item_status_label(status: str) -> str:
    return ItemStatus(status).label


def accepted_text(a: dict[str, str]) -> str:
    return f"{point_label(a['point'])}{DefectType(a['defect_type']).label}（最高{Severity(a['max_severity']).label}）"


# ------------------------------------------------------------- 侧边栏


def meta(conn: sqlite3.Connection) -> repo.Meta | None:
    return repo.get_meta(conn)


# ------------------------------------------------------------- 买家


def buyer_requests(conn: sqlite3.Connection, buyer_id: str) -> list[st.RequestRecord]:
    return st.list_requests(conn, buyer_id)


def request_view(conn: sqlite3.Connection, request_id: str) -> dict[str, Any] | None:
    """买家页面需要的全部内容（不含经济参数）。"""
    try:
        s = st.load_state(conn, request_id)
    except Rejection:
        return None
    req = s.request
    view: dict[str, Any] = {
        "request_id": req.request_id,
        "status": req.status.value,
        "status_label": req.status.label,
        "echo": messages.request_echo(req.spec),
        "accepted": [accepted_text(a) for a in req.accepted_defects],
        "required": [point_label(p) for p in req.required_conditions],
        "waiting": None,
        "evidence": None,
        "quote": None,
        "reservation": None,
        "no_match": req.no_match,
    }
    open_tasks = [t for t in s.tasks if t.status == "OPEN"]
    if req.status is RequestStatus.WAITING_FOR_INSPECTION and open_tasks:
        task = open_tasks[-1]
        view["waiting"] = messages.waiting_text(task.item_id, [p["point"] for p in task.content["points"]])
    if s.quote is not None and s.quote.status in ("ACTIVE", "ACCEPTED") and s.evidence_package is not None:
        view["evidence"] = s.evidence_package.content
        view["quote"] = {**s.quote.content, "status": s.quote.status}
    if s.reservation is not None:
        view["reservation"] = {"reservation_id": s.reservation.artifact_id, **s.reservation.content}
    return view


def source_label(source: str) -> str:
    return EvidenceSource(source).label


# ------------------------------------------------------------- 检查员


def open_tasks(conn: sqlite3.Connection) -> list[st.Artifact]:
    return [t for t in st.artifacts(conn, "INSPECTION_TASK") if t.status == "OPEN"]


def evidence_ref_options(item_id: str, point: str) -> list[str]:
    base = f"synthetic://inspection/{item_id}/{point.lower()}"
    return [f"{base}-1", f"{base}-2"]


# ------------------------------------------------------------- 控制台


def items_table(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = []
    for s in repo.list_item_snapshots(conn):
        row = {"商品": s.item_id, "型号": s.product_id, "标签码": s.tag_code,
               "状态": s.item_status.label, "状态版本": s.condition_version}
        for p in Point:
            state = s.point(p)
            text = state.status.label
            if state.defects:
                text += "：" + "、".join(defect_text(d.type, d.severity) for d in state.defects)
            row[p.label] = text
        row["合成数据"] = "是"
        rows.append(row)
    return rows


def requests_table(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [{"请求": r.request_id, "买家": r.buyer_id, "型号": r.product_id, "状态": r.status.label,
             "接受的缺陷": "；".join(accepted_text(a) for a in r.accepted_defects) or "无",
             "要求完好": "、".join(point_label(p) for p in r.required_conditions) or "无",
             "请求级排除": "；".join(f"{k}：{'、'.join(v)}" for k, v in r.exclusions.items()) or "无"}
            for r in st.list_requests(conn)]


_PARAM_ROWS = [
    ("C", "检查成本"), ("p", "通过概率"), ("H", "交付处理成本"), ("R_fail", "失败后净回收"),
    ("B", "检查预算余额"), ("pass_quote", "通过后报价（按价格规则计算）"),
]


def decision_view(decision: st.Artifact) -> dict[str, Any]:
    c = decision.content
    inputs = c["inputs"]
    v_none_kind = (c.get("v_none") or {}).get("kind")
    budget = c.get("budget_check")
    return {
        "title": f"{c['decision_id']} · {c['item_id']} · 请求 {c['request_id']} · 判定 {c['verdict']}",
        "verdict": c["verdict"],
        "reason_codes": c["reason_codes"],
        "inputs": [{"参数": name, "符号": key, "值": inputs.get(key) if inputs.get(key) is not None else "缺失",
                    "来源": "合成" if key != "pass_quote" else "价格规则（合成）"} for key, name in _PARAM_ROWS],
        "steps": [{"步骤": s["name"], "算式": s["expr"], "结果": s["value"]} for s in c["steps"]],
        "options": [{"不检查选项": o["label"], "类型": NoInspectionOptionKind(o["kind"]).label, "净回收": o["net"],
                     "被选为 V_none": "是" if o["kind"] == v_none_kind else ""}
                    for o in inputs["no_inspection_options"]],
        "budget": (f"C = {budget['C']} {'≤' if budget['ok'] else '>'} B = {budget['B']}" if budget else "参数无效，未比较"),
        "recommended": c.get("recommended_disposition"),
        "invalid_inputs": c.get("invalid_inputs", []),
        "meta": f"状态版本 {c['condition_version']}｜参数组 {c['parameter_set']}｜预算版本 {c['budget_version']}｜"
                f"待检查部位 {'、'.join(point_label(p) for p in c['inspection_points'])}",
    }


def decisions(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [decision_view(d) for d in st.artifacts(conn, "DECISION")]


def agent_runs(conn: sqlite3.Connection, epoch: int) -> list[dict[str, Any]]:
    """按运行汇总 AGENT_RUN_STARTED / AGENT_TOOL_CALL / AGENT_RUN_ENDED（最新在前）。"""
    runs: dict[str, dict[str, Any]] = {}
    for e in audit.list_events(conn, epoch=epoch):
        run_id = e.payload.get("run_id") if isinstance(e.payload, dict) else None
        if e.event_type == "AGENT_RUN_STARTED":
            runs[run_id] = {"run_id": run_id, "request_id": e.request_id, "trigger": e.payload["trigger"],
                            "mode_label": e.payload["mode_label"], "started": e.ts, "calls": [], "end": None,
                            "resume_of": e.payload.get("resume_of")}
        elif e.event_type == "AGENT_TOOL_CALL" and run_id in runs:
            p = e.payload
            runs[run_id]["calls"].append({
                "序号": p["seq"], "工具": p["tool"], "参数": json.dumps(p["args"], ensure_ascii=False),
                "结果": "成功" + ("（重放）" if p["result"]["replayed"] else "") if p["result"]["ok"]
                else "被拒：" + "、".join(p["result"]["reason_codes"]),
                "理由（经数值守卫）": p["rationale_display"] or "（无）",
                "调用后状态": p["result"]["request_status"] or "",
            })
        elif e.event_type == "AGENT_RUN_ENDED" and run_id in runs:
            runs[run_id]["end"] = e.payload
    return list(reversed(list(runs.values())))


def refused_runs(conn: sqlite3.Connection, epoch: int) -> list[dict[str, Any]]:
    return [{"时间": e.ts, "请求": e.request_id, "原因": e.outcome, "说明": e.payload.get("reason")}
            for e in audit.list_events(conn, epoch=epoch, event_type="AGENT_RUN_REFUSED")]


def audit_rows(conn: sqlite3.Connection, *, epoch: int | None, item_id: str | None,
               request_id: str | None) -> list[dict[str, Any]]:
    rows = []
    for e in audit.list_events(conn, epoch=epoch, item_id=item_id, request_id=request_id):
        payload = json.dumps(e.payload, ensure_ascii=False)
        rows.append({"轮次": e.epoch, "事件": e.event_id, "时间": e.ts, "操作者": f"{e.actor_type}:{e.actor_id}",
                     "类型": e.event_type, "商品": e.item_id or "", "请求": e.request_id or "", "结果": e.outcome,
                     "相关编号": "、".join(e.related_ids),
                     "内容": payload if len(payload) <= 300 else payload[:300] + "…"})
    return rows
