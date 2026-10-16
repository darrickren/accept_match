"""Translation between the AcceptMatch FastAPI contract and the customer site's views.

Pure functions, no I/O. Request bodies follow plan section 6 exactly. Points
the plan leaves open are marked ASSUMPTION; each is a one-line change once the
backend publishes contracts/openapi.yaml (see docs/FRONTEND_AGENT_INTEGRATION.md).
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .api_schemas import (PURCHASABLE_GRADES, SHOE_VIEWS, SIMULATED, first, item_problems)

CATEGORY_LABELS = {"SHOES": "Shoes", "BAGS": "Bags", "HANDBAGS": "Handbags", "ACCESSORIES": "Accessories"}
SIZE_SYSTEM_LABELS = {"US_WOMEN": "US women's", "US_MEN": "US men's", "EU": "EU"}
GRADE_LABELS = {"A": "Grade A", "B": "Grade B", "C": "Grade C", "UNKNOWN": "Grade not confirmed"}
GRADE_NOTES = {"A": "Minor cosmetic marks, no known effect on wear.",
               "B": "Noticeable cosmetic marks, checked as wearable.",
               "C": "May affect structure or function. Not sold.",
               "UNKNOWN": "Not graded yet. Not sold until it is."}
DEFECT_LABELS = {"stain": "Stain", "scratch": "Scratch", "scuff": "Scuff", "discoloration": "Discoloration",
                 "stitching_issue": "Stitching issue", "sole_separation": "Sole separation",
                 "broken_heel": "Broken heel", "other": "Other imperfection"}
INVENTORY_LABELS = {"AWAITING_RECEIPT": "Waiting for the item to arrive", "INSPECTION_PENDING": "Received · inspection pending",
                    "AVAILABLE": "Available", "QUARANTINED": "Not for resale", "SOLD": "Sold"}
VIEW_GUIDE = {
    "pair_top": ("Both shoes from above", "Pair side by side, toes forward, whole pair in frame."),
    "pair_outer": ("Outer sides", "Both shoes side by side, outer sides facing the camera."),
    "pair_inner": ("Inner sides", "Both shoes side by side, inner sides facing the camera."),
    "pair_soles": ("Soles", "Turn both shoes over so both soles are fully visible."),
    "pair_heels": ("Heels", "Both shoes from behind, heels facing the camera."),
    "defect_closeup": ("Close-up of the issue", "Fill the frame with the problem area, in daylight."),
    "size_label": ("Size label", "The size or model label inside the shoe, readable."),
}
CLAIM_LABELS = {"STANDARD_RETURN": "Standard return", "DEFECT_CLAIM": "Defect claim"}
UPLOAD_PURPOSE = "RETURN_EVIDENCE"  # ASSUMPTION: the plan names the `purpose` field but not its values
TECHNICAL = re.compile(r"(?i)traceback|exception|arn:aws|s3://|dynamodb|botocore|boto3|\bsql\b")


def _plain(value: Any, limit: int = 280) -> Optional[str]:
    """Single-line, length-limited text from the backend (rendered escaped by the UI)."""
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text[:limit] if text else None


def cents_to_cad(cents: Any) -> Optional[float]:
    return round(int(cents) / 100, 2) if isinstance(cents, int) and not isinstance(cents, bool) else None


def size_label(system: Any, value: Any) -> Optional[str]:
    if system in (None, "") and value in (None, ""):
        return None
    prefix = SIZE_SYSTEM_LABELS.get(str(system), str(system or "").replace("_", " ").title())
    return f"{prefix} {value}".strip()


# ----------------------------------------------------------------- items

def defect_view(defect: Dict[str, Any]) -> Dict[str, Any]:
    return {"defect_id": str(first(defect, "defect_id")), "type": defect["type"],
            "label": DEFECT_LABELS.get(defect["type"], "Imperfection"), "severity": defect["severity"],
            "location": _plain(defect.get("location"), 80), "description": _plain(defect.get("description")),
            "evidence_photo_ids": [str(p) for p in defect.get("evidence_photo_ids") or []],
            "uncertainty": _plain(defect.get("uncertainty"), 40), "verified_by": _plain(defect.get("verified_by"), 40)}


def defect_line(defect: Dict[str, Any]) -> str:
    where = f" · {defect['location']}" if defect.get("location") else ""
    grade = f"grade {defect['severity']}" if defect.get("severity") in ("A", "B", "C") else "not graded"
    return f"{defect['label']}{where} ({grade})"


def photo_view(photo: Any, index: int) -> Optional[Dict[str, Any]]:
    """A photo is a URL string or {photo_id, url, alt, view}. Photos without any URL can't be shown."""
    if isinstance(photo, str):
        return {"photo_id": None, "url": photo, "alt": f"Photo {index + 1}", "view": None}
    if isinstance(photo, dict) and isinstance(photo.get("url"), str) and photo["url"]:
        return {"photo_id": _plain(photo.get("photo_id"), 60), "url": photo["url"],
                "alt": _plain(photo.get("alt"), 120) or f"Photo {index + 1}", "view": _plain(photo.get("view"), 40)}
    return None


