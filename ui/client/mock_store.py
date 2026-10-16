"""Mock data access: a read-only synthetic catalogue shared by all sessions,
plus a per-session overlay that holds everything a visitor creates.

The catalogue is cached once per process and never mutated; every reader gets
copies. The overlay lives in the visitor's own Streamlit session, so two
browser sessions can never see each other's drafts, photos or returns.
"""

from __future__ import annotations

import copy
import hashlib
import json
from functools import lru_cache
from typing import Any, Dict, List, Optional

from . import seed

DERIVED_VERIFICATION = ("pending", "verified", "mismatch", "inconclusive")


@lru_cache(maxsize=1)
def _catalogue() -> Dict[str, Any]:
    return seed.build()


def catalogue() -> Dict[str, Any]:
    """A private deep copy of the shared catalogue."""
    return copy.deepcopy(_catalogue())


def fingerprint() -> str:
    return hashlib.sha256(json.dumps(_catalogue(), sort_keys=True).encode()).hexdigest()


def new_overlay() -> Dict[str, Any]:
    return {
        "idempotency": {},       # (operation, client_request_id) -> response
        "counters": {"trace": 0, "case": 0, "photo": 0, "upload": 0},
        "uploads": {},           # upload_id -> pending upload (bytes stay here until completed)
        "photos": {},            # photo_id -> accepted return photo (draft or attached to a case)
        "returns": {},           # case_id -> return case
        "audit": [],             # decision summaries and checksums, never photo content
        "photo_bytes": 0,
        "units": {},             # session-only additions (for example a verified return)
        "listings": {},
        "inspections": {},
        "evidence": {},
        "unit_photos": {},
    }


# ----------------------------------------------------------------- lookups

def _products() -> Dict[str, Dict[str, Any]]:
    return {p["product_id"]: p for p in _catalogue()["products"]}


