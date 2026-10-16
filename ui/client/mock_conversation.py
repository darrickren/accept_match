"""Scripted Buy Defective conversation used by the mock Agent.

Deterministic and stateless: the next turn is a pure function of the
client-held context and the read-only catalogue. It only asks questions whose
answer can still change the results, skips follow-ups that no longer apply,
and clears answers that depended on an answer the customer changed.
"""

from __future__ import annotations

import copy
import re
from typing import Any, Dict, List, Optional, Tuple

from . import mock_store as store
from .contracts import (CATEGORY_LABELS, COMPONENT_LABELS, DEFECT_OPTIONS, POLICY_MAX_SEVERITY, empty_preferences)
from .matching import evaluate

QUESTION_ORDER = ("q_category", "q_location", "q_types", "q_severity", "q_components", "q_colour", "q_style",
                  "q_price")
MULTI = {"q_category", "q_types", "q_components", "q_colour", "q_style"}
DEPENDS = {"q_types": ("q_location",), "q_severity": ("q_location", "q_types"), "q_components": ("q_category",),
           "q_colour": ("q_category",), "q_style": ("q_category",)}
PROMPTS = {
    "q_category": ("What are you looking for?", "Choose one or more. Every piece is an individual returned item."),
    "q_location": ("Where would you accept a cosmetic mark?",
                   "This is a limit, not a requirement: cleaner pieces still qualify."),
    "q_types": ("Which imperfections are you happy with?", "Choose all that are fine with you."),
    "q_severity": ("How noticeable can they be?", "This is the most you'd accept."),
    "q_components": ("Which parts must be fully working?",
                     "We only call it a verified match when these have been checked."),
    "q_colour": ("Any colour preference?", "Choose all that you like."),
    "q_style": ("Any style preference?", None),
    "q_price": ("What's your price range?", "Prices are in Canadian dollars."),
}
LOCATIONS = {"front": ("front",), "back": ("back",), "both": ("front", "back"), "neither": ()}
LOCATION_LABELS = {"front": "Front only", "back": "Back only", "both": "Front or back", "neither": "Neither"}
SEVERITY_OPTIONS = {"minor": "Minor only", "moderate": "Minor or moderate"}
PRICE_BUCKETS = {"under_100": (None, 100.0, "Under CA$100"), "100_130": (100.0, 130.0, "CA$100–130"),
                 "over_130": (130.0, None, "Over CA$130"), "any": (None, None, "Any price")}
COLOR_LABELS = {"black": "Black", "taupe": "Taupe", "cognac": "Cognac", "ivory": "Ivory", "burgundy": "Burgundy"}
STYLE_LABELS = {"structured": "Structured", "soft": "Soft"}
DOMAIN = {
    "q_category": set(CATEGORY_LABELS), "q_location": set(LOCATIONS), "q_types": set(DEFECT_OPTIONS),
    "q_severity": set(SEVERITY_OPTIONS), "q_components": set(COMPONENT_LABELS) | {"none"},
    "q_colour": set(COLOR_LABELS) | {"any"}, "q_style": set(STYLE_LABELS) | {"any"}, "q_price": set(PRICE_BUCKETS),
}
EXCLUSIVE = {"none", "any"}


# ----------------------------------------------------------------- preferences

