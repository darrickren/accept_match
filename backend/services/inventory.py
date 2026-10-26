"""Inventory: one record per physical unit, shared by every customer session.

Checkout is a single DynamoDB transaction: every acceptance for the current
listing version must exist, the unit must still be AVAILABLE at that version
and price, and the purchase record must not exist yet. The same idempotency
key always returns the same purchase; two customers racing get one success
and one 409 ITEM_UNAVAILABLE.
"""

from __future__ import annotations

import base64
import hashlib
from typing import Any, Dict, List, Optional, Tuple

from ..agent.preferences import Match, Preferences, match, rank
from ..errors import ApiError, conflict, not_found
from ..repository import (ConditionFailed, Repository, TransactionCancelled, accept_key, inv_key, now_iso,
                          purchase_key, serialize)

PUBLIC_STATUSES = ("AVAILABLE", "SOLD")
SEARCH_FILTERS = ("category", "size_system", "size_value", "color", "min_price_cents", "max_price_cents",
                  "defect_type", "max_grade")


def public(item: Dict[str, Any], found: Optional[Match] = None) -> Dict[str, Any]:
    """ItemPublic: no order, return, customer, reviewer or storage details."""
    view = {k: item.get(k) for k in ("item_id", "title", "description", "category", "shoe_type", "size_system",
                                     "size_value", "color", "price_cents", "currency", "grade", "status",
                                     "listing_version")}
    view["size_value"] = str(item.get("size_value")) if item.get("size_value") is not None else None
    view["currency"] = "CAD"
    view["defects"] = [{k: d.get(k) for k in ("defect_id", "type", "severity", "location", "description",
                                               "evidence_photo_ids", "uncertainty", "verified_by")}
                       for d in item.get("defects") or []]
    for defect in view["defects"]:
        defect["evidence_photo_ids"] = defect["evidence_photo_ids"] or []
    view["photos"] = [{"photo_id": p["photo_id"], "url": f"/api/items/{item['item_id']}/photos/{p['photo_id']}",
                       "alt": p.get("alt"), "view": p.get("view")} for p in item.get("photos") or []]
    if found is not None and found.status != "EXCLUDED":
        view["match_status"] = found.status
        view["match_reasons"] = found.reasons[:6]
        view["pending_confirmations"] = found.pending[:6]
    return view


def _cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(f"o:{offset}".encode()).decode().rstrip("=")


def _offset(cursor: Optional[str]) -> int:
    if not cursor:
        return 0
    try:
        text = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        if text.startswith("o:") and text[2:].isdigit():
            return int(text[2:])
    except (ValueError, UnicodeDecodeError):
        pass
    raise ApiError(400, "INVALID_CURSOR", "That page link is no longer valid.")


