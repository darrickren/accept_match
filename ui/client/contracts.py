"""DRAFT contract between the Streamlit UI and the AcceptMatch agent backend.

Status: frontend proposal only, NOT agreed with Hengru yet. Names, fields and
enums may change. The UI depends only on these shapes, so the mock can be
replaced by any object that satisfies AgentClient.

Every call returns an AgentTurn: a full snapshot of the session plus the trace
events produced by that call. Wire-level requests would also carry session_id;
the client object holds it.

Enums used in payloads:
  stage            start | rules_ready | awaiting_inspection | offer_ready | reserved | verified | no_match
  candidate status match | needs_check | rejected | unavailable
  evidence status  recorded | passed | failed | unknown | none
  agent_action     keep_candidate | request_human_check | reject
  check decision   request | queue | skip
  trace type       plan | tool_call | observation | decision | human_input | guardrail
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol, Tuple, TypedDict

# Rule vocabulary for the editor (draft; Zikun's data schema decides the real values).
AREAS = ["Front", "Back", "Zipper", "Strap", "Corners", "Interior"]
DEFECT_CONDITIONS = ["Minor scratch", "Minor scuff", "Minor stain", "Minor mark"]
CHECK_CONDITIONS = ["Intact", "Works"]
CONDITIONS = DEFECT_CONDITIONS + CHECK_CONDITIONS
REQUIREMENTS = ["acceptable", "required"]


class Rule(TypedDict, total=False):
    rule_id: Optional[str]  # "BR-01"; None for rows the buyer added (IDs assigned on search)
    area: str  # "Back"
    condition: str  # "Minor scratch" | "Intact" | "Works"
    requirement: str  # acceptable | required
    label: str  # "Back minor scratch"


class Citation(TypedDict, total=False):
    item_id: str
    field: str
    rule_id: str
    record_id: str


class TraceEvent(TypedDict, total=False):
    seq: int  # unique and increasing per session; the UI de-duplicates on it
    turn: int
    turn_label: str
    type: str
    summary: str
    citations: List[Citation]
    tool: Optional[Dict[str, Any]]  # {"name", "input", "output"}
    simulated: bool


class AgentTurn(TypedDict, total=False):
    session_id: str
    turn: int
    action: str
    stage: str
    message: str  # agent's message to the buyer
    rules: List[Rule]
    candidates: List[Dict[str, Any]]  # item_id, product, colour, status, reason, evidence[], check_decision
    tickets: List[Dict[str, Any]]  # ticket_id, item_id, check, rule_id, cost, status, result, notes, inspector_id
    offer: Optional[Dict[str, Any]]  # offer_id, item_id, price, pricing_check, photos, disclosures, checks, after_sales
    reservation: Optional[Dict[str, Any]]  # reservation_id, item_id, offer_id, price, status, buyer_confirmed
    handover: Optional[Dict[str, Any]]  # reservation_id, expected_item_id, scanned_item_id, match, status
    trace: List[TraceEvent]  # events produced by this call only
    error: Optional[Dict[str, str]]  # {"code", "message"}
    simulated: bool


class AgentClient(Protocol):
    session_id: str
    simulated: bool
    label: str

    def interpret_preferences(self, buyer_text: str) -> AgentTurn: ...

    def search_candidates(self, rules: List[Rule]) -> AgentTurn: ...

    def submit_inspection_result(
        self, ticket_id: str, result: str, notes: str = "", inspector_id: str = ""
    ) -> AgentTurn: ...

    def confirm_reservation(self, offer_id: str, item_id: str, buyer_acknowledged: bool) -> AgentTurn: ...

    def verify_handover(self, reservation_id: str, scanned_item_id: str) -> AgentTurn: ...


def rule_label(rule: Rule) -> str:
    return f"{rule['area']} {rule['condition'].lower()}"


def validate_rules(rules: List[Rule]) -> Tuple[List[Rule], List[str]]:
    """Check rule rows and assign IDs to new ones. Returns (clean rules, problems)."""
    cleaned: List[Rule] = []
    problems: List[str] = []
    used = {r.get("rule_id") for r in rules if r.get("rule_id")}
    next_number = 1
    for position, raw in enumerate(rules, start=1):
        area, condition = raw.get("area"), raw.get("condition")
        requirement = (raw.get("requirement") or "").lower()
        if area not in AREAS or condition not in CONDITIONS or requirement not in REQUIREMENTS:
            problems.append(f"Row {position} is incomplete: choose an area, a condition and a requirement.")
            continue
        if requirement == "acceptable" and condition not in DEFECT_CONDITIONS:
            problems.append(f"Row {position}: 'Acceptable' needs a defect, such as 'Minor scratch'.")
            continue
        if requirement == "required" and condition not in CHECK_CONDITIONS:
            problems.append(f"Row {position}: 'Required' needs 'Intact' or 'Works'.")
            continue
        rule_id = raw.get("rule_id")
        if not rule_id:
            while f"BR-{next_number:02d}" in used:
                next_number += 1
            rule_id = f"BR-{next_number:02d}"
            used.add(rule_id)
        rule: Rule = {"rule_id": rule_id, "area": area, "condition": condition, "requirement": requirement}
        rule["label"] = rule_label(rule)
        cleaned.append(rule)
    if not cleaned and not problems:
        problems.append("Add at least one rule.")
    return cleaned, problems


# ===================================================================== V3 customer contract (DRAFT)
# Proposal for the customer site, to be agreed with the Agent team. The V1
# types above stay for app_v1.py. Payloads are JSON-shaped so the same
# contract works for the mock, a direct Python call or AgentCore.

import copy  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import uuid  # noqa: E402

API_VERSION = "v0-draft"
OPERATIONS = (
    "get_agent_info", "start_conversation", "submit_answer", "interpret_answer", "update_preferences",
    "search_products", "get_listing", "get_return_requirements", "create_photo_upload", "complete_photo_upload",
    "submit_return", "assess_return", "get_return_case", "add_return_info",
)
ERROR_CODES = ("invalid_input", "session_expired", "not_found", "state_conflict", "photo_rejected", "rate_limited",
               "agent_timeout", "agent_unavailable", "contract_violation", "internal")
SOURCES = ("mock", "agent")

SESSION_PREFIX = {"shop": "am-shop-", "return": "am-retn-", "status": "am-stat-"}  # 8 chars + 32 hex = 40
SESSION_ID_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9-]{32,99}")

CATEGORY_LABELS = {"crossbody": "Crossbody", "tote": "Tote", "handbag": "Handbag"}
AREA_LABELS = {"front": "Front", "back": "Back"}
DEFECT_NAMES = {"scratch": "scratch", "scuff": "scuff", "stain": "stain", "discoloration": "discoloration",
                "corner_wear": "corner wear"}
DEFECT_OPTIONS = {"scratch": "Minor scratches", "scuff": "Scuffs", "stain": "Small stains",
                  "discoloration": "Discoloration", "corner_wear": "Corner wear"}
SEVERITY_RANK = {"minor": 1, "moderate": 2, "severe": 3}
POLICY_MAX_SEVERITY = "moderate"  # draft publication policy: severe damage is not listed
COMPONENT_LABELS = {"zipper": "Zipper", "strap": "Strap", "handles": "Handles", "interior": "Interior"}
MATCH_LEVELS = ("verified", "possible")
AREA_STATUSES = ("verified_ok", "defect", "unknown")
COMPONENT_STATUSES = ("verified_ok", "failed", "unknown")
IMAGE_HOSTS = ()  # https hosts allowed for `url` images; empty until the Agent team provides a CDN


def new_session_id(kind: str) -> str:
    """Runtime session id: letters, digits and hyphens only, 40 characters (AgentCore needs >= 33)."""
    return SESSION_PREFIX[kind] + uuid.uuid4().hex


def new_request_id() -> str:
    return "cr_" + uuid.uuid4().hex[:20]


def empty_preferences() -> Dict[str, Any]:
    return {"category": [], "tolerated_defects": [], "required_working": [], "colors": [], "styles": [],
            "price": {"min": None, "max": None, "currency": "CAD"}, "notes": None}


def empty_context() -> Dict[str, Any]:
    return {"context_version": 0, "answers": {}, "order": [], "preferences": empty_preferences()}


def context_size(context: Dict[str, Any]) -> int:
    return len(json.dumps(context, sort_keys=True, separators=(",", ":")))


def compact_context(context: Dict[str, Any], max_answers: int = 20, max_bytes: int = 16_000) -> Dict[str, Any]:
    """Deterministic budget for the client-held conversation context.

    Keeps at most `max_answers` answers (latest first-asked order wins), trims
    free text, and drops free text entirely if the context is still too big.
    Photos, traces and product results never belong in the context.
    """
    ctx = copy.deepcopy(context)
    ctx = {k: ctx[k] for k in ("context_version", "answers", "order", "preferences") if k in ctx}
    order = [q for q in ctx.get("order", []) if q in ctx.get("answers", {})][-max_answers:]
    ctx["order"] = order
    ctx["answers"] = {q: {"values": list(ctx["answers"][q].get("values") or [])[:12],
                          "text": (ctx["answers"][q].get("text") or None)} for q in order}
    for answer in ctx["answers"].values():
        if answer["text"]:
            answer["text"] = answer["text"][:280]
    prefs = ctx.get("preferences") or empty_preferences()
    if prefs.get("notes"):
        prefs["notes"] = str(prefs["notes"])[:280]
    if context_size(ctx) > max_bytes:
        for answer in ctx["answers"].values():
            answer["text"] = None
        prefs["notes"] = None
    return ctx


def error_body(code: str, user_message: str, retryable: bool = False, retry_after: Optional[float] = None,
               details: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {"code": code, "retryable": retryable, "user_message": user_message, "retry_after": retry_after,
            "details": details or {}}


def envelope(operation: str, client_request_id: Optional[str], source: str, data: Any = None,
             error: Optional[Dict[str, Any]] = None, trace: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    return {"ok": error is None, "data": data if error is None else None, "error": error,
            "meta": {"source": source, "api_version": API_VERSION, "operation": operation,
                     "client_request_id": client_request_id, "trace": trace or []}}


def validate_envelope(response: Any) -> List[str]:
    """Structural checks on every response. Content checks live with each screen's guardrails."""
    if not isinstance(response, dict):
        return ["response is not a JSON object"]
    problems = [f"missing '{k}'" for k in ("ok", "data", "error", "meta") if k not in response]
    if problems:
        return problems
    meta = response["meta"] if isinstance(response["meta"], dict) else {}
    if meta.get("source") not in SOURCES:
        problems.append("meta.source must be 'mock' or 'agent'")
    if not isinstance(meta.get("api_version"), str):
        problems.append("meta.api_version missing")
    if response["ok"] is True and response["error"] is not None:
        problems.append("ok=true with an error")
    if response["ok"] is False:
        error = response["error"]
        if not isinstance(error, dict) or error.get("code") not in ERROR_CODES:
            problems.append("ok=false without a known error code")
    if response["ok"] not in (True, False):
        problems.append("ok must be a boolean")
    return problems