def published_views(overlay: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [store.listing_view(row, overlay) for row in store.listings(overlay) if row["publication_status"] == "published"]


def derive_preferences(answers: Dict[str, Any]) -> Dict[str, Any]:
    """Structured filters from answers. Unanswered questions add no constraint."""
    prefs = empty_preferences()

    def values(qid: str) -> List[str]:
        return list((answers.get(qid) or {}).get("values") or [])

    prefs["category"] = values("q_category")
    if "q_location" in answers:
        areas = LOCATIONS[values("q_location")[0]]
        types = values("q_types") or sorted(DEFECT_OPTIONS)
        severity = (values("q_severity") or [POLICY_MAX_SEVERITY])[0]
    else:
        areas, types, severity = ("front", "back"), sorted(DEFECT_OPTIONS), POLICY_MAX_SEVERITY
    prefs["tolerated_defects"] = [{"area": a, "types": types, "max_severity": severity} for a in areas]
    prefs["required_working"] = [v for v in values("q_components") if v != "none"]
    prefs["colors"] = [v for v in values("q_colour") if v != "any"]
    prefs["styles"] = [v for v in values("q_style") if v != "any"]
    if "q_price" in answers:
        low, high, _ = PRICE_BUCKETS[values("q_price")[0]]
        prefs["price"] = {"min": low, "max": high, "currency": "CAD"}
    return prefs


def candidates(views: List[Dict[str, Any]], prefs: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [v for v in views if evaluate(v, prefs)["level"] != "excluded"]


def _with(answers: Dict[str, Any], qid: str, vals: List[str]) -> Dict[str, Any]:
    trial = dict(answers)
    trial[qid] = {"values": vals, "text": None}
    return trial


def _count(views, answers) -> int:
    return len(candidates(views, derive_preferences(answers)))


# ----------------------------------------------------------------- questions

def options_for(qid: str, answers: Dict[str, Any], views: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    base = {k: v for k, v in answers.items() if k != qid}
    if qid == "q_category":
        present = sorted({v["product"]["category"] for v in views})
        return [{"value": c, "label": CATEGORY_LABELS[c], "count": _count(views, _with(base, qid, [c]))}
                for c in present]
    if qid == "q_location":
        return [{"value": k, "label": LOCATION_LABELS[k], "count": _count(views, _with(base, qid, [k]))}
                for k in LOCATIONS]
    pool = candidates(views, derive_preferences(base))
    if qid == "q_types":
        areas = set(LOCATIONS[(answers.get("q_location") or {"values": ["both"]})["values"][0]])
        present = {d["type"] for v in pool for a in v["condition"]["areas"] if a["area"] in areas
                   for d in a["defects"]}
        return [{"value": t, "label": DEFECT_OPTIONS[t], "count": _count(views, _with(base, qid, [t]))}
                for t in DEFECT_OPTIONS if t in present]
    if qid == "q_severity":
        return [{"value": k, "label": label, "count": _count(views, _with(base, qid, [k]))}
                for k, label in SEVERITY_OPTIONS.items()]
    if qid == "q_components":
        cats = (answers.get("q_category") or {}).get("values") or sorted(CATEGORY_LABELS)
        templates = store.catalogue()["category_templates"]
        present = {c for cat in cats for c in templates.get(cat, {}).get("components", [])}
        return [{"value": c, "label": COMPONENT_LABELS[c]} for c in COMPONENT_LABELS if c in present] + \
            [{"value": "none", "label": "None of these", "exclusive": True}]
    if qid in ("q_colour", "q_style"):
        field, labels, anyone = ("color", COLOR_LABELS, "Any colour") if qid == "q_colour" else \
            ("style", STYLE_LABELS, "Any style")
        present = sorted({v["product"][field] for v in pool if v["product"].get(field)})
        return [{"value": p, "label": labels.get(p, p.title()), "count": sum(1 for v in pool if v["product"][field] == p)}
                for p in present] + [{"value": "any", "label": anyone, "exclusive": True}]
    if qid == "q_price":
        opts = []
        for key, (low, high, label) in PRICE_BUCKETS.items():
            count = _count(views, _with(base, qid, [key]))
            if key == "any" or count:
                opts.append({"value": key, "label": label, "count": count})
        return opts
    return []


def askable(qid: str, answers: Dict[str, Any], views: List[Dict[str, Any]]) -> Optional[List[Dict[str, Any]]]:
    """Options if the question still matters, else None (skipped)."""
    location = ((answers.get("q_location") or {}).get("values") or [None])[0]
    if qid in ("q_types", "q_severity") and location == "neither":
        return None
    if qid == "q_severity" and "q_types" not in answers:
        return None
    options = options_for(qid, answers, views)
    real = [o for o in options if o["value"] not in EXCLUSIVE]
    if not real:
        return None
    if qid in ("q_colour", "q_style") and len(real) <= 1:
        return None
    if qid == "q_price" and len(candidates(views, derive_preferences(answers))) <= 1:
        return None
    return options


def next_question(answers: Dict[str, Any], views: List[Dict[str, Any]]) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    for qid in QUESTION_ORDER:
        if qid in answers:
            continue
        options = askable(qid, answers, views)
        if options is not None:
            return qid, options
    return None, []


def dependents(qid: str) -> List[str]:
    found: List[str] = []
    frontier = [qid]
    while frontier:
        current = frontier.pop()
        for child, parents in DEPENDS.items():
            if current in parents and child not in found:
                found.append(child)
                frontier.append(child)
    return found


def normalise(qid: str, values: List[str]) -> Optional[List[str]]:
    """Validate against the question's domain; None if invalid."""
    if qid not in DOMAIN or not isinstance(values, list) or not values:
        return None
    clean = [v for v in dict.fromkeys(values) if v in DOMAIN[qid]]
    if len(clean) != len(set(values)):
        return None
    if qid not in MULTI:
        return clean[:1] if len(clean) == 1 else None
    real = [v for v in clean if v not in EXCLUSIVE]
    return real or clean[:1]


def apply_answer(context: Dict[str, Any], qid: str, values: Optional[List[str]], text: Optional[str]) -> Dict[str, Any]:
    """New context (version + 1). Changing or clearing an answer clears the answers that depend on it."""
    ctx = copy.deepcopy(context)
    answers, order = ctx.setdefault("answers", {}), ctx.setdefault("order", [])
    previous = (answers.get(qid) or {}).get("values")
    if values is None or previous != values:
        for child in dependents(qid):
            answers.pop(child, None)
            if child in order:
                order.remove(child)
    if values is None:
        answers.pop(qid, None)
        if qid in order:
            order.remove(qid)
    else:
        answers[qid] = {"values": values, "text": (text or None)}
        if qid in order:
            order.remove(qid)
        order.append(qid)
    ctx["order"] = [q for q in QUESTION_ORDER if q in answers]
    ctx["preferences"] = derive_preferences(answers)
    ctx["context_version"] = int(ctx.get("context_version", 0)) + 1
    return ctx


def answer_label(qid: str, values: List[str]) -> str:
    labels = {"q_category": CATEGORY_LABELS, "q_location": LOCATION_LABELS, "q_types": DEFECT_OPTIONS,
              "q_severity": SEVERITY_OPTIONS, "q_components": {**COMPONENT_LABELS, "none": "None of these"},
              "q_colour": {**COLOR_LABELS, "any": "Any colour"}, "q_style": {**STYLE_LABELS, "any": "Any style"},
              "q_price": {k: v[2] for k, v in PRICE_BUCKETS.items()}}[qid]
    return ", ".join(labels.get(v, v) for v in values)


def summary(context: Dict[str, Any]) -> List[Dict[str, str]]:
    """Human-readable preference rows for the 'Your preferences' card."""
    titles = {"q_category": "Looking for", "q_location": "Marks accepted on", "q_types": "Imperfections",
              "q_severity": "How noticeable", "q_components": "Must work", "q_colour": "Colour", "q_style": "Style",
              "q_price": "Price"}
    answers = context.get("answers") or {}
    return [{"question_id": q, "title": titles[q], "value": answer_label(q, answers[q]["values"])}
            for q in QUESTION_ORDER if q in answers]


def turn(session_id: str, context: Dict[str, Any], views: List[Dict[str, Any]]) -> Dict[str, Any]:
    answers = context.get("answers") or {}
    qid, options = next_question(answers, views)
    remaining = sum(1 for q in QUESTION_ORDER if q not in answers and askable(q, answers, views) is not None)
    question = None
    message = "That's everything I need. Your matches are ready."
    if qid:
        message = PROMPTS[qid][0]
        question = {"question_id": qid, "type": "multi_choice" if qid in MULTI else "single_choice",
                    "options": options, "allow_free_text": True, "can_skip": False, "help": PROMPTS[qid][1]}
    count = len(candidates(views, derive_preferences(answers)))
    return {"session_id": session_id, "turn_id": f"t_{int(context.get('context_version', 0)):02d}",
            "message": message, "question": question, "context": context, "summary": summary(context),
            "progress": {"answered": len(answers), "estimated_total": len(answers) + remaining},
            "can_search": "q_category" in answers and "q_location" in answers, "match_count": count}


# ----------------------------------------------------------------- free text (keyword rules, labelled scripted)

KEYWORDS = {
    "q_category": [(r"cross\s*-?\s*body|sling", "crossbody"), (r"\btotes?\b|shopper", "tote"),
                   (r"hand\s*bag|top[- ]?handle|purse|satchel", "handbag")],
    "q_types": [(r"scratch", "scratch"), (r"scuff", "scuff"), (r"stain|spot", "stain"),
                (r"discolou?r|fad(e|ed|ing)", "discoloration"), (r"corner|edge wear|worn edge", "corner_wear")],
    "q_severity": [(r"\b(minor|small|light|tiny|subtle|barely|faint)\b", "minor"),
                   (r"\b(moderate|noticeable|visible|medium|obvious)\b", "moderate")],
    "q_components": [(r"zip", "zipper"), (r"strap", "strap"), (r"handle", "handles"),
                     (r"interior|lining|inside|pocket", "interior"),
                     (r"\b(none|nothing|don'?t care|no preference)\b", "none")],
    "q_colour": [(r"black", "black"), (r"taupe|grey|gray|beige", "taupe"), (r"cognac|brown|tan|camel", "cognac"),
                 (r"ivory|cream|white", "ivory"), (r"burgundy|wine|maroon|\bred\b", "burgundy"),
                 (r"\b(any|don'?t mind|no preference|whatever)\b", "any")],
    "q_style": [(r"structured|stiff|boxy", "structured"), (r"soft|slouchy|relaxed", "soft"),
                (r"\b(any|don'?t mind|no preference)\b", "any")],
}
ACCEPT = r"\b(fine|ok|okay|accept|acceptable|don'?t mind|no problem|happy|alright|good with)\b"
REQUIRE = r"\b(must|intact|perfect|clean|flawless|no marks|without|not ok|no scratches|pristine)\b"


def _location(text: str) -> Optional[str]:
    if re.search(r"\b(neither|nowhere|flawless everywhere|no marks at all|perfect condition)\b", text):
        return "neither"
    if re.search(r"\b(both|either|anywhere|any side|front and back|back and front)\b", text) \
            and not re.search(REQUIRE, text):
        return "both"
    accepted = set()
    for clause in re.split(r"[.;,]|\bbut\b|\bhowever\b", text):
        sides = {s for s, pat in (("front", r"\bfront\b"), ("back", r"\b(back|rear)\b")) if re.search(pat, clause)}
        if not sides:
            continue
        if re.search(REQUIRE, clause):
            continue
        if re.search(ACCEPT, clause) or len(sides) == 1:
            accepted |= sides
    if accepted == {"front", "back"}:
        return "both"
    return next(iter(accepted)) if accepted else None


def _price(text: str) -> Optional[str]:
    if re.search(r"\b(any|don'?t mind|no limit|whatever)\b", text):
        return "any"
    number = re.search(r"(\d{2,4})", text)
    if not number:
        return None
    amount = int(number.group(1))
    if re.search(r"\b(over|above|more than|at least)\b", text):
        return "over_130" if amount >= 130 else "any"
    if amount <= 100:
        return "under_100"
    return "100_130" if amount <= 130 else "over_130"


def interpret(qid: str, text: str, options: List[Dict[str, Any]]) -> Dict[str, Any]:
    lowered = (text or "").lower()
    allowed = [o["value"] for o in options]
    if qid == "q_location":
        found = [v for v in [_location(lowered)] if v]
    elif qid == "q_price":
        found = [v for v in [_price(lowered)] if v]
    else:
        found = [value for pattern, value in KEYWORDS.get(qid, []) if re.search(pattern, lowered)]
    values = [v for v in dict.fromkeys(found) if v in allowed]
    if qid not in MULTI:
        values = values[:1]
    elif any(v not in EXCLUSIVE for v in values):
        values = [v for v in values if v not in EXCLUSIVE]
    understood = bool(values)
    return {"question_id": qid, "values": values, "understood": understood,
            "summary": answer_label(qid, values) if understood else None}