def _texts(values: Any, limit: int = 8) -> List[str]:
    out = []
    for value in values or []:
        if isinstance(value, dict):
            value = value.get("label") or value.get("text") or value.get("message") or value.get("constraint")
        text = _plain(value, 200)
        if text and not TECHNICAL.search(text):
            out.append(text)
    return out[:limit]


def item_view(item: Dict[str, Any]) -> Dict[str, Any]:
    """A validated ItemPublic as the footwear card/detail view. Purchasability is never invented here."""
    reasons = _texts(item.get("match_reasons"))
    pending = _texts(item.get("pending_confirmations"))
    match_status = item.get("match_status") or ("NEEDS_CONFIRMATION" if pending else ("MATCH" if reasons else None))
    photos = [p for p in (photo_view(raw, i) for i, raw in enumerate(item.get("photos") or [])) if p]
    category = str(item["category"])
    return {
        "item_id": item["item_id"], "title": _plain(item["title"], 120), "description": _plain(item.get("description"), 600),
        "category": category, "category_label": CATEGORY_LABELS.get(category.upper(), category.title()),
        "shoe_type": _plain(item.get("shoe_type") or item.get("type"), 60),
        "size_system": item.get("size_system"), "size_value": item.get("size_value"),
        "size_label": size_label(item.get("size_system"), item.get("size_value")),
        "color": _plain(item.get("color"), 40), "price_cents": item["price_cents"],
        "price": cents_to_cad(item["price_cents"]), "currency": item["currency"], "grade": item["grade"],
        "grade_label": GRADE_LABELS[item["grade"]], "grade_note": GRADE_NOTES[item["grade"]],
        "status": item["status"], "status_label": INVENTORY_LABELS[item["status"]],
        "purchasable": item["status"] == "AVAILABLE" and item["grade"] in PURCHASABLE_GRADES,
        "defects": [defect_view(d) for d in item["defects"]], "photos": photos,
        "match_status": match_status, "match_reasons": reasons, "pending_confirmations": pending,
        "listing_version": item["listing_version"],
    }


