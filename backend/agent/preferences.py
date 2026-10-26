"""Shopping preferences (plan section 5): schema, rule-based extraction for mock mode, and matching.

Rules that matter for honesty:
- NO is a hard exclusion, even if the same pair also has an accepted defect.
- UNKNOWN is never YES: such pairs are NEEDS_CONFIRMATION, never a full match.
- Size, budget, grade, colour, style and rejected locations are hard filters.
- Saying "scratches are fine" only filters the search. It isn't agreement to a
  specific pair's scratch: that needs an explicit per-defect acceptance.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field

from ..schemas import DEFECT_TYPES, DefectType, SizeSystem

Answer = Literal["YES", "NO", "UNKNOWN"]
SHOE_TYPES = ("Ankle boot", "Chelsea boot", "Knee-high boot", "Sneaker", "Loafer", "Pump", "Sandal", "Oxford",
              "Mule", "Flat")
TYPE_WORDS = [(r"ankle[- ]?boots?|booties?", ["Ankle boot"]), (r"chelsea", ["Chelsea boot"]),
              (r"knee[- ]?high|tall boots?", ["Knee-high boot"]), (r"sneakers?|trainers?|runners?", ["Sneaker"]),
              (r"loafers?", ["Loafer"]),
              (r"pumps?|stilettos?|high heels|(?<!the )(?<!both )(?<!on )(?<!my )\bheels\b(?! (?:area|counter))",
               ["Pump"]),
              (r"sandals?", ["Sandal"]), (r"oxfords?|brogues?", ["Oxford"]), (r"mules?|slides?", ["Mule"]),
              (r"flats|ballet", ["Flat"])]
ANY_BOOT = (r"\bboots?\b", ["Ankle boot", "Chelsea boot", "Knee-high boot"])
COLOURS = {"black": r"black", "brown": r"brown|cognac|chocolate", "tan": r"\btan\b|camel", "beige": r"beige|nude|cream",
           "white": r"white", "navy": r"navy", "grey": r"gr[ae]y", "burgundy": r"burgundy|wine|oxblood"}
DEFECT_WORDS = {"scratch": r"scratch\w*", "scuff": r"scuff\w*", "stain": r"stain\w*|spots?\b|water marks?",
                "discoloration": r"discolou?r\w*|fad(e|ed|ing)|yellowing", "stitching_issue": r"stitch\w*|seams?",
                "sole_separation": r"sole separation|separat\w* sole|sole (is )?(coming|peeling) off|unglued",
                "broken_heel": r"broken heels?|heels? (is )?(broken|loose|snapped)"}
DEFECT_LABELS = {"scratch": "Scratches", "scuff": "Scuffs", "stain": "Stains", "discoloration": "Discoloration",
                 "stitching_issue": "Stitching issues", "sole_separation": "Sole separation",
                 "broken_heel": "Broken heel", "other": "Other marks"}
LOCATIONS = {"toe": r"\btoes?\b|\bfront\b", "heel": r"\bheels?\b|\bback\b", "sole": r"\bsoles?\b",
             "side": r"\bsides?\b", "inside": r"\binside\b|\blining\b|\binsole\b"}
POSITIVE = r"\b(fine|ok|okay|acceptable|accept|don'?t mind|do not mind|happy with|no problem|good with|alright|is fine|are fine)\b"
NEGATIVE = r"\b(no|not|without|avoid|never|hate|don'?t want|do not want|deal ?breaker|can'?t stand|exclude)\b"
SIZE_LABELS = {"US_WOMEN": "US women's", "US_MEN": "US men's", "EU": "EU"}
TYPE_PLURALS = {"Ankle boot": "Ankle boots", "Chelsea boot": "Chelsea boots", "Knee-high boot": "Knee-high boots",
                "Sneaker": "Sneakers", "Loafer": "Loafers", "Pump": "Pumps", "Sandal": "Sandals", "Oxford": "Oxfords",
                "Mule": "Mules", "Flat": "Flats"}
# Functions a buyer may require. The demo inspection never tests function, so these are never verified:
# a pair can only ever "need confirmation" for them, never fully match.
FEATURES = {"zipper_works": "Zipper works", "heels_stable": "Heels are stable", "soles_intact": "Soles are intact",
            "waterproof": "Waterproof"}
FEATURE_WORDS = {"zipper_works": r"zip(per)?s? (must |should |has to |needs to )?(work|function)",
                 "heels_stable": r"heels? (must|should|needs? to) be (stable|sturdy|solid)|stable heels?",
                 "soles_intact": r"soles? (must|should|needs? to) be (intact|fine|ok)|no sole (damage|separation)",
                 "waterproof": r"waterproof|water-?resistant"}


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Size(Strict):
    system: SizeSystem
    value: str = Field(pattern=r"^\d{1,2}(\.5)?$")


class Preferences(Strict):
    category: Optional[Literal["SHOES"]] = None
    shoe_types: List[str] = Field(default_factory=list, max_length=10)
    size: Optional[Size] = None
    max_price_cents: Optional[int] = Field(default=None, ge=0, le=500_000)
    any_price: bool = False
    accepted_defect_types: Dict[DefectType, Answer] = Field(default_factory=dict)
    max_grade: Optional[Literal["A", "B"]] = None
    rejected_locations: List[str] = Field(default_factory=list, max_length=6)
    colors: List[str] = Field(default_factory=list, max_length=6)
    required_features: List[str] = Field(default_factory=list, max_length=6)

    def answer(self, defect_type: str) -> str:
        return self.accepted_defect_types.get(defect_type, "UNKNOWN")


def size_label(system: Optional[str], value: Optional[str]) -> str:
    return f"{SIZE_LABELS.get(system or '', system or '')} {value or ''}".strip()


def money(cents: Optional[int]) -> str:
    return f"CA${(cents or 0) / 100:,.0f}" if (cents or 0) % 100 == 0 else f"CA${(cents or 0) / 100:,.2f}"


# ----------------------------------------------------------------- extraction (mock mode only)

def _clauses(text: str) -> List[str]:
    return [c.strip() for c in re.split(r"[.;!?\n]|,|\bbut\b|\bhowever\b|\balthough\b", text.lower()) if c.strip()]


def extract(text: str) -> Dict[str, Any]:
    """A partial update from free text. Deterministic keyword rules, labelled SIMULATED by the caller."""
    low = " " + text.lower().replace("’", "'") + " "
    patch: Dict[str, Any] = {}
    size = re.search(r"\b(?:us\s*)?(women'?s|womens|ladies|female)\s*(?:size\s*)?(\d{1,2}(?:\.5)?)\b", low)
    size = size or re.search(r"\b(?:size\s*)?(\d{1,2}(?:\.5)?)\s*(?:in\s*)?(?:us\s*)?(women'?s|womens|ladies)\b", low)
    size = size or re.search(r"\b(women'?s|womens|ladies)\b[^.;]{0,30}?\bsize\s*(\d{1,2}(?:\.5)?)\b", low)
    if size:
        value = size.group(2) if size.group(1)[0].isalpha() else size.group(1)
        patch["size"] = {"system": "US_WOMEN", "value": value}
    else:
        men = re.search(r"\b(?:us\s*)?(?<!wo)(men'?s|mens|male)\s*(?:size\s*)?(\d{1,2}(?:\.5)?)\b", low)
        eu = re.search(r"\b(?:eu|eur|european)\s*(?:size\s*)?(\d{2})\b", low) or \
            re.search(r"\bsize\s*(3\d|4\d)\b", low)
        if men:
            patch["size"] = {"system": "US_MEN", "value": men.group(2)}
        elif eu:
            patch["size"] = {"system": "EU", "value": eu.group(1)}
    if re.search(r"any price|no budget|don'?t mind the price|price doesn'?t matter|any budget", low):
        patch["max_price_cents"], patch["any_price"] = None, True
    else:
        budget = re.search(r"(?:under|below|less than|max(?:imum)?|up to|no more than|budget(?: of| is)?|<)\s*"
                           r"(?:cad|ca)?\s*\$?\s*(\d{2,4})", low) or re.search(r"\$\s*(\d{2,4})\s*(?:or less|max)", low)
        if budget:
            patch["max_price_cents"], patch["any_price"] = int(budget.group(1)) * 100, False
    types: List[str] = []
    for pattern, names in TYPE_WORDS:
        if re.search(pattern, low):
            types += [n for n in names if n not in types]
    if not any("boot" in t for t in types) and re.search(ANY_BOOT[0], low):
        types += ANY_BOOT[1]  # "boots" with no specific kind: any boot
    if types:
        patch["shoe_types"], patch["category"] = types, "SHOES"
    elif re.search(r"\bshoes?\b|footwear", low):
        patch["category"] = "SHOES"
    if re.search(r"any (style|kind|type)", low):
        patch["shoe_types"], patch["category"] = [], "SHOES"
    colours = [name for name, pattern in COLOURS.items() if re.search(pattern, low)]
    if colours and not re.search(r"any colou?r", low):
        patch["colors"] = colours
    elif re.search(r"any colou?r", low):
        patch["colors"] = []
    answers: Dict[str, str] = {}
    for clause in _clauses(text):
        mentioned = [t for t, pattern in DEFECT_WORDS.items() if re.search(pattern, clause)]
        if re.search(r"\bmarks?\b|imperfections?|flaws?", clause) and not mentioned and re.search(r"minor|small|tiny",
                                                                                                   clause):
            patch["max_grade"] = "A"
        if not mentioned:
            continue
        if re.search(POSITIVE, clause) and not re.search(r"\b(not|isn'?t|aren'?t) (fine|ok|okay|acceptable)\b",
                                                         clause):
            value = "YES"
        elif re.search(NEGATIVE, clause):
            value = "NO"
        else:
            continue
        for defect_type in mentioned:
            answers[defect_type] = value
    if re.search(r"(only|just) (minor|small|tiny)|barely (visible|noticeable)", low):
        patch["max_grade"] = "A"
    if answers:
        patch["accepted_defect_types"] = answers
    rejected = []
    for clause in _clauses(text):
        if not re.search(r"\b(no|not|nothing|avoid|never|clean|must be)\b", clause):
            continue
        if re.search(r"\b(anywhere|any place)\b.*\b(fine|ok)\b", clause):
            continue
        for location, pattern in LOCATIONS.items():
            if re.search(pattern, clause) and re.search(r"\b(on|at|near|the|marks?|area)\b", clause):
                rejected.append(location)
    if rejected:
        patch["rejected_locations"] = sorted(set(rejected))
    if re.search(r"anywhere is fine|any place is fine|location doesn'?t matter", low):
        patch["rejected_locations"] = []
    features = [name for name, pattern in FEATURE_WORDS.items() if re.search(pattern, low)]
    if features:
        patch["required_features"] = features
    return patch


def apply(prefs: Preferences, patch: Dict[str, Any]) -> Preferences:
    data = prefs.model_dump()
    for key, value in patch.items():
        if key == "accepted_defect_types":
            data[key] = {**data.get(key, {}), **value}
        elif key in ("rejected_locations", "required_features") and value:
            data[key] = sorted(set(data.get(key, [])) | set(value))
        else:
            data[key] = value
    return Preferences(**data)


# ----------------------------------------------------------------- matching (deterministic, every mode)

@dataclass
class Match:
    status: str  # MATCH | NEEDS_CONFIRMATION | EXCLUDED
    reasons: List[str] = field(default_factory=list)
    pending: List[str] = field(default_factory=list)
    exclusions: List[str] = field(default_factory=list)


def _location_hit(location: str, rejected: List[str]) -> Optional[str]:
    text = (location or "").lower()
    for name in rejected:
        pattern = LOCATIONS.get(name, re.escape(name))
        if re.search(pattern, text):
            return name
    return None


def match(item: Dict[str, Any], prefs: Preferences) -> Match:
    out = Match("MATCH")
    if item.get("status") != "AVAILABLE" or item.get("grade") not in ("A", "B") or not item.get("resale_eligible"):
        out.exclusions.append("not available to buy")
    if prefs.shoe_types and item.get("shoe_type") not in prefs.shoe_types:
        out.exclusions.append(f"not a {', '.join(t.lower() for t in prefs.shoe_types)}")
    if prefs.size and (item.get("size_system"), str(item.get("size_value"))) != (prefs.size.system, prefs.size.value):
        out.exclusions.append(f"not size {size_label(prefs.size.system, prefs.size.value)}")
    if prefs.max_price_cents is not None and int(item.get("price_cents", 0)) > prefs.max_price_cents:
        out.exclusions.append(f"over {money(prefs.max_price_cents)}")
    if prefs.colors and item.get("color") not in prefs.colors:
        out.exclusions.append("a different colour")
    if prefs.max_grade == "A" and item.get("grade") != "A":
        out.exclusions.append("marks are more than minor")
    unknown = []
    for defect in item.get("defects") or []:
        kind, where = defect["type"], defect.get("location") or ""
        label = DEFECT_LABELS.get(kind, kind).lower().rstrip("s") if kind != "discoloration" else "discoloration"
        answer = prefs.answer(kind)
        hit = _location_hit(where, prefs.rejected_locations)
        if answer == "NO":
            out.exclusions.append(f"has a {label} ({where}), which you said isn't OK")
        elif hit:
            out.exclusions.append(f"has a mark on the {hit}, where you don't want one")
        elif answer == "YES":
            out.reasons.append(f"{label.capitalize()} on the {where} is something you're fine with")
        elif kind not in unknown:
            unknown.append(kind)
    for kind in unknown:
        out.pending.append(f"You haven't said whether {DEFECT_LABELS.get(kind, kind).lower()} are OK")
    for feature in prefs.required_features:
        label = FEATURES.get(feature, feature)
        if feature in (item.get("verified_features") or []):
            out.reasons.append(f"{label}: checked at inspection")
        else:  # unknown evidence never counts as verified
            out.pending.append(f"{label}: not verified for this pair (inspections don't test function)")
    if out.exclusions:
        out.status = "EXCLUDED"
        return out
    if prefs.size:
        out.reasons.insert(0, f"Size {size_label(prefs.size.system, prefs.size.value)}")
    if prefs.max_price_cents is not None:
        out.reasons.insert(1 if prefs.size else 0, f"Under {money(prefs.max_price_cents)}")
    if out.pending:
        out.status = "NEEDS_CONFIRMATION"
    return out


def blocking_constraints(prefs: Preferences) -> List[str]:
    rows = []
    if prefs.shoe_types:
        rows.append(", ".join(prefs.shoe_types) + " only")
    if prefs.size:
        rows.append(f"Size {size_label(prefs.size.system, prefs.size.value)}")
    if prefs.max_price_cents is not None:
        rows.append(f"Under {money(prefs.max_price_cents)}")
    if prefs.colors:
        rows.append(", ".join(c.title() for c in prefs.colors) + " only")
    rows += [f"No {DEFECT_LABELS.get(t, t).lower()}" for t, v in sorted(prefs.accepted_defect_types.items()) if v == "NO"]
    rows += [f"Nothing on the {loc}" for loc in prefs.rejected_locations]
    if prefs.max_grade == "A":
        rows.append("Minor marks only")
    return rows


def summary(prefs: Preferences) -> List[Dict[str, str]]:
    """What the backend understood, for the site's "Your preferences" panel. Deterministic, never model text."""
    rows: List[Tuple[str, str, str]] = []
    if prefs.shoe_types:
        rows.append(("style", "Style", ", ".join(TYPE_PLURALS.get(t, t) for t in prefs.shoe_types)))
    elif prefs.category == "SHOES":
        rows.append(("style", "Style", "Any style"))
    if prefs.size:
        rows.append(("size", "Size", size_label(prefs.size.system, prefs.size.value)))
    if prefs.max_price_cents is not None:
        rows.append(("budget", "Budget", f"Under {money(prefs.max_price_cents)}"))
    elif prefs.any_price:
        rows.append(("budget", "Budget", "Any price"))
    if prefs.colors:
        rows.append(("colors", "Colour", ", ".join(c.title() for c in prefs.colors)))
    ok = [DEFECT_LABELS.get(t, t) for t, v in sorted(prefs.accepted_defect_types.items()) if v == "YES"]
    no = [DEFECT_LABELS.get(t, t) for t, v in sorted(prefs.accepted_defect_types.items()) if v == "NO"]
    if ok:
        rows.append(("defects", "Fine with", ", ".join(ok)))
    if no:
        rows.append(("defects", "Not OK", ", ".join(no)))
    if prefs.max_grade == "A":
        rows.append(("severity", "Severity", "Minor marks only"))
    if prefs.rejected_locations:
        rows.append(("locations", "No marks on", ", ".join(prefs.rejected_locations)))
    if prefs.required_features:
        rows.append(("features", "Must have", ", ".join(FEATURES.get(f, f) for f in prefs.required_features)))
    return [{"key": key, "title": title, "value": value} for key, title, value in rows]


def progress(prefs: Preferences) -> Dict[str, int]:
    """Style, size, budget and imperfections: the four answers that decide which pairs fit."""
    answered = sum([bool(prefs.shoe_types) or prefs.category == "SHOES", prefs.size is not None,
                    prefs.max_price_cents is not None or prefs.any_price, bool(prefs.accepted_defect_types)])
    return {"answered": answered, "estimated_total": 4}


def rank(rows: List[Tuple[Dict[str, Any], Match]]) -> List[Tuple[Dict[str, Any], Match]]:
    return sorted(rows, key=lambda r: (0 if r[1].status == "MATCH" else 1, len(r[1].pending),
                                       int(r[0].get("price_cents", 0)), r[0]["item_id"]))


__all__ = ["Preferences", "Size", "extract", "apply", "match", "rank", "blocking_constraints", "summary", "progress",
           "DEFECT_LABELS", "SHOE_TYPES", "DEFECT_TYPES", "FEATURES", "TYPE_PLURALS", "size_label", "money"]
