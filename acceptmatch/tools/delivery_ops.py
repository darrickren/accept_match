"""交付核验（需求 11，tasks.md 任务 9）。页面发起的人工操作，不暴露给 Agent。

两步：
1. start_delivery_check：交付人员扫描实物标签。
   - 标签与预留商品不一致：记录 DELIVERY_MISMATCH（预留的商品、扫描到的标签），阻止交付，预留不变。
   - 一致：返回买家确认时那个状态版本的证据，供逐个部位对照。
2. complete_delivery_check：交付人员逐个部位确认实物与证据是否一致。
   - 全部一致：商品改为 DELIVERED，预留改为 FULFILLED，请求改为 DELIVERED。
   - 有不一致：记录 CONDITION_MISMATCH，阻止交付，转人工处理（该预留不能再发起交付核验）。

核验记录保存在预留记录的 content_json["delivery_checks"] 中，并同时写审计。
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any, Iterable, Mapping

from acceptmatch.domain.enums import ItemStatus, Point, RequestStatus
from acceptmatch.store import audit, ids, repo
from acceptmatch.store.db import dumps
from acceptmatch.tools import state as st
from acceptmatch.tools.envelope import Actor, Rejection, ToolCode, ToolResult
from acceptmatch.tools.runner import Outcome, require_actor, run
from acceptmatch.tools.runtime import Runtime

START = "start_delivery_check"
COMPLETE = "complete_delivery_check"

AWAITING = "AWAITING_CONFIRMATION"
PASSED = "PASSED"

_OPERATOR_RE = re.compile(r"SYN-DELIVERY-\d+")


def _check_actor(actor: Actor, operator_id: str) -> None:
    require_actor(actor, "delivery")
    if actor.actor_id != operator_id or not _OPERATOR_RE.fullmatch(operator_id or ""):
        raise Rejection(ToolCode.ACTOR_NOT_ALLOWED, message="交付人员编号必须是本人的合成编号（SYN-DELIVERY-xx）")


def _active_reservation(conn: sqlite3.Connection, reservation_id: str) -> st.Artifact:
    res = st.get_artifact(conn, reservation_id)
    if res is None or res.kind != "RESERVATION":
        raise Rejection(ToolCode.RESERVATION_NOT_ACTIVE, message=f"预留 {reservation_id} 不存在")
    if res.status != "ACTIVE":
        raise Rejection(ToolCode.RESERVATION_NOT_ACTIVE, message=f"预留 {reservation_id} 已不是有效状态（{res.status}）")
    return res


def _save(conn: sqlite3.Connection, res: st.Artifact, content: dict[str, Any], status: str | None = None) -> None:
    conn.execute("UPDATE artifacts SET content_json = ?, status = COALESCE(?, status) WHERE artifact_id = ?",
                 (dumps(content), status, res.artifact_id))


def _audit(rt: Runtime, actor: Actor, event_type: str, res: st.Artifact, outcome: str, related: list[str],
           payload: dict[str, Any]) -> None:
    audit.append(rt.conn, actor_type=actor.actor_type, actor_id=actor.actor_id, event_type=event_type,
                 outcome=outcome, item_id=res.item_id, request_id=res.request_id,
                 related_ids=[res.artifact_id, *related], payload={**payload, "synthetic": True}, ts=rt.now())


def evidence_points(conn: sqlite3.Connection, res: st.Artifact) -> list[dict[str, Any]]:
    """买家确认时那个状态版本的证据（不是当前版本），用于交付时逐项对照。"""
    stored = repo.condition_points(conn, res.item_id, res.condition_version)
    return [{"point": p.value, "label": p.label, **stored[p.value]} for p in Point]


def _known_request(rt: Runtime, reservation_id: str) -> str | None:
    res = st.get_artifact(rt.conn, reservation_id)
    return res.request_id if res is not None and res.kind == "RESERVATION" else None


# ------------------------------------------------------------ 第一步：扫码


def start_delivery_check(rt: Runtime, reservation_id: str, operator_id: str, scanned_tag_code: str,
                         actor: Actor | None = None) -> ToolResult:
    actor = actor or Actor("delivery", operator_id)
    args = {"reservation_id": reservation_id, "operator_id": operator_id, "scanned_tag_code": scanned_tag_code}

    def body() -> Outcome:
        _check_actor(actor, operator_id)
        rt.require_meta()
        conn = rt.conn
        res = _active_reservation(conn, reservation_id)
        args["item_id"] = res.item_id
        content = dict(res.content)
        checks = list(content.get("delivery_checks", []))
        if content.get("needs_manual_review"):
            raise Rejection(ToolCode.MANUAL_REVIEW_REQUIRED, message="上次核验发现实物与证据不一致，已转人工处理")
        if any(c["outcome"] == AWAITING for c in checks):
            raise Rejection(ToolCode.DELIVERY_CHECK_PENDING, message="这件商品已有一次核验在等待逐项确认，请先完成")
        scanned = (scanned_tag_code or "").strip()
        if not scanned:
            raise Rejection(ToolCode.INVALID_ARGUMENTS, message="请扫描或输入实物上的标签码")

        items = {s.item_id: s for s in repo.list_item_snapshots(conn)}
        reserved = items[res.item_id]
        check_id = ids.next_delivery_check_id(conn)
        check: dict[str, Any] = {
            "check_id": check_id, "operator_id": operator_id, "scanned_tag_code": scanned,
            "tag_match": scanned == reserved.tag_code, "started_at": rt.now(), "confirmations": None,
            "completed_at": None,
        }

        if not check["tag_match"]:
            owner = next((s.item_id for s in items.values() if s.tag_code == scanned), None)
            check.update(outcome=ToolCode.DELIVERY_MISMATCH.value, scanned_item_id=owner)
            content["delivery_checks"] = [*checks, check]
            _save(conn, res, content)
            _audit(rt, actor, "DELIVERY_MISMATCH", res, ToolCode.DELIVERY_MISMATCH.value,
                   [check_id, *([owner] if owner else [])],
                   {"check_id": check_id, "reserved_item_id": res.item_id, "scanned_tag_code": scanned,
                    "scanned_item_id": owner})
            owner_text = f"（这是 {owner} 的标签）" if owner else "（不属于任何合成商品）"
            return Outcome(
                ok=False, reason_codes=(ToolCode.DELIVERY_MISMATCH,), recorded=(check_id,),
                message=f"扫描到的标签 {scanned}{owner_text}不是预留商品 {res.item_id} 的标签，已阻止交付",
                data={"check_id": check_id, "reservation_id": reservation_id, "item_id": res.item_id,
                      "scanned_tag_code": scanned, "scanned_item_id": owner,
                      "item_status": reserved.item_status.value, "reservation_status": res.status},
                request_id=res.request_id,
            )

        check["outcome"] = AWAITING
        content["delivery_checks"] = [*checks, check]
        _save(conn, res, content)
        _audit(rt, actor, "DELIVERY_CHECK_STARTED", res, "OK", [check_id],
               {"check_id": check_id, "scanned_tag_code": scanned, "condition_version": res.condition_version})
        return Outcome(
            data={"check_id": check_id, "reservation_id": reservation_id, "item_id": res.item_id,
                  "tag_code": reserved.tag_code, "condition_version": res.condition_version,
                  "points": evidence_points(conn, res), "synthetic": True},
            recorded=(check_id,), request_id=res.request_id,
        )

    return run(rt, START, actor, args, _known_request(rt, reservation_id), body)


# ------------------------------------------------------- 第二步：逐项对照


def _parse_confirmations(confirmations: Iterable[Mapping[str, Any]]) -> dict[str, bool]:
    parsed: dict[str, bool] = {}
    for c in confirmations:
        point = c.get("point")
        if point not in {p.value for p in Point} or point in parsed:
            raise Rejection(ToolCode.INVALID_ARGUMENTS, message=f"部位无效或重复：{point!r}")
        matches = c.get("matches")
        if matches is None:
            continue  # 视为未确认，下面统一报告缺失
        if not isinstance(matches, bool):
            raise Rejection(ToolCode.INVALID_ARGUMENTS, message=f"{point} 的确认结果必须是 true 或 false")
        parsed[point] = matches
    missing = [p.value for p in Point if p.value not in parsed]
    if missing:
        raise Rejection(ToolCode.FINDING_INCOMPLETE, message="还有部位没有确认：" + "、".join(Point(p).label for p in missing))
    return parsed


def complete_delivery_check(rt: Runtime, reservation_id: str, check_id: str, operator_id: str,
                            confirmations: Iterable[Mapping[str, Any]], actor: Actor | None = None) -> ToolResult:
    actor = actor or Actor("delivery", operator_id)
    confirmation_list = [dict(c) for c in confirmations]
    args = {"reservation_id": reservation_id, "check_id": check_id, "operator_id": operator_id,
            "confirmations": confirmation_list}

    def body() -> Outcome:
        _check_actor(actor, operator_id)
        rt.require_meta()
        conn = rt.conn
        res = _active_reservation(conn, reservation_id)
        args["item_id"] = res.item_id
        content = dict(res.content)
        checks = [dict(c) for c in content.get("delivery_checks", [])]
        index = next((i for i, c in enumerate(checks) if c["check_id"] == check_id and c["outcome"] == AWAITING), None)
        if index is None:
            raise Rejection(ToolCode.DELIVERY_CHECK_NOT_PENDING, message=f"核验 {check_id} 不在等待确认的状态")
        parsed = _parse_confirmations(confirmation_list)
        mismatched = [p for p, ok in parsed.items() if not ok]
        check = checks[index]
        check.update(confirmations=parsed, completed_at=rt.now())

        if mismatched:  # 实物与证据不一致：阻止交付，转人工处理（需求 11.5）
            check["outcome"] = ToolCode.CONDITION_MISMATCH.value
            check["mismatched_points"] = mismatched
            content["delivery_checks"] = checks
            content["needs_manual_review"] = True
            _save(conn, res, content)
            _audit(rt, actor, "CONDITION_MISMATCH", res, ToolCode.CONDITION_MISMATCH.value, [check_id],
                   {"check_id": check_id, "mismatched_points": mismatched, "needs_manual_review": True})
            labels = "、".join(Point(p).label for p in mismatched)
            return Outcome(ok=False, reason_codes=(ToolCode.CONDITION_MISMATCH,), recorded=(check_id,),
                           message=f"{labels}的实物与证据不一致，已阻止交付并转人工处理",
                           data={"check_id": check_id, "reservation_id": reservation_id, "item_id": res.item_id,
                                 "mismatched_points": mismatched, "needs_manual_review": True},
                           request_id=res.request_id)

        check["outcome"] = PASSED
        content["delivery_checks"] = checks
        _save(conn, res, content, status="FULFILLED")
        conn.execute("UPDATE items SET item_status = ? WHERE item_id = ?", (ItemStatus.DELIVERED.value, res.item_id))
        if res.request_id is not None:
            conn.execute("UPDATE buyer_requests SET status = ? WHERE request_id = ?",
                         (RequestStatus.DELIVERED.value, res.request_id))
        _audit(rt, actor, "DELIVERY_COMPLETED", res, "OK", [check_id],
               {"check_id": check_id, "confirmations": parsed, "item_status": ItemStatus.DELIVERED.value})
        return Outcome(data={"check_id": check_id, "reservation_id": reservation_id, "item_id": res.item_id,
                             "item_status": ItemStatus.DELIVERED.value, "reservation_status": "FULFILLED",
                             "synthetic": True},
                       recorded=(check_id,), request_id=res.request_id)

    return run(rt, COMPLETE, actor, args, _known_request(rt, reservation_id), body)
