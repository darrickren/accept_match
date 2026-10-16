"""Matching rules for Buy Defective (pure functions, no I/O).

- A tolerance is a maximum, never a requirement: cleaner pieces still match.
- Unknown evidence is never treated as verified.
- Any verified defect outside the tolerance excludes the piece.
- A required part that failed its check excludes the piece; an unchecked one
  makes it a possible match at best.
- Publication policy (for example "no-defect returns are not listed") is NOT
  a matching rule; it lives with the catalogue.
"""

from __future__ import annotations

from typing import Any, Dict, List

from .contracts import (AREA_LABELS, CATEGORY_LABELS, COMPONENT_LABELS, DEFECT_NAMES, POLICY_MAX_SEVERITY,
                        SEVERITY_RANK)


def tolerance_for(preferences: Dict[str, Any], area: str) -> Dict[str, int]:
    """Accepted defect type -> highest accepted severity rank, for one area."""
    accepted: Dict[str, int] = {}
    for rule in preferences.get("tolerated_defects") or []:
        if rule.get("area") != area:
            continue
        rank = SEVERITY_RANK.get(rule.get("max_severity") or POLICY_MAX_SEVERITY, 0)
        for defect_type in rule.get("types") or []:
            accepted[defect_type] = max(accepted.get(defect_type, 0), rank)
    return accepted


def describe(defect: Dict[str, Any]) -> str:
    return f"{defect.get('severity', '')} {DEFECT_NAMES.get(defect.get('type'), str(defect.get('type')))}".strip()


def evaluate(listing: Dict[str, Any], preferences: Dict[str, Any]) -> Dict[str, Any]:
    """Return level ('verified' | 'possible' | 'excluded'), reasons, missing evidence and exclusions."""
    product = listing.get("product") or {}
    amount = (listing.get("price") or {}).get("amount")
    exclusions: List[str] = []
    within: List[str] = []
    verified: List[str] = []
    missing: List[str] = []

    categories = preferences.get("category") or []
    if categories and product.get("category") not in categories:
        exclusions.append(f"Not a {', '.join(CATEGORY_LABELS.get(c, c).lower() for c in categories)}")
    colors = preferences.get("colors") or []
    if colors and product.get("color") not in colors:
        exclusions.append("Different colour")
    styles = preferences.get("styles") or []
    if styles and product.get("style") not in styles:
        exclusions.append("Different style")
    price = preferences.get("price") or {}
    if price.get("min") is not None and (amount is None or amount < price["min"]):
        exclusions.append("Below your price range")
    if price.get("max") is not None and (amount is None or amount > price["max"]):
        exclusions.append("Above your price range")

    condition = listing.get("condition") or {}
    areas = condition.get("areas") or []
    if not areas:
        missing.append("The exterior hasn't been checked yet")
    for area in areas:
        name = area.get("area")
        label = AREA_LABELS.get(name, str(name).title())
        status = area.get("status")
        defects = area.get("defects") or []
        if status == "verified_ok":
            verified.append(f"{label} verified free of marks")
        elif status == "defect" and defects:
            accepted = tolerance_for(preferences, name)
            for defect in defects:
                text = describe(defect)
                rank = SEVERITY_RANK.get(defect.get("severity"), 99)
                if not accepted:
                    exclusions.append(f"{label}: {text}, and you didn't accept marks on the {label.lower()}")
                elif defect.get("type") not in accepted:
                    exclusions.append(f"{label}: {text} isn't one of the imperfections you accepted")
                elif rank > accepted[defect["type"]]:
                    exclusions.append(f"{label}: {text} is more than you said you'd accept")
                else:
                    within.append(f"{label}: {text}, within your limits")
        else:
            missing.append(f"The {label.lower()} hasn't been checked yet")

    statuses = {c.get("component"): c.get("status") for c in condition.get("components") or []}
    for component in preferences.get("required_working") or []:
        if component not in statuses:
            continue  # not part of this item (for example handles on a crossbody)
        label = COMPONENT_LABELS.get(component, component.title())
        status = statuses[component]
        if status == "verified_ok":
            verified.append(f"{label} verified working")
        elif status == "failed":
            exclusions.append(f"{label} failed its check")
        else:
            missing.append(f"{label} hasn't been checked yet")

    level = "excluded" if exclusions else ("possible" if missing else "verified")
    return {"level": level, "reasons": within + verified, "missing_evidence": missing, "exclusions": exclusions}


def defect_score(listing: Dict[str, Any]) -> int:
    """Lower is cleaner. Used to rank within a match level."""
    score = 0
    for area in (listing.get("condition") or {}).get("areas") or []:
        for defect in area.get("defects") or []:
            score += SEVERITY_RANK.get(defect.get("severity"), 3)
    return score
