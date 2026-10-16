"""Vocabulary and response checks for the AcceptMatch FastAPI contract.

Source: AcceptMatch_MVP_Implementation_Plan.md (2026-10-15), sections 3-6.
There is no OpenAPI file yet, so these checks follow the plan's fixed keys
and enums. They fail closed on missing or mistyped contract fields and
ignore extra fields. Every function returns a list of problems; an empty list
means the payload can be used. Alternative field names are only accepted
where the plan itself is ambiguous (they are listed in FIELD_ALIASES).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

NEXT_ACTIONS = ("ASK_QUESTION", "UPLOAD_PHOTOS", "SHOW_ITEMS", "REQUEST_ACCEPTANCE", "SHOW_RETURN_STATUS",
                "HUMAN_REVIEW", "COMPLETE")
SESSION_MODES = ("return", "shopping")
RETURN_STATUSES = ("DRAFT", "INFO_COLLECTION", "NEEDS_MORE_EVIDENCE", "APPROVED_DEMO", "HUMAN_REVIEW_REQUIRED",
                   "REJECTED_AFTER_REVIEW")
INVENTORY_STATUSES = ("AWAITING_RECEIPT", "INSPECTION_PENDING", "AVAILABLE", "QUARANTINED", "SOLD")
GRADES = ("A", "B", "C", "UNKNOWN")
PURCHASABLE_GRADES = ("A", "B")
DEFECT_TYPES = ("stain", "scratch", "scuff", "discoloration", "stitching_issue", "sole_separation", "broken_heel",
                "other")
SIZE_SYSTEMS = ("US_WOMEN", "US_MEN", "EU")
SHOE_VIEWS = ("pair_top", "pair_outer", "pair_inner", "pair_soles", "pair_heels", "defect_closeup", "size_label")
CLAIM_TYPES = ("STANDARD_RETURN", "DEFECT_CLAIM")
MATCH_STATUSES = ("MATCH", "NEEDS_CONFIRMATION", "NO_MATCH")
SIMULATED = "SIMULATED"
CHAT_KEYS = ("session_id", "mode", "message", "next_action", "questions", "required_photos", "return_status",
             "items", "pending_acceptances", "can_purchase", "analysis_source", "request_id", "errors")
ERROR_KEYS = ("request_id", "code", "message", "retryable", "detail")
CONFLICT_CODES = ("STALE_LISTING_VERSION", "INCOMPLETE_ACCEPTANCES", "ITEM_UNAVAILABLE")
ITEM_REQUIRED = ("item_id", "title", "category", "price_cents", "currency", "grade", "defects", "status",
                 "listing_version")
FIELD_ALIASES = {
    "return_id": ("return_id", "id"),
    "defect_id": ("defect_id", "id"),
    "token": ("access_token", "token"),
    "next_cursor": ("next_cursor", "cursor"),
}


def first(body: Dict[str, Any], name: str) -> Any:
    """Value of a field, accepting the aliases listed in FIELD_ALIASES."""
    for key in FIELD_ALIASES.get(name, (name,)):
        if isinstance(body, dict) and body.get(key) not in (None, ""):
            return body[key]
    return None


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _optional_text(value: Any) -> bool:
    return value is None or isinstance(value, str)


def _whole(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _list_of_text(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


# ----------------------------------------------------------------- chat

def chat_problems(body: Any) -> List[str]:
    """Structural checks for ChatResponse. Items are checked one by one by the mapper."""
    if not isinstance(body, dict):
        return ["chat response is not an object"]
    problems = [f"chat response missing '{key}'" for key in CHAT_KEYS if key not in body]
    if problems:
        return problems
    if not _text(body["session_id"]):
        problems.append("session_id must be a non-empty string")
    if body["mode"] not in SESSION_MODES:
        problems.append("mode must be 'return' or 'shopping'")
    if not isinstance(body["message"], str):
        problems.append("message must be a string")
    if body["next_action"] not in NEXT_ACTIONS:
        problems.append(f"unknown next_action {str(body['next_action'])[:40]!r}")
    for key in ("questions", "required_photos", "items", "pending_acceptances", "errors"):
        if not isinstance(body[key], list):
            problems.append(f"{key} must be a list")
    if not isinstance(body["can_purchase"], bool):
        problems.append("can_purchase must be a boolean")
    if body["return_status"] is not None and body["return_status"] not in RETURN_STATUSES:
        problems.append(f"unknown return_status {str(body['return_status'])[:40]!r}")
    if not _optional_text(body["analysis_source"]):
        problems.append("analysis_source must be a string or null")
    if not _text(body["request_id"]):
        problems.append("request_id must be a non-empty string")
    if problems:
        return problems
    for index, question in enumerate(body["questions"]):
        if isinstance(question, str):
            continue
        if not isinstance(question, dict) or not any(_text(question.get(k)) for k in ("text", "question", "prompt")):
            problems.append(f"questions[{index}] needs text")
    for index, photo in enumerate(body["required_photos"]):
        if not isinstance(photo, dict) or not _text(photo.get("view")) or not _optional_text(photo.get("reason")):
            problems.append(f"required_photos[{index}] needs a view and an optional reason")
    if body["next_action"] == "UPLOAD_PHOTOS" and not body["required_photos"]:
        problems.append("UPLOAD_PHOTOS without required_photos")
    return problems


# ----------------------------------------------------------------- inventory

def defect_problems(defect: Any) -> List[str]:
    if not isinstance(defect, dict):
        return ["defect is not an object"]
    problems = []
    if not _text(first(defect, "defect_id")):
        problems.append("defect without defect_id")
    if defect.get("type") not in DEFECT_TYPES:
        problems.append(f"unknown defect type {str(defect.get('type'))[:30]!r}")
    if defect.get("severity") not in GRADES:
        problems.append("defect severity must be A, B, C or UNKNOWN")
    for key in ("location", "description", "uncertainty"):
        if not _optional_text(defect.get(key)):
            problems.append(f"defect {key} must be text")
    if "evidence_photo_ids" in defect and not _list_of_text(defect["evidence_photo_ids"] or []):
        problems.append("evidence_photo_ids must be a list of ids")
    return problems


def item_problems(item: Any) -> List[str]:
    """ItemPublic card/detail (plan section 5). Shoes must carry size_system + size_value."""
    if not isinstance(item, dict):
        return ["item is not an object"]
    problems = [f"item missing '{key}'" for key in ITEM_REQUIRED if key not in item]
    if problems:
        return problems
    label = str(item.get("item_id"))[:30]
    if not _text(item["item_id"]):
        problems.append("item_id must be a non-empty string")
    if not _text(item["title"]):
        problems.append(f"{label}: title missing")
    if not _whole(item["price_cents"]) or item["price_cents"] < 0:
        problems.append(f"{label}: price_cents must be a whole number of cents")
    if item["currency"] != "CAD":
        problems.append(f"{label}: currency must be CAD")
    if item["grade"] not in GRADES:
        problems.append(f"{label}: unknown grade")
    if item["status"] not in INVENTORY_STATUSES:
        problems.append(f"{label}: unknown status")
    if not _whole(item["listing_version"]) or item["listing_version"] < 1:
        problems.append(f"{label}: listing_version must be a positive whole number")
    if not isinstance(item["defects"], list):
        problems.append(f"{label}: defects must be a list")
    else:
        for defect in item["defects"]:
            problems += [f"{label}: {p}" for p in defect_problems(defect)]
        ids = [first(d, "defect_id") for d in item["defects"] if isinstance(d, dict)]
        if len(ids) != len(set(ids)):
            problems.append(f"{label}: duplicate defect ids")
    if str(item["category"]).upper() == "SHOES":
        if item.get("size_system") not in SIZE_SYSTEMS:
            problems.append(f"{label}: shoes need size_system US_WOMEN, US_MEN or EU")
        if item.get("size_value") in (None, "") or not isinstance(item.get("size_value"), (str, int, float)):
            problems.append(f"{label}: shoes need size_value")
    for key in ("match_reasons", "pending_confirmations"):
        if key in item and item[key] is not None and not isinstance(item[key], list):
            problems.append(f"{label}: {key} must be a list")
    if "photos" in item and item["photos"] is not None and not isinstance(item["photos"], list):
        problems.append(f"{label}: photos must be a list")
    if item.get("match_status") not in (None,) + MATCH_STATUSES:
        problems.append(f"{label}: unknown match_status")
    for key in ("description", "color"):
        if not _optional_text(item.get(key)):
            problems.append(f"{label}: {key} must be text")
    return problems


def items_page(body: Any) -> Tuple[List[Any], Optional[str], List[str]]:
    """GET /api/items: {items, next_cursor} (a bare list is tolerated)."""
    if isinstance(body, list):
        return body, None, []
    if not isinstance(body, dict) or not isinstance(body.get("items"), list):
        return [], None, ["items response needs an 'items' list"]
    cursor = first(body, "next_cursor")
    return body["items"], (str(cursor) if cursor is not None else None), []


def item_from_detail(body: Any) -> Any:
    """GET /api/items/{id}: the item itself, or {"item": {...}}."""
    if isinstance(body, dict) and isinstance(body.get("item"), dict) and "item_id" not in body:
        return body["item"]
    return body


# ----------------------------------------------------------------- returns, uploads, purchase, auth

def return_problems(body: Any) -> List[str]:
    """Return record from POST /api/returns, /evaluate and GET /api/returns/{id}."""
    if not isinstance(body, dict):
        return ["return response is not an object"]
    problems = []
    if not _text(first(body, "return_id")):
        problems.append("return response without return_id")
    if body.get("status") not in RETURN_STATUSES:
        problems.append(f"unknown return status {str(body.get('status'))[:40]!r}")
    if body.get("claim_type") not in (None,) + CLAIM_TYPES:
        problems.append("unknown claim_type")
    missing = body.get("missing_views")
    if missing is not None and (not isinstance(missing, list) or not all(
            isinstance(m, str) or (isinstance(m, dict) and _text(m.get("view"))) for m in missing)):
        problems.append("missing_views must be a list of views")
    if body.get("status") == "NEEDS_MORE_EVIDENCE" and not missing and not body.get("required_photos"):
        problems.append("NEEDS_MORE_EVIDENCE without missing_views")
    for key in ("analysis_source", "model_id", "next_step"):
        if not _optional_text(body.get(key)):
            problems.append(f"{key} must be text")
    if body.get("inventory_status") not in (None,) + INVENTORY_STATUSES:
        problems.append("unknown inventory_status")
    return problems


def upload_problems(body: Any) -> List[str]:
    if not isinstance(body, dict) or not _text(body.get("photo_id")):
        return ["upload response without photo_id"]
    return []


def login_problems(body: Any) -> List[str]:
    if not isinstance(body, dict) or not _text(first(body, "token")):
        return ["login response without a token"]
    if not _optional_text(body.get("role")):
        return ["login role must be text"]
    expires = body.get("expires_in")
    if expires is not None and (not isinstance(expires, (int, float)) or isinstance(expires, bool) or expires <= 0):
        return ["expires_in must be a positive number of seconds"]
    return []


def session_problems(body: Any) -> List[str]:
    if not isinstance(body, dict) or not _text(body.get("session_id")):
        return ["session response without session_id"]
    return []


def acceptance_problems(body: Any) -> List[str]:
    if not isinstance(body, dict):
        return ["acceptance response is not an object"]
    if "listing_version" in body and not _whole(body["listing_version"]):
        return ["listing_version must be a whole number"]
    return []


def checkout_problems(body: Any) -> List[str]:
    if not isinstance(body, dict):
        return ["checkout response is not an object"]
    problems = []
    if not _text(body.get("purchase_id")):
        problems.append("checkout response without purchase_id")
    if not _text(body.get("item_id")):
        problems.append("checkout response without item_id")
    if "amount_cents" in body and (not _whole(body["amount_cents"]) or body["amount_cents"] < 0):
        problems.append("amount_cents must be a whole number of cents")
    if body.get("currency") not in (None, "CAD"):
        problems.append("currency must be CAD")
    return problems


def error_problems(body: Any) -> List[str]:
    """True contract errors carry at least a code and a message (plan section 6)."""
    if not isinstance(body, dict):
        return ["error body is not an object"]
    if not _text(body.get("code")) or not isinstance(body.get("message"), str):
        return ["error body without code and message"]
    return []


def unwrap_error(body: Any) -> Any:
    """FastAPI's HTTPException wraps custom bodies in {"detail": {...}}; accept both forms."""
    if isinstance(body, dict) and "code" not in body and isinstance(body.get("detail"), dict) \
            and "code" in body["detail"]:
        return body["detail"]
    return body
