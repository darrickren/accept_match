"""Deterministic MOCK Agent for the customer site (V3).

Scripted, not AI: replies come from fixed rules over synthetic data, and the
UI labels them "Demo assistant · scripted". Same request in, same response
out. Writes are idempotent on client_request_id. Trace events are short,
whitelisted operation summaries marked scripted.
"""

from __future__ import annotations

import copy
from typing import Any, Callable, Dict, List, Optional

from . import mock_conversation as convo
from . import mock_store as store
from .contracts import (OPERATIONS, compact_context, context_size, envelope, error_body)
from .matching import defect_score, evaluate
from .mock_returns import ReturnOps

AGENT_VERSION = "mock-0.3"


class MockAgentClient(ReturnOps):
    source = "mock"
    label = "Demo assistant · scripted"

    def __init__(self, journey: str, overlay: Dict[str, Any], settings: Any = None) -> None:
        self.journey = journey
        self._overlay = overlay
        self._settings = settings
        self._max_context = getattr(settings, "context_max_bytes", 16_000)

    # ------------------------------------------------------------ plumbing

    def _respond(self, operation: str, client_request_id: Optional[str], body: Callable[[List[Dict[str, Any]]], Any],
                 idempotent: bool = True) -> Dict[str, Any]:
        cache = self._overlay.setdefault("idempotency", {})
        key = f"{operation}:{client_request_id}"
        if idempotent and client_request_id and key in cache:
            return copy.deepcopy(cache[key])
        trace: List[Dict[str, Any]] = []
        try:
            data = body(trace)
            response = envelope(operation, client_request_id, self.source, data=data, trace=trace)
        except _Refusal as refusal:
            response = envelope(operation, client_request_id, self.source, error=refusal.error, trace=trace)
        if idempotent and client_request_id:
            cache[key] = copy.deepcopy(response)
        return response

    def _event(self, trace: List[Dict[str, Any]], kind: str, summary: str, **citation: str) -> None:
        counters = self._overlay.setdefault("counters", {})
        counters["trace"] = counters.get("trace", 0) + 1
        trace.append({"seq": counters["trace"], "type": kind, "summary": summary,
                      "citations": [citation] if citation else [], "scripted": True})

    def _context(self, context: Any) -> Dict[str, Any]:
        if not isinstance(context, dict) or "context_version" not in context:
            raise _Refusal("invalid_input", "We lost track of your answers. Please start again.")
        if context_size(context) > self._max_context:
            raise _Refusal("invalid_input", "That's a lot of detail. Please shorten your answers.")
        return compact_context(context)

    # ------------------------------------------------------------ info

    def get_agent_info(self, client_request_id: Optional[str] = None) -> Dict[str, Any]:
        return self._respond("get_agent_info", client_request_id, lambda trace: {
            "agent_version": AGENT_VERSION, "supported_operations": list(OPERATIONS), "journeys": ["shop", "return"],
            "scripted": True}, idempotent=False)

    # ------------------------------------------------------------ conversation

    def start_conversation(self, session_id: str, client_request_id: str, locale: str = "en-CA") -> Dict[str, Any]:
        def body(trace):
            views = convo.published_views(self._overlay)
            context = convo.apply_answer({"context_version": 0, "answers": {}, "order": []}, "q_category", None, None)
            self._event(trace, "plan", f"Ask one question at a time over {len(views)} published pieces; "
                                       "skip questions that would not change the results")
            return {"turn": convo.turn(session_id, context, views)}
        return self._respond("start_conversation", client_request_id, body)

    def submit_answer(self, session_id: str, question_id: str, answer: Dict[str, Any], context: Dict[str, Any],
                      client_request_id: str) -> Dict[str, Any]:
        def body(trace):
            ctx = self._context(context)
            views = convo.published_views(self._overlay)
            expected, options = convo.next_question(ctx.get("answers") or {}, views)
            if question_id != expected:
                raise _Refusal("invalid_input", "That question has changed. Please answer the one on screen.")
            values = convo.normalise(question_id, list((answer or {}).get("values") or []))
            if values is None or not set(values) <= {o["value"] for o in options}:
                raise _Refusal("invalid_input", "Please choose one of the options.")
            new_ctx = convo.apply_answer(ctx, question_id, values, (answer or {}).get("text"))
            result = convo.turn(session_id, new_ctx, views)
            self._event(trace, "observation", f"{question_id} = {', '.join(values)} → "
                                              f"{result['match_count']} pieces still fit", field=question_id)
            nxt = (result.get("question") or {}).get("question_id")
            self._event(trace, "decision", f"Next: {nxt}" if nxt else "All needed answers collected")
            return {"turn": result}
        return self._respond("submit_answer", client_request_id, body)

    def interpret_answer(self, session_id: str, question_id: str, text: str, context: Dict[str, Any],
                         client_request_id: str) -> Dict[str, Any]:
        def body(trace):
            ctx = self._context(context)
            views = convo.published_views(self._overlay)
            expected, options = convo.next_question(ctx.get("answers") or {}, views)
            if question_id != expected:
                raise _Refusal("invalid_input", "That question has changed. Please answer the one on screen.")
            result = convo.interpret(question_id, (text or "")[:280], options)
            self._event(trace, "tool_call", "keyword_rules(text) → " + (result["summary"] or "no match"),
                        field=question_id)
            return {"interpretation": result, "context_version": ctx["context_version"]}
        return self._respond("interpret_answer", client_request_id, body)

    def update_preferences(self, session_id: str, patches: List[Dict[str, Any]], context: Dict[str, Any],
                           client_request_id: str) -> Dict[str, Any]:
        """Each patch sets or clears (values=None) one answer; dependants of changed answers are cleared."""
        def body(trace):
            ctx = self._context(context)
            views = convo.published_views(self._overlay)
            if not patches:
                raise _Refusal("invalid_input", "Nothing to change.")
            for patch in patches:
                qid = patch.get("question_id")
                if qid not in convo.QUESTION_ORDER:
                    raise _Refusal("invalid_input", "That preference can't be changed here.")
                values = patch.get("values")
                if values is not None:
                    values = convo.normalise(qid, list(values))
                    if values is None:
                        raise _Refusal("invalid_input", "Please choose one of the options.")
                ctx = convo.apply_answer(ctx, qid, values, None)
                ctx["context_version"] -= 1  # one version step per request, not per patch
            ctx["context_version"] += 1
            result = convo.turn(session_id, ctx, views)
            self._event(trace, "observation", f"Preferences updated → {result['match_count']} pieces fit")
            return {"turn": result}
        return self._respond("update_preferences", client_request_id, body)

    # ------------------------------------------------------------ catalogue

    def search_products(self, session_id: str, preferences: Dict[str, Any], expected_context_version: int,
                        client_request_id: str, sort: str = "best") -> Dict[str, Any]:
        def body(trace):
            views = convo.published_views(self._overlay)
            results = []
            for view in views:
                match = evaluate(view, preferences)
                if match["level"] == "excluded":
                    continue
                view["match"] = {k: match[k] for k in ("level", "reasons", "missing_evidence")}
                results.append(view)
            results.sort(key=lambda v: _rank(v, sort))
            self._event(trace, "tool_call", f"search_listings(published) → {len(views)} pieces, {len(results)} fit")
            for view in results:
                if view["match"]["level"] == "possible":
                    self._event(trace, "guardrail", f"{view['unit_id']}: {view['match']['missing_evidence'][0]} → "
                                                    "possible match, not verified", item_id=view["unit_id"])
            return {"results": results, "total": len(results),
                    "counts": {lvl: sum(1 for r in results if r["match"]["level"] == lvl)
                               for lvl in ("verified", "possible")},
                    "applied_preferences": preferences,
                    "matched_against_context_version": expected_context_version,
                    "suggestions": _suggestions(views, preferences, len(results))}
        return self._respond("search_products", client_request_id, body)

    def get_listing(self, session_id: str, listing_id: str, preferences: Dict[str, Any], expected_context_version: int,
                    client_request_id: str) -> Dict[str, Any]:
        def body(trace):
            row = store.listing(listing_id, self._overlay)
            if not row or row["publication_status"] != "published":
                raise _Refusal("not_found", "This piece is no longer listed.")
            view = store.listing_view(row, self._overlay)
            match = evaluate(view, preferences)
            view["match"] = {k: match[k] for k in ("level", "reasons", "missing_evidence", "exclusions")}
            item = store.product(view["product"]["product_id"]) or {}
            view["details"] = {"description": item.get("description"), "material": item.get("material"),
                               "dimensions": item.get("dimensions")}
            view["evidence"] = _evidence_rows(view)
            self._event(trace, "tool_call", f"get_listing({listing_id}) → {match['level']}", item_id=view["unit_id"])
            return {"listing": view, "matched_against_context_version": expected_context_version}
        return self._respond("get_listing", client_request_id, body)


