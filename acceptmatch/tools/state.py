"""请求状态与前置条件（design §6.4）。

plan() 是唯一的下一步规则：
- 生成返回给 Agent 的 allowed_next_actions；
- Agent 工具在事务内用同一个 plan() 校验调用是否被允许，
  因此"允许列表"和"实际校验"不会出现不一致。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Mapping

from acceptmatch.domain.enums import (
    DefectType,
    ItemStatus,
    MatchResult,
    Point,
    ReasonCode,
    RequestStatus,
    Severity,
    Verdict,
)
from acceptmatch.domain.models import AcceptedDefect, BuyerRequestSpec, ItemSnapshot
from acceptmatch.store import repo
from acceptmatch.store.db import loads
from acceptmatch.store.repo import Meta
from acceptmatch.tools.envelope import Awaiting, NextAction, Rejection, ToolCode

# 工具名
EVALUATE = "evaluate_request"
DECIDE = "decide_inspection"
CREATE_TASK = "create_inspection_task"
BUILD_EVIDENCE = "build_evidence_package"
GENERATE_QUOTE = "generate_quote"
FINALIZE_NO_MATCH = "finalize_no_match"
GET_STATUS = "get_request_status"

AGENT_TOOLS = (GET_STATUS, EVALUATE, DECIDE, CREATE_TASK, BUILD_EVIDENCE, GENERATE_QUOTE, FINALIZE_NO_MATCH)

_EVALUATE_FROM = {
    RequestStatus.SUBMITTED,
    RequestStatus.INSPECTION_RESULT_RECORDED,
    RequestStatus.NEEDS_REEVALUATION,
}


@dataclass(frozen=True)
class Artifact:
    artifact_id: str
    kind: str
    request_id: str | None
    item_id: str | None
    condition_version: int | None
    status: str
    natural_key: str
    content: dict[str, Any]
    created_at: str


@dataclass(frozen=True)
class RequestRecord:
    request_id: str
    buyer_id: str
    product_id: str
    status: RequestStatus
    spec: BuyerRequestSpec
    exclusions: Mapping[str, tuple[ReasonCode, ...]]
    no_match: dict[str, Any] | None
    accepted_defects: list[dict[str, str]]
    required_conditions: list[str]


@dataclass(frozen=True)
class RequestState:
    request: RequestRecord
    meta: Meta
    items: Mapping[str, ItemSnapshot]
    evaluation: Artifact | None
    decisions: tuple[Artifact, ...]
    tasks: tuple[Artifact, ...]
    evidence_package: Artifact | None
    quote: Artifact | None
    reservation: Artifact | None


@dataclass(frozen=True)
class Plan:
    actions: tuple[NextAction, ...]
    awaiting: str | None


# ------------------------------------------------------------------ 读取


def _artifact(row: sqlite3.Row) -> Artifact:
    return Artifact(
        artifact_id=row["artifact_id"],
        kind=row["kind"],
        request_id=row["request_id"],
        item_id=row["item_id"],
        condition_version=row["condition_version"],
        status=row["status"],
        natural_key=row["natural_key"],
        content=loads(row["content_json"]),
        created_at=row["created_at"],
    )


def artifacts(conn: sqlite3.Connection, kind: str, *, request_id: str | None = None,
              item_id: str | None = None) -> list[Artifact]:
    sql, params = "SELECT * FROM artifacts WHERE kind = ?", [kind]
    if request_id is not None:
        sql += " AND request_id = ?"
        params.append(request_id)
    if item_id is not None:
        sql += " AND item_id = ?"
        params.append(item_id)
    return [_artifact(r) for r in conn.execute(sql + " ORDER BY rowid", params)]


def get_artifact(conn: sqlite3.Connection, artifact_id: str) -> Artifact | None:
    row = conn.execute("SELECT * FROM artifacts WHERE artifact_id = ?", (artifact_id,)).fetchone()
    return _artifact(row) if row else None


def find_by_key(conn: sqlite3.Connection, kind: str, natural_key: str) -> Artifact | None:
    row = conn.execute("SELECT * FROM artifacts WHERE kind = ? AND natural_key = ?", (kind, natural_key)).fetchone()
    return _artifact(row) if row else None


def list_requests(conn: sqlite3.Connection, buyer_id: str | None = None) -> list[RequestRecord]:
    """只读：按创建顺序列出请求（可按买家筛选）。"""
    sql, params = "SELECT request_id FROM buyer_requests", []
    if buyer_id is not None:
        sql += " WHERE buyer_id = ?"
        params.append(buyer_id)
    return [load_request(conn, r["request_id"]) for r in conn.execute(sql + " ORDER BY rowid", params)]


def tasks_for_request(conn: sqlite3.Connection, request_id: str) -> list[Artifact]:
    return [t for t in artifacts(conn, "INSPECTION_TASK") if request_id in t.content.get("request_ids", [])]


def spec_from_json(product_id: str, accepted: list[dict[str, str]], required: list[str]) -> BuyerRequestSpec:
    return BuyerRequestSpec(
        product_id=product_id,
        accepted_defects=tuple(
            AcceptedDefect(Point(a["point"]), DefectType(a["defect_type"]), Severity(a["max_severity"]))
            for a in accepted
        ),
        required_conditions=tuple(Point(p) for p in required),
    )


def load_request(conn: sqlite3.Connection, request_id: str) -> RequestRecord:
    row = conn.execute("SELECT * FROM buyer_requests WHERE request_id = ?", (request_id,)).fetchone()
    if row is None:
        raise Rejection(ToolCode.REQUEST_NOT_FOUND, message=f"请求 {request_id} 不存在")
    accepted = loads(row["accepted_defects_json"])
    required = loads(row["required_conditions_json"])
    exclusions = {k: tuple(ReasonCode(c) for c in v) for k, v in loads(row["exclusions_json"]).items()}
    return RequestRecord(
        request_id=row["request_id"],
        buyer_id=row["buyer_id"],
        product_id=row["product_id"],
        status=RequestStatus(row["status"]),
        spec=spec_from_json(row["product_id"], accepted, required),
        exclusions=exclusions,
        no_match=loads(row["no_match_json"]),
        accepted_defects=accepted,
        required_conditions=required,
    )


def load_state(conn: sqlite3.Connection, request_id: str) -> RequestState:
    request = load_request(conn, request_id)
    meta = repo.get_meta(conn)
    if meta is None:
        raise Rejection(ToolCode.INVALID_STATE, message="演示尚未初始化")
    items = {s.item_id: s for s in repo.list_item_snapshots(conn)}
    evaluations = artifacts(conn, "EVALUATION", request_id=request_id)
    packages = artifacts(conn, "EVIDENCE_PACKAGE", request_id=request_id)
    quotes = artifacts(conn, "QUOTE", request_id=request_id)
    reservations = artifacts(conn, "RESERVATION", request_id=request_id)
    return RequestState(
        request=request,
        meta=meta,
        items=items,
        evaluation=evaluations[-1] if evaluations else None,
        decisions=tuple(artifacts(conn, "DECISION", request_id=request_id)),
        tasks=tuple(tasks_for_request(conn, request_id)),
        evidence_package=packages[-1] if packages else None,
        quote=quotes[-1] if quotes else None,
        reservation=reservations[-1] if reservations else None,
    )


# ------------------------------------------------------------------ 规则


def evaluation_fresh(state: RequestState) -> bool:
    """最近一次评估所依据的版本和可售状态是否仍是当前值。"""
    ev = state.evaluation
    if ev is None:
        return False
    versions = {i: s.condition_version for i, s in state.items.items()}
    statuses = {i: s.item_status.value for i, s in state.items.items()}
    return ev.content["versions"] == versions and ev.content["statuses"] == statuses


def evaluation_results(state: RequestState) -> dict[str, dict[str, Any]]:
    assert state.evaluation is not None
    return {r["item_id"]: r for r in state.evaluation.content["results"]}


def qualified_ids(state: RequestState) -> list[str]:
    results = evaluation_results(state)
    return [i for i in state.evaluation.content["ranking"] if results[i]["result"] == MatchResult.QUALIFIED]


def next_candidate(state: RequestState) -> str | None:
    """排名中第一个未被排除的 NEEDS_INSPECTION 商品。"""
    results = evaluation_results(state)
    for item_id in state.evaluation.content["ranking"]:
        if results[item_id]["result"] == MatchResult.NEEDS_INSPECTION and item_id not in state.request.exclusions:
            return item_id
    return None


def decision_key(request_id: str, item_id: str, condition_version: int, parameter_set: str,
                 budget_version: int) -> str:
    return f"{request_id}:{item_id}:v{condition_version}:{parameter_set}:b{budget_version}"


def current_decision(state: RequestState, item_id: str) -> Artifact | None:
    item = state.items[item_id]
    key = decision_key(state.request.request_id, item_id, item.condition_version, state.meta.parameter_set,
                       state.meta.budget_version)
    for d in reversed(state.decisions):
        if d.natural_key == key:
            return d
    return None


def plan(state: RequestState) -> Plan:
    rid = state.request.request_id
    status = state.request.status

    def act(tool: str, **args: Any) -> Plan:
        return Plan((NextAction(tool, {"request_id": rid, **args}),), None)

    if status in _EVALUATE_FROM:
        return act(EVALUATE)
    if status is RequestStatus.EVALUATED:
        if not evaluation_fresh(state):
            return act(EVALUATE)
        qualified = qualified_ids(state)
        if qualified:
            return act(BUILD_EVIDENCE, item_id=qualified[0])
        candidate = next_candidate(state)
        if candidate is None:
            return act(FINALIZE_NO_MATCH)
        decision = current_decision(state, candidate)
        if decision is None:
            return act(DECIDE, item_id=candidate)
        if decision.content["verdict"] == Verdict.INSPECT:
            return act(CREATE_TASK, item_id=candidate, decision_id=decision.artifact_id)
        # SKIP 判定会同时写入排除项，正常不会走到这里；保守起见不给出任何动作
        return Plan((), Awaiting.OPERATOR)
    if status is RequestStatus.EVIDENCE_READY and state.evidence_package is not None:
        pkg = state.evidence_package
        item = state.items[pkg.item_id]
        if pkg.condition_version != item.condition_version or item.item_status is not ItemStatus.AVAILABLE:
            return act(EVALUATE)  # 证据整理后商品变了：先重新评估，不能按旧证据报价
        return act(GENERATE_QUOTE, item_id=pkg.item_id, evidence_package_id=pkg.artifact_id)
    if status is RequestStatus.WAITING_FOR_INSPECTION:
        return Plan((), Awaiting.INSPECTOR)
    if status is RequestStatus.OFFERED:
        return Plan((), Awaiting.BUYER)
    if status is RequestStatus.RESERVED:
        return Plan((), Awaiting.DELIVERY)
    return Plan((), None)


def require_allowed(state: RequestState, tool: str, **args: Any) -> None:
    """调用必须与 plan() 给出的某个下一步完全一致，否则抛出带原因码的 Rejection。"""
    call = NextAction(tool, {"request_id": state.request.request_id, **args})
    allowed = plan(state).actions
    if call in allowed:
        return
    raise Rejection(_rejection_code(state, tool, args), message=_describe(allowed))


def _describe(allowed: tuple[NextAction, ...]) -> str:
    if not allowed:
        return "当前没有允许的 Agent 动作"
    return "当前允许：" + "；".join(f"{a.tool}({', '.join(f'{k}={v}' for k, v in a.args.items())})" for a in allowed)


def _rejection_code(state: RequestState, tool: str, args: dict[str, Any]) -> ToolCode:
    status = state.request.status
    if tool == EVALUATE:
        return ToolCode.INVALID_STATE
    if tool == GENERATE_QUOTE:
        if status is not RequestStatus.EVIDENCE_READY:
            return ToolCode.INVALID_STATE
        if plan(state).actions and plan(state).actions[0].tool == EVALUATE:
            return ToolCode.EVALUATION_STALE
        return ToolCode.EVIDENCE_PACKAGE_MISMATCH
    if status is not RequestStatus.EVALUATED:
        return ToolCode.INVALID_STATE
    if not evaluation_fresh(state):
        return ToolCode.EVALUATION_STALE
    if tool == BUILD_EVIDENCE:
        return ToolCode.NOT_TOP_QUALIFIED
    if qualified_ids(state):
        return ToolCode.INVALID_STATE  # 存在可直接报价的商品，不做经济判断、不建检查、不宣布无匹配
    if tool == FINALIZE_NO_MATCH:
        return ToolCode.CANDIDATES_REMAIN
    if tool == CREATE_TASK:
        decision = next((d for d in state.decisions if d.artifact_id == args.get("decision_id")), None)
        if decision is None or decision.item_id != args.get("item_id"):
            return ToolCode.DECISION_NOT_FOUND
        if decision.content["verdict"] != Verdict.INSPECT:
            return ToolCode.DECISION_NOT_INSPECT
        if current_decision(state, decision.item_id) != decision:
            return ToolCode.DECISION_STALE
    return ToolCode.OUT_OF_ORDER
