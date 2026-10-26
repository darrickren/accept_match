"""APP_MODE=mock: a deterministic, scripted agent and a simulated photo analysis (analysis_source SIMULATED).

It follows the same rules the real agent must follow: 1-2 questions per turn,
never re-ask an answered question, options built from what is really in stock,
items only from the inventory service, and no claims about photo content
beyond the deterministic quality checks.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from ..services.inventory import public
from ..services.photos import hamming
from ..vision import PhotoQuality, VisionDefect, VisionResult
from .base import ReturnTools, TurnResult, question
from .preferences import (DEFECT_LABELS, DEFECT_WORDS, TYPE_PLURALS, Preferences, apply, blocking_constraints,
                          extract, money, size_label)

SOURCE = "SIMULATED"
MAX_ITEMS = 6
BUDGETS = (6000, 8000, 10000, 15000)


# ----------------------------------------------------------------- shopping

def greeting(inventory: Any) -> Dict[str, Any]:
    facts = inventory.catalogue_facts()
    options = [{"value": t, "label": TYPE_PLURALS.get(t, t)} for t in facts["shoe_types"]] + \
        [{"value": "any", "label": "Any style"}]
    return {"message": "Hi! I'll help you find a returned pair whose imperfections you're happy with. "
                       "What kind of shoes are you looking for? You can also describe it in your own words.",
            "questions": [question("style", "What kind of shoes are you looking for?", options)],
            "state": {"preferences": {}, "asked": ["style"], "pending": {"question_id": "style"}}}


def _structured_reply(text: str, labels: List[str]) -> Optional[List[str]]:
    """The reply is exactly one or more option labels (how the site sends chip answers)."""
    parts = [p.strip().lower() for p in text.split(",") if p.strip()]
    lowered = {label.lower(): label for label in labels}
    if parts and all(p in lowered for p in parts):
        return [lowered[p] for p in parts]
    return None


def _interpret(text: str, state: Dict[str, Any]) -> Dict[str, Any]:
    patch = extract(text)
    pending = state.get("pending") or {}
    if pending.get("question_id") == "defects":
        labels = {DEFECT_LABELS[t]: t for t in pending.get("types", [])}
        chosen = _structured_reply(text, list(labels) + ["None of these"])
        if chosen is not None:
            picked = {labels[c] for c in chosen if c in labels}
            # The question said "anything you don't choose counts as not OK".
            patch["accepted_defect_types"] = {t: ("YES" if t in picked else "NO") for t in pending.get("types", [])}
    if pending.get("question_id") == "style":
        if _structured_reply(text, ["Any style"]):
            patch["shoe_types"], patch["category"] = [], "SHOES"
    return patch


def _size_question(facts: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    sizes = facts["sizes"][:9]
    if not sizes:
        return None
    return question("size", "Which size do you wear?", [{"value": f"{s}:{v}", "label": size_label(s, v)}
                                                        for s, v in sizes], help="Sizes in stock right now.")


def _budget_question() -> Dict[str, Any]:
    return question("budget", "What's your budget?", [f"Under {money(b)}" for b in BUDGETS] + ["Any price"])


def _defect_question(types: List[str]) -> Dict[str, Any]:
    return question("defects", "Which imperfections are OK for you?",
                    [DEFECT_LABELS[t] for t in types] + ["None of these"], kind="multi_choice",
                    help="Choose all that are fine. Anything you don't choose counts as not OK.")


def _location_question() -> Dict[str, Any]:
    return question("locations", "Is there anywhere a mark isn't OK?",
                    ["Not on the toe", "Not on the heel", "Anywhere is fine"])


def shopping_turn(state: Dict[str, Any], text: str, inventory: Any) -> TurnResult:
    state = {"preferences": {}, "asked": [], **(state or {})}
    prefs = apply(Preferences(**state["preferences"]), _interpret(text, state))
    asked = list(dict.fromkeys(state.get("asked") or []))
    facts = inventory.catalogue_facts(prefs)
    found = inventory.find_for(prefs)
    unknown_types = sorted({d["type"] for row, _ in found for d in row.get("defects") or []
                            if prefs.answer(d["type"]) == "UNKNOWN"})
    questions: List[Dict[str, Any]] = []
    if prefs.size is None and "size" not in asked:
        q = _size_question(facts)
        if q:
            questions.append(q)
    if prefs.max_price_cents is None and not prefs.any_price and "budget" not in asked:
        questions.append(_budget_question())
    basics_done = len(questions) == 0
    expressed = bool(prefs.accepted_defect_types)
    if basics_done and unknown_types and "defects" not in asked:
        questions.append(_defect_question(unknown_types))
    if basics_done and len(found) > 1 and "locations" not in asked and not prefs.rejected_locations and \
            len(questions) < 2 and ("defects" in asked or expressed):
        questions.append(_location_question())
    show = basics_done and found and ("defects" in asked or expressed or not unknown_types)
    pending = {"question_id": questions[0]["question_id"]} if questions else {}
    if questions and questions[0]["question_id"] == "defects":
        pending["types"] = unknown_types
    asked += [q["question_id"] for q in questions]
    understood = blocking_constraints(prefs)
    summary = ("Got it: " + "; ".join(understood) + ". ") if understood else ""
    new_state = {"preferences": prefs.model_dump(), "asked": asked, "pending": pending}
    if show:
        items = [public(row, m) for row, m in found[:MAX_ITEMS]]
        full = sum(1 for _, m in found if m.status == "MATCH")
        message = f"{summary}I found {len(found)} {'pair' if len(found) == 1 else 'pairs'} in stock that fit. "
        if full < len(found):
            message += f"{len(found) - full} still depend on something you haven't confirmed."
        return TurnResult(message.strip(), "SHOW_ITEMS", new_state, questions[:1], items=items,
                          analysis_source=SOURCE)
    if basics_done and not found:
        relax = []
        if prefs.max_price_cents is not None:
            relax.append("Any price")
        relax += [f"{DEFECT_LABELS[t]} are fine" for t, v in sorted(prefs.accepted_defect_types.items()) if v == "NO"][:2]
        relax.append("Keep my preferences")
        new_state["pending"] = {"question_id": "relax"}
        return TurnResult(f"{summary}Nothing in stock fits all of that right now. I won't change your preferences "
                          "unless you say so.", "ASK_QUESTION", new_state,
                          [question("relax", "Would you like to relax one of these?", relax)],
                          blocking=blocking_constraints(prefs), analysis_source=SOURCE)
    if not questions:  # nothing left to ask but nothing to show yet (should not happen); ask style again
        questions = [greeting(inventory)["questions"][0]]
    return TurnResult(f"{summary}{'A couple of quick questions' if len(questions) > 1 else 'One quick question'} so "
                      "I only show pairs that fit.", "ASK_QUESTION", new_state, questions, analysis_source=SOURCE)


# ----------------------------------------------------------------- returns

WORN = [(r"\b(not|never|haven'?t|have not)\s+(been\s+)?worn|unworn|brand new|still in the box", "NOT_WORN"),
        (r"\bindoors?\b|inside (the )?house|tried them on", "INDOORS"),
        (r"\boutdoors?\b|outside|walked|wore them out|worn (them )?(to|for)", "OUTDOORS")]
ONSET = [(r"unbox|out of the box|arrived|when i opened|on delivery", "ON_ARRIVAL"),
         (r"first time i wore|first wear|first time", "FIRST_WEAR"),
         (r"after (a|one|two|few|several)|weeks?|months?|later", "AFTER_WEAR")]


def return_facts(text: str) -> Dict[str, Any]:
    low = text.lower().replace("’", "'")
    facts: Dict[str, Any] = {}
    order = re.search(r"\b([A-Z]{3}-[A-Z0-9-]{2,30})\b", text.upper())
    if order:
        facts["order_id"] = order.group(1)
    line = re.search(r"\bline\s+(\d{1,2})\b", low)
    if line:
        facts["order_line"] = line.group(1)
    if "standard return" in low:  # the site states the type explicitly; that wins over keywords
        facts["claim_type"] = "STANDARD_RETURN"
    elif "defect claim" in low:
        facts["claim_type"] = "DEFECT_CLAIM"
    elif re.search(r"defective|damaged|broke", low):
        facts["claim_type"] = "DEFECT_CLAIM"
    elif re.search(r"changed my mind|doesn'?t fit|unworn", low):
        facts["claim_type"] = "STANDARD_RETURN"
    happened = re.search(r"what happened:\s*(.+)$", text, re.I | re.S)
    if happened:
        facts["description"] = happened.group(1).strip()[:1500]
    if re.search(r"\b(haven'?t|have not|not|never)\s+(been\s+)?(repaired|fixed|altered)|no repairs?|^no$", low) or \
            low.strip() in ("no", "no, never"):
        facts["repaired"] = False
    elif re.search(r"\b(repaired|re-?glued|fixed|cobbler|altered)\b", low) or low.strip() == "yes":
        facts["repaired"] = True
    for pattern, value in WORN:
        if re.search(pattern, low):
            facts["worn"] = value
            break
    for pattern, value in ONSET:
        if re.search(pattern, low):
            facts["onset"] = value
            break
    return facts


def return_turn(state: Dict[str, Any], text: str, tools: ReturnTools, rules: Dict[str, Any]) -> TurnResult:
    state = {"facts": {}, "asked": [], **(state or {})}
    facts = dict(state["facts"])
    found = return_facts(text)
    pending = (state.get("pending") or {}).get("question_id")
    if pending == "repaired" and "repaired" not in found:
        if text.strip().lower().startswith("no"):
            found["repaired"] = False
        elif text.strip().lower().startswith("yes"):
            found["repaired"] = True
    if pending != "order" and "order_id" in found and facts.get("order_id") and found["order_id"] != facts["order_id"]:
        found.pop("order_id")  # an order id inside a later answer doesn't silently replace the first one
    facts.update({k: v for k, v in found.items() if v is not None})
    asked = list(state.get("asked") or [])
    order = tools.lookup_demo_order(facts["order_id"]) if facts.get("order_id") else None
    new_state = {"facts": facts, "asked": asked, "pending": {}}
    if not order:
        new_state["pending"] = {"question_id": "order"}
        what = f"I couldn't find demo order {facts['order_id']} for your account." if facts.get("order_id") else \
            "Which demo order is this about?"
        return TurnResult(f"{what} Please check the demo order ID (for example ORD-001).", "ASK_QUESTION", new_state,
                          [question("order", "What's the demo order ID?", [], kind="free_text")],
                          return_status="INFO_COLLECTION", analysis_source=SOURCE)
    facts.setdefault("claim_type", "DEFECT_CLAIM")
    wanted = ["worn", "repaired"] + (["onset"] if facts["claim_type"] == "DEFECT_CLAIM" else [])
    missing = [f for f in wanted if facts.get(f) is None and f not in asked]
    questions = []
    texts = {"worn": question("worn", "Have the shoes been worn?", ["Not worn", "Only indoors", "Yes, outdoors"]),
             "repaired": question("repaired", "Have they been repaired or altered?", ["No", "Yes"]),
             "onset": question("onset", "When did you notice the issue?",
                               ["When I unboxed them", "The first time I wore them", "After a few wears"])}
    for name in missing[:2]:
        questions.append(texts[name])
    if questions:
        new_state["asked"] = asked + [q["question_id"] for q in questions]
        new_state["pending"] = {"question_id": questions[0]["question_id"]}
        line = order["lines"][0]
        return TurnResult(f"Thanks. I found {order['order_id']} ({line['product_name']}, "
                          f"{size_label(line['size_system'], line['size_value'])}). "
                          f"{'Two' if len(questions) > 1 else 'One'} quick question{'s' if len(questions) > 1 else ''}.",
                          "ASK_QUESTION", new_state, questions, return_status="INFO_COLLECTION",
                          analysis_source=SOURCE)
    views = rules["required_views"][facts["claim_type"]]
    reasons = rules.get("view_reasons", {})
    structural = re.search(r"sole.{0,20}(off|separat|peel)|heel.{0,20}(broke|broken|loose|snap)",
                           (facts.get("description") or "").lower())
    note = " A specialist may need to look at this one, but the photos help either way." if structural else ""
    return TurnResult(f"Thanks, that's everything I need to know. Please add these {len(views)} photos of both shoes. "
                      f"The review checks what each photo actually shows.{note}", "UPLOAD_PHOTOS", new_state,
                      required_photos=[{"view": v, "reason": reasons.get(v)} for v in views],
                      return_status="INFO_COLLECTION", analysis_source=SOURCE)


# ----------------------------------------------------------------- simulated photo analysis

LOCATION_WORDS = [(r"heel", "heel"), (r"toe", "toe"), (r"sole", "sole"), (r"strap", "strap"), (r"side", "side"),
                  (r"shaft", "shaft"), (r"lining|inside|insole", "inside")]


def analyze(photos: List[Dict[str, Any]], expected_views: List[str], facts: Dict[str, Any],
            line: Dict[str, Any], rules: Dict[str, Any]) -> VisionResult:
    """SIMULATED: trusts declared views, uses our measured quality, and reads defects from the description."""
    quality, covered, seen = [], [], []
    limit = int(rules.get("quality", {}).get("duplicate_max_distance", 12))
    newest: Dict[str, Dict[str, Any]] = {}
    for photo in sorted(photos, key=lambda p: p.get("uploaded_at", "")):
        newest[photo["declared_view"]] = photo
    for photo in sorted(photos, key=lambda p: p.get("uploaded_at", "")):
        verdict = photo.get("verdict") or {}
        duplicate = next((other["photo_id"] for other in seen
                          if other["declared_view"] != photo["declared_view"] and
                          hamming(other["dhash"], photo["dhash"]) <= limit), None)
        seen.append(photo)
        ok = verdict.get("sharpness") == "PASS" and verdict.get("exposure") == "PASS" and \
            verdict.get("resolution") == "PASS"
        quality.append(PhotoQuality(photo_id=photo["photo_id"], sharpness=verdict.get("sharpness", "FAIL"),
                                    exposure=verdict.get("exposure", "FAIL"), occlusion="LOW",
                                    looks_like_footwear=True, duplicate_of=duplicate,
                                    detected_view=photo["declared_view"]))
        if ok and not duplicate and newest.get(photo["declared_view"]) is photo:
            covered.append(photo["declared_view"])
    covered = [v for v in expected_views if v in covered]
    description = (facts.get("description") or "").lower()
    closeups = [p["photo_id"] for p in photos if p["declared_view"] == "defect_closeup"]
    defects = []
    for kind, pattern in DEFECT_WORDS.items():
        if re.search(pattern, description):
            location = next((name for pat, name in LOCATION_WORDS if re.search(pat, description)), "upper")
            side = "left " if "left" in description else ("right " if "right" in description else "")
            severity = "C" if kind in ("sole_separation", "broken_heel") else \
                ("B" if re.search(r"large|big|very|obvious|deep", description) else "A")
            defects.append(VisionDefect(type=kind, location=f"{side}{location}".strip(), severity=severity,
                                        description=f"Customer-described {DEFECT_LABELS.get(kind, kind).lower()} "
                                                    "(simulated observation)", photo_ids=closeups[:2],
                                        uncertainty="medium"))
    conflicts = []
    ordered = (line.get("shoe_type") or "").lower()
    for word, shoe in (("sneaker", "sneaker"), ("boot", "boot"), ("loafer", "loafer"), ("sandal", "sandal"),
                       ("pump", "pump"), ("oxford", "oxford")):
        if re.search(rf"\b{word}s?\b", description) and shoe not in ordered:
            conflicts.append(f"{word}s described, {line.get('shoe_type', 'another style').lower()}s ordered")
            break
    return VisionResult(photo_quality=quality, covered_views=covered,
                        missing_views=[v for v in expected_views if v not in covered], defects=defects,
                        possible_structural_or_safety_issue=any(d.severity == "C" for d in defects),
                        text_photo_conflicts=conflicts,
                        cannot_verify=["photo content (simulated analysis: no model looked at the photos)",
                                       "cause of the defect", "durability"])