def _rows(table: str, overlay: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows = list(_catalogue()[table])
    extra_key = {"units": "units", "listings": "listings", "inspections": "inspections", "evidence": "evidence",
                 "photos": "unit_photos"}[table]
    if overlay:
        rows += list(overlay.get(extra_key, {}).values())
    return rows


def product(product_id: str) -> Optional[Dict[str, Any]]:
    found = _products().get(product_id)
    return copy.deepcopy(found) if found else None


def unit(unit_id: str, overlay: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    for row in _rows("units", overlay):
        if row["unit_id"] == unit_id:
            return copy.deepcopy(row)
    return None


def listings(overlay: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    return [copy.deepcopy(row) for row in _rows("listings", overlay)]


def listing(listing_id: str, overlay: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    return next((row for row in listings(overlay) if row["listing_id"] == listing_id), None)


def latest_inspection(unit_id: str, overlay: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    done = [i for i in _rows("inspections", overlay) if i["unit_id"] == unit_id and i["status"] == "completed"]
    return copy.deepcopy(max(done, key=lambda i: i["recorded_at"])) if done else None


def verification_status(unit_id: str, overlay: Optional[Dict[str, Any]] = None) -> str:
    """Derived only from the latest completed inspection; never stored on its own."""
    latest = latest_inspection(unit_id, overlay)
    return latest["outcome"] if latest else "pending"


def condition(unit_id: str, overlay: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Condition report built from the latest completed, verified inspection only."""
    record = unit(unit_id, overlay) or {}
    template = _catalogue()["category_templates"][(product(record.get("product_id", "")) or {}).get("category",
                                                                                                    "crossbody")]
    latest = latest_inspection(unit_id, overlay)
    usable = latest if latest and latest["outcome"] == "verified" else None
    rows = [e for e in _rows("evidence", overlay) if usable and e["inspection_id"] == usable["inspection_id"]]
    areas = []
    for area in template["areas"]:
        found = [e for e in rows if e["area"] == area]
        defects = [{"type": e["defect_type"], "severity": e["severity"], "evidence_id": e["evidence_id"],
                    "photo_id": e["photo_id"]} for e in found if e["finding"] == "defect" and e["defect_type"]]
        if defects:
            status = "defect"
        elif any(e["finding"] == "ok" for e in found):
            status = "verified_ok"
        else:
            status = "unknown"
        areas.append({"area": area, "status": status, "defects": defects,
                      "evidence_ids": [e["evidence_id"] for e in found]})
    components = []
    for component in template["components"]:
        found = [e for e in rows if e["component"] == component]
        if any(e["finding"] == "defect" for e in found):
            status = "failed"
        elif any(e["finding"] == "ok" for e in found):
            status = "verified_ok"
        else:
            status = "unknown"
        components.append({"component": component, "status": status,
                           "evidence_ids": [e["evidence_id"] for e in found]})
    return {"areas": areas, "components": components,
            "inspected_at": (usable["recorded_at"][:10] if usable else None),
            "inspection_type": usable["type"] if usable else None,
            "inspection_id": usable["inspection_id"] if usable else None,
            "verification": verification_status(unit_id, overlay)}


def unit_photos(unit_id: str, overlay: Optional[Dict[str, Any]] = None, listing_only: bool = True) -> List[Dict[str, Any]]:
    inspection_ids = {i["inspection_id"] for i in _rows("inspections", overlay) if i["unit_id"] == unit_id}
    photos = [p for p in _rows("photos", overlay) if p.get("inspection_id") in inspection_ids
              and (p.get("show_on_listing") or not listing_only)]
    order = {"front": 0, "back": 1}
    return [copy.deepcopy(p) for p in sorted(photos, key=lambda p: (order.get(p["view"], 2), p["photo_id"]))]


def purchasable(row: Dict[str, Any], overlay: Optional[Dict[str, Any]] = None) -> bool:
    """Computed here (the backend), never by the page."""
    record = unit(row["unit_id"], overlay) or {}
    return row["publication_status"] == "published" and record.get("availability") == "available"


def listing_view(row: Dict[str, Any], overlay: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Contract shape for one listing (without the match block)."""
    record = unit(row["unit_id"], overlay) or {}
    item = product(record.get("product_id", "")) or {}
    return {
        "listing_id": row["listing_id"],
        "unit_id": row["unit_id"],
        "product": {"product_id": item.get("product_id"), "name": item.get("name"), "category": item.get("category"),
                    "color": (record.get("variant") or {}).get("color"), "style": item.get("style"),
                    "season": item.get("season_code"), "collection": item.get("collection")},
        "images": [{"photo_id": p["photo_id"], "image_key": p["image_key"], "url": None, "alt": p["alt"],
                    "view": p["view"]} for p in unit_photos(row["unit_id"], overlay)],
        "price": {"amount": row["price"], "currency": row["currency"], "reference_amount": row.get("reference_price")},
        "availability": {"purchasable": purchasable(row, overlay), "status": record.get("availability")},
        "condition": condition(row["unit_id"], overlay),
    }


# ----------------------------------------------------------------- policy and integrity

def publication_problems(row: Dict[str, Any], overlay: Optional[Dict[str, Any]] = None) -> List[str]:
    """Draft publication policy. Separate from matching on purpose."""
    policy = _catalogue()["policy"]
    problems = []
    if verification_status(row["unit_id"], overlay) != "verified":
        problems.append("unit is not verified by a completed inspection")
    if not policy["price_floor"] <= float(row.get("price") or 0) <= policy["price_ceiling"]:
        problems.append("price outside merchant limits")
    report = condition(row["unit_id"], overlay)
    views = {p["view"] for p in unit_photos(row["unit_id"], overlay)}
    if not set(policy["required_views"]) <= views:
        problems.append("required photos missing")
    if policy["requires_verified_defect"]:
        has_defect = any(a["defects"] for a in report["areas"]) or any(c["status"] == "failed"
                                                                         for c in report["components"])
        if not has_defect:
            problems.append("no verified imperfection: not listed on Buy Defective")
    return problems


def integrity_problems(data: Optional[Dict[str, Any]] = None) -> List[str]:
    """Model constraints that the real database would enforce."""
    data = data or _catalogue()
    problems = []
    unit_ids = [u["unit_id"] for u in data["units"]]
    if len(unit_ids) != len(set(unit_ids)):
        problems.append("duplicate unit ids")
    for photo in data["photos"]:
        owners = [photo.get(k) for k in ("inspection_id", "product_id", "return_case_id")]
        if sum(1 for o in owners if o) != 1:
            problems.append(f"photo {photo['photo_id']} must have exactly one owner")
    for row in data["evidence"]:
        if bool(row.get("area")) == bool(row.get("component")):
            problems.append(f"evidence {row['evidence_id']} needs exactly one of area or component")
    open_listings: Dict[str, int] = {}
    for row in data["listings"]:
        if row["unit_id"] not in unit_ids:
            problems.append(f"listing {row['listing_id']} points to a missing unit")
        if row["publication_status"] != "archived":
            open_listings[row["unit_id"]] = open_listings.get(row["unit_id"], 0) + 1
    problems += [f"unit {u} has more than one open listing" for u, n in open_listings.items() if n > 1]
    for inspection in data["inspections"]:
        if inspection["status"] == "completed" and inspection["outcome"] not in ("verified", "mismatch", "inconclusive"):
            problems.append(f"inspection {inspection['inspection_id']} has no valid outcome")
    for row in data["listings"]:
        if row["publication_status"] == "published":
            problems += [f"{row['listing_id']}: {p}" for p in publication_problems(row)]
    return problems
