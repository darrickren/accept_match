"""7 个 Agent 工具（design §6.2，需求 3–9、12）。

每个写工具：
1. 在 BEGIN IMMEDIATE 事务内读取当前状态；
2. 若按自然键已存在相同结果，直接重放（replayed=True），不再写入；
3. 否则用 state.require_allowed() 校验调用与 plan() 给出的下一步完全一致；
4. 调用领域层纯函数，写入业务表和审计，一起提交。
被拒的调用回滚全部业务写入（见 runner.run）。
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from typing import Any

from acceptmatch.domain import economics, matching, pricing
from acceptmatch.domain.economics import EconomicsInputs, NoInspectionChoice
from acceptmatch.domain.enums import MatchResult, Point, ReasonCode, RequestStatus, Verdict
from acceptmatch.domain.matching import ItemMatch
from acceptmatch.domain.models import ItemSnapshot
from acceptmatch.domain.pricing import PricingError
from acceptmatch.store import audit, ids, repo
from acceptmatch.store.db import cents, dumps
from acceptmatch.tools import state as st
from acceptmatch.tools.envelope import AGENT, Actor, Rejection, ToolCode, ToolResult
from acceptmatch.tools.runner import Outcome, run
from acceptmatch.tools.runtime import Runtime
from acceptmatch.ui import messages


# ------------------------------------------------------------------ 公共


def _item(state: st.RequestState, item_id: str) -> ItemSnapshot:
    try:
        return state.items[item_id]
    except KeyError:
        raise Rejection(ToolCode.INVALID_ARGUMENTS, message=f"商品 {item_id} 不存在") from None


def _set_status(conn: sqlite3.Connection, request_id: str, status: RequestStatus) -> None:
    conn.execute("UPDATE buyer_requests SET status = ? WHERE request_id = ?", (status.value, request_id))


def _insert_artifact(conn: sqlite3.Connection, rt: Runtime, *, artifact_id: str, kind: str, request_id: str | None,
                     item_id: str | None, condition_version: int | None, status: str, natural_key: str,
                     content: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO artifacts (artifact_id, kind, request_id, item_id, condition_version, status, natural_key, "
        "content_json, created_at, synthetic) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
        (artifact_id, kind, request_id, item_id, condition_version, status, natural_key, dumps(content), rt.now()),
    )


def _audit(rt: Runtime, actor: Actor, event_type: str, *, request_id: str | None = None, item_id: str | None = None,
           related: tuple[str, ...] | list[str] = (), payload: dict[str, Any] | None = None) -> None:
    audit.append(rt.conn, actor_type=actor.actor_type, actor_id=actor.actor_id, event_type=event_type,
                 request_id=request_id, item_id=item_id, related_ids=related, payload=payload, ts=rt.now())


def _match_dict(m: ItemMatch, item: ItemSnapshot) -> dict[str, Any]:
    defects = {d.defect_id: d for _, d in item.verified_defects()}
    details = []
    for d in m.details:
        entry: dict[str, Any] = {"code": d.code.value, "point": d.point.value if d.point else None,
                                 "defect_id": d.defect_id}
        if d.defect_id in defects:
            entry["defect_type"] = defects[d.defect_id].type.value
            entry["severity"] = defects[d.defect_id].severity.value
        details.append(entry)
    return {
        "item_id": m.item_id,
        "condition_version": m.condition_version,
        "result": m.result.value,
        "reason_codes": [c.value for c in m.reason_codes],
        "details": details,
        "unknown_points": [p.value for p in m.unknown_points],
    }


# ------------------------------------------------------- get_request_status


def get_request_status(rt: Runtime, request_id: str, actor: Actor = AGENT) -> ToolResult:
    """只读：请求的当前状态、评估摘要、判定、任务、证据包、报价和预留。"""

    def body() -> Outcome:
        rt.require_meta()
        s = st.load_state(rt.conn, request_id)
        evaluation = None
        if s.evaluation is not None:
            results = st.evaluation_results(s)
            evaluation = {
                "evaluation_id": s.evaluation.artifact_id,
                "fresh": st.evaluation_fresh(s),
                "ranking": s.evaluation.content["ranking"],
                "qualified": st.qualified_ids(s),
                "needs_inspection": [i for i, r in results.items() if r["result"] == MatchResult.NEEDS_INSPECTION],
                "excluded": {i: r["reason_codes"] for i, r in results.items() if r["result"] == MatchResult.EXCLUDED},
            }
        open_tasks = [t.artifact_id for t in s.tasks if t.status == "OPEN"]
        return Outcome(data={
            "request": {
                "request_id": s.request.request_id,
                "buyer_id": s.request.buyer_id,
                "product_id": s.request.product_id,
                "status": s.request.status.value,
                "accepted_defects": s.request.accepted_defects,
                "required_conditions": s.request.required_conditions,
                "exclusions": {k: [c.value for c in v] for k, v in s.request.exclusions.items()},
            },
            "evaluation": evaluation,
            "decisions": [{"decision_id": d.artifact_id, "item_id": d.item_id, "verdict": d.content["verdict"],
                           "reason_codes": d.content["reason_codes"]} for d in s.decisions],
            "open_task_id": open_tasks[-1] if open_tasks else None,
            "evidence_package_id": s.evidence_package.artifact_id if s.evidence_package else None,
            "quote": ({"quote_id": s.quote.artifact_id, "status": s.quote.status,
                       "amount": s.quote.content["amount"]} if s.quote else None),
            "reservation_id": s.reservation.artifact_id if s.reservation else None,
            "synthetic": True,
        })

    # 只读工具也走同一框架：事务内读取，保证一致性；不写任何业务数据
    return run(rt, st.GET_STATUS, actor, {"request_id": request_id}, request_id, body)


# ------------------------------------------------------- evaluate_request


def evaluate_request(rt: Runtime, request_id: str, actor: Actor = AGENT) -> ToolResult:
    """逐件匹配（需求 3）；从检查结果进入时记录检查是否通过（需求 6.4–6.6）。"""

    def body() -> Outcome:
        rt.require_meta()
        conn = rt.conn
        s = st.load_state(conn, request_id)
        if s.request.status is RequestStatus.EVALUATED and st.evaluation_fresh(s):
            return Outcome(data=s.evaluation.content, replayed=True)
        st.require_allowed(s, st.EVALUATE)

        ev = matching.evaluate(s.items.values(), s.request.spec, s.request.exclusions)
        results = [_match_dict(m, s.items[m.item_id]) for m in ev.results]

        inspection_outcome = None
        if s.request.status is RequestStatus.INSPECTION_RESULT_RECORDED:
            completed = [t for t in s.tasks if t.status == "COMPLETED"]
            if completed:
                task = completed[-1]
                m = ev.result_for(task.item_id)
                if ReasonCode.INSPECTION_INCONCLUSIVE in m.reason_codes:
                    verdict = "INCONCLUSIVE"
                elif m.result is MatchResult.QUALIFIED:
                    verdict = "PASS"
                else:
                    verdict = "FAIL"
                inspection_outcome = {"task_id": task.artifact_id, "item_id": task.item_id, "outcome": verdict}

        superseded = [r[0] for r in conn.execute(
            "SELECT artifact_id FROM artifacts WHERE kind = 'QUOTE' AND request_id = ? AND status = 'ACTIVE'",
            (request_id,))]
        if superseded:  # 重新评估时，旧报价一律失效
            conn.execute("UPDATE artifacts SET status = 'SUPERSEDED' WHERE kind = 'QUOTE' AND request_id = ? "
                         "AND status = 'ACTIVE'", (request_id,))

        eval_id = ids.next_artifact_id(conn, "EVALUATION")
        content = {
            "evaluation_id": eval_id,
            "results": results,
            "ranking": list(ev.ranking),
            "versions": {i: snap.condition_version for i, snap in s.items.items()},
            "statuses": {i: snap.item_status.value for i, snap in s.items.items()},
            "exclusions": {k: [c.value for c in v] for k, v in s.request.exclusions.items()},
            "inspection_outcome": inspection_outcome,
            "synthetic": True,
        }
        _insert_artifact(conn, rt, artifact_id=eval_id, kind="EVALUATION", request_id=request_id, item_id=None,
                         condition_version=None, status="RECORDED", natural_key=eval_id, content=content)
        _set_status(conn, request_id, RequestStatus.EVALUATED)
        _audit(rt, actor, "MATCH_EVALUATED", request_id=request_id, related=[eval_id, *superseded],
               payload={"evaluation_id": eval_id, "ranking": list(ev.ranking),
                        "qualified": list(ev.qualified), "needs_inspection": list(ev.needs_inspection),
                        "inspection_outcome": inspection_outcome, "superseded_quotes": superseded})
        return Outcome(data=content, recorded=(eval_id, *superseded))

    return run(rt, st.EVALUATE, actor, {"request_id": request_id}, request_id, body)


# ------------------------------------------------------- decide_inspection


def _economics_inputs(rt: Runtime, s: st.RequestState, item: ItemSnapshot) -> tuple[EconomicsInputs, list[str]]:
    params = rt.economics(s.meta).items.get(item.item_id)
    unknown = [p.value for p in item.unknown_points()]
    problems: list[str] = []
    pass_quote: Decimal | None = None
    try:
        pass_quote = pricing.price_item(item, rt.base_price(item.product_id), rt.price_rules,
                                        assume_unknown_ok=True).amount
    except PricingError as exc:
        problems.append(f"pass_quote：{exc.code}")
    if params is None:
        problems.append("未配置该商品的检查经济参数")
        return EconomicsInputs(None, None, None, None, s.meta.budget_remaining, pass_quote, (),
                               input_problems=tuple(problems)), unknown
    if sorted(p.value for p in params.inspection_points) != sorted(unknown):
        problems.append(f"inspection_points 与未核实部位不一致：参数 {[p.value for p in params.inspection_points]}，"
                        f"未核实 {unknown}")
    return EconomicsInputs(
        C=params.C, p=params.p, H=params.H, R_fail=params.R_fail, B=s.meta.budget_remaining, pass_quote=pass_quote,
        options=tuple(NoInspectionChoice(o.kind, o.label, o.net) for o in params.no_inspection_options),
        input_problems=tuple(problems),
    ), unknown


def _choice(c: NoInspectionChoice | None) -> dict[str, Any] | None:
    return None if c is None else {"kind": c.kind.value if hasattr(c.kind, "value") else str(c.kind),
                                   "label": c.label, "net": c.net}


def decide_inspection(rt: Runtime, request_id: str, item_id: str, actor: Actor = AGENT) -> ToolResult:
    """检查前的经济判断（需求 4）。判定为 SKIP 时写入请求级排除项。"""

    def body() -> Outcome:
        meta = rt.require_meta()
        conn = rt.conn
        s = st.load_state(conn, request_id)
        item = _item(s, item_id)
        key = st.decision_key(request_id, item_id, item.condition_version, meta.parameter_set, meta.budget_version)
        existing = st.find_by_key(conn, "DECISION", key)
        if existing is not None:
            return Outcome(data=existing.content, replayed=True)
        st.require_allowed(s, st.DECIDE, item_id=item_id)

        inputs, unknown = _economics_inputs(rt, s, item)
        result = economics.decide(inputs)
        decision_id = ids.next_artifact_id(conn, "DECISION")
        content = {
            "decision_id": decision_id,
            "request_id": request_id,
            "item_id": item_id,
            "condition_version": item.condition_version,
            "parameter_set": meta.parameter_set,
            "budget_version": meta.budget_version,
            "inspection_points": unknown,
            "inputs": {
                "C": inputs.C, "p": inputs.p, "H": inputs.H, "R_fail": inputs.R_fail, "B": inputs.B,
                "pass_quote": inputs.pass_quote,
                "no_inspection_options": [_choice(o) for o in inputs.options],
                "synthetic": True,
            },
            "steps": [{"name": x.name, "expr": x.expr, "value": x.value,
                       **({"option": x.option.value} if x.option else {})} for x in result.steps],
            "ev_inspect": result.ev_inspect,
            "v_none": _choice(result.v_none),
            "delta_ev": result.delta_ev,
            "budget_check": (None if result.budget_check is None else
                             {"C": result.budget_check.C, "B": result.budget_check.B, "ok": result.budget_check.ok}),
            "verdict": result.verdict.value,
            "reason_codes": [c.value for c in result.reason_codes],
            "recommended_disposition": _choice(result.recommended_disposition),
            "invalid_inputs": list(result.invalid_inputs),
            "synthetic": True,
        }
        _insert_artifact(conn, rt, artifact_id=decision_id, kind="DECISION", request_id=request_id, item_id=item_id,
                         condition_version=item.condition_version, status="RECORDED", natural_key=key,
                         content=content)
        if result.verdict is Verdict.SKIP:
            exclusions = {k: [c.value for c in v] for k, v in s.request.exclusions.items()}
            merged = exclusions.get(item_id, [])
            merged += [c.value for c in result.reason_codes if c.value not in merged]
            exclusions[item_id] = merged
            conn.execute("UPDATE buyer_requests SET exclusions_json = ? WHERE request_id = ?",
                         (dumps(exclusions), request_id))
        _audit(rt, actor, "INSPECTION_DECIDED", request_id=request_id, item_id=item_id, related=[decision_id],
               payload={"decision_id": decision_id, "verdict": result.verdict.value,
                        "reason_codes": content["reason_codes"], "ev_inspect": result.ev_inspect,
                        "delta_ev": result.delta_ev, "budget_check": content["budget_check"],
                        "recommended_disposition": content["recommended_disposition"],
                        "parameter_set": meta.parameter_set, "synthetic": True})
        return Outcome(data=content, recorded=(decision_id,))

    return run(rt, st.DECIDE, actor, {"request_id": request_id, "item_id": item_id}, request_id, body)


# --------------------------------------------------- create_inspection_task


def _task_view(task: st.Artifact | dict[str, Any], link: dict[str, Any]) -> dict[str, Any]:
    content = task.content if isinstance(task, st.Artifact) else task
    return {
        "task_id": content["task_id"],
        "item_id": content["item_id"],
        "tag_code": content["tag_code"],
        "points": content["points"],
        "decision_id": link["decision_id"],
        "linked_existing": link["linked_existing"],
        "budget": {"before": link["budget_before"], "after": link["budget_after"]},
        "synthetic": True,
    }


def create_inspection_task(rt: Runtime, request_id: str, item_id: str, decision_id: str,
                           actor: Actor = AGENT) -> ToolResult:
    """为判定 INSPECT 的商品创建（或关联）人工检查任务，并扣减预算（需求 5）。"""

    def body() -> Outcome:
        meta = rt.require_meta()
        conn = rt.conn
        s = st.load_state(conn, request_id)
        item = _item(s, item_id)

        # 重放：这个判定已经创建或关联过任务
        for task in st.artifacts(conn, "INSPECTION_TASK", item_id=item_id):
            for link in task.content["links"]:
                if link["decision_id"] == decision_id and link["request_id"] == request_id:
                    return Outcome(data=_task_view(task, link), replayed=True)
        st.require_allowed(s, st.CREATE_TASK, item_id=item_id, decision_id=decision_id)

        decision = st.get_artifact(conn, decision_id)
        assert decision is not None and decision.content["verdict"] == Verdict.INSPECT
        cost = Decimal(decision.content["inputs"]["C"])
        before = meta.budget_remaining
        open_tasks = [t for t in st.artifacts(conn, "INSPECTION_TASK", item_id=item_id) if t.status == "OPEN"]

        if open_tasks:  # 同一商品已有未完成任务：只关联，不再扣预算（需求 5.5）
            task = open_tasks[0]
            link = {"request_id": request_id, "decision_id": decision_id, "linked_existing": True,
                    "budget_before": before, "budget_after": before}
            content = dict(task.content)
            content["request_ids"] = [*content["request_ids"], request_id]
            content["links"] = [*content["links"], link]
            conn.execute("UPDATE artifacts SET content_json = ? WHERE artifact_id = ?",
                         (dumps(content), task.artifact_id))
            task_id, event, recorded = task.artifact_id, "INSPECTION_TASK_LINKED", (task.artifact_id,)
            view = _task_view(content, link)
        else:
            if cost > before:  # 事务内重新检查预算
                raise Rejection(ReasonCode.INSPECTION_OVER_BUDGET, message="检查成本超出当前预算余额")
            after = before - cost
            task_id = ids.next_artifact_id(conn, "INSPECTION_TASK")
            link = {"request_id": request_id, "decision_id": decision_id, "linked_existing": False,
                    "budget_before": before, "budget_after": after}
            points = [{"point": p.value, "label": p.label, "method": rt.inspection_method(p.value)}
                      for p in item.unknown_points()]
            content = {
                "task_id": task_id, "item_id": item_id, "tag_code": item.tag_code,
                "condition_version": item.condition_version, "points": points, "decision_id": decision_id,
                "request_ids": [request_id], "links": [link], "result": None, "synthetic": True,
            }
            _insert_artifact(conn, rt, artifact_id=task_id, kind="INSPECTION_TASK", request_id=request_id,
                             item_id=item_id, condition_version=item.condition_version, status="OPEN",
                             natural_key=f"decision:{decision_id}", content=content)
            conn.execute("UPDATE meta SET budget_remaining_cents = budget_remaining_cents - ?, "
                         "budget_version = budget_version + 1 WHERE id = 1", (cents(cost),))
            _audit(rt, actor, "BUDGET_DEDUCTED", request_id=request_id, item_id=item_id,
                   related=[task_id, decision_id],
                   payload={"task_id": task_id, "amount": cost, "before": before, "after": after,
                            "budget_version": meta.budget_version + 1, "synthetic": True})
            event, recorded = "INSPECTION_TASK_CREATED", (task_id,)
            view = _task_view(content, link)

        _set_status(conn, request_id, RequestStatus.WAITING_FOR_INSPECTION)
        _audit(rt, actor, event, request_id=request_id, item_id=item_id, related=[task_id, decision_id],
               payload={"task_id": task_id, "points": [p["point"] for p in view["points"]],
                        "linked_existing": link["linked_existing"]})
        return Outcome(data={**view, "waiting_text": messages.waiting_text(
            item_id, [p["point"] for p in view["points"]])}, recorded=recorded)

    return run(rt, st.CREATE_TASK, actor,
               {"request_id": request_id, "item_id": item_id, "decision_id": decision_id}, request_id, body)


# --------------------------------------------------- build_evidence_package


def build_evidence_package(rt: Runtime, request_id: str, item_id: str, actor: Actor = AGENT) -> ToolResult:
    """为排名第一的 QUALIFIED 商品整理证据（需求 8）。"""

    def body() -> Outcome:
        rt.require_meta()
        conn = rt.conn
        s = st.load_state(conn, request_id)
        item = _item(s, item_id)
        key = f"{request_id}:{item_id}:v{item.condition_version}"
        existing = st.find_by_key(conn, "EVIDENCE_PACKAGE", key)
        if existing is not None:
            return Outcome(data=existing.content, replayed=True)
        st.require_allowed(s, st.BUILD_EVIDENCE, item_id=item_id)

        stored = repo.condition_points(conn, item_id, item.condition_version)
        spec = s.request.spec
        points = []
        for p in Point:
            rec = stored[p.value]
            defects = []
            for d, domain_d in zip(rec["defects"], item.point(p).defects):
                index = spec.accepted_index(p, domain_d)
                if index is None:  # QUALIFIED 保证每个缺陷都被接受；出现即为数据不一致
                    raise Rejection(ToolCode.INTERNAL_ERROR, message=f"{d['defect_id']} 未被买家接受")
                defects.append({**{k: d[k] for k in ("defect_id", "type", "severity")},
                                "accepted_defect_index": index,
                                "accepted_defect": s.request.accepted_defects[index]})
            points.append({"point": p.value, "label": p.label, "status": rec["status"], "defects": defects,
                           "evidence": rec["evidence"]})
        proof = [{"point": p.value, "evidence_ids": [e["evidence_id"] for e in stored[p.value]["evidence"]]}
                 for p in spec.required_conditions]
        package_id = ids.next_artifact_id(conn, "EVIDENCE_PACKAGE")
        content = {
            "evidence_package_id": package_id, "request_id": request_id, "item_id": item_id,
            "tag_code": item.tag_code, "condition_version": item.condition_version, "points": points,
            "required_conditions_proof": proof, "synthetic": True,
        }
        _insert_artifact(conn, rt, artifact_id=package_id, kind="EVIDENCE_PACKAGE", request_id=request_id,
                         item_id=item_id, condition_version=item.condition_version, status="RECORDED",
                         natural_key=key, content=content)
        _set_status(conn, request_id, RequestStatus.EVIDENCE_READY)
        _audit(rt, actor, "EVIDENCE_PACKAGE_BUILT", request_id=request_id, item_id=item_id, related=[package_id],
               payload={"evidence_package_id": package_id, "condition_version": item.condition_version})
        return Outcome(data=content, recorded=(package_id,))

    return run(rt, st.BUILD_EVIDENCE, actor, {"request_id": request_id, "item_id": item_id}, request_id, body)


# ------------------------------------------------------------ generate_quote


def generate_quote(rt: Runtime, request_id: str, item_id: str, evidence_package_id: str,
                   actor: Actor = AGENT) -> ToolResult:
    """按价格规则报价，报价绑定 Item_ID 和 Condition_Record 版本（需求 9）。"""

    def body() -> Outcome:
        rt.require_meta()
        conn = rt.conn
        s = st.load_state(conn, request_id)
        item = _item(s, item_id)
        key = f"{request_id}:{item_id}:v{item.condition_version}"
        existing = st.find_by_key(conn, "QUOTE", key)
        if existing is not None and existing.content["evidence_package_id"] == evidence_package_id:
            return Outcome(data=existing.content, replayed=True)
        st.require_allowed(s, st.GENERATE_QUOTE, item_id=item_id, evidence_package_id=evidence_package_id)

        try:
            price = pricing.price_item(item, rt.base_price(item.product_id), rt.price_rules)
        except PricingError as exc:
            raise Rejection(exc.code, message=str(exc)) from None
        quote_id = ids.next_artifact_id(conn, "QUOTE")
        content = {
            "quote_id": quote_id, "request_id": request_id, "buyer_id": s.request.buyer_id, "item_id": item_id,
            "tag_code": item.tag_code, "condition_version": item.condition_version,
            "evidence_package_id": evidence_package_id, "currency": price.currency,
            "base_price": price.base_price,
            "discounts": [{"defect_id": l.defect_id, "point": l.point.value, "type": l.defect_type.value,
                           "severity": l.severity.value, "rate": l.rate, "amount": l.amount}
                          for l in price.discounts],
            "verified_defect_ids": [l.defect_id for l in price.discounts],
            "total_rate": price.total_rate, "floor_applied": price.floor_applied, "amount": price.amount,
            "synthetic": True,
        }
        _insert_artifact(conn, rt, artifact_id=quote_id, kind="QUOTE", request_id=request_id, item_id=item_id,
                         condition_version=item.condition_version, status="ACTIVE", natural_key=key, content=content)
        _set_status(conn, request_id, RequestStatus.OFFERED)
        _audit(rt, actor, "QUOTE_GENERATED", request_id=request_id, item_id=item_id,
               related=[quote_id, evidence_package_id],
               payload={"quote_id": quote_id, "amount": price.amount, "condition_version": item.condition_version,
                        "synthetic": True})
        return Outcome(data=content, recorded=(quote_id,))

    return run(rt, st.GENERATE_QUOTE, actor,
               {"request_id": request_id, "item_id": item_id, "evidence_package_id": evidence_package_id},
               request_id, body)


# --------------------------------------------------------- finalize_no_match


def _inspection_conclusion(s: st.RequestState, item_id: str) -> dict[str, Any] | None:
    completed = [t for t in s.tasks if t.item_id == item_id and t.status == "COMPLETED" and t.content.get("result")]
    if not completed:
        return None
    task = completed[-1]
    findings = [{k: f.get(k) for k in ("point", "conclusion", "defect_type", "severity")}
                for f in task.content["result"]["findings"]]
    return {"task_id": task.artifact_id, "findings": findings}


def finalize_no_match(rt: Runtime, request_id: str, actor: Actor = AGENT) -> ToolResult:
    """没有任何候选时，告知没有合适商品并逐件说明原因（需求 7.4）。不含经济参数。"""

    def body() -> Outcome:
        rt.require_meta()
        conn = rt.conn
        s = st.load_state(conn, request_id)
        if s.request.status is RequestStatus.NO_MATCH and s.request.no_match is not None:
            return Outcome(data=s.request.no_match, replayed=True)
        st.require_allowed(s, st.FINALIZE_NO_MATCH)

        entries = []
        for item_id, r in sorted(st.evaluation_results(s).items()):
            codes = list(r["reason_codes"])
            codes += [c.value for c in s.request.exclusions.get(item_id, ()) if c.value not in codes]
            entry = {"item_id": item_id, "reason_codes": codes, "details": r["details"],
                     "unknown_points": r["unknown_points"],
                     "inspection_conclusion": _inspection_conclusion(s, item_id)}
            entry["buyer_messages"] = messages.item_reason_lines(entry)
            entries.append(entry)
        no_match = {"summary": messages.no_match_summary(), "items": entries, "synthetic": True}
        conn.execute("UPDATE buyer_requests SET status = ?, no_match_json = ? WHERE request_id = ?",
                     (RequestStatus.NO_MATCH.value, dumps(no_match), request_id))
        _audit(rt, actor, "NO_MATCH_FINALIZED", request_id=request_id,
               payload={"item_count": len(entries),
                        "reasons": {e["item_id"]: e["reason_codes"] for e in entries}})
        return Outcome(data=no_match, recorded=(request_id,))

    return run(rt, st.FINALIZE_NO_MATCH, actor, {"request_id": request_id}, request_id, body)


AGENT_TOOL_FUNCTIONS = {
    st.GET_STATUS: get_request_status,
    st.EVALUATE: evaluate_request,
    st.DECIDE: decide_inspection,
    st.CREATE_TASK: create_inspection_task,
    st.BUILD_EVIDENCE: build_evidence_package,
    st.GENERATE_QUOTE: generate_quote,
    st.FINALIZE_NO_MATCH: finalize_no_match,
}


def call(rt: Runtime, tool: str, args: dict[str, Any], actor: Actor = AGENT) -> ToolResult:
    """按名称调用 Agent 工具（供编排层和测试使用）。人工操作不在这里。"""
    try:
        fn = AGENT_TOOL_FUNCTIONS[tool]
    except KeyError:
        return ToolResult(ok=False, tool=tool, request_id=args.get("request_id"),
                          reason_codes=(ToolCode.INVALID_ARGUMENTS,), message=f"未知工具 {tool}")
    return fn(rt, actor=actor, **args)
