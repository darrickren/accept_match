"""MOCK backend: a simulated AcceptMatch agent for UI development.

This is not the real agent. Responses and trace events come from simple,
deterministic rules over synthetic data (mock_data.py), so the UI can be built
and rehearsed without Bedrock.

One MockClient instance is one buyer session. Every action is idempotent:
repeating a call (Streamlit rerun, double click) returns the current state
instead of creating a second ticket, offer or reservation.
"""

from __future__ import annotations

import copy
import re
import uuid
from typing import Any, Dict, List, Optional

from . import mock_data as data
from .contracts import AgentTurn, Rule, rule_label, validate_rules

ACCEPT_WORDS = {"fine", "ok", "okay", "acceptable", "accept", "accepted"}
ACCEPT_PHRASES = ("don't mind", "do not mind", "no problem")
REQUIRE_WORDS = {"must", "need", "needs", "required", "require", "intact", "work", "works", "working"}
AREA_WORDS = {
    "front": "Front", "back": "Back", "zipper": "Zipper", "zip": "Zipper", "strap": "Strap",
    "corner": "Corners", "corners": "Corners", "interior": "Interior", "inside": "Interior", "lining": "Interior",
}
DEFECT_WORDS = {
    "scratch": "Minor scratch", "scratches": "Minor scratch", "scuff": "Minor scuff", "scuffs": "Minor scuff",
    "stain": "Minor stain", "stains": "Minor stain", "mark": "Minor mark", "marks": "Minor mark",
}

STATUS_ORDER = {"match": 0, "needs_check": 1, "rejected": 2, "unavailable": 3}
DECISION_ORDER = {"request": 0, "queue": 1, "skip": 2}


def money(value: float) -> str:
    return f"${value:,.2f}"


def check_field(area: str, condition: str) -> str:
    """Map a Required rule to the check it needs, e.g. Zipper + Works -> zipper_function."""
    return f"{area.lower()}_{'function' if condition == 'Works' else 'intact'}"


def field_label(field: str) -> str:
    return field.replace("_", " ").capitalize()


def parse_preferences(text: str) -> List[Rule]:
    """Simulated keyword parser. The real agent will interpret free text with the LLM."""
    rules: List[Rule] = []
    seen = set()
    for sentence in re.split(r"[.;!?\n]+|\bbut\b|\bhowever\b", text.lower()):
        words = re.findall(r"[a-z']+", sentence)
        areas: List[str] = []
        for word in words:
            area = AREA_WORDS.get(word)
            if area and area not in areas:
                areas.append(area)
        if not areas:
            continue
        accepts = bool(ACCEPT_WORDS.intersection(words)) or any(p in sentence for p in ACCEPT_PHRASES)
        requires = bool(REQUIRE_WORDS.intersection(words))
        defect = next((DEFECT_WORDS[w] for w in words if w in DEFECT_WORDS), "Minor scratch")
        for area in areas:
            if requires:
                key = (area, "Works" if area == "Zipper" else "Intact", "required")
            elif accepts:
                key = (area, defect, "acceptable")
            else:
                continue
            if key not in seen:
                seen.add(key)
                rules.append({"rule_id": None, "area": key[0], "condition": key[1], "requirement": key[2]})
    for number, rule in enumerate(rules, start=1):
        rule["rule_id"] = f"BR-{number:02d}"
        rule["label"] = rule_label(rule)
    return rules


def _rule_key(rule: Rule):
    return rule["area"], rule["condition"], rule["requirement"]


def _row(rule: Rule, field: str, evidence: str, status: str, source_id: Optional[str], action: str) -> Dict[str, Any]:
    return {
        "rule_id": rule["rule_id"],
        "rule_label": rule["label"],
        "requirement": rule["requirement"],
        "field": field,
        "evidence": evidence,
        "status": status,
        "source_id": source_id,
        "verified_by": "human" if source_id else None,
        "agent_action": action,
    }


