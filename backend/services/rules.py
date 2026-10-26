"""Deterministic return policy (plan section 4). Pure functions: no I/O, no model calls.

evaluate_return and evaluate_resale are deliberately separate. Automatic
outcomes are APPROVED_DEMO (a demo triage result, never an ALDO decision),
NEEDS_MORE_EVIDENCE and HUMAN_REVIEW_REQUIRED. REJECTED_AFTER_REVIEW only
ever comes from a (simulated) human decision.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..vision import VisionResult
from .photos import retake_reason

GRADE_ORDER = {"A": 1, "B": 2, "C": 3, "UNKNOWN": 4}


@dataclass
class Decision:
    status: str
    code: str
    reasons: List[str] = field(default_factory=list)
    missing: List[Dict[str, Optional[str]]] = field(default_factory=list)
    retake_reason: Optional[str] = None
    grade: Optional[str] = None
    review_required: bool = False


def overall_grade(defects: List[Dict[str, Any]]) -> str:
    if not defects:
        return "UNKNOWN"
    return max((d.get("severity") or "UNKNOWN" for d in defects), key=lambda g: GRADE_ORDER.get(g, 4))


def evidence_gaps(vision: VisionResult, photos: List[Dict[str, Any]], required: List[str], rules: Dict[str, Any]):
    """Missing or unusable views. Coverage comes from the analysis, quality from our own measurements."""
    reasons = rules.get("view_reasons", {})
    by_id = {p["photo_id"]: p for p in photos}
    quality = {q.photo_id: q for q in vision.photo_quality}
    usable_views = set()
    failed_views: Dict[str, str] = {}
    for photo_id, photo in by_id.items():
        q = quality.get(photo_id)
        view = (q.detected_view if q and q.detected_view else None) or photo.get("declared_view")
        measured = photo.get("verdict") or {}
        bad = [k for k, v in measured.items() if v == "FAIL"]
        if q and (q.sharpness == "FAIL" or q.exposure == "FAIL" or q.occlusion == "HIGH" or not q.looks_like_footwear
                  or q.duplicate_of):
            bad.append("analysis")
        if bad:
            if view and view not in usable_views:
                failed_views[view] = retake_reason(measured) if any(k != "analysis" for k in bad) else \
                    ("This photo looks like a duplicate of another one." if q and q.duplicate_of else
                     "We couldn't see the shoes clearly in this photo.")
            continue
        if view in vision.covered_views:
            usable_views.add(view)
            failed_views.pop(view, None)
    missing = []
    for view in required:
        if view in usable_views:
            continue
        missing.append({"view": view, "reason": failed_views.get(view) or reasons.get(view)})
    return missing


def evaluate_return(rules: Dict[str, Any], order: Dict[str, Any], line: Dict[str, Any], claim_type: str,
                    facts: Dict[str, Any], vision: VisionResult, photos: List[Dict[str, Any]],
                    today: dt.date) -> Decision:
    required = list(rules["required_views"][claim_type])
    missing = evidence_gaps(vision, photos, required, rules)
    if missing:
        unclear = any(word in (m["reason"] or "") for m in missing
                      for word in ("blurry", "dark", "small", "duplicate", "couldn't see"))
        return Decision("NEEDS_MORE_EVIDENCE", "EVIDENCE_INCOMPLETE",
                        [f"{len(missing)} required view(s) missing or unusable"], missing,
                        "Some views are missing or unclear." if unclear else "Some required photos are missing.")
    defects = [d.model_dump() for d in vision.defects]
    grade = overall_grade(defects) if defects else None
    purchased = dt.date.fromisoformat(order["purchased_at"][:10])
    age_days = (today - purchased).days
    reasons: List[str] = []
    review: List[str] = []
    if order.get("channel") == "THIRD_PARTY":
        review.append("bought from a third-party retailer")
    if facts.get("repaired") is True:
        review.append("the item has been repaired")
    if vision.text_photo_conflicts:
        review.append("the photos or description don't match the order: " + "; ".join(vision.text_photo_conflicts[:2]))
    if claim_type == "STANDARD_RETURN":
        if age_days > int(rules["standard_return_days"]):
            review.append(f"outside the {rules['standard_return_days']}-day return window ({age_days} days)")
        if order.get("is_final_sale"):
            review.append("final-sale purchase")
        if facts.get("worn") not in (None, "NOT_WORN"):
            review.append("the item has been worn")
        if defects:
            review.append("visible defects on a standard return")
        if review:
            return Decision("HUMAN_REVIEW_REQUIRED", "NEEDS_REVIEW", review, review_required=True, grade=grade)
        return Decision("APPROVED_DEMO", "DEMO_STANDARD_RETURN",
                        [f"bought {age_days} days ago", "unworn, complete photos"], grade=grade)
    # DEFECT_CLAIM
    if age_days > int(rules["defect_claim_days"]):
        review.append(f"reported after the defect window ({age_days} days)")
    if order.get("is_final_sale"):
        review.append("final-sale purchase with a defect claim")
    if not defects:
        review.append("no defect was visible in the photos")
    structural = set(rules.get("structural_defect_types", []))
    if vision.possible_structural_or_safety_issue or any(d["type"] in structural for d in defects):
        review.append("possible structural or safety issue")
    if defects and grade not in rules.get("auto_approve_defect_grades", ["A"]):
        review.append(f"defect grade {grade} needs a person to confirm")
    if any(d["uncertainty"] == "high" for d in defects):
        review.append("the analysis was uncertain")
    if review:
        return Decision("HUMAN_REVIEW_REQUIRED", "NEEDS_REVIEW", review, review_required=True, grade=grade)
    reasons += [f"reported {age_days} days after purchase", f"minor cosmetic defect (grade {grade})",
                "all seven views present and clear"]
    return Decision("APPROVED_DEMO", "DEMO_TRIAGE_APPROVAL", reasons, grade=grade)


def evaluate_resale(rules: Dict[str, Any], grade: Optional[str], defects: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Can this unit be sold after inspection, and at what price ratio? C/UNKNOWN never go on sale."""
    structural = set(rules.get("structural_defect_types", []))
    eligible = bool(grade in rules.get("resale_grades", ["A", "B"]) and defects and
                    not any(d.get("type") in structural for d in defects) and
                    len(defects) <= int(rules.get("max_public_defects", 8)))
    return {"resale_eligible": eligible, "price_ratio": rules.get("resale_price_ratio", {}).get(grade)}


def resale_price(unit_price_cents: int, ratio: Optional[float]) -> int:
    if not ratio:
        return 0
    return int(round(unit_price_cents * float(ratio)))
