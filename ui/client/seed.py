"""SYNTHETIC seed for the customer-site mock (V3).

Every product, unit, inspection, photo and price here is invented for the
demo and labelled as such in the UI. Nothing comes from the raw datasets.
Model: product -> inventory_unit -> inspection / condition_evidence -> listing,
with photos owned by exactly one of an inspection, a product or a return case.
"""

from __future__ import annotations

from typing import Any, Dict, List

CURRENCY = "CAD"
PUBLICATION_POLICY = {
    "policy_version": "PUB-DRAFT-1",
    "price_floor": 40.0,
    "price_ceiling": 200.0,
    "required_views": ("front", "back"),
    "requires_verified_defect": True,  # no-defect returns are not published to Buy Defective
}

CATEGORY_TEMPLATES = {
    "crossbody": {"areas": ("front", "back"), "components": ("zipper", "strap", "interior")},
    "tote": {"areas": ("front", "back"), "components": ("handles", "interior")},
    "handbag": {"areas": ("front", "back"), "components": ("handles", "zipper", "interior")},
}

PRODUCTS: List[Dict[str, Any]] = [
    {"product_id": "SYN-CB-01", "name": "Corvo crossbody", "category": "crossbody", "style": "structured",
     "material": "Vegan leather, recycled lining", "dimensions": "24 × 17 × 7 cm",
     "description": "A structured everyday crossbody with a front flap, a top zip and an adjustable strap.",
     "season_code": "FW25", "collection": None},
    {"product_id": "SYN-CB-02", "name": "Pebble mini crossbody", "category": "crossbody", "style": "soft",
     "material": "Soft vegan leather", "dimensions": "19 × 13 × 6 cm",
     "description": "A soft mini crossbody with a top zip and a slim adjustable strap.",
     "season_code": None, "collection": None},
    {"product_id": "SYN-TT-01", "name": "Halden tote", "category": "tote", "style": "soft",
     "material": "Textured vegan leather", "dimensions": "38 × 30 × 13 cm",
     "description": "A roomy open tote with two shoulder handles and an inner pocket.",
     "season_code": "SS25", "collection": None},
    {"product_id": "SYN-HB-01", "name": "Verra top-handle bag", "category": "handbag", "style": "structured",
     "material": "Smooth vegan leather", "dimensions": "27 × 19 × 11 cm",
     "description": "A structured top-handle bag with a zip top and a sculpted handle.",
     "season_code": None, "collection": None},
]

OK, UNKNOWN, FAILED = "ok", "unknown", "failed"

# unit, product, colour, availability, price, reference price, findings (None = inspection still pending)
UNIT_SPECS = [
    ("AM-001", "SYN-CB-01", "black", "available", 102.0, 120.0,
     {"front": [("scuff", "minor")], "back": OK, "zipper": OK, "strap": OK, "interior": OK}),
    ("AM-002", "SYN-CB-01", "black", "available", 95.0, 120.0,
     {"front": OK, "back": [("scratch", "minor")], "zipper": FAILED, "strap": OK, "interior": OK}),
    ("AM-003", "SYN-CB-01", "black", "available", 104.0, 120.0,
     {"front": OK, "back": [("stain", "minor")], "zipper": OK, "strap": OK, "interior": OK}),
    ("AM-004", "SYN-CB-01", "black", "available", 90.0, 120.0,
     {"front": OK, "back": [("scratch", "moderate")], "zipper": OK, "strap": OK, "interior": OK}),
    ("AM-005", "SYN-TT-01", "cognac", "available", 128.0, 145.0,
     {"front": OK, "back": [("scratch", "minor")], "handles": OK, "interior": OK}),
    ("AM-006", "SYN-CB-01", "black", "reserved", 108.0, 120.0,
     {"front": OK, "back": [("scratch", "minor")], "zipper": OK, "strap": OK, "interior": OK}),
    ("AM-007", "SYN-CB-01", "black", "available", 108.0, 120.0,
     {"front": OK, "back": [("scratch", "minor")], "zipper": UNKNOWN, "strap": OK, "interior": OK}),
    ("AM-008", "SYN-CB-02", "taupe", "available", 86.0, 95.0,
     {"front": OK, "back": [("scratch", "minor")], "zipper": OK, "strap": OK, "interior": OK}),
    ("AM-009", "SYN-CB-01", "black", "in_intake", 108.0, 120.0, None),
    ("AM-010", "SYN-CB-01", "black", "available", 98.0, 120.0,
     {"front": OK, "back": [("scratch", "minor"), ("stain", "minor")], "zipper": OK, "strap": OK, "interior": OK}),
    ("AM-011", "SYN-HB-01", "ivory", "available", 142.0, 165.0,
     {"front": [("corner_wear", "minor")], "back": OK, "handles": OK, "zipper": OK, "interior": OK}),
    ("AM-012", "SYN-TT-01", "cognac", "available", 120.0, 145.0,
     {"front": [("discoloration", "minor")], "back": OK, "handles": UNKNOWN, "interior": OK}),
    ("AM-013", "SYN-HB-01", "burgundy", "available", 125.0, 165.0,
     {"front": OK, "back": [("scuff", "moderate")], "handles": OK, "zipper": OK, "interior": UNKNOWN}),
]