class MockClient:
    """Simulated agent session. See the module docstring."""

    simulated = True
    label = "Mock backend (simulated agent)"

    def __init__(self) -> None:
        self.session_id = f"S-{uuid.uuid4().hex[:6].upper()}"
        self._units = {u["item_id"]: copy.deepcopy(u) for u in data.UNITS}
        self._stage = "start"
        self._interpreted: List[Rule] = []
        self._rules: List[Rule] = []
        self._candidates: List[Dict[str, Any]] = []
        self._tickets: List[Dict[str, Any]] = []
        self._offer: Optional[Dict[str, Any]] = None
        self._reservation: Optional[Dict[str, Any]] = None
        self._handover: Optional[Dict[str, Any]] = None
        self._message = ""
        self._turn = 0
        self._turn_label = ""
        self._seq = 0
        self._events: List[Dict[str, Any]] = []
        self._fresh = False
        self._counters = {"IT": 0, "Q": 0, "R": 0}

    # ------------------------------------------------------------------ API

    def interpret_preferences(self, buyer_text: str) -> AgentTurn:
        action = "interpret_preferences"
        if self._stage not in ("start", "rules_ready"):
            return self._result(action, ("locked", "Preferences are locked after the search. Start a new search to change them."))
        text = (buyer_text or "").strip()
        self._begin("Interpret preferences")
        self._log("human_input", f'Buyer wrote: "{text}"' if text else "Buyer submitted empty preferences.")
        self._log("plan", "Turn the buyer's words into structured rules: tolerated defects (Acceptable) "
                          "and conditions that must be verified (Required).")
        rules = parse_preferences(text)
        self._log("tool_call", f"parse_preferences (simulated keyword parser) → {len(rules)} rule(s)",
                  citations=[{"rule_id": r["rule_id"]} for r in rules],
                  tool={"name": "parse_preferences", "input": {"text": text}, "output": {"rules": rules}})
        if rules:
            self._log("decision", "; ".join(f"{r['rule_id']} {r['label']}: {r['requirement'].capitalize()}" for r in rules) + ".")
            self._message = (f"I found {len(rules)} rule(s) in what you wrote. Check them below, "
                             "fix anything I got wrong, then search.")
        else:
            self._message = "I couldn't find any rules in that text. Add them in the table below or rephrase."
        self._log("plan", "Next: the buyer confirms or edits the rules. Nothing is searched until they confirm.")
        self._interpreted = rules
        self._stage = "rules_ready"
        return self._result(action)

    def search_candidates(self, rules: List[Rule]) -> AgentTurn:
        action = "search_candidates"
        if self._stage != "rules_ready":
            return self._result(action)  # search already ran: no-op
        cleaned, problems = validate_rules(rules)
        if problems:
            return self._result(action, ("invalid_rules", " ".join(problems)))
        self._begin("Search and match")
        edited = [_rule_key(r) for r in cleaned] != [_rule_key(r) for r in self._interpreted]
        self._log("human_input", f"Buyer confirmed {len(cleaned)} rule(s){' after editing them' if edited else ''}.",
                  citations=[{"rule_id": r["rule_id"]} for r in cleaned])
        self._rules = cleaned
        self._log("plan", "Goal: find one exact returned unit that meets every rule, using recorded, human-verified "
                          "evidence. Unknown never counts as passed. Request a check only if the expected extra "
                          "recovery covers its cost.",
                  citations=[{"rule_id": "CTL-01"}, {"rule_id": "CTL-07"}])
        units = list(self._units.values())
        unavailable = [u["item_id"] for u in units if u["status"] != "available"]
        self._log("tool_call", f"search_units → {len(units)} returned units of {data.PRODUCT['name']}, "
                               f"{len(units) - len(unavailable)} available",
                  citations=[{"item_id": i, "field": "status"} for i in unavailable],
                  tool={"name": "search_units", "input": {"style_id": data.PRODUCT["style_id"]},
                        "output": {"found": len(units), "unavailable": unavailable}})
        self._evaluate_all()
        counts = self._counts()
        self._log("tool_call", f"match_units → {counts['match']} verified match(es), {counts['needs_check']} need "
                               f"checks, {counts['rejected']} rejected",
                  tool={"name": "match_units", "input": {"rules": [r["rule_id"] for r in cleaned]}, "output": counts})
        ruled_out = [c for c in self._candidates if c["status"] in ("rejected", "unavailable")]
        if ruled_out:
            self._log("observation", "Ruled out: " + "; ".join(f"{c['item_id']} {c['reason']}" for c in ruled_out) + ".",
                      citations=[c["citation"] for c in ruled_out if c.get("citation")])
        for c in self._candidates:
            if c["status"] == "needs_check":
                self._log("observation", f"{c['item_id']}: " + "; ".join(
                    f"{r['rule_id']} {r['evidence'].lower()}" for r in c["evidence"]) + ".",
                          citations=[{"item_id": c["item_id"], "field": r["field"], "rule_id": r["rule_id"],
                                      "record_id": r["source_id"]} for r in c["evidence"]])
            decision = c.get("check_decision") or {}
            if decision.get("decision") == "skip":
                self._log("decision", f"Skip {c['item_id']}: {len(decision['checks'])} check(s) cost "
                                      f"{money(decision['cost'])}, more than its expected extra recovery of "
                                      f"{money(decision['expected_extra_recovery'])}.",
                          citations=[{"item_id": c["item_id"], "rule_id": "CTL-07"}])
        self._plan_next()
        return self._result(action)

    def submit_inspection_result(self, ticket_id: str, result: str, notes: str = "", inspector_id: str = "") -> AgentTurn:
        action = "submit_inspection_result"
        ticket = next((t for t in self._tickets if t["ticket_id"] == ticket_id), None)
        if ticket is None:
            return self._result(action, ("not_found", f"Ticket {ticket_id} does not exist."))
        if ticket["status"] != "open":
            return self._result(action)  # already recorded: no-op
        result = (result or "").strip().lower()
        if result not in ("pass", "fail"):
            return self._result(action, ("invalid_result", "Choose Pass or Fail."))
        self._begin("Inspection result")
        status = "passed" if result == "pass" else "failed"
        inspector = (inspector_id or "").strip() or "INS-01"
        item_id, field = ticket["item_id"], ticket["check"]
        ticket.update(status="resolved", result=result, notes=(notes or "").strip(), inspector_id=inspector)
        self._units[item_id]["checks"][field] = {"status": status, "source_id": ticket_id, "verified_by": "human"}
        cite = {"item_id": item_id, "field": field, "rule_id": ticket["rule_id"], "record_id": ticket_id}
        note_text = f' Notes: "{ticket["notes"]}"' if ticket["notes"] else ""
        self._log("human_input", f"Inspector {inspector} recorded {status.upper()} for {field} on {item_id} "
                                 f"({ticket_id}).{note_text}", citations=[cite, {"rule_id": "CTL-02"}])
        self._log("tool_call", f"get_inspection_result({ticket_id}) → {status}", citations=[cite],
                  tool={"name": "get_inspection_result", "input": {"ticket_id": ticket_id},
                        "output": {"item_id": item_id, "field": field, "status": status}})
        self._evaluate_all()
        candidate = self._candidate(item_id)
        self._log("observation", f"Re-assessed {item_id}: " + "; ".join(
            f"{r['rule_id']} {r['status']}" for r in candidate["evidence"]) + ".", citations=[cite])
        prefix = ""
        if status == "failed":
            self._log("decision", f"Reject {item_id}: {ticket['rule_id']} failed ({ticket_id}). The failed check "
                                  "changes the plan, so look for another candidate.", citations=[cite])
            prefix = (f"The {field_label(field).lower()} check on {item_id} failed ({ticket_id}), "
                      "so I rejected that unit. ")
        self._plan_next()
        self._message = prefix + self._message
        return self._result(action)

    def confirm_reservation(self, offer_id: str, item_id: str, buyer_acknowledged: bool) -> AgentTurn:
        action = "confirm_reservation"
        if self._reservation is not None:
            return self._result(action)  # already reserved: no-op
        offer = self._offer
        if self._stage != "offer_ready" or offer is None or offer["offer_id"] != offer_id or offer["item_id"] != item_id:
            return self._result(action, ("no_offer", "There is no open offer for that item."))
        if not buyer_acknowledged:
            self._begin("Reservation blocked")
            self._log("guardrail", "Reservation blocked: the buyer has not confirmed the disclosure yet.",
                      citations=[{"item_id": item_id, "rule_id": "CTL-05"}])
            return self._result(action, ("confirmation_required", "Confirm you've reviewed the disclosure before reserving."))
        self._begin("Reservation")
        self._log("human_input", f"Buyer confirmed {offer_id} for {item_id} after reviewing the disclosure.",
                  citations=[{"item_id": item_id, "record_id": offer_id, "rule_id": "CTL-05"}])
        self._counters["R"] += 1
        reservation = {
            "reservation_id": f"R-{self._counters['R']:03d}",
            "item_id": item_id,
            "offer_id": offer_id,
            "price": offer["price"],
            "currency": offer["currency"],
            "status": "reserved",
            "buyer_confirmed": True,
        }
        self._units[item_id]["status"] = "reserved"
        rid = reservation["reservation_id"]
        self._log("tool_call", f"reserve_unit({item_id}, {offer_id}, buyer_confirmed=true) → {rid}",
                  citations=[{"item_id": item_id, "record_id": rid}],
                  tool={"name": "reserve_unit", "input": {"item_id": item_id, "offer_id": offer_id, "buyer_confirmed": True},
                        "output": {"reservation_id": rid, "status": "reserved"}})
        self._log("plan", f"Next: verify the item ID at handover. Only {item_id} may be handed over for {rid}.")
        self._reservation = reservation
        self._stage = "reserved"
        self._message = (f"{item_id} is reserved for you at {money(offer['price'])}. At handover, staff scan the tag "
                         "to confirm it is the exact unit you reviewed.")
        return self._result(action)

    def verify_handover(self, reservation_id: str, scanned_item_id: str) -> AgentTurn:
        action = "verify_handover"
        reservation = self._reservation
        if reservation is None or reservation["reservation_id"] != reservation_id:
            return self._result(action, ("no_reservation", "There is no reservation with that ID."))
        if reservation["status"] == "verified":
            return self._result(action)  # already verified: no-op
        scanned = (scanned_item_id or "").strip().upper()
        if not scanned:
            return self._result(action, ("missing_id", "Scan or type the item ID first."))
        self._begin("Handover check")
        expected = reservation["item_id"]
        matched = scanned == expected
        attempts = (self._handover or {}).get("attempts", 0) + 1
        self._handover = {
            "reservation_id": reservation_id,
            "expected_item_id": expected,
            "scanned_item_id": scanned,
            "match": matched,
            "status": "verified" if matched else "mismatch",
            "attempts": attempts,
        }
        cite = {"item_id": scanned, "record_id": reservation_id}
        self._log("human_input", f"Staff scanned {scanned} for {reservation_id}.", citations=[cite])
        self._log("tool_call", f"verify_handover({reservation_id}, {scanned}) → {'match' if matched else 'mismatch'}",
                  citations=[cite],
                  tool={"name": "verify_handover", "input": {"reservation_id": reservation_id, "scanned_item_id": scanned},
                        "output": {"expected_item_id": expected, "match": matched}})
        if matched:
            reservation["status"] = "verified"
            self._stage = "verified"
            self._log("observation", f"Verified: {scanned} is the reserved unit. Handover approved.", citations=[cite])
            self._message = f"Verified at handover: this is {expected}, the exact unit you reviewed and reserved."
        else:
            self._log("guardrail", f"Mismatch: scanned {scanned}, but {reservation_id} is for {expected}. Handover blocked.",
                      citations=[cite, {"item_id": expected}])
            self._message = (f"The tag reads {scanned}, not {expected}. Don't hand this item over. "
                             f"Find {expected} and scan again.")
        return self._result(action)

    # ------------------------------------------------------------ internals

    def _begin(self, label: str) -> None:
        self._turn += 1
        self._turn_label = label
        self._events = []
        self._fresh = True

    def _log(self, kind: str, summary: str, citations: Optional[List[Dict[str, Any]]] = None,
             tool: Optional[Dict[str, Any]] = None) -> None:
        self._seq += 1
        clean = [{k: v for k, v in c.items() if v} for c in (citations or [])]
        self._events.append({
            "seq": self._seq, "turn": self._turn, "turn_label": self._turn_label, "type": kind,
            "summary": summary, "citations": [c for c in clean if c], "tool": tool, "simulated": True,
        })

    def _result(self, action: str, error=None) -> AgentTurn:
        trace = self._events if self._fresh else []
        self._fresh = False
        return copy.deepcopy({
            "session_id": self.session_id,
            "turn": self._turn,
            "action": action,
            "stage": self._stage,
            "message": self._message,
            "rules": self._rules or self._interpreted,
            "candidates": self._candidates,
            "tickets": self._tickets,
            "offer": self._offer,
            "reservation": self._reservation,
            "handover": self._handover,
            "trace": trace,
            "error": {"code": error[0], "message": error[1]} if error else None,
            "simulated": True,
        })

    def _candidate(self, item_id: str) -> Dict[str, Any]:
        return next(c for c in self._candidates if c["item_id"] == item_id)

    def _counts(self) -> Dict[str, int]:
        counts = {"match": 0, "needs_check": 0, "rejected": 0, "unavailable": 0}
        for c in self._candidates:
            counts[c["status"]] += 1
        return counts

    def _evaluate_all(self) -> None:
        open_items = {t["item_id"] for t in self._tickets if t["status"] == "open"}
        candidates = [self._evaluate_unit(u, open_items) for u in self._units.values()]
        candidates.sort(key=lambda c: (
            STATUS_ORDER[c["status"]],
            DECISION_ORDER.get((c.get("check_decision") or {}).get("decision"), 9),
            -c["net"],
            c["item_id"],
        ))
        self._candidates = candidates

    def _evaluate_unit(self, unit: Dict[str, Any], open_items: set) -> Dict[str, Any]:
        item_id = unit["item_id"]
        candidate: Dict[str, Any] = {
            "item_id": item_id, "product": data.PRODUCT["name"], "colour": data.PRODUCT["colour"],
            "evidence": [], "check_decision": None, "net": 0.0, "expected_extra_recovery": 0.0, "citation": None,
        }
        if unit["status"] != "available":
            candidate.update(status="unavailable", reason="is reserved by another buyer",
                             citation={"item_id": item_id, "field": "status"})
            return candidate

        rows: List[Dict[str, Any]] = []
        accepted = set()
        for rule in self._rules:
            if rule["requirement"] == "acceptable":
                kind = rule["condition"].split()[-1].lower()
                field = f"defects.{rule['area'].lower()}"
                hits = [d for d in unit["defects"] if d["area"] == rule["area"] and d["type"] == kind]
                for d in hits:
                    accepted.add(d["record_id"])
                    rows.append(_row(rule, field, f"Minor {d['type']} recorded", "recorded", d["record_id"], "keep_candidate"))
                if not hits:
                    rows.append(_row(rule, field, f"No {kind} recorded", "none", None, "keep_candidate"))
            else:
                field = check_field(rule["area"], rule["condition"])
                check = unit["checks"].get(field) or {"status": "unknown", "source_id": None}
                status = check["status"]
                evidence = {"passed": "Passed", "failed": "Failed", "unknown": "Unknown (not yet verified)"}[status]
                action = {"passed": "keep_candidate", "failed": "reject", "unknown": "request_human_check"}[status]
                rows.append(_row(rule, field, evidence, status, check.get("source_id"), action))
        for d in unit["defects"]:
            if d["record_id"] not in accepted:
                rows.append({
                    "rule_id": "SYS-01", "rule_label": f"{d['area']} minor {d['type']} (not in your rules)",
                    "requirement": "not_accepted", "field": f"defects.{d['area'].lower()}",
                    "evidence": f"Minor {d['type']} recorded", "status": "recorded",
                    "source_id": d["record_id"], "verified_by": "human", "agent_action": "reject",
                })

        extra = (unit["match_price"] - data.MARKDOWN_PRICE) + data.REPEAT_RETURN_SAVING
        candidate.update(evidence=rows, expected_extra_recovery=extra)
        rejects = [r for r in rows if r["agent_action"] == "reject"]
        if rejects:
            first = rejects[0]
            if first["rule_id"] == "SYS-01":
                reason = f"{first['rule_label'].replace(' (not in your rules)', '').lower()} not accepted (SYS-01, {first['source_id']})"
            else:
                reason = f"{first['rule_label'].lower()} failed ({first['rule_id']}, {first['source_id']})"
            candidate.update(status="rejected", reason=reason, citation={
                "item_id": item_id, "field": first["field"], "rule_id": first["rule_id"], "record_id": first["source_id"]})
            return candidate

        unknown = [r for r in rows if r["agent_action"] == "request_human_check"]
        if not unknown:
            candidate.update(status="match", reason="meets every rule with verified evidence", net=extra)
            return candidate

        cost = data.CHECK_COST * len(unknown)
        net = extra - cost
        decision = "request" if item_id in open_items else ("queue" if extra >= cost else "skip")
        checks = [r["field"] for r in unknown]
        candidate.update(
            status="needs_check",
            reason=", ".join(field_label(f).lower() for f in checks) + " unknown",
            net=net,
            check_decision={"decision": decision, "checks": checks, "rule_ids": [r["rule_id"] for r in unknown],
                            "cost": cost, "expected_extra_recovery": extra, "net": net},
        )
        return candidate

    def _plan_next(self) -> None:
        open_tickets = [t for t in self._tickets if t["status"] == "open"]
        if open_tickets:
            self._stage = "awaiting_inspection"
            self._message = f"Waiting for {len(open_tickets)} open inspection(s) before I can make an offer."
            return
        matches = [c for c in self._candidates if c["status"] == "match"]
        if matches:
            best = max(matches, key=lambda c: c["expected_extra_recovery"])
            self._log("decision", f"Offer {best['item_id']}: it meets every rule with verified evidence, "
                                  "so no extra check is needed.", citations=[{"item_id": best["item_id"]}])
            self._make_offer(best)
            return
        worth = [c for c in self._candidates if (c.get("check_decision") or {}).get("decision") == "queue"]
        if not worth:
            self._stage = "no_match"
            self._log("decision", "No available unit can meet every rule, and the remaining checks would cost more "
                                  "than they are expected to recover.", citations=[{"rule_id": "CTL-07"}])
            self._message = "No available unit meets all your rules right now. Start a new search to try different rules."
            return

        chosen = worth[0]  # candidates are sorted by net expected gain
        decision = chosen["check_decision"]
        decision["decision"] = "request"
        text = (f"Check {chosen['item_id']} first: expected extra recovery {money(decision['expected_extra_recovery'])} "
                f"vs check cost {money(decision['cost'])} (net {money(decision['net'])}).")
        if len(worth) > 1:
            text += f" Backup: {worth[1]['item_id']} (net {money(worth[1]['check_decision']['net'])})."
        self._log("decision", text, citations=[{"item_id": chosen["item_id"], "rule_id": "CTL-07"}])
        ticket_ids = []
        for row in chosen["evidence"]:
            if row["agent_action"] != "request_human_check":
                continue
            ticket = self._open_ticket(chosen["item_id"], row)
            ticket_ids.append(ticket["ticket_id"])
            self._log("tool_call", f"create_inspection_ticket({chosen['item_id']}, {row['field']}, {row['rule_id']}) "
                                   f"→ {ticket['ticket_id']}",
                      citations=[{"item_id": chosen["item_id"], "field": row["field"], "rule_id": row["rule_id"],
                                  "record_id": ticket["ticket_id"]}],
                      tool={"name": "create_inspection_ticket",
                            "input": {"item_id": chosen["item_id"], "check": row["field"], "rule_id": row["rule_id"]},
                            "output": {"ticket_id": ticket["ticket_id"], "status": "open"}})
        checks = ", ".join(field_label(f).lower() for f in decision["checks"])
        self._log("plan", f"Next: wait for a human to verify {checks} on {chosen['item_id']}.",
                  citations=[{"rule_id": "CTL-02"}])
        self._stage = "awaiting_inspection"
        self._evaluate_all()  # refresh decisions now that the ticket is open
        kept = sum(1 for r in chosen["evidence"] if r["agent_action"] == "keep_candidate")
        total = len(self._rules)
        self._message = (f"I checked {len(self._units)} returned units against your {total} rules and none is fully "
                         f"verified yet. {chosen['item_id']} is the best candidate: it meets {kept} of {total} rules, "
                         f"but its {checks} is unknown. Unknown never counts as passed, so I opened inspection "
                         f"ticket {', '.join(ticket_ids)} for a human check.")

    def _open_ticket(self, item_id: str, row: Dict[str, Any]) -> Dict[str, Any]:
        for ticket in self._tickets:
            if ticket["item_id"] == item_id and ticket["check"] == row["field"]:
                return ticket  # idempotent: never open a second ticket for the same check
        self._counters["IT"] += 1
        ticket = {
            "ticket_id": f"IT-{self._counters['IT']:03d}",
            "item_id": item_id,
            "check": row["field"],
            "check_label": field_label(row["field"]),
            "rule_id": row["rule_id"],
            "reason": f"{row['rule_id']} requires {row['rule_label'].lower()}; current evidence is unknown.",
            "cost": data.CHECK_COST,
            "currency": data.CURRENCY,
            "requested_by": "AcceptMatch agent (simulated)",
            "status": "open",
            "result": None,
            "notes": "",
            "inspector_id": "",
        }
        self._tickets.append(ticket)
        return ticket

    def _make_offer(self, candidate: Dict[str, Any]) -> None:
        item_id = candidate["item_id"]
        if self._offer and self._offer["item_id"] == item_id:
            self._stage = "offer_ready"
            return
        unit = self._units[item_id]
        policy = data.PRICING_POLICY
        proposed = unit["match_price"]
        price = min(max(proposed, policy["floor"]), policy["ceiling"])
        self._counters["Q"] += 1
        offer_id = f"Q-{self._counters['Q']:03d}"
        checks = [{"check": f, "label": field_label(f), "status": c["status"], "source_id": c["source_id"],
                   "verified_by": c["verified_by"]} for f, c in unit["checks"].items()]
        photos = [{"view": "Front", "caption": "Overall front view", "path": None}]
        photos += [{"view": d["area"], "caption": f"{d['description']} ({d['record_id']})", "path": None}
                   for d in unit["defects"]]
        photos += [{"view": c["label"], "caption": f"{c['label']}: {c['status']} ({c['source_id']})", "path": None}
                   for c in checks if (c["source_id"] or "").startswith("IT-")]
        self._offer = {
            "offer_id": offer_id,
            "item_id": item_id,
            "product": data.PRODUCT["name"],
            "colour": data.PRODUCT["colour"],
            "price": price,
            "currency": data.CURRENCY,
            "pricing_check": {"rule_id": policy["rule_id"], "within_limits": price == proposed, "proposed": proposed,
                              "floor": policy["floor"], "ceiling": policy["ceiling"]},
            "photos": photos,
            "disclosures": copy.deepcopy(unit["defects"]),
            "checks": checks,
            "evidence": copy.deepcopy(candidate["evidence"]),
            "after_sales": dict(data.AFTER_SALES),
        }
        self._log("tool_call", f"create_quote({item_id}, proposed {money(proposed)}) → {offer_id}",
                  citations=[{"item_id": item_id, "record_id": offer_id}],
                  tool={"name": "create_quote", "input": {"item_id": item_id, "proposed_price": proposed},
                        "output": {"offer_id": offer_id, "price": price}})
        if price == proposed:
            self._log("guardrail", f"{policy['rule_id']}: {money(price)} is within merchant limits "
                                   f"({money(policy['floor'])} to {money(policy['ceiling'])}). Quote approved.",
                      citations=[{"item_id": item_id, "rule_id": policy["rule_id"]}, {"rule_id": "CTL-04"}])
        else:
            self._log("guardrail", f"{policy['rule_id']}: proposed {money(proposed)} adjusted to {money(price)} "
                                   "to stay within merchant limits.",
                      citations=[{"item_id": item_id, "rule_id": policy["rule_id"]}, {"rule_id": "CTL-04"}])
        self._log("guardrail", f"Disclosure ready for {item_id}: {len(unit['defects'])} defect record(s), "
                               f"{len(checks)} check result(s), {len(photos)} photo(s). After-sales rights preserved.",
                  citations=[{"item_id": item_id, "rule_id": "CTL-03"}, {"rule_id": "CTL-06"},
                             {"rule_id": data.AFTER_SALES["rule_id"]}])
        self._log("plan", f"Next: wait for the buyer to confirm before reserving {item_id}.",
                  citations=[{"rule_id": "CTL-05"}])
        self._stage = "offer_ready"
        self._message = (f"{item_id} now meets all {len(self._rules)} of your rules with verified evidence. Here are "
                         f"its photos, disclosed defects and check results, with a quote of {money(price)} that is "
                         "within our pricing policy.")