def screen_items(raw_items: Iterable[Any]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Validated item views, plus notes for every item that was dropped or downgraded."""
    views, notes = [], []
    for raw in raw_items or []:
        problems = item_problems(raw)
        if problems:
            notes.append(problems[0])
            continue
        view = item_view(raw)
        if raw["status"] == "AVAILABLE" and raw["grade"] not in PURCHASABLE_GRADES:
            notes.append(f"{view['item_id']}: listed as AVAILABLE with grade {raw['grade']}; purchase disabled")
        if view["match_status"] == "MATCH" and view["pending_confirmations"]:
            view["match_status"] = "NEEDS_CONFIRMATION"  # UNKNOWN preferences are never a full match
            notes.append(f"{view['item_id']}: MATCH with pending confirmations shown as needs confirmation")
        views.append(view)
    return views, notes


# ----------------------------------------------------------------- chat

def _option(raw: Any) -> Optional[Dict[str, str]]:
    if isinstance(raw, str) and raw.strip():
        return {"value": raw.strip()[:80], "label": _plain(raw, 80)}
    if isinstance(raw, dict):
        label = _plain(raw.get("label") or raw.get("text") or raw.get("value"), 80)
        value = _plain(raw.get("value") or raw.get("id") or label, 80)
        if label and value:
            return {"value": value, "label": label}
    return None


def question_view(raw: Any, index: int) -> Dict[str, Any]:
    """ASSUMPTION: a question is a string or {question_id|id, text|question|prompt, options|choices, type, help}."""
    if isinstance(raw, str):
        return {"question_id": f"q{index + 1}", "text": _plain(raw, 300), "options": [], "multi": False, "help": None}
    options = [o for o in (_option(x) for x in (raw.get("options") or raw.get("choices") or [])) if o][:12]
    kind = str(raw.get("type") or "").lower()
    return {"question_id": _plain(raw.get("question_id") or raw.get("id"), 60) or f"q{index + 1}",
            "text": _plain(raw.get("text") or raw.get("question") or raw.get("prompt"), 300),
            "options": options, "multi": bool(raw.get("multiple")) or kind in ("multi_choice", "multi", "multi_select"),
            "help": _plain(raw.get("help"), 200)}


def photo_slot(raw: Any, reason: Optional[str] = None) -> Dict[str, Any]:
    """A requested view ({view, reason} or a bare view name) with customer guidance."""
    if isinstance(raw, dict):
        view, reason = str(raw.get("view")), raw.get("reason") or reason
    else:
        view = str(raw)
    label, hint = VIEW_GUIDE.get(view, (view.replace("_", " ").capitalize()[:60], "Clear photo, in daylight."))
    return {"view": view, "label": label, "hint": hint, "reason": _plain(reason, 200), "known": view in SHOE_VIEWS}


def chat_view(body: Dict[str, Any]) -> Dict[str, Any]:
    """A validated ChatResponse as the assistant's view of one turn."""
    items, notes = screen_items(body["items"])
    slots, seen = [], set()
    for raw in body["required_photos"]:
        slot = photo_slot(raw)
        if slot["view"] not in seen:
            seen.add(slot["view"])
            slots.append(slot)
        if not slot["known"]:
            notes.append(f"unknown photo view {slot['view'][:30]!r} shown with generic guidance")
    return {
        "session_id": body["session_id"], "mode": body["mode"], "message": _plain(body["message"], 1200) or "",
        "next_action": body["next_action"],
        "questions": [question_view(q, i) for i, q in enumerate(body["questions"])][:3],
        "required_photos": slots, "return_status": body["return_status"], "items": items,
        "pending_acceptances": _texts(body["pending_acceptances"], 12), "can_purchase": body["can_purchase"],
        "analysis_source": body["analysis_source"], "model_id": _plain(body.get("model_id"), 120),
        "request_id": str(body["request_id"])[:80], "errors": _texts(body["errors"], 5),
        "blocking_constraints": _texts(body.get("blocking_constraints"), 6),
        "notes": notes,
    }


# ----------------------------------------------------------------- returns and purchases

def return_view(body: Dict[str, Any]) -> Dict[str, Any]:
    missing_raw = body.get("missing_views") or body.get("required_photos") or []
    retake = body.get("retake_reason")
    if isinstance(retake, list):
        retake = "; ".join(_texts(retake, 6))
    decision = body.get("rule_decision") if isinstance(body.get("rule_decision"), dict) else {}
    return {
        "return_id": str(first(body, "return_id")), "status": body["status"],
        "claim_type": body.get("claim_type"), "claim_label": CLAIM_LABELS.get(body.get("claim_type")),
        "order_id": _plain(body.get("order_id"), 40), "order_line": _plain(body.get("order_line"), 10),
        "missing": [photo_slot(m) for m in missing_raw][:10], "retake_reason": _plain(retake, 300),
        "next_step": _plain(body.get("next_step"), 300), "demo_address": _address(body.get("demo_address")),
        "inventory_status": body.get("inventory_status"),
        "inventory_label": INVENTORY_LABELS.get(body.get("inventory_status")),
        "item_id": _plain(body.get("item_id"), 60), "analysis_source": body.get("analysis_source"),
        "model_id": _plain(body.get("model_id"), 120), "policy_version": _plain(body.get("policy_version"), 60),
        "decision_code": _plain(decision.get("code"), 60),
        "final_merchant_decision": decision.get("final_merchant_decision") is True,
        "request_id": _plain(body.get("request_id"), 80),
        "photo_ids": [str(p) for p in body.get("photo_ids") or []][:20],
    }


def _address(raw: Any) -> List[str]:
    """The backend's fictional demo address, as display lines."""
    if isinstance(raw, str):
        return [line for line in (_plain(part, 120) for part in raw.splitlines()) if line][:6]
    if isinstance(raw, dict):
        lines = raw.get("lines") or raw.get("address_lines")
        if isinstance(lines, list):
            return _texts(lines, 6)
        return [line for line in (_plain(raw.get(k), 120) for k in ("label", "name", "line1", "line2", "city",
                                                                     "region", "postal_code", "country")) if line]
    if isinstance(raw, list):
        return _texts(raw, 6)
    return []


def purchase_view(body: Dict[str, Any]) -> Dict[str, Any]:
    return {"purchase_id": str(body["purchase_id"])[:60], "item_id": str(body["item_id"])[:60],
            "listing_version": body.get("listing_version"), "amount_cents": body.get("amount_cents"),
            "amount": cents_to_cad(body.get("amount_cents")), "currency": body.get("currency") or "CAD",
            "status": _plain(body.get("status"), 40), "payment": _plain(body.get("payment") or body.get("payment_type"), 40),
            "created_at": _plain(body.get("created_at"), 40)}


def analysis_label(source: Optional[str], model_id: Optional[str]) -> Tuple[str, str]:
    """(label, tone) for the source pill. Real AI is only claimed with a non-simulated source and a model id."""
    if not source:
        return "AcceptMatch service", "demo"
    if str(source).upper() == SIMULATED:
        return "Live service · simulated AI", "demo"
    if model_id:
        return f"AI-assisted · {str(model_id).split(':')[0][:40]}", "verified"
    return "Live service · AI not confirmed", "demo"


# ----------------------------------------------------------------- request bodies (plan section 6)

def session_body(mode: str) -> Dict[str, Any]:
    return {"mode": mode}


def message_body(text: str, photo_ids: Iterable[str], client_request_id: str) -> Dict[str, Any]:
    return {"text": text, "photo_ids": list(photo_ids), "client_request_id": client_request_id}


def return_body(session_id: str, order_id: str, order_line: str, claim_type: str, reason: str) -> Dict[str, Any]:
    # ASSUMPTION: photos uploaded with this session_id are linked by the backend (no photo_ids field in the plan).
    return {"session_id": session_id, "order_id": order_id, "order_line": order_line, "claim_type": claim_type,
            "reason": reason}


def evaluate_body(client_request_id: str) -> Dict[str, Any]:
    return {"client_request_id": client_request_id}


def acceptance_body(listing_version: int, defect_ids: Iterable[str]) -> Dict[str, Any]:
    return {"listing_version": listing_version,
            "acceptances": [{"defect_id": defect_id, "accepted": True} for defect_id in defect_ids]}


def checkout_body(item_id: str, listing_version: int, idempotency_key: str) -> Dict[str, Any]:
    return {"item_id": item_id, "listing_version": listing_version, "idempotency_key": idempotency_key}


def upload_fields(session_id: str, view: str) -> Dict[str, str]:
    return {"session_id": session_id, "declared_view": view, "purpose": UPLOAD_PURPOSE}


ITEM_FILTERS = ("category", "size_system", "size_value", "color", "min_price_cents", "max_price_cents", "defect_type",
                "max_grade")


def items_query(filters: Dict[str, Any], cursor: Optional[str] = None) -> Dict[str, Any]:
    query = {k: filters.get(k) for k in ITEM_FILTERS if filters.get(k) not in (None, "")}
    if cursor:
        query["cursor"] = cursor
    return query
