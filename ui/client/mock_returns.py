"""Return operations for the MOCK Agent (scripted, synthetic).

Two-phase submission: submit_return writes the case first and returns its id
and status token; assess_return then decides. The demo assessment applies
DRAFT rules (not ALDO policy) and only checks that required photos are
present: it does not analyse photo content. Photo uploads follow the
create -> PUT -> complete pattern, with server-side re-validation on complete.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import re
import secrets
from typing import Any, Dict, List, Optional

from PIL import Image, UnidentifiedImageError

RETURN_RULES = [
    {"rule_id": "RR-01", "text": "Photos of the front, the back and each reported issue", "status": "draft"},
    {"rule_id": "RR-02", "text": "Bought within the last 30 days (when a purchase date is given)", "status": "draft"},
    {"rule_id": "RR-03", "text": "Final-sale purchases can't be returned (demo IDs starting DEMO-9)", "status": "draft"},
    {"rule_id": "RR-04", "text": "Water, fire, odour or authenticity concerns go to a specialist", "status": "draft"},
]
REVIEW_WORDS = r"\b(water|flood|wet|fire|smoke|burn|mou?ld|smell|odou?r|counterfeit|fake|authentic)\w*"
SHIPPING = {"destination": "AcceptMatch Returns Centre (demo)", "address_lines": ["Demo address · not a real location"],
            "instructions": ["Pack the item with any packaging you still have.", "Write your return ID on the parcel.",
                             "Ship it within 14 days."], "simulated": True}
COMPONENT_HINTS = {"zipper": "Show it open, where it sticks or breaks.", "strap": "Show the damaged part of the strap.",
                   "handles": "Show where the handle is damaged.", "interior": "Show the inside lining."}
COMPONENT_WORDS = {"zipper": r"\bzip", "strap": r"\bstrap", "handles": r"\bhandle", "interior": r"lining|inside|interior"}
MARK_WORDS = r"scratch|scuff|stain|mark|spot|fad|discolou?r|corner|tear|rip|hole|crack|peel"
OPTIONAL_BY_CATEGORY = {"crossbody": "strap", "tote": "handles", "handbag": "handles"}
DEMO_ORDER = re.compile(r"^DEMO-\d{4,6}$")


class _Error(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


def requirements(category: str, reason: str, description: str, max_photos: int) -> Dict[str, Any]:
    """Adaptive photo checklist from the reported issue (keyword rules)."""
    text = (description or "").lower()
    slots = [{"slot": "front", "label": "Front of the item", "required": True, "hint": "Whole item in frame."},
             {"slot": "back", "label": "Back of the item", "required": True, "hint": "Whole item in frame."}]
    issues: List[str] = []
    for component, pattern in COMPONENT_WORDS.items():
        if re.search(pattern, text):
            issues.append(component)
            slots.append({"slot": f"issue-{component}", "label": f"Close-up of the {component}", "required": True,
                          "hint": COMPONENT_HINTS[component]})
    for area in ("front", "back"):
        clause = re.search(rf"[^.;]*\b{area}\b[^.;]*", text)
        if clause and re.search(MARK_WORDS, clause.group(0)):
            issues.append(area)
            slots.append({"slot": f"issue-{area}", "label": f"Close-up of the mark on the {area}", "required": True,
                          "hint": "Fill the frame with the mark, in daylight."})
    if not issues and (re.search(MARK_WORDS, text) or reason in ("damaged", "not_working")):
        slots.append({"slot": "issue-general", "label": "Close-up of the issue", "required": True,
                      "hint": "Fill the frame with the problem area."})
    extra = OPTIONAL_BY_CATEGORY.get(category)
    if extra and extra not in issues:
        slots.append({"slot": f"part-{extra}", "label": f"The {extra}", "required": False,
                      "hint": "Optional, helps us check it quickly."})
    slots.append({"slot": "label", "label": "Product label", "required": False,
                  "hint": "Optional. The inside label, if there is one. No receipts or documents."})
    slots = slots[:max_photos]
    required = [s for s in slots if s["required"]]
    return {"slots": slots, "min_photos": len(required), "max_photos": max_photos, "reported_issues": issues,
            "rules_status": "draft"}


class ReturnOps:
    """Mixin for MockAgentClient. Uses self._respond / self._event / self._overlay."""

    def _limits(self):
        settings = self._settings
        return (getattr(settings, "photo_max_px", 2048), getattr(settings, "photo_max_mb", 10.0),
                getattr(settings, "session_photo_budget_mb", 40.0), getattr(settings, "return_photos_max", 8))

    def _guard(self, body):
        def wrapped(trace):
            try:
                return body(trace)
            except _Error as err:
                from .mock_agent import _Refusal

                raise _Refusal(err.code, err.message)
        return wrapped

    # ------------------------------------------------------------ checklist

    def get_return_requirements(self, session_id: str, category: str, reason: str, description: str,
                                client_request_id: str) -> Dict[str, Any]:
        def body(trace):
            result = requirements(category, reason, description, self._limits()[3])
            drafts = self._overlay.setdefault("drafts", {})
            drafts[session_id] = {"requirements": result, "category": category}
            self._event(trace, "tool_call", f"photo_checklist → {len(result['slots'])} slots, "
                                            f"issues: {', '.join(result['reported_issues']) or 'none detected'}")
            return result
        return self._respond("get_return_requirements", client_request_id, self._guard(body))

    # ------------------------------------------------------------ photos (create -> PUT -> complete)

    def create_photo_upload(self, session_id: str, draft_id: str, slot: str, content_type: str, byte_size: int,
                            sha256: str, client_request_id: str) -> Dict[str, Any]:
        def body(trace):
            _, max_mb, _, _ = self._limits()
            if content_type != "image/jpeg":
                raise _Error("photo_rejected", "Please upload a JPG, PNG or WebP photo.")
            if byte_size <= 0 or byte_size > max_mb * 1024 * 1024:
                raise _Error("photo_rejected", f"Please use a photo under {max_mb:g} MB.")
            counters = self._overlay.setdefault("counters", {})
            counters["upload"] = counters.get("upload", 0) + 1
            upload_id = f"UP-{counters['upload']:04d}"
            self._overlay.setdefault("uploads", {})[upload_id] = {
                "upload_id": upload_id, "draft_id": draft_id, "slot": slot, "content_type": content_type,
                "byte_size": byte_size, "sha256": sha256, "data": None}
            return {"upload_id": upload_id, "upload_url": f"mock://uploads/{upload_id}", "expires_in_s": 900}
        return self._respond("create_photo_upload", client_request_id, self._guard(body))

    def put_upload(self, upload_id: str, data: bytes) -> None:
        """Stands in for the presigned PUT to object storage."""
        upload = self._overlay.get("uploads", {}).get(upload_id)
        if upload is not None:
            upload["data"] = bytes(data)

    def complete_photo_upload(self, session_id: str, upload_id: str, client_request_id: str) -> Dict[str, Any]:
        """Server-side re-validation: decode, type, size, pixels, checksum, slot and owner."""
        def body(trace):
            max_px, max_mb, budget_mb, _ = self._limits()
            upload = self._overlay.get("uploads", {}).pop(upload_id, None)
            if not upload:
                raise _Error("not_found", "That upload has expired. Please add the photo again.")
            draft = self._overlay.get("drafts", {}).get(upload["draft_id"]) or {}
            slots = {s["slot"] for s in (draft.get("requirements") or {}).get("slots", [])}
            data = upload.get("data") or b""
            reason = None
            if upload["slot"] not in slots:
                reason = "That photo slot isn't part of this return."
            elif not data or len(data) != upload["byte_size"] or len(data) > max_mb * 1024 * 1024:
                reason = "The upload didn't arrive complete. Please try again."
            elif hashlib.sha256(data).hexdigest() != upload["sha256"]:
                reason = "The upload didn't arrive intact. Please try again."
            else:
                try:
                    with Image.open(io.BytesIO(data)) as probe:
                        kind, size = probe.format, probe.size
                        probe.verify()
                    if kind != "JPEG":
                        reason = "Please upload a JPG, PNG or WebP photo."
                    elif max(size) > max_px or size[0] * size[1] > 40_000_000:
                        reason = "That photo is too large."
                except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
                    reason = "We couldn't read that file as a photo."
            used = self._overlay.get("photo_bytes", 0)
            if reason is None and used + len(data) > budget_mb * 1024 * 1024:
                reason = "You've added a lot of photos. Please remove one before adding another."
            if reason:
                self._event(trace, "guardrail", f"upload {upload_id} rejected: {reason}")
                return {"photo_id": None, "validation_status": "rejected", "reason": reason}
            counters = self._overlay.setdefault("counters", {})
            counters["photo"] = counters.get("photo", 0) + 1
            photo_id = f"RP-{counters['photo']:04d}"
            self._overlay.setdefault("photos", {})[photo_id] = {
                "photo_id": photo_id, "draft_id": upload["draft_id"], "return_case_id": None, "slot": upload["slot"],
                "data": data, "sha256": upload["sha256"], "width": size[0], "height": size[1],
                "validation_status": "accepted"}
            self._overlay["photo_bytes"] = used + len(data)
            self._event(trace, "tool_call", f"validate_photo({upload['slot']}) → accepted")
            return {"photo_id": photo_id, "validation_status": "accepted", "reason": None, "width": size[0],
                    "height": size[1], "sha256": upload["sha256"]}
        return self._respond("complete_photo_upload", client_request_id, self._guard(body))

    def discard_photo(self, session_id: str, draft_id: str, photo_id: str, client_request_id: str) -> Dict[str, Any]:
        def body(trace):
            photo = self._overlay.get("photos", {}).get(photo_id)
            if photo and photo["draft_id"] == draft_id and photo["return_case_id"] is None:
                self._overlay["photo_bytes"] = max(0, self._overlay.get("photo_bytes", 0) - len(photo["data"] or b""))
                del self._overlay["photos"][photo_id]
            return {"photo_id": photo_id, "discarded": True}
        return self._respond("discard_photo", client_request_id, self._guard(body))

    # ------------------------------------------------------------ cases

    def submit_return(self, session_id: str, draft_id: str, product: Dict[str, Any], reason: str, description: str,
                      photo_ids: List[str], client_request_id: str) -> Dict[str, Any]:
        def body(trace):
            draft = self._overlay.get("drafts", {}).get(draft_id)
            if not draft:
                raise _Error("invalid_input", "Please add your photos again.")
            photos = self._overlay.get("photos", {})
            for photo_id in photo_ids:
                photo = photos.get(photo_id)
                if not photo or photo["draft_id"] != draft_id or photo["validation_status"] != "accepted":
                    raise _Error("invalid_input", "One of the photos can't be used. Please add it again.")
            order = (product or {}).get("order_ref") or ""
            if order and not DEMO_ORDER.match(order):
                raise _Error("invalid_input", "Please use a demo order ID like DEMO-10023, or leave it blank.")
            counters = self._overlay.setdefault("counters", {})
            counters["case"] = counters.get("case", 0) + 1
            case_id = f"RET-{100000 + counters['case']}"
            token = secrets.token_hex(4).upper()
            now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
            case = {"case_id": case_id, "status_token_hash": hashlib.sha256(token.encode()).hexdigest(),
                    "state_version": 1, "product": dict(product or {}), "reason": reason,
                    "description": (description or "")[:2000], "photo_ids": list(photo_ids),
                    "requirements": draft["requirements"], "assessment_status": "pending",
                    "logistics_status": "not_started", "shipping": None, "assessments": [],
                    "created_at": now, "events": [{"label": "Request received", "at": now}],
                    "policy_version": "RR-DRAFT-1"}
            for photo_id in photo_ids:
                photos[photo_id]["return_case_id"] = case_id
            self._overlay.setdefault("returns", {})[case_id] = case
            self._audit(case_id, "submitted", {"photos": [photos[p]["sha256"] for p in photo_ids]})
            self._event(trace, "tool_call", f"create_return_case → {case_id} (pending)", record_id=case_id)
            return {"case_id": case_id, "status_token": token, "state_version": 1, "assessment_status": "pending"}
        return self._respond("submit_return", client_request_id, self._guard(body))

    def _case(self, case_id: str, status_token: str) -> Dict[str, Any]:
        case = self._overlay.get("returns", {}).get((case_id or "").strip().upper())
        token = (status_token or "").replace("-", "").strip().upper()
        if not case or hashlib.sha256(token.encode()).hexdigest() != case["status_token_hash"]:
            raise _Error("not_found", "We couldn't find a return with that ID and code.")
        return case

    def assess_return(self, session_id: str, case_id: str, status_token: str, client_request_id: str) -> Dict[str, Any]:
        def body(trace):
            case = self._case(case_id, status_token)
            done = [a for a in case["assessments"] if a["case_state_version"] == case["state_version"]]
            if done or case["assessment_status"] != "pending":
                return self._view(case)  # at most one assessment per case version
            decision, reasons, requested = self._decide(case)
            assessment = {"assessment_id": f"AS-{case['case_id'][4:]}-{case['state_version']}",
                          "case_state_version": case["state_version"], "decision": decision,
                          "summary": SUMMARIES[decision], "reasons": reasons, "requested_info": requested,
                          "source": "mock"}
            case["assessments"].append(assessment)
            case["assessment_status"] = decision
            now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
            case["events"].append({"label": EVENT_LABELS[decision], "at": now})
            if decision == "approved":
                case["logistics_status"] = "awaiting_receipt"
                case["shipping"] = dict(SHIPPING)
            if decision == "rejected":
                self._purge_photos(case)
            self._audit(case["case_id"], decision, {"rules": [r["rule_id"] for r in reasons]})
            for reason in reasons:
                self._event(trace, "decision", f"{reason['rule_id']} {reason['result']}: {reason['text']}",
                            rule_id=reason["rule_id"], record_id=case["case_id"])
            return self._view(case)
        return self._respond("assess_return", client_request_id, self._guard(body))

    def get_return_case(self, session_id: str, case_id: str, status_token: str, client_request_id: str):
        return self._respond("get_return_case", client_request_id,
                             self._guard(lambda trace: self._view(self._case(case_id, status_token))),
                             idempotent=False)

    def add_return_info(self, session_id: str, case_id: str, status_token: str, text: str, photo_ids: List[str],
                        expected_state_version: int, client_request_id: str) -> Dict[str, Any]:
        def body(trace):
            case = self._case(case_id, status_token)
            if case["state_version"] != expected_state_version:
                from .mock_agent import _Refusal

                raise _Refusal("state_conflict", "This return was updated in another window. "
                                                 "We've loaded the latest version.")
            if case["assessment_status"] != "needs_more_info":
                raise _Error("invalid_input", "This return doesn't need anything else right now.")
            photos = self._overlay.get("photos", {})
            for photo_id in photo_ids:
                photo = photos.get(photo_id)
                if not photo or photo["validation_status"] != "accepted" or photo["return_case_id"] not in (None, case_id):
                    raise _Error("invalid_input", "One of the photos can't be used. Please add it again.")
                photo["return_case_id"] = case_id
                case["photo_ids"].append(photo_id)
            if text:
                case["description"] = (case["description"] + "\n\nAdded later: " + text)[:3000]
            case["assessment_status"] = "pending"
            case["state_version"] += 1
            case["events"].append({"label": "More information added",
                                   "at": dt.datetime.now().astimezone().isoformat(timespec="seconds")})
            return {"case_id": case["case_id"], "assessment_status": "pending", "state_version": case["state_version"]}
        return self._respond("add_return_info", client_request_id, self._guard(body))

    # ------------------------------------------------------------ helpers

    def _decide(self, case: Dict[str, Any]):
        slots = {s["slot"]: s for s in case["requirements"]["slots"]}
        have = {self._overlay["photos"][p]["slot"] for p in case["photo_ids"] if p in self._overlay.get("photos", {})}
        missing = [s for s in slots.values() if s["required"] and s["slot"] not in have]
        if missing:
            return "needs_more_info", [{"rule_id": "RR-01", "result": "missing", "text": "Some photos are missing"}], \
                [{"slot": s["slot"], "label": s["label"], "hint": s["hint"]} for s in missing]
        reasons = [{"rule_id": "RR-01", "result": "pass", "text": "All required photos are present"}]
        product = case["product"]
        purchased = product.get("purchase_date")
        if purchased:
            days = (dt.date.today() - dt.date.fromisoformat(purchased)).days
            if days > 30:
                return "rejected", reasons + [{"rule_id": "RR-02", "result": "fail",
                                               "text": "It was bought more than 30 days ago."}], []
            reasons.append({"rule_id": "RR-02", "result": "pass", "text": "Bought within the last 30 days"})
        if str(product.get("order_ref") or "").startswith("DEMO-9"):
            return "rejected", reasons + [{"rule_id": "RR-03", "result": "fail",
                                           "text": "It was a final-sale purchase."}], []
        if re.search(REVIEW_WORDS, case["description"].lower()):
            return "manual_review", reasons + [{"rule_id": "RR-04", "result": "review",
                                                "text": "A specialist needs to look at this one."}], []
        return "approved", reasons, []

    def _purge_photos(self, case: Dict[str, Any]) -> None:
        """Final rejection: delete photo content, keep only checksums in the audit trail."""
        photos = self._overlay.get("photos", {})
        for photo_id in case["photo_ids"]:
            photo = photos.get(photo_id)
            if photo and photo.get("data") is not None:
                self._overlay["photo_bytes"] = max(0, self._overlay.get("photo_bytes", 0) - len(photo["data"]))
                photo["data"] = None
                photo["deleted"] = True

    def _audit(self, case_id: str, event: str, detail: Dict[str, Any]) -> None:
        self._overlay.setdefault("audit", []).append({"case_id": case_id, "event": event, **detail})

    def _view(self, case: Dict[str, Any]) -> Dict[str, Any]:
        latest = case["assessments"][-1] if case["assessments"] else None
        return {"case_id": case["case_id"], "state_version": case["state_version"],
                "assessment_status": case["assessment_status"], "logistics_status": case["logistics_status"],
                "verification_result": None, "assessment": latest,
                "shipping": case["shipping"] if case["assessment_status"] == "approved" else None,
                "timeline": list(case["events"]), "product": dict(case["product"]),
                "photo_count": len(case["photo_ids"]), "rules_status": "draft"}


SUMMARIES = {"approved": "Your return is approved.", "needs_more_info": "We need a little more to decide.",
             "manual_review": "A specialist will take a closer look.", "rejected": "We can't accept this return."}
EVENT_LABELS = {"approved": "Approved", "needs_more_info": "More information requested",
                "manual_review": "Sent to a specialist", "rejected": "Not accepted"}