class InventoryService:
    def __init__(self, repo: Repository, settings: Any) -> None:
        self.repo = repo
        self.settings = settings

    # ------------------------------------------------------------ reads

    def all_rows(self) -> List[Dict[str, Any]]:
        rows = self.repo.query("INV", "ITEM#", limit=self.settings.max_inventory_rows + 1)
        if len(rows) > self.settings.max_inventory_rows:
            raise ApiError(503, "SEARCH_LIMIT_EXCEEDED", "The catalogue is too large for this demo search.", True)
        return rows

    def get_row(self, item_id: str) -> Dict[str, Any]:
        row = self.repo.get(inv_key(item_id))
        if not row:
            raise not_found("ITEM_NOT_FOUND", "This piece isn't listed.")
        return row

    def get_public(self, item_id: str) -> Dict[str, Any]:
        row = self.get_row(item_id)
        if row.get("status") not in PUBLIC_STATUSES:
            raise not_found("ITEM_NOT_FOUND", "This piece isn't listed.")
        return public(row)

    def sellable(self) -> List[Dict[str, Any]]:
        return [r for r in self.all_rows() if r.get("status") == "AVAILABLE" and r.get("resale_eligible")
                and r.get("grade") in self.settings.rules.get("resale_grades", ["A", "B"])]

    def search(self, filters: Dict[str, Any], cursor: Optional[str], limit: int = 12) -> Tuple[List[Dict], Optional[str]]:
        rows = self.sellable()
        f = {k: v for k, v in filters.items() if v not in (None, "")}
        out = []
        for row in rows:
            if f.get("category") and str(row.get("category", "")).upper() != str(f["category"]).upper():
                continue
            if f.get("size_system") and row.get("size_system") != f["size_system"]:
                continue
            if f.get("size_value") and str(row.get("size_value")) != str(f["size_value"]):
                continue
            if f.get("color") and str(row.get("color", "")).lower() != str(f["color"]).lower():
                continue
            if f.get("min_price_cents") is not None and row["price_cents"] < int(f["min_price_cents"]):
                continue
            if f.get("max_price_cents") is not None and row["price_cents"] > int(f["max_price_cents"]):
                continue
            if f.get("defect_type") and f["defect_type"] not in {d["type"] for d in row.get("defects") or []}:
                continue
            if f.get("max_grade") == "A" and row.get("grade") != "A":
                continue
            out.append(row)
        out.sort(key=lambda r: (r["price_cents"], r["item_id"]))
        start = _offset(cursor)
        page = out[start:start + limit]
        nxt = _cursor(start + limit) if start + limit < len(out) else None
        return [public(r) for r in page], nxt

    def find_for(self, prefs: Preferences) -> List[Tuple[Dict[str, Any], Match]]:
        """Deterministic matching for the agent tools: hard filters exclude, UNKNOWN needs confirmation."""
        rows = [(row, match(row, prefs)) for row in self.sellable()]
        return rank([(row, found) for row, found in rows if found.status != "EXCLUDED"])

    def catalogue_facts(self, prefs: Optional[Preferences] = None) -> Dict[str, Any]:
        """What is actually in stock, so questions only offer real options."""
        rows = self.sellable()
        if prefs is not None:
            relaxed = prefs.model_copy(update={"size": None, "max_price_cents": None})
            rows = [r for r in rows if match(r, relaxed).status != "EXCLUDED"]
        sizes = sorted({(r["size_system"], str(r["size_value"])) for r in rows},
                       key=lambda s: (["US_WOMEN", "US_MEN", "EU"].index(s[0]), float(s[1])))
        return {"sizes": sizes, "shoe_types": sorted({r["shoe_type"] for r in self.sellable()}),
                "prices": sorted(r["price_cents"] for r in rows),
                "defect_types": sorted({d["type"] for r in rows for d in r.get("defects") or []}), "count": len(rows)}

    # ------------------------------------------------------------ acceptances and checkout

    def accept(self, user: str, item_id: str, listing_version: int, acceptances: List[Dict[str, Any]],
               request_id: str) -> Dict[str, Any]:
        row = self.get_row(item_id)
        if row.get("status") != "AVAILABLE":
            raise conflict("ITEM_UNAVAILABLE", "This piece is no longer available.")
        if int(row["listing_version"]) != listing_version:
            raise conflict("STALE_LISTING_VERSION", "The listing changed. Review the imperfections and confirm again.",
                           {"current_listing_version": int(row["listing_version"])})
        defects = {d["defect_id"]: d for d in row.get("defects") or []}
        for entry in acceptances:
            if entry["defect_id"] not in defects:
                raise ApiError(422, "UNKNOWN_DEFECT", "One of the confirmations doesn't match this listing.")
            if entry["accepted"] is not True:
                raise ApiError(422, "ACCEPTANCE_REQUIRED", "Each imperfection must be explicitly accepted.")
        stamp = now_iso()
        for entry in acceptances:
            defect = defects[entry["defect_id"]]
            snapshot = hashlib.sha256(f"{defect['type']}|{defect.get('location')}|{defect.get('description')}|"
                                      f"{defect['severity']}".encode()).hexdigest()[:16]
            self.repo.put({"PK": accept_key(user, item_id, listing_version, entry["defect_id"])[0],
                           "SK": accept_key(user, item_id, listing_version, entry["defect_id"])[1],
                           "entity_type": "defect_acceptance", "accepted": True, "accepted_at": stamp,
                           "snapshot_text_hash": snapshot, "user": user, "item_id": item_id,
                           "listing_version": listing_version, "dataset_id": "runtime"})
        stored = self.repo.query(f"ACCEPT#{user}#{item_id}#v{listing_version}", "DEFECT#")
        accepted = sorted(r["SK"].split("#", 1)[1] for r in stored if r.get("accepted") is True)
        self.repo.audit(f"ITEM#{item_id}", "defects_accepted", {"user": user, "version": listing_version,
                                                                "defects": accepted}, user)
        return {"item_id": item_id, "listing_version": listing_version, "accepted_defect_ids": accepted,
                "complete": set(accepted) >= set(defects), "request_id": request_id}

    def checkout(self, user: str, item_id: str, listing_version: int, idempotency_key: str,
                 header_key: Optional[str], request_id: str) -> Dict[str, Any]:
        if not header_key or header_key != idempotency_key:
            raise ApiError(400, "IDEMPOTENCY_KEY_MISMATCH", "The Idempotency-Key header must match idempotency_key.")
        existing = self.repo.get(purchase_key(user, idempotency_key))
        if existing:
            return self._replay(existing, item_id, request_id)
        row = self.get_row(item_id)
        if row.get("status") != "AVAILABLE" or not row.get("resale_eligible") or row.get("grade") not in ("A", "B"):
            raise conflict("ITEM_UNAVAILABLE", "This piece is no longer available.")
        if int(row["listing_version"]) != listing_version:
            raise conflict("STALE_LISTING_VERSION", "The listing changed. Review the imperfections and confirm again.",
                           {"current_listing_version": int(row["listing_version"])})
        defect_ids = [d["defect_id"] for d in row.get("defects") or []]
        if len(defect_ids) > int(self.settings.rules.get("max_public_defects", 8)):
            raise conflict("NEEDS_STAFF", "This piece needs a staff member to complete the sale.")
        accepted = {r["SK"].split("#", 1)[1] for r in self.repo.query(f"ACCEPT#{user}#{item_id}#v{listing_version}",
                                                                      "DEFECT#") if r.get("accepted") is True}
        missing = [d for d in defect_ids if d not in accepted]
        if missing:
            raise conflict("INCOMPLETE_ACCEPTANCES", "Every imperfection must be accepted before buying.",
                           {"missing_defect_ids": missing})
        number = self.repo.next_id("purchase", 1)
        purchase = {"purchase_id": f"PUR-{number:04d}", "user_id": user, "item_id": item_id,
                    "listing_version": listing_version, "amount_cents": int(row["price_cents"]), "currency": "CAD",
                    "status": "CONFIRMED_DEMO", "payment": "SIMULATED", "shipping": "SIMULATED",
                    "created_at": now_iso(), "idempotency_key": idempotency_key, "dataset_id": "runtime",
                    "entity_type": "demo_purchase"}
        pk, sk = purchase_key(user, idempotency_key)
        ops: List[Dict[str, Any]] = []
        for defect_id in defect_ids:
            apk, ask = accept_key(user, item_id, listing_version, defect_id)
            ops.append({"ConditionCheck": {"Key": {"PK": apk, "SK": ask}, "ConditionExpression": "accepted = :t",
                                           "ExpressionAttributeValues": {":t": True}}})
        ipk, isk = inv_key(item_id)
        ops.append({"Update": {"Key": {"PK": ipk, "SK": isk},
                               "UpdateExpression": "SET #s = :sold, sold_at = :at, sold_to = :u",
                               "ConditionExpression": "#s = :available AND listing_version = :v AND price_cents = :p "
                                                      "AND resale_eligible = :t",
                               "ExpressionAttributeNames": {"#s": "status"},
                               "ExpressionAttributeValues": {":sold": "SOLD", ":available": "AVAILABLE",
                                                             ":v": listing_version, ":p": int(row["price_cents"]),
                                                             ":t": True, ":at": purchase["created_at"], ":u": user}}})
        ops.append({"Put": {"Item": {"PK": pk, "SK": sk, **purchase}, "ConditionExpression": "attribute_not_exists(PK)"}})
        try:
            self.repo.transact(ops)
        except TransactionCancelled as exc:
            again = self.repo.get(purchase_key(user, idempotency_key))
            if again:  # the same request won a race with itself (retry)
                return self._replay(again, item_id, request_id)
            fresh = self.repo.get(inv_key(item_id)) or {}
            if fresh.get("status") != "AVAILABLE":
                raise conflict("ITEM_UNAVAILABLE", "This piece is no longer available.") from None
            if int(fresh.get("listing_version", 0)) != listing_version:
                raise conflict("STALE_LISTING_VERSION", "The listing changed. Review the imperfections and confirm "
                               "again.", {"current_listing_version": int(fresh.get("listing_version", 0))}) from None
            raise conflict("INCOMPLETE_ACCEPTANCES", "Every imperfection must be accepted before buying.",
                           {"reasons": exc.reasons}) from None
        self.repo.audit(f"ITEM#{item_id}", "sold", {"purchase_id": purchase["purchase_id"], "user": user,
                                                    "version": listing_version}, user)
        return self._view(purchase, request_id)

    def _replay(self, purchase: Dict[str, Any], item_id: str, request_id: str) -> Dict[str, Any]:
        if purchase["item_id"] != item_id:
            raise conflict("IDEMPOTENCY_KEY_REUSED", "This checkout key was already used for another piece.")
        return self._view(purchase, request_id)

    @staticmethod
    def _view(purchase: Dict[str, Any], request_id: str) -> Dict[str, Any]:
        return {k: purchase[k] for k in ("purchase_id", "item_id", "listing_version", "amount_cents", "currency",
                                         "status", "payment", "shipping", "created_at")} | {"request_id": request_id}


__all__ = ["InventoryService", "public", "SEARCH_FILTERS", "serialize", "ConditionFailed"]