def validate_listing(listing: Any) -> List[str]:
    """Shape checks for one listing result. A listing without a condition report is invalid."""
    if not isinstance(listing, dict):
        return ["listing is not an object"]
    problems = [f"listing missing '{k}'" for k in ("listing_id", "unit_id", "product", "price", "availability",
                                                   "condition", "match") if k not in listing]
    if problems:
        return problems
    condition = listing.get("condition")
    if not isinstance(condition, dict) or not isinstance(condition.get("areas"), list) \
            or not isinstance(condition.get("components"), list):
        problems.append(f"{listing['listing_id']}: condition report missing")
    match = listing.get("match") or {}
    if match.get("level") not in MATCH_LEVELS:
        problems.append(f"{listing['listing_id']}: unknown match level")
    price = listing.get("price") or {}
    if price.get("currency") != "CAD" or not isinstance(price.get("amount"), (int, float)):
        problems.append(f"{listing['listing_id']}: price must be a CAD amount")
    for image in listing.get("images") or []:
        has_key, has_url = bool(image.get("image_key")), bool(image.get("url"))
        if has_key == has_url:
            problems.append(f"{listing['listing_id']}: each image needs exactly one of image_key or url")
        if has_url:
            url = str(image["url"])
            host = url.split("/")[2] if url.startswith("https://") and url.count("/") >= 2 else ""
            if host not in IMAGE_HOSTS:
                problems.append(f"{listing['listing_id']}: image host not allowed")
    return problems
