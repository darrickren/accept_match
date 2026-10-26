"""Return requests: create (one per order line), photo uploads, evaluation, and registration into inventory.

Evaluation = deterministic quality checks + the agent's visual observation
(mock or Bedrock) + deterministic rules. APPROVED_DEMO creates exactly one
inventory unit in AWAITING_RECEIPT (conditional put on a deterministic id), so
repeated evaluations can never list the same pair twice. Approved returns are
not purchasable until a (simulated) inspection publishes them.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional

from ..agent.base import AgentFailure
from ..errors import ApiError, conflict, not_found
from ..repository import (ConditionFailed, Repository, TransactionCancelled, idem_key, inv_key, line_key, now_iso,
                          order_key, photo_key, return_key, return_line_key, review_key)
from . import photos as photo_checks
from .rules import evaluate_resale, evaluate_return, resale_price

FINAL = ("APPROVED_DEMO", "HUMAN_REVIEW_REQUIRED", "REJECTED_AFTER_REVIEW")


class ReturnService:
    def __init__(self, repo: Repository, store: Any, invoker: Any, sessions: Any, settings: Any) -> None:
        self.repo, self.store, self.invoker, self.sessions, self.settings = repo, store, invoker, sessions, settings

    # ------------------------------------------------------------ photos

    def upload(self, user: str, session_id: str, declared_view: str, purpose: str, data: bytes, content_type: str,
               request_id: str) -> Dict[str, Any]:
        session = self.sessions.get_owned(user, session_id)
        if session["mode"] != "return":
            raise ApiError(422, "WRONG_SESSION", "Photos can only be added to a return conversation.")
        if purpose != "RETURN_EVIDENCE":
            raise ApiError(422, "INVALID_PURPOSE", "purpose must be RETURN_EVIDENCE.")
        if len(self.sessions.photos(session_id)) >= 30:
            raise ApiError(422, "TOO_MANY_PHOTOS", "This return already has a lot of photos.")
        clean, metrics = photo_checks.clean_upload(data, content_type, self.settings.max_image_bytes,
                                                   self.settings.rules)
        photo_id = f"P-{self.repo.next_id('photo', 5001):05d}"
        key = f"returns/{session_id}/{photo_id}.jpg"
        self.store.put(key, clean)
        stamp = now_iso()
        record = {"PK": photo_key(session_id, photo_id)[0], "SK": photo_key(session_id, photo_id)[1],
                  "entity_type": "photo", "photo_id": photo_id, "owner": user, "owner_type": "session",
                  "session_id": session_id, "storage_key": key, "declared_view": declared_view, "purpose": purpose,
                  "source_type": "SELF_CAPTURED", "uploaded_at": stamp, "dataset_id": "runtime", **metrics}
        self.repo.put(record, if_absent=True)
        return {"photo_id": photo_id, "declared_view": declared_view, "session_id": session_id,
                "quality": {k: metrics[k] for k in ("width", "height", "sharpness", "brightness", "verdict")},
                "request_id": request_id}

    # ------------------------------------------------------------ create

    def _idempotent(self, user: str, scope: str, key: Optional[str], payload: Dict[str, Any]):
        if not key:
            return None, None
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        stored = self.repo.get(idem_key(user, scope, key))
        if stored:
            if stored.get("request_hash") != digest:
                raise conflict("IDEMPOTENCY_KEY_REUSED", "This request key was already used for a different request.")
            return stored, digest
        return None, digest

    def _remember(self, user: str, scope: str, key: Optional[str], digest: Optional[str], response: Dict) -> None:
        if key:
            pk, sk = idem_key(user, scope, key)
            try:
                self.repo.put({"PK": pk, "SK": sk, "entity_type": "idempotency", "request_hash": digest,
                               "response": response, "created_at": now_iso(), "dataset_id": "runtime"}, if_absent=True)
            except ConditionFailed:
                pass

    def create(self, user: str, body: Dict[str, Any], idempotency_key: Optional[str], request_id: str) -> Dict:
        stored, digest = self._idempotent(user, "returns", idempotency_key, body)
        if stored:
            return self.view(self.get_owned(user, stored["response"]["return_id"]), request_id)
        session = self.sessions.get_owned(user, body["session_id"])
        if session["mode"] != "return":
            raise ApiError(422, "WRONG_SESSION", "Use a return conversation for return requests.")
        order_id, line_no = body["order_id"].strip().upper(), str(body["order_line"])
        order = self.repo.get(order_key(order_id))
        if not order or order.get("demo_user_id") != user:
            raise not_found("ORDER_NOT_FOUND", f"Order {order_id} was not found for this demo account.")
        line = self.repo.get(line_key(order_id, line_no))
        if not line:
            raise not_found("ORDER_LINE_NOT_FOUND", f"Order {order_id} has no line {line_no}.")
        guard = self.repo.get(return_line_key(order_id, line_no))
        if guard:  # one return per order line: return the existing request
            existing = self.get_owned(user, guard["return_id"])
            self._remember(user, "returns", idempotency_key, digest, {"return_id": existing["return_id"]})
            return self.view(existing, request_id)
        return_id = f"RET-{self.repo.next_id('return', 101):03d}"
        stamp = now_iso()
        facts = dict((session.get("state") or {}).get("facts") or {})
        record = {"PK": return_key(return_id)[0], "SK": "META", "entity_type": "return_request",
                  "return_id": return_id, "order_id": order_id, "order_line": line_no, "demo_user_id": user,
                  "claim_type": body["claim_type"], "reason": body["reason"][:200], "status": "INFO_COLLECTION",
                  "facts": facts, "session_id": body["session_id"], "policy_version": self.settings.policy_version,
                  "missing_views": [], "rule_decision": {"code": None, "reasons": [], "final_merchant_decision": False},
                  "analysis_source": self.invoker.analysis_source, "model_id": None, "photo_ids": [],
                  "evaluations": 0, "version": 1, "created_at": stamp, "updated_at": stamp, "dataset_id": "runtime"}
        gpk, gsk = return_line_key(order_id, line_no)
        try:
            self.repo.transact([
                {"Put": {"Item": {"PK": gpk, "SK": gsk, "entity_type": "return_guard", "return_id": return_id,
                                  "dataset_id": "runtime"}, "ConditionExpression": "attribute_not_exists(PK)"}},
                {"Put": {"Item": record, "ConditionExpression": "attribute_not_exists(PK)"}},
            ])
        except TransactionCancelled:
            guard = self.repo.get(return_line_key(order_id, line_no))
            if guard:
                return self.view(self.get_owned(user, guard["return_id"]), request_id)
            raise
        self.repo.audit(f"RETURN#{return_id}", "created", {"order_id": order_id, "line": line_no}, user)
        self._remember(user, "returns", idempotency_key, digest, {"return_id": return_id})
        return self.view(record, request_id)

    # ------------------------------------------------------------ evaluate

    def get_owned(self, user: str, return_id: str) -> Dict[str, Any]:
        record = self.repo.get(return_key(return_id))
        if not record or record.get("demo_user_id") != user:
            raise not_found("RETURN_NOT_FOUND", "We couldn't find that return for this demo account.")
        return record

    def evaluate(self, user: str, return_id: str, client_request_id: str, request_id: str) -> Dict[str, Any]:
        record = self.get_owned(user, return_id)
        stored, digest = self._idempotent(user, f"evaluate#{return_id}", client_request_id, {"id": return_id})
        if stored:
            return self.view(self.get_owned(user, return_id), request_id)
        if record["status"] in FINAL:
            return self.view(record, request_id)  # at most one decision per return
        order = self.repo.get(order_key(record["order_id"]))
        line = self.repo.get(line_key(record["order_id"], record["order_line"]))
        evidence = [p for p in self.sessions.photos(record["session_id"]) if p.get("purpose") == "RETURN_EVIDENCE"]
        required = list(self.settings.rules["required_views"][record["claim_type"]])
        images = {p["photo_id"]: self.store.get(p["storage_key"]) or b"" for p in evidence}
        try:
            vision, run = self.invoker.analyze_photos(evidence, required, record.get("facts") or {}, line or {},
                                                      images)
        except AgentFailure as failure:
            if failure.code == "AI_OUTPUT_INVALID":  # plan: no normal decision; a person takes over
                self._route_to_review(record, ["AI_OUTPUT_INVALID"], "The AI analysis failed validation.", user)
            raise
        problems = vision.check_photo_ids([p["photo_id"] for p in evidence])
        if problems:
            self._route_to_review(record, ["AI_OUTPUT_INVALID"], "; ".join(problems[:3]), user)
            raise ApiError(502, "AI_OUTPUT_INVALID", "The AI returned a result that failed validation.", False,
                           {"problems": problems[:5]})
        decision = evaluate_return(self.settings.rules, order or {}, line or {}, record["claim_type"],
                                   record.get("facts") or {}, vision, evidence, self.settings.demo_date)
        fields: Dict[str, Any] = {
            "status": decision.status, "missing_views": decision.missing, "retake_reason": decision.retake_reason,
            "rule_decision": {"code": decision.code, "reasons": decision.reasons, "final_merchant_decision": False},
            "visual_observation": vision.model_dump(), "analysis_source": self.invoker.analysis_source,
            "model_id": self.invoker.vision_model_id, "photo_ids": [p["photo_id"] for p in evidence],
            "grade": decision.grade, "evaluations": int(record.get("evaluations", 0)) + 1, "updated_at": now_iso(),
            "version": int(record["version"]) + 1, "agent_run": run}
        if decision.status == "APPROVED_DEMO":
            fields["item_id"] = self._register(record, line or {}, vision.model_dump()["defects"], decision.grade,
                                               evidence)
        try:
            record = self.repo.update(return_key(return_id), fields, condition="version = :c_v",
                                      values={":c_v": int(record["version"])})
        except ConditionFailed:
            raise conflict("RETURN_CONFLICT", "This return was updated at the same time. Please check its status.",
                           retryable=True) from None
        if decision.review_required:
            self._route_to_review(record, [r[:60] for r in decision.reasons], "; ".join(decision.reasons), user,
                                  update=False)
        self.repo.audit(f"RETURN#{return_id}", "evaluated", {"status": decision.status, "code": decision.code,
                                                             "source": self.invoker.analysis_source}, user)
        self._remember(user, f"evaluate#{return_id}", client_request_id, digest, {"return_id": return_id})
        return self.view(record, request_id)

    def _route_to_review(self, record: Dict[str, Any], reasons: List[str], summary: str, actor: str,
                         update: bool = True) -> None:
        review_id = f"REV-{record['return_id']}-CLAIM"
        pk, sk = review_key(review_id)
        try:
            self.repo.put({"PK": pk, "SK": sk, "entity_type": "human_review", "review_id": review_id,
                           "return_id": record["return_id"], "review_stage": "CLAIM", "status": "OPEN",
                           "reason_codes": reasons[:6], "summary": summary[:300], "simulated": True,
                           "created_at": now_iso(), "dataset_id": "runtime"}, if_absent=True)
        except ConditionFailed:
            pass
        if update:
            self.repo.update(return_key(record["return_id"]), {
                "status": "HUMAN_REVIEW_REQUIRED", "updated_at": now_iso(),
                "rule_decision": {"code": "AI_OUTPUT_INVALID", "reasons": [summary[:200]],
                                  "final_merchant_decision": False}})

    def _register(self, record: Dict[str, Any], line: Dict[str, Any], defects: List[Dict[str, Any]],
                  grade: Optional[str], evidence: List[Dict[str, Any]]) -> Optional[str]:
        """Exactly one unit per approved return. Standard returns without defects aren't listed (policy)."""
        if not defects:
            return None
        item_id = f"ITM-{record['return_id']}"
        resale = evaluate_resale(self.settings.rules, grade, defects)
        price = resale_price(int(line.get("unit_price_cents", 0)), resale["price_ratio"])
        source = "AI_OBSERVATION" if self.invoker.analysis_source != "SIMULATED" else "DEMO_SIMULATED"
        rows = [{"defect_id": f"D-{record['return_id']}-{i + 1}", "type": d["type"], "severity": d["severity"],
                 "location": d["location"], "description": d["description"], "evidence_photo_ids": d["photo_ids"],
                 "uncertainty": d["uncertainty"], "verified_by": source} for i, d in enumerate(defects)]
        stamp = now_iso()
        item = {"PK": inv_key(item_id)[0], "SK": inv_key(item_id)[1], "entity_type": "inventory_item",
                "item_id": item_id, "source_return_id": record["return_id"], "sku": line.get("sku"),
                "title": line.get("product_name", "Returned pair"),
                "description": f"Returned {str(line.get('product_name', 'pair')).lower()} from a demo defect claim. "
                               "Listed only after a simulated warehouse inspection.",
                "category": "SHOES", "shoe_type": line.get("shoe_type"), "size_system": line.get("size_system"),
                "size_value": str(line.get("size_value")), "color": line.get("color"), "price_cents": price,
                "currency": "CAD", "grade": grade or "UNKNOWN", "resale_eligible": False,
                "resale_candidate": resale["resale_eligible"], "status": "AWAITING_RECEIPT", "listing_version": 1,
                "defects": rows, "photos": [], "evidence_session_id": record["session_id"],
                "created_at": stamp, "updated_at": stamp, "dataset_id": "runtime"}
        try:
            self.repo.put(item, if_absent=True)
            self.repo.audit(f"ITEM#{item_id}", "registered", {"return_id": record["return_id"],
                                                              "status": "AWAITING_RECEIPT"}, "system")
        except ConditionFailed:
            pass  # already registered by an earlier evaluation
        return item_id

    # ------------------------------------------------------------ views

    def view(self, record: Dict[str, Any], request_id: str) -> Dict[str, Any]:
        status = record["status"]
        inventory_status = None
        if status == "APPROVED_DEMO" and record.get("item_id"):
            item = self.repo.get(inv_key(record["item_id"])) or {}
            inventory_status = item.get("status")
        approved = status == "APPROVED_DEMO"
        next_step = {"NEEDS_MORE_EVIDENCE": "Add the photos listed, then send them for review.",
                     "HUMAN_REVIEW_REQUIRED": "A specialist will review this request (simulated review queue).",
                     "REJECTED_AFTER_REVIEW": "This was a simulated reviewer's decision, not an ALDO decision.",
                     "INFO_COLLECTION": "Add the requested photos, then submit for review.",
                     "DRAFT": "Add the requested photos, then submit for review."}.get(status)
        if approved:
            next_step = self.settings.rules.get("approved_next_step")
        decision = record.get("rule_decision") or {}
        return {"return_id": record["return_id"], "status": status, "order_id": record["order_id"],
                "order_line": str(record["order_line"]), "claim_type": record["claim_type"],
                "reason": record.get("reason"), "policy_version": record.get("policy_version") or
                self.settings.policy_version,
                "missing_views": [m for m in record.get("missing_views") or [] if m.get("view")],
                "retake_reason": record.get("retake_reason"), "next_step": next_step,
                "demo_address": self.settings.rules.get("demo_return_address") if approved else None,
                "inventory_status": inventory_status, "item_id": record.get("item_id") if approved else None,
                "analysis_source": record.get("analysis_source"), "model_id": record.get("model_id"),
                "rule_decision": {"code": decision.get("code"), "reasons": list(decision.get("reasons") or [])[:6],
                                  "final_merchant_decision": False},
                "photo_ids": list(record.get("photo_ids") or []), "created_at": record["created_at"],
                "updated_at": record["updated_at"], "agent": record.get("agent_run"), "request_id": request_id}
