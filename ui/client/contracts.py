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