class _Refusal(Exception):
    def __init__(self, code: str, user_message: str, retryable: bool = False) -> None:
        super().__init__(user_message)
        self.error = error_body(code, user_message, retryable)


def _rank(view: Dict[str, Any], sort: str):
    level = 0 if view["match"]["level"] == "verified" else 1
    available = 0 if view["availability"]["purchasable"] else 1
    price = view["price"]["amount"]
    if sort == "price_asc":
        return (level, available, price)
    if sort == "price_desc":
        return (level, available, -price)
    return (level, available, defect_score(view), price)


def _suggestions(views: List[Dict[str, Any]], prefs: Dict[str, Any], current: int) -> List[Dict[str, Any]]:
    """Up to three one-tap relaxations, each with how many more pieces it would add."""
    def count(p):
        return sum(1 for v in views if evaluate(v, p)["level"] != "excluded")

    options = []
    tolerated = prefs.get("tolerated_defects") or []
    areas = [r["area"] for r in tolerated]
    types = sorted({t for r in tolerated for t in r.get("types") or []})
    severity = (tolerated[0]["max_severity"] if tolerated else None)
    present = sorted({d["type"] for v in views for a in v["condition"]["areas"] for d in a["defects"]})
    if not tolerated:  # "Neither": offer minor marks on one side
        for side in ("back", "front"):
            on_side = sorted({d["type"] for v in views for a in v["condition"]["areas"] if a["area"] == side
                              for d in a["defects"]})
            if not on_side:
                continue
            patched = copy.deepcopy(prefs)
            patched["tolerated_defects"] = [{"area": side, "types": on_side, "max_severity": "minor"}]
            options.append((f"Accept minor marks on the {side}", patched,
                            [{"question_id": "q_location", "values": [side]},
                             {"question_id": "q_types", "values": on_side},
                             {"question_id": "q_severity", "values": ["minor"]}]))
    if tolerated:
        for extra in [t for t in present if t not in types]:
            patched = copy.deepcopy(prefs)
            for rule in patched["tolerated_defects"]:
                rule["types"] = sorted(set(rule["types"]) | {extra})
            options.append((f"Also accept {convo.DEFECT_OPTIONS[extra].lower()}", patched,
                            [{"question_id": "q_types", "values": sorted(set(types) | {extra})}]))
        if severity == "minor":
            patched = copy.deepcopy(prefs)
            for rule in patched["tolerated_defects"]:
                rule["max_severity"] = "moderate"
            options.append(("Allow moderate imperfections", patched,
                            [{"question_id": "q_severity", "values": ["moderate"]}]))
        if set(areas) != {"front", "back"}:
            patched = copy.deepcopy(prefs)
            patched["tolerated_defects"] = [{"area": a, "types": types, "max_severity": severity}
                                            for a in ("front", "back")]
            options.append(("Also accept marks on the " + ("front" if "back" in areas else "back"), patched,
                            [{"question_id": "q_location", "values": ["both"]},
                             {"question_id": "q_types", "values": types},
                             {"question_id": "q_severity", "values": [severity]}]))
    if prefs.get("colors"):
        patched = dict(prefs, colors=[])
        options.append(("Any colour", patched, [{"question_id": "q_colour", "values": ["any"]}]))
    if prefs.get("styles"):
        patched = dict(prefs, styles=[])
        options.append(("Any style", patched, [{"question_id": "q_style", "values": ["any"]}]))
    if (prefs.get("price") or {}).get("max") is not None or (prefs.get("price") or {}).get("min") is not None:
        patched = dict(prefs, price={"min": None, "max": None, "currency": "CAD"})
        options.append(("Any price", patched, [{"question_id": "q_price", "values": ["any"]}]))
    scored = []
    for label, patched, patches in options:
        gain = count(patched) - current
        if gain > 0:
            scored.append({"label": label, "adds": gain, "patches": patches})
    scored.sort(key=lambda s: -s["adds"])
    return scored[:3]


def _evidence_rows(view: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Plain-language condition report rows with their source."""
    cond = view["condition"]
    source = "Intake inspection" if cond.get("inspection_type") == "intake" else "Inspection"
    rows = []
    for area in cond["areas"]:
        label = area["area"].title()
        if area["status"] == "defect":
            value = "; ".join(f"{d['severity'].title()} {d['type'].replace('_', ' ')}" for d in area["defects"])
        elif area["status"] == "verified_ok":
            value = "No marks"
        else:
            value = "Not checked yet"
        rows.append({"label": label, "value": value, "status": area["status"],
                     "source": source if area["status"] != "unknown" else None,
                     "date": cond.get("inspected_at") if area["status"] != "unknown" else None})
    for comp in cond["components"]:
        value = {"verified_ok": "Works", "failed": "Doesn't work", "unknown": "Not checked yet"}[comp["status"]]
        rows.append({"label": comp["component"].title(), "value": value, "status": comp["status"],
                     "source": source if comp["status"] != "unknown" else None,
                     "date": cond.get("inspected_at") if comp["status"] != "unknown" else None, "component": True})
    return rows
