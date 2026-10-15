"""页面发起的人工操作（design §6.3）。这些操作不暴露给 Agent。

- submit_buyer_request：买家提交结构化需求（需求 2）
- submit_inspection_result：检查员提交检查结果，必须核对标签码（需求 6）
- confirm_reservation：只接受买家本人的确认（需求 10、12.8）
- decline_quote：买家拒绝报价（需求 10.7）
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping

from acceptmatch.domain.enums import (
    DefectType,
    InspectionConclusion,
    ItemStatus,
    Point,
    PointStatus,
    ReasonCode,
    RequestStatus,
    Severity,
)
from acceptmatch.store import audit, ids, repo
from acceptmatch.store.db import dumps
from acceptmatch.tools import state as st
from acceptmatch.tools.envelope import Actor, Rejection, ToolCode, ToolResult
from acceptmatch.tools.runner import Outcome, require_actor, run
from acceptmatch.tools.runtime import Runtime
from acceptmatch.ui import messages

SUBMIT_REQUEST = "submit_buyer_request"
SUBMIT_INSPECTION = "submit_inspection_result"
CONFIRM = "confirm_reservation"
DECLINE = "decline_quote"

_INSPECTOR_RE = re.compile(r"SYN-INSPECTOR-\d+")


def _audit(rt: Runtime, actor: Actor, event_type: str, **kwargs: Any) -> None:
    audit.append(rt.conn, actor_type=actor.actor_type, actor_id=actor.actor_id, event_type=event_type,
                 ts=rt.now(), **kwargs)


def _enum(enum_cls, value: Any, field: str):
    try:
        return enum_cls(value)
    except ValueError:
        raise Rejection(ToolCode.INVALID_ARGUMENTS, message=f"{field} 取值无效：{value!r}") from None


# ------------------------------------------------------ submit_buyer_request


def submit_buyer_request(
    rt: Runtime,
    buyer_id: str,
    product_id: str,
    accepted_defects: Iterable[Mapping[str, str]],
    required_conditions: Iterable[str],
    actor: Actor | None = None,
) -> ToolResult:
    actor = actor or Actor("buyer", buyer_id)
    accepted_in = [dict(a) for a in accepted_defects]
    required_in = list(required_conditions)
    args = {"buyer_id": buyer_id, "product_id": product_id, "accepted_defects": accepted_in,
            "required_conditions": required_in}

    def body() -> Outcome:
        require_actor(actor, "buyer")
        if actor.actor_id != buyer_id:
            raise Rejection(ToolCode.ACTOR_NOT_ALLOWED, message="只能以本人身份提交需求")
        rt.require_meta()
        if not rt.buyer_exists(buyer_id):
            raise Rejection(ToolCode.UNKNOWN_BUYER, message=f"买家 {buyer_id} 不存在")
        if not rt.product_exists(product_id):
            raise Rejection(ToolCode.UNKNOWN_PRODUCT, message=f"型号 {product_id} 不存在")
        accepted = []
        for a in accepted_in:
            if set(a) != {"point", "defect_type", "max_severity"}:
                raise Rejection(ToolCode.INVALID_ARGUMENTS, message="每条可接受缺陷必须包含 point、defect_type、max_severity")
            accepted.append({
                "point": _enum(Point, a["point"], "point").value,
                "defect_type": _enum(DefectType, a["defect_type"], "defect_type").value,
                "max_severity": _enum(Severity, a["max_severity"], "max_severity").value,
            })
        required = [_enum(Point, p, "required_conditions").value for p in required_in]
        if len(set(required)) != len(required):
            raise Rejection(ToolCode.INVALID_ARGUMENTS, message="required_conditions 部位重复")
        if len({tuple(sorted(a.items())) for a in accepted}) != len(accepted):
            raise Rejection(ToolCode.INVALID_ARGUMENTS, message="accepted_defects 有重复条目")
        required = [p.value for p in Point if p.value in required]  # 固定顺序
        spec = st.spec_from_json(product_id, accepted, required)
        conflicts = spec.conflicting_points()
        if conflicts:
            raise Rejection(ToolCode.REQUEST_CONFLICT_POINT,
                            message=f"部位同时出现在可接受缺陷和必须完好中：{messages.points_label(conflicts)}")

        conn = rt.conn
        request_id = ids.next_request_id(conn)
        conn.execute(
            "INSERT INTO buyer_requests (request_id, buyer_id, product_id, accepted_defects_json, "
            "required_conditions_json, status, exclusions_json, no_match_json, created_at, synthetic) "
            "VALUES (?, ?, ?, ?, ?, 'SUBMITTED', '{}', NULL, ?, 1)",
            (request_id, buyer_id, product_id, dumps(accepted), dumps(required), rt.now()),
        )
        echo = messages.request_echo(spec)
        _audit(rt, actor, "REQUEST_SUBMITTED", request_id=request_id,
               payload={"product_id": product_id, "accepted_defects": accepted,
                        "required_conditions": required, "echo": echo, "synthetic": True})
        return Outcome(
            data={"request_id": request_id, "buyer_id": buyer_id, "product_id": product_id,
                  "accepted_defects": accepted, "required_conditions": required, "echo": echo, "synthetic": True},
            recorded=(request_id,),
            request_id=request_id,
        )

    return run(rt, SUBMIT_REQUEST, actor, args, None, body)


# -------------------------------------------------- submit_inspection_result


def _parse_findings(task_points: list[str], findings: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    parsed: dict[str, dict[str, Any]] = {}
    for f in findings:
        point = _enum(Point, f.get("point"), "point").value
        if point in parsed:
            raise Rejection(ToolCode.INVALID_ARGUMENTS, message=f"部位 {point} 重复提交")
        if point not in task_points:
            raise Rejection(ToolCode.INVALID_ARGUMENTS, message=f"部位 {point} 不在本任务的检查范围内")
        conclusion = _enum(InspectionConclusion, f.get("conclusion"), "conclusion")
        refs = list(f.get("evidence_refs") or [])
        for ref in refs:
            if not isinstance(ref, str) or not ref.startswith("synthetic://"):
                raise Rejection(ToolCode.INVALID_ARGUMENTS, message=f"证据引用必须是 synthetic:// 占位：{ref!r}")
        entry = {"point": point, "conclusion": conclusion.value, "defect_type": None, "severity": None,
                 "evidence_refs": refs}
        if conclusion is InspectionConclusion.DEFECT:
            defect = f.get("defect") or {}
            if not defect.get("type") or not defect.get("severity"):
                raise Rejection(ToolCode.FINDING_INCOMPLETE, message=f"{point} 结论为 DEFECT 时必须填写缺陷类型和程度")
            entry["defect_type"] = _enum(DefectType, defect["type"], "defect.type").value
            entry["severity"] = _enum(Severity, defect["severity"], "defect.severity").value
        if conclusion is not InspectionConclusion.INCONCLUSIVE and not refs:
            raise Rejection(ToolCode.EVIDENCE_REQUIRED, message=f"{point} 结论为 {conclusion} 时至少需要一条证据引用")
        parsed[point] = entry
    missing = [p for p in task_points if p not in parsed]
    if missing:
        raise Rejection(ToolCode.FINDING_INCOMPLETE, message=f"缺少这些部位的结论：{', '.join(missing)}")
    if not any(e["evidence_refs"] for e in parsed.values()):
        raise Rejection(ToolCode.EVIDENCE_REQUIRED, message="检查结果至少需要一条证据引用")
    return [parsed[p] for p in task_points]


def submit_inspection_result(
    rt: Runtime,
    task_id: str,
    inspector_id: str,
    scanned_tag_code: str,
    inspected_at: str,
    findings: list[Mapping[str, Any]],
    actor: Actor | None = None,
) -> ToolResult:
    actor = actor or Actor("inspector", inspector_id)
    args = {"task_id": task_id, "inspector_id": inspector_id, "scanned_tag_code": scanned_tag_code,
            "inspected_at": inspected_at, "findings": [dict(f) for f in findings]}
    # 只用于返回结果和拒绝审计中显示关联请求；校验仍在事务内重新读取任务
    known_task = st.get_artifact(rt.conn, task_id)
    linked = known_task.content.get("request_ids", []) if known_task and known_task.kind == "INSPECTION_TASK" else []
    if known_task is not None:
        args["item_id"] = known_task.item_id

    def body() -> Outcome:
        require_actor(actor, "inspector")
        if actor.actor_id != inspector_id or not _INSPECTOR_RE.fullmatch(inspector_id or ""):
            raise Rejection(ToolCode.ACTOR_NOT_ALLOWED, message="检查员编号必须是本人的合成编号（SYN-INSPECTOR-xx）")
        rt.require_meta()
        conn = rt.conn
        task = st.get_artifact(conn, task_id)
        if task is None or task.kind != "INSPECTION_TASK":
            raise Rejection(ToolCode.TASK_NOT_FOUND, message=f"检查任务 {task_id} 不存在")
        if task.status != "OPEN":
            raise Rejection(ToolCode.TASK_NOT_OPEN, message=f"检查任务 {task_id} 已完成，不能重复提交")
        item = repo.item_snapshot(conn, task.item_id)
        if scanned_tag_code != item.tag_code:
            raise Rejection(ToolCode.TAG_MISMATCH,
                            message=f"扫描到的标签 {scanned_tag_code} 不是任务商品 {task.item_id} 的标签")
        try:
            if datetime.fromisoformat(inspected_at).tzinfo is None:
                raise ValueError
        except (TypeError, ValueError):
            raise Rejection(ToolCode.INVALID_ARGUMENTS, message="inspected_at 必须是带时区的 ISO 时间") from None
        task_points = [p["point"] for p in task.content["points"]]
        parsed = _parse_findings(task_points, findings)

        # 新的 Condition_Record 版本：保留原有 Evidence，追加检查得到的 Evidence
        points = repo.condition_points(conn, task.item_id, item.condition_version)
        evidence_needed = sum(len(f["evidence_refs"]) for f in parsed if f["conclusion"] != "INCONCLUSIVE")
        new_evidence_ids = iter(ids.next_evidence_ids(conn, evidence_needed))
        new_defect_ids = iter(ids.next_defect_ids(conn, sum(1 for f in parsed if f["conclusion"] == "DEFECT")))
        recorded_evidence: list[str] = []
        new_defects: list[str] = []
        for f in parsed:
            if f["conclusion"] == "INCONCLUSIVE":
                continue  # 部位保持 UNKNOWN；引用只记录在任务结果中
            rec = points[f["point"]]
            evidence = []
            for ref in f["evidence_refs"]:
                ev_id = next(new_evidence_ids)
                evidence.append({"evidence_id": ev_id, "point": f["point"], "source": "INSPECTION",
                                 "inspector_id": inspector_id, "recorded_at": inspected_at, "photo_ref": ref,
                                 "inspection_task_id": task_id, "synthetic": True})
                recorded_evidence.append(ev_id)
            ev_ids = [e["evidence_id"] for e in evidence]
            if f["conclusion"] == "OK":
                points[f["point"]] = {"status": PointStatus.OK.value, "evidence_ids": ev_ids, "defects": [],
                                      "evidence": rec["evidence"] + evidence}
            else:
                defect_id = next(new_defect_ids)
                new_defects.append(defect_id)
                points[f["point"]] = {
                    "status": PointStatus.DEFECT.value, "evidence_ids": [],
                    "defects": [{"defect_id": defect_id, "type": f["defect_type"], "severity": f["severity"],
                                 "evidence_ids": ev_ids}],
                    "evidence": rec["evidence"] + evidence,
                }
        new_version = item.condition_version + 1
        conn.execute(
            "INSERT INTO condition_versions (item_id, version, points_json, source_ref, created_at, synthetic) "
            "VALUES (?, ?, ?, ?, ?, 1)",
            (task.item_id, new_version, dumps(points), f"inspection:{task_id}", rt.now()),
        )
        # 商品保持原状态（通常是 AVAILABLE），检查得到的缺陷可供其他买家匹配（需求 6.8）
        conn.execute("UPDATE items SET condition_version = ? WHERE item_id = ?", (new_version, task.item_id))

        result = {"inspector_id": inspector_id, "scanned_tag_code": scanned_tag_code, "inspected_at": inspected_at,
                  "findings": parsed, "new_condition_version": new_version, "evidence_ids": recorded_evidence,
                  "defect_ids": new_defects}
        content = {**task.content, "result": result}
        conn.execute("UPDATE artifacts SET status = 'COMPLETED', content_json = ? WHERE artifact_id = ?",
                     (dumps(content), task_id))

        inconclusive = any(f["conclusion"] == "INCONCLUSIVE" for f in parsed)
        updated = []
        for rid in task.content["request_ids"]:
            req = st.load_request(conn, rid)
            if req.status is not RequestStatus.WAITING_FOR_INSPECTION:
                continue
            exclusions = {k: [c.value for c in v] for k, v in req.exclusions.items()}
            if inconclusive:
                codes = exclusions.get(task.item_id, [])
                if ReasonCode.INSPECTION_INCONCLUSIVE.value not in codes:
                    codes.append(ReasonCode.INSPECTION_INCONCLUSIVE.value)
                exclusions[task.item_id] = codes
            conn.execute("UPDATE buyer_requests SET status = ?, exclusions_json = ? WHERE request_id = ?",
                         (RequestStatus.INSPECTION_RESULT_RECORDED.value, dumps(exclusions), rid))
            updated.append(rid)

        _audit(rt, actor, "INSPECTION_RESULT_RECORDED", item_id=task.item_id,
               request_id=updated[0] if len(updated) == 1 else None,
               related_ids=[task_id, *updated, *recorded_evidence, *new_defects],
               payload={"task_id": task_id, "scanned_tag_code": scanned_tag_code,
                        "findings": [{k: f[k] for k in ("point", "conclusion", "defect_type", "severity")}
                                     for f in parsed],
                        "new_condition_version": new_version, "requests": updated, "synthetic": True})
        return Outcome(
            data={"task_id": task_id, "item_id": task.item_id, "new_condition_version": new_version,
                  "findings": parsed, "evidence_ids": recorded_evidence, "defect_ids": new_defects,
                  "requests_updated": updated, "item_status": item.item_status.value, "synthetic": True},
            recorded=(task_id, *recorded_evidence),
            request_id=updated[0] if updated else None,
        )

    return run(rt, SUBMIT_INSPECTION, actor, args, linked[0] if linked else None, body)


# ------------------------------------------------------- confirm_reservation


def _load_offer(conn: sqlite3.Connection, request_id: str, buyer_id: str, quote_id: str) -> tuple[st.RequestRecord, st.Artifact]:
    req = st.load_request(conn, request_id)
    if req.buyer_id != buyer_id:
        raise Rejection(ToolCode.QUOTE_NOT_OWNED, message="该请求不属于这位买家")
    quote = st.get_artifact(conn, quote_id)
    if quote is None or quote.kind != "QUOTE":
        raise Rejection(ToolCode.QUOTE_NOT_FOUND, message=f"报价 {quote_id} 不存在")
    if quote.request_id != request_id or quote.content["buyer_id"] != buyer_id:
        raise Rejection(ToolCode.QUOTE_NOT_OWNED, message="该报价不属于这位买家或这个请求")
    return req, quote


def _supersede_and_reevaluate(rt: Runtime, actor: Actor, request_id: str, quote: st.Artifact, code: ToolCode,
                              message: str) -> Outcome:
    """确认被拒但需要记录：报价失效，请求转为 NEEDS_REEVALUATION（需求 10.4、10.5）。"""
    conn = rt.conn
    conn.execute("UPDATE artifacts SET status = 'SUPERSEDED' WHERE artifact_id = ?", (quote.artifact_id,))
    conn.execute("UPDATE buyer_requests SET status = ? WHERE request_id = ?",
                 (RequestStatus.NEEDS_REEVALUATION.value, request_id))
    _audit(rt, actor, "CONFIRMATION_REJECTED", request_id=request_id, item_id=quote.item_id,
           related_ids=[quote.artifact_id], outcome=code.value,
           payload={"quote_id": quote.artifact_id, "reason": message, "quote_status": "SUPERSEDED",
                    "request_status": RequestStatus.NEEDS_REEVALUATION.value})
    return Outcome(ok=False, reason_codes=(code,), message=message, recorded=(quote.artifact_id, request_id))


def confirm_reservation(
    rt: Runtime,
    request_id: str,
    buyer_id: str,
    item_id: str,
    quote_id: str,
    acknowledged_defect_ids: Iterable[str],
    acknowledged_amount: str | Decimal,
    actor: Actor | None = None,
) -> ToolResult:
    actor = actor or Actor("buyer", buyer_id)
    acknowledged = list(acknowledged_defect_ids)
    args = {"request_id": request_id, "buyer_id": buyer_id, "item_id": item_id, "quote_id": quote_id,
            "acknowledged_defect_ids": acknowledged, "acknowledged_amount": str(acknowledged_amount)}

    def body() -> Outcome:
        # 只接受买家本人的确认输入（需求 12.8）
        if actor.actor_type != "buyer" or actor.actor_id != buyer_id:
            raise Rejection(ToolCode.CONFIRMATION_NOT_FROM_BUYER, message="预留只能由买家本人确认")
        rt.require_meta()
        conn = rt.conn
        req, quote = _load_offer(conn, request_id, buyer_id, quote_id)
        if req.status is not RequestStatus.OFFERED:
            raise Rejection(ToolCode.INVALID_STATE, message=f"请求当前状态为 {req.status.label}，不能确认预留")
        if quote.item_id != item_id:
            raise Rejection(ToolCode.QUOTE_ITEM_MISMATCH, message=f"报价 {quote_id} 对应的是 {quote.item_id}，不是 {item_id}")
        if quote.status != "ACTIVE":
            raise Rejection(ToolCode.QUOTE_NOT_ACTIVE, message=f"报价 {quote_id} 已失效（{quote.status}）")
        try:
            amount = Decimal(str(acknowledged_amount))
        except InvalidOperation:
            raise Rejection(ToolCode.ACKNOWLEDGEMENT_INCOMPLETE, message="确认金额无效") from None
        expected_defects = quote.content["verified_defect_ids"]
        if sorted(acknowledged) != sorted(expected_defects) or amount != Decimal(quote.content["amount"]):
            raise Rejection(ToolCode.ACKNOWLEDGEMENT_INCOMPLETE,
                            message="必须逐条确认该商品的全部已核实缺陷，并确认报价金额")

        item = repo.item_snapshot(conn, item_id)
        if item.condition_version != quote.condition_version:
            return _supersede_and_reevaluate(rt, actor, request_id, quote, ToolCode.CONDITION_VERSION_CHANGED,
                                             f"商品状态已从版本 {quote.condition_version} 更新到 {item.condition_version}，"
                                             "需要重新评估和报价")
        if item.item_status is not ItemStatus.AVAILABLE:
            return _supersede_and_reevaluate(rt, actor, request_id, quote, ToolCode.ITEM_NOT_AVAILABLE,
                                             f"商品 {item_id} 当前不可售")

        reservation_id = ids.next_artifact_id(conn, "RESERVATION")
        try:
            conn.execute(
                "INSERT INTO artifacts (artifact_id, kind, request_id, item_id, condition_version, status, "
                "natural_key, content_json, created_at, synthetic) VALUES (?, 'RESERVATION', ?, ?, ?, 'ACTIVE', ?, ?, ?, 1)",
                (reservation_id, request_id, item_id, item.condition_version, f"quote:{quote_id}",
                 dumps({"reservation_id": reservation_id, "buyer_id": buyer_id, "quote_id": quote_id,
                        "amount": quote.content["amount"], "acknowledged_defect_ids": sorted(acknowledged),
                        "source": "BUYER_CONFIRMATION", "synthetic": True}),
                 rt.now()),
            )
        except sqlite3.IntegrityError:
            # 数据库层的最后防线：同一商品已有有效预留
            return _supersede_and_reevaluate(rt, actor, request_id, quote, ToolCode.ITEM_NOT_AVAILABLE,
                                             f"商品 {item_id} 已被预留")
        conn.execute("UPDATE items SET item_status = 'RESERVED' WHERE item_id = ?", (item_id,))
        conn.execute("UPDATE artifacts SET status = 'ACCEPTED' WHERE artifact_id = ?", (quote_id,))
        conn.execute("UPDATE buyer_requests SET status = ? WHERE request_id = ?",
                     (RequestStatus.RESERVED.value, request_id))
        _audit(rt, actor, "RESERVATION_CREATED", request_id=request_id, item_id=item_id,
               related_ids=[reservation_id, quote_id],
               payload={"reservation_id": reservation_id, "quote_id": quote_id, "amount": quote.content["amount"],
                        "acknowledged_defect_ids": sorted(acknowledged), "synthetic": True})
        return Outcome(
            data={"reservation_id": reservation_id, "item_id": item_id, "tag_code": item.tag_code,
                  "quote_id": quote_id, "amount": quote.content["amount"], "item_status": "RESERVED",
                  "synthetic": True},
            recorded=(reservation_id,),
        )

    return run(rt, CONFIRM, actor, args, request_id, body)


def decline_quote(rt: Runtime, request_id: str, buyer_id: str, quote_id: str, actor: Actor | None = None) -> ToolResult:
    actor = actor or Actor("buyer", buyer_id)
    args = {"request_id": request_id, "buyer_id": buyer_id, "quote_id": quote_id}

    def body() -> Outcome:
        if actor.actor_type != "buyer" or actor.actor_id != buyer_id:
            raise Rejection(ToolCode.CONFIRMATION_NOT_FROM_BUYER, message="只有买家本人可以拒绝报价")
        rt.require_meta()
        conn = rt.conn
        req, quote = _load_offer(conn, request_id, buyer_id, quote_id)
        if req.status is not RequestStatus.OFFERED:
            raise Rejection(ToolCode.INVALID_STATE, message=f"请求当前状态为 {req.status.label}，不能拒绝报价")
        if quote.status != "ACTIVE":
            raise Rejection(ToolCode.QUOTE_NOT_ACTIVE, message=f"报价 {quote_id} 已失效（{quote.status}）")
        conn.execute("UPDATE artifacts SET status = 'DECLINED' WHERE artifact_id = ?", (quote_id,))
        conn.execute("UPDATE buyer_requests SET status = ? WHERE request_id = ?",
                     (RequestStatus.DECLINED.value, request_id))
        _audit(rt, actor, "QUOTE_DECLINED", request_id=request_id, item_id=quote.item_id, related_ids=[quote_id],
               payload={"quote_id": quote_id})
        item = repo.item_snapshot(conn, quote.item_id)  # 拒绝报价不改变商品状态
        return Outcome(data={"quote_id": quote_id, "item_id": quote.item_id, "item_status": item.item_status.value,
                             "synthetic": True}, recorded=(quote_id,))

    return run(rt, DECLINE, actor, args, request_id, body)
