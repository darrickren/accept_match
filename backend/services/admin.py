"""Protected admin operations: the simulated claim review queue, warehouse receipt and inspection.

Every action is explicit (an admin API call), recorded with simulated=true,
the operator and a timestamp, and audited. Inventory only moves along the
plan's lifecycle with conditional writes:
AWAITING_RECEIPT -> INSPECTION_PENDING -> AVAILABLE | QUARANTINED (-> SOLD at checkout).
"""

from __future__ import annotations

import base64
from typing import Any, Dict, List, Optional, Tuple

from ..errors import ApiError, conflict, not_found
from ..repository import ConditionFailed, Repository, inv_key, line_key, now_iso, return_key, review_key
from .rules import evaluate_resale, overall_grade


def _page(rows: List[Dict[str, Any]], cursor: Optional[str], limit: int) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    start = 0
    if cursor:
        try:
            start = int(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode())
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "INVALID_CURSOR", "That page link is no longer valid.") from None
    nxt = base64.urlsafe_b64encode(str(start + limit).encode()).decode().rstrip("=") if start + limit < len(rows) \
        else None
    return rows[start:start + limit], nxt


class AdminService:
    def __init__(self, repo: Repository, store: Any, returns: Any, settings: Any) -> None:
        self.repo, self.store, self.returns, self.settings = repo, store, returns, settings

    # ------------------------------------------------------------ reviews

    @staticmethod
    def review_view(row: Dict[str, Any]) -> Dict[str, Any]:
        return {k: row.get(k) for k in ("review_id", "return_id", "review_stage", "status", "reason_codes", "summary",
                                        "conclusion", "decided_by", "decided_at", "created_at")} | \
            {"simulated": True, "reason_codes": list(row.get("reason_codes") or [])}

    def reviews(self, stage: Optional[str], status: Optional[str], cursor: Optional[str], limit: int = 20):
        rows = [r for r in self.repo.query("REVIEWS", "REVIEW#")
                if (not stage or r.get("review_stage") == stage) and (not status or r.get("status") == status)]
        rows.sort(key=lambda r: (r.get("status") != "OPEN", r.get("created_at", ""), r["review_id"]))
        page, nxt = _page(rows, cursor, limit)
        return [self.review_view(r) for r in page], nxt

    def decide(self, actor: str, review_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        review = self.repo.get(review_key(review_id))
        if not review:
            raise not_found("REVIEW_NOT_FOUND", "That review doesn't exist.")
        if review.get("status") == "DECIDED":
            raise conflict("REVIEW_ALREADY_DECIDED", "This review was already decided.",
                           {"conclusion": review.get("conclusion")})
        record = self.repo.get(return_key(review["return_id"]))
        if not record or record.get("status") != "HUMAN_REVIEW_REQUIRED":
            raise conflict("RETURN_NOT_IN_REVIEW", "The return isn't waiting for a review.")
        item_id = None
        if body["conclusion"] == "APPROVE":
            labels = body.get("corrected_labels")
            if labels:
                defects = [{"type": d["type"], "severity": d["severity"], "location": d["location"],
                            "description": d.get("description") or d["type"].replace("_", " "), "photo_ids": [],
                            "uncertainty": "low"} for d in labels]
            else:
                defects = list((record.get("visual_observation") or {}).get("defects") or [])
            line = self.repo.get(line_key(record["order_id"], record["order_line"])) or {}
            evidence = [p for p in self.returns.sessions.photos(record["session_id"])]
            item_id = self.returns._register(record, line, defects, overall_grade(defects) if defects else None,
                                             evidence)
            new_status = "APPROVED_DEMO"
        else:
            new_status = "REJECTED_AFTER_REVIEW"
        stamp = now_iso()
        try:
            self.repo.update(review_key(review_id), {"status": "DECIDED", "conclusion": body["conclusion"],
                                                     "reason": body["reason"], "decided_by": actor,
                                                     "decided_at": stamp, "simulated": True,
                                                     "corrected_labels": body.get("corrected_labels")},
                             condition="#status = :c_open", values={":c_open": "OPEN"})
        except ConditionFailed:
            raise conflict("REVIEW_ALREADY_DECIDED", "This review was already decided.") from None
        decision = {"code": "HUMAN_REVIEW_" + body["conclusion"], "reasons": [f"simulated reviewer: {body['reason']}"],
                    "final_merchant_decision": False}
        self.repo.update(return_key(record["return_id"]), {"status": new_status, "rule_decision": decision,
                                                           "item_id": item_id, "updated_at": stamp,
                                                           "version": int(record.get("version", 1)) + 1})
        self.repo.audit(f"RETURN#{record['return_id']}", "review_decided",
                        {"review_id": review_id, "conclusion": body["conclusion"], "simulated": True}, actor)
        fresh = self.repo.get(review_key(review_id))
        return {"review": self.review_view(fresh), "return_status": new_status, "item_id": item_id}

    # ------------------------------------------------------------ warehouse

    def items(self, status: Optional[str]) -> List[Dict[str, Any]]:
        rows = self.repo.query("INV", "ITEM#")
        return [self.item_view(r) for r in rows if not status or r.get("status") == status]

    @staticmethod
    def item_view(row: Dict[str, Any]) -> Dict[str, Any]:
        return {"item_id": row["item_id"], "status": row["status"], "grade": row.get("grade", "UNKNOWN"),
                "resale_eligible": bool(row.get("resale_eligible")), "listing_version": int(row["listing_version"]),
                "source_return_id": row.get("source_return_id"), "inspection": row.get("inspection")}

    def receive(self, actor: str, item_id: str) -> Dict[str, Any]:
        stamp = now_iso()
        try:
            row = self.repo.update(inv_key(item_id), {"status": "INSPECTION_PENDING", "received_at": stamp,
                                                      "received": {"simulated": True, "operator": actor, "at": stamp},
                                                      "updated_at": stamp},
                                   condition="attribute_exists(PK) AND #status = :c_s",
                                   values={":c_s": "AWAITING_RECEIPT"})
        except ConditionFailed:
            self._transition_error(item_id, "AWAITING_RECEIPT")
        self.repo.audit(f"ITEM#{item_id}", "received", {"simulated": True}, actor)
        return self.item_view(row)

    def inspect(self, actor: str, item_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        row = self.repo.get(inv_key(item_id))
        if not row:
            raise not_found("ITEM_NOT_FOUND", "That unit doesn't exist.")
        if row.get("status") != "INSPECTION_PENDING":
            self._transition_error(item_id, "INSPECTION_PENDING")
        grade = body.get("grade") or row.get("grade")
        resale = evaluate_resale(self.settings.rules, grade, row.get("defects") or [])
        publish = body["outcome"] == "PASS" and resale["resale_eligible"]
        if body["outcome"] == "PASS" and not publish:
            if grade in (None, "UNKNOWN"):
                raise ApiError(422, "GRADE_REQUIRED", "Set a grade (A, B or C) to pass an ungraded unit.")
        stamp = now_iso()
        fields: Dict[str, Any] = {
            "status": "AVAILABLE" if publish else "QUARANTINED", "resale_eligible": publish, "grade": grade,
            "inspection": {"simulated": True, "operator": actor, "outcome": body["outcome"], "notes": body.get("notes"),
                           "grade": grade, "at": stamp, "label": "Simulated warehouse inspection"},
            "updated_at": stamp}
        if publish:
            fields["photos"] = self._publish_photos(row, body.get("publish_photo_ids"))
            if not fields["photos"]:
                raise ApiError(422, "PHOTOS_REQUIRED", "A unit needs at least one photo before it can be listed.")
        try:
            row = self.repo.update(inv_key(item_id), fields, condition="#status = :c_s",
                                   values={":c_s": "INSPECTION_PENDING"})
        except ConditionFailed:
            self._transition_error(item_id, "INSPECTION_PENDING")
        self.repo.audit(f"ITEM#{item_id}", "inspected", {"outcome": body["outcome"], "status": row["status"],
                                                         "simulated": True}, actor)
        return self.item_view(row)

    def _publish_photos(self, row: Dict[str, Any], chosen: Optional[List[str]]) -> List[Dict[str, Any]]:
        """Seed units already have inspection photos. Units from returns publish selected evidence photos."""
        if row.get("photos"):
            return list(row["photos"])
        session_id = row.get("evidence_session_id")
        if not session_id:
            return []
        evidence = self.returns.sessions.photos(session_id)
        if chosen:
            picked = [p for p in evidence if p["photo_id"] in set(chosen)]
            if len(picked) != len(set(chosen)):
                raise ApiError(422, "UNKNOWN_PHOTO", "Some of those photos don't belong to this unit's return.")
        else:
            wanted = self.settings.rules.get("publish_views", [])
            newest: Dict[str, Dict[str, Any]] = {}
            for photo in sorted(evidence, key=lambda p: p.get("uploaded_at", "")):
                if photo.get("declared_view") in wanted and (photo.get("verdict") or {}).get("sharpness") == "PASS":
                    newest[photo["declared_view"]] = photo
            picked = [newest[v] for v in wanted if v in newest]
        published = []
        for photo in picked:
            key = f"items/{row['item_id']}/{photo['photo_id']}.jpg"
            self.store.copy(photo["storage_key"], key)  # re-encoded at upload: no camera metadata
            published.append({"photo_id": photo["photo_id"], "view": photo.get("declared_view"),
                              "alt": f"{row.get('title', 'Returned pair')}, {photo.get('declared_view', '').replace('_', ' ')}",
                              "storage_key": key})
        return published

    def _transition_error(self, item_id: str, expected: str) -> None:
        row = self.repo.get(inv_key(item_id))
        if not row:
            raise not_found("ITEM_NOT_FOUND", "That unit doesn't exist.")
        raise conflict("INVALID_TRANSITION", f"The unit is {row.get('status')}, not {expected}.",
                       {"status": row.get("status")})
