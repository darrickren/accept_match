"""Deterministic demo dataset `demo-v1` (synthetic, plan section 9).

24 footwear units: grades A=14, B=5, C=3, UNKNOWN=2. Statuses: AVAILABLE 15,
SOLD 2, AWAITING_RECEIPT 2, INSPECTION_PENDING 2, QUARANTINED 3. Grade C is
never AVAILABLE. 12 return cases with their demo orders: 4 are left open for
live demos (ORD-001..004) and 8 are seeded as already processed.
Nothing here is real ALDO data.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

DATASET = "demo-v1"
SEED_VERSION = "2026-10-16.1"


def _defect(defect_id: str, kind: str, severity: str, location: str, description: str) -> Dict[str, Any]:
    return {"defect_id": defect_id, "type": kind, "severity": severity, "location": location,
            "description": description, "uncertainty": "low", "verified_by": "DEMO_SIMULATED"}


def _item(n: int, title: str, shoe: str, system: str, size: str, colour: str, cents: int, grade: str, status: str,
          defects: List[Dict[str, Any]], source_return: Optional[str] = None) -> Dict[str, Any]:
    item_id = f"ITM-{n:03d}"
    # Every unit has inspection photos; they are only visible while the unit is AVAILABLE or SOLD.
    photos = [{"photo_id": f"P-{n:03d}A", "view": "pair_top", "alt": f"{title}, both shoes from above",
               "storage_key": f"items/{item_id}/P-{n:03d}A.jpg"},
              {"photo_id": f"P-{n:03d}B", "view": "defect_closeup", "alt": f"{title}, close-up of the imperfection",
               "storage_key": f"items/{item_id}/P-{n:03d}B.jpg"}]
    for defect in defects:
        defect["evidence_photo_ids"] = [f"P-{n:03d}B"]
    return {"item_id": item_id, "sku": f"{shoe[:3].upper()}-{colour[:3].upper()}-{size}", "title": title,
            "description": f"Returned {title.lower()}, inspected and graded (synthetic demo unit).", "category": "SHOES",
            "shoe_type": shoe, "size_system": system, "size_value": size, "color": colour, "price_cents": cents,
            "currency": "CAD", "grade": grade, "status": status,
            "resale_eligible": status in ("AVAILABLE", "SOLD") and grade in ("A", "B"),
            "listing_version": 1, "defects": defects, "photos": photos, "source_return_id": source_return,
            "image_colour": colour}


def items() -> List[Dict[str, Any]]:
    d = _defect
    return [
        _item(1, "Black Ankle Boots", "Ankle boot", "US_WOMEN", "7", "black", 6900, "A", "AVAILABLE",
              [d("D-001", "scratch", "A", "back of the left heel", "Small cosmetic mark on the heel counter")]),
        _item(2, "Black Chelsea Boots", "Chelsea boot", "US_WOMEN", "7", "black", 8900, "A", "AVAILABLE",
              [d("D-002", "scuff", "A", "right toe", "Light scuff on the toe cap")]),
        _item(3, "Black Ankle Boots", "Ankle boot", "US_WOMEN", "7", "black", 7400, "A", "SOLD",
              [d("D-003", "scratch", "A", "right toe", "Faint scratch on the toe")]),
        _item(4, "Tan Ankle Boots", "Ankle boot", "US_WOMEN", "7", "tan", 7900, "B", "AVAILABLE",
              [d("D-004", "stain", "B", "left outer side", "Visible water mark on the outer side"),
               d("D-005", "scratch", "A", "right heel", "Small scratch on the heel")]),
        _item(5, "Black Sneakers", "Sneaker", "US_WOMEN", "7", "black", 5900, "A", "AVAILABLE",
              [d("D-006", "scuff", "A", "right toe", "Light scuff on the toe"),
               d("D-007", "discoloration", "A", "left heel", "Slight yellowing on the heel tab")]),
        _item(6, "Black Knee-High Boots", "Knee-high boot", "US_WOMEN", "7", "black", 12900, "A", "AVAILABLE",
              [d("D-008", "scratch", "A", "left shaft", "Thin scratch on the boot shaft")]),
        _item(7, "Brown Loafers", "Loafer", "US_MEN", "9", "brown", 8500, "B", "AVAILABLE",
              [d("D-009", "scuff", "B", "left toe", "Noticeable scuff on the toe"),
               d("D-010", "stain", "A", "right inside", "Faint mark on the lining")]),
        _item(8, "Brown Oxfords", "Oxford", "US_MEN", "10", "brown", 9900, "A", "AVAILABLE",
              [d("D-011", "scratch", "A", "right toe", "Small scratch on the toe cap")]),
        _item(9, "Black Oxfords", "Oxford", "US_MEN", "9", "black", 10900, "A", "SOLD",
              [d("D-012", "scuff", "A", "left side", "Light scuff on the side")]),
        _item(10, "White Sneakers", "Sneaker", "US_MEN", "11", "white", 6500, "A", "AVAILABLE",
              [d("D-013", "discoloration", "A", "right side", "Slight discoloration on the side panel")]),
        _item(11, "Beige Pumps", "Pump", "EU", "38", "beige", 7900, "A", "AVAILABLE",
              [d("D-014", "scratch", "A", "left heel", "Small scratch on the heel tip")]),
        _item(12, "Black Pumps", "Pump", "EU", "38", "black", 6900, "B", "AVAILABLE",
              [d("D-015", "scuff", "B", "right toe", "Noticeable scuff on the toe")]),
        _item(13, "Beige Sandals", "Sandal", "EU", "39", "beige", 4900, "A", "AVAILABLE",
              [d("D-016", "stain", "A", "left inside", "Small mark on the footbed")]),
        _item(14, "Navy Flats", "Flat", "EU", "37", "navy", 3900, "A", "AVAILABLE",
              [d("D-017", "scuff", "A", "right toe", "Light scuff on the toe")]),
        _item(15, "Burgundy Mules", "Mule", "EU", "40", "burgundy", 5500, "A", "AVAILABLE",
              [d("D-018", "stitching_issue", "A", "left strap", "One loose decorative stitch")]),
        _item(16, "Grey Sneakers", "Sneaker", "EU", "42", "grey", 6200, "B", "AVAILABLE",
              [d("D-019", "stain", "B", "right side", "Visible stain on the side"),
               d("D-020", "scuff", "A", "left toe", "Light scuff on the toe")]),
        _item(17, "Black Chelsea Boots", "Chelsea boot", "US_MEN", "10", "black", 11900, "A", "AVAILABLE",
              [d("D-021", "scratch", "A", "left heel", "Small scratch on the heel")]),
        _item(18, "Brown Ankle Boots", "Ankle boot", "US_WOMEN", "8", "brown", 8200, "A", "AWAITING_RECEIPT",
              [d("D-022", "scratch", "A", "right heel", "Small scratch on the heel")], source_return="RET-011"),
        _item(19, "Tan Loafers", "Loafer", "US_WOMEN", "6", "tan", 7200, "UNKNOWN", "AWAITING_RECEIPT", []),
        _item(20, "Black Sandals", "Sandal", "US_WOMEN", "9", "black", 5600, "B", "INSPECTION_PENDING",
              [d("D-023", "stitching_issue", "B", "right strap", "Several loose stitches on the strap")]),
        _item(21, "White Sneakers", "Sneaker", "EU", "39", "white", 5800, "UNKNOWN", "INSPECTION_PENDING", []),
        _item(22, "Black Pumps", "Pump", "US_WOMEN", "7", "black", 9900, "C", "QUARANTINED",
              [d("D-024", "broken_heel", "C", "left heel", "Heel is loose")]),
        _item(23, "Brown Ankle Boots", "Ankle boot", "EU", "41", "brown", 9400, "C", "QUARANTINED",
              [d("D-025", "sole_separation", "C", "right sole", "Sole separating at the toe")]),
        _item(24, "Black Sneakers", "Sneaker", "US_MEN", "9", "black", 7000, "C", "QUARANTINED",
              [d("D-026", "sole_separation", "C", "left sole", "Sole separating at the heel"),
               d("D-027", "scuff", "A", "right toe", "Light scuff")]),
    ]


def _order(order_id: str, user: str, purchased: str, channel: str, final_sale: bool, name: str, shoe: str,
           system: str, size: str, colour: str, cents: int, case: str, expected: str) -> Dict[str, Any]:
    return {"order_id": order_id, "demo_user_id": user, "purchased_at": purchased, "channel": channel,
            "currency": "CAD", "is_final_sale": final_sale, "payment_type": "SIMULATED", "case": case,
            "expected": expected,
            "lines": [{"line": "1", "sku": f"{shoe[:3].upper()}-{colour[:3].upper()}-{size}", "category": "SHOES",
                       "product_name": name, "shoe_type": shoe, "size_system": system, "size_value": size,
                       "color": colour, "unit_price_cents": cents}]}


def orders() -> List[Dict[str, Any]]:
    o = _order
    return [
        o("ORD-001", "cust-01", "2026-09-20", "ONLINE", False, "Black Ankle Boots", "Ankle boot", "US_WOMEN", "7",
          "black", 12900, "RC-01 minor heel scratch (live demo)", "APPROVED_DEMO"),
        o("ORD-002", "cust-01", "2026-09-01", "ONLINE", True, "Black Pumps", "Pump", "EU", "38", "black", 9900,
          "RC-02 final sale x defect (live demo)", "HUMAN_REVIEW_REQUIRED"),
        o("ORD-003", "cust-01", "2026-10-05", "STORE", False, "White Sneakers", "Sneaker", "US_WOMEN", "7", "white",
          8900, "RC-03 standard return, unworn (live demo)", "APPROVED_DEMO"),
        o("ORD-004", "cust-01", "2026-08-10", "ONLINE", False, "Brown Ankle Boots", "Ankle boot", "EU", "41", "brown",
          14900, "RC-04 sole separation (live demo)", "HUMAN_REVIEW_REQUIRED"),
        o("ORD-105", "cust-01", "2026-09-12", "ONLINE", False, "Beige Pumps", "Pump", "EU", "38", "beige", 11900,
          "RC-05 wrong angle", "NEEDS_MORE_EVIDENCE"),
        o("ORD-106", "cust-01", "2026-09-28", "ONLINE", False, "Navy Flats", "Flat", "EU", "37", "navy", 7900,
          "RC-06 blurry photo", "NEEDS_MORE_EVIDENCE"),
        o("ORD-107", "cust-01", "2026-10-02", "STORE", False, "Black Loafers", "Loafer", "US_MEN", "9", "black",
          10900, "RC-07 not enough photos", "NEEDS_MORE_EVIDENCE"),
        o("ORD-108", "cust-02", "2026-07-30", "ONLINE", False, "Brown Oxfords", "Oxford", "US_MEN", "10", "brown",
          15900, "RC-08 already repaired", "HUMAN_REVIEW_REQUIRED"),
        o("ORD-109", "cust-02", "2026-09-15", "ONLINE", False, "Black Ankle Boots", "Ankle boot", "US_WOMEN", "8",
          "black", 12900, "RC-09 photos don't match the order", "HUMAN_REVIEW_REQUIRED"),
        o("ORD-110", "cust-02", "2026-09-05", "ONLINE", False, "Tan Ankle Boots", "Ankle boot", "US_WOMEN", "7",
          "tan", 13900, "RC-10 several defects, grade B", "HUMAN_REVIEW_REQUIRED"),
        o("ORD-111", "cust-03", "2026-08-20", "ONLINE", False, "Brown Ankle Boots", "Ankle boot", "US_WOMEN", "8",
          "brown", 14900, "RC-11 approved, waiting for receipt", "APPROVED_DEMO"),
        o("ORD-112", "cust-03", "2026-08-01", "THIRD_PARTY", False, "Black Sandals", "Sandal", "US_WOMEN", "9",
          "black", 9900, "RC-12 human second review, rejected", "REJECTED_AFTER_REVIEW"),
    ]


def _return(n: int, order_id: str, user: str, claim: str, status: str, reason: str, facts: Dict[str, Any],
            code: str, reasons: List[str], missing: Optional[List[Dict[str, str]]] = None,
            item_id: Optional[str] = None, created: str = "2026-10-12T15:00:00Z") -> Dict[str, Any]:
    return {"return_id": f"RET-{n:03d}", "order_id": order_id, "order_line": "1", "demo_user_id": user,
            "claim_type": claim, "reason": reason, "status": status, "facts": facts,
            "missing_views": missing or [], "retake_reason": "Some views are missing or unclear." if missing else None,
            "rule_decision": {"code": code, "reasons": reasons, "final_merchant_decision": False},
            "analysis_source": "SIMULATED", "model_id": None, "photo_ids": [], "item_id": item_id,
            "session_id": f"SEED-{n:03d}", "created_at": created, "updated_at": created, "version": 1}


def returns() -> List[Dict[str, Any]]:
    r = _return
    defect = {"worn": "INDOORS", "repaired": False, "onset": "FIRST_WEAR"}
    return [
        r(5, "ORD-105", "cust-01", "DEFECT_CLAIM", "NEEDS_MORE_EVIDENCE", "defect",
          dict(defect, description="Scratch on the left heel"), "EVIDENCE_INCOMPLETE", ["1 required view missing"],
          [{"view": "pair_soles", "reason": "The photo showed the box, not the soles."}]),
        r(6, "ORD-106", "cust-01", "DEFECT_CLAIM", "NEEDS_MORE_EVIDENCE", "defect",
          dict(defect, description="Scuff on the right toe"), "EVIDENCE_INCOMPLETE", ["1 required view unusable"],
          [{"view": "pair_heels", "reason": "The photo is too blurry to check. Hold steady, in good light."}]),
        r(7, "ORD-107", "cust-01", "DEFECT_CLAIM", "NEEDS_MORE_EVIDENCE", "defect",
          dict(defect, description="Stain on the toe"), "EVIDENCE_INCOMPLETE", ["4 required views missing"],
          [{"view": v, "reason": "This view hasn't been added yet."} for v in
           ("pair_outer", "pair_inner", "pair_soles", "size_label")]),
        r(8, "ORD-108", "cust-02", "DEFECT_CLAIM", "HUMAN_REVIEW_REQUIRED", "defect",
          dict(defect, repaired=True, description="Sole re-glued by a cobbler, now peeling"), "NEEDS_REVIEW",
          ["the item has been repaired"]),
        r(9, "ORD-109", "cust-02", "DEFECT_CLAIM", "HUMAN_REVIEW_REQUIRED", "defect",
          dict(defect, description="My sneakers have a scuff"), "NEEDS_REVIEW",
          ["the photos or description don't match the order: sneakers described, ankle boots ordered"]),
        r(10, "ORD-110", "cust-02", "DEFECT_CLAIM", "HUMAN_REVIEW_REQUIRED", "defect",
          dict(defect, description="A large water stain on the side and a scratch on the heel"), "NEEDS_REVIEW",
          ["defect grade B needs a person to confirm"]),
        r(11, "ORD-111", "cust-03", "DEFECT_CLAIM", "APPROVED_DEMO", "defect",
          dict(defect, description="Small scratch on the right heel"), "DEMO_TRIAGE_APPROVAL",
          ["minor cosmetic defect (grade A)", "all seven views present and clear"], item_id="ITM-018"),
        r(12, "ORD-112", "cust-03", "DEFECT_CLAIM", "REJECTED_AFTER_REVIEW", "defect",
          dict(defect, worn="OUTDOORS", onset="AFTER_WEAR", description="Strap broke after a month of wear"),
          "REJECTED_AFTER_REVIEW", ["simulated reviewer: wear and tear, bought from a third-party retailer"]),
    ]


def reviews() -> List[Dict[str, Any]]:
    def rev(return_id: str, reasons: List[str], summary: str, conclusion: Optional[str] = None) -> Dict[str, Any]:
        row = {"review_id": f"REV-{return_id}-CLAIM", "return_id": return_id, "review_stage": "CLAIM",
               "status": "DECIDED" if conclusion else "OPEN", "reason_codes": reasons, "summary": summary,
               "simulated": True, "created_at": "2026-10-12T15:00:00Z"}
        if conclusion:
            row.update(conclusion=conclusion, decided_by="admin", decided_at="2026-10-13T10:00:00Z",
                       reason="Damage consistent with wear; third-party purchase (simulated reviewer).")
        return row
    return [rev("RET-008", ["REPAIRED"], "Item repaired before the claim."),
            rev("RET-009", ["ORDER_MISMATCH"], "Sneakers described; ankle boots ordered."),
            rev("RET-010", ["GRADE_B"], "Water stain looks grade B."),
            rev("RET-012", ["THIRD_PARTY", "WEAR"], "Strap broke after a month.", conclusion="REJECT")]


COUNTERS = {"return": 100, "photo": 5000, "purchase": 0, "audit": 0}