def build() -> Dict[str, Any]:
    """Expand the compact specs into normalised tables."""
    units, inspections, evidence, photos, listings = [], [], [], [], []
    for index, (unit_id, product_id, colour, availability, price, reference, findings) in enumerate(UNIT_SPECS):
        num = unit_id.split("-")[1]
        inspection_id = f"INS-{num}-1"
        day = f"2026-10-{1 + index % 9:02d}"
        units.append({"unit_id": unit_id, "product_id": product_id, "variant": {"color": colour},
                      "source_type": "seed", "source_return_case_id": None, "availability": availability})
        if findings is None:
            inspections.append({"inspection_id": inspection_id, "unit_id": unit_id, "type": "intake",
                                "status": "pending", "outcome": None, "recorded_by": None, "recorded_at": None})
            listings.append({"listing_id": f"LS-1{num}", "unit_id": unit_id, "price": price, "currency": CURRENCY,
                             "reference_price": reference, "publication_status": "draft", "published_by": None,
                             "policy_version": PUBLICATION_POLICY["policy_version"]})
            continue
        inspections.append({"inspection_id": inspection_id, "unit_id": unit_id, "type": "intake",
                            "status": "completed", "outcome": "verified", "recorded_by": "SYN-INSPECTOR-01",
                            "recorded_at": f"{day}T10:00:00-04:00"})
        for view in ("front", "back"):
            photos.append(_photo(f"PH-{num}-{view}", inspection_id, unit_id, view, f"{view.title()} of {unit_id}"))
        for name, finding in findings.items():
            is_area = name in ("front", "back")
            base = {"inspection_id": inspection_id, "area": name if is_area else None,
                    "component": None if is_area else name}
            if finding == UNKNOWN:
                continue  # never inspected: no evidence row, so the status stays unknown
            if finding == OK:
                evidence.append({**base, "evidence_id": f"EV-{num}-{name}", "finding": "ok", "defect_type": None,
                                 "severity": None, "photo_id": f"PH-{num}-{name}" if is_area else None})
            elif finding == FAILED:
                evidence.append({**base, "evidence_id": f"EV-{num}-{name}", "finding": "defect", "defect_type": None,
                                 "severity": None, "photo_id": None})
            else:
                for n, (defect_type, severity) in enumerate(finding, start=1):
                    photo_id = f"PH-{num}-detail-{name}-{n}"
                    photos.append(_photo(photo_id, inspection_id, unit_id, f"detail-{name}-{n}",
                                         f"Close-up: {severity} {defect_type.replace('_', ' ')} on the {name}"))
                    evidence.append({**base, "evidence_id": f"EV-{num}-{name}-{n}", "finding": "defect",
                                     "defect_type": defect_type, "severity": severity, "photo_id": photo_id})
        listings.append({"listing_id": f"LS-1{num}", "unit_id": unit_id, "price": price, "currency": CURRENCY,
                         "reference_price": reference, "publication_status": "published",
                         "published_by": "SYN-MERCH-01", "policy_version": PUBLICATION_POLICY["policy_version"]})
    return {"products": [dict(p) for p in PRODUCTS], "units": units, "inspections": inspections,
            "evidence": evidence, "photos": photos, "listings": listings,
            "category_templates": {k: {"areas": list(v["areas"]), "components": list(v["components"])}
                                   for k, v in CATEGORY_TEMPLATES.items()},
            "policy": dict(PUBLICATION_POLICY, required_views=list(PUBLICATION_POLICY["required_views"]))}


def _photo(photo_id: str, inspection_id: str, unit_id: str, view: str, alt: str) -> Dict[str, Any]:
    return {"photo_id": photo_id, "inspection_id": inspection_id, "product_id": None, "return_case_id": None,
            "view": view, "image_key": f"units/{unit_id}/{view}.jpg", "alt": alt, "show_on_listing": True}
