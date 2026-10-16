"""Front-end safety net for Agent responses (defensive, not a substitute for backend rules).

- Shape problems in a listing are contract violations; the listing is dropped.
- A "verified" claim the evidence doesn't support is shown as a possible match.
- A listing our rules would exclude is not shown as a match.
Every correction is recorded for diagnostics.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List

from . import state
from .client.contracts import validate_listing
from .client.matching import evaluate

SHOP = "shop"


def check_listing(listing: Dict[str, Any], preferences: Dict[str, Any], allow_excluded: bool = False):
    """Return (listing or None, list of violation notes)."""
    notes: List[str] = []
    problems = validate_listing(dict(listing, match=dict(listing.get("match") or {}, level=(
        "possible" if (listing.get("match") or {}).get("level") == "excluded" and allow_excluded
        else (listing.get("match") or {}).get("level")))))
    if problems:
        return None, problems
    fixed = copy.deepcopy(listing)
    ours = evaluate(fixed, preferences)
    claimed = fixed["match"].get("level")
    if ours["level"] == "excluded":
        if not allow_excluded:
            return None, [f"{fixed['unit_id']}: returned as a match but violates the preferences"]
        fixed["match"] = dict(fixed["match"], level="excluded", exclusions=ours["exclusions"])
    elif claimed == "verified" and ours["level"] == "possible":
        fixed["match"] = dict(fixed["match"], level="possible",
                              missing_evidence=sorted(set(fixed["match"].get("missing_evidence") or [])
                                                      | set(ours["missing_evidence"])))
        notes.append(f"{fixed['unit_id']}: claimed verified without the required evidence; shown as possible")
    return fixed, notes


def screen_results(result: Dict[str, Any], preferences: Dict[str, Any]) -> Dict[str, Any]:
    kept = []
    for listing in result.get("results") or []:
        fixed, notes = check_listing(listing, preferences)
        for note in notes:
            state.record_violation(SHOP, "contract_violation", note)
        if fixed is not None:
            kept.append(fixed)
    kept.sort(key=lambda r: 0 if r["match"]["level"] == "verified" else 1)  # verified always first
    screened = dict(result, results=kept, total=len(kept))
    screened["counts"] = {lvl: sum(1 for r in kept if r["match"]["level"] == lvl) for lvl in ("verified", "possible")}
    return screened


def screen_listing(result: Dict[str, Any], preferences: Dict[str, Any]) -> Dict[str, Any]:
    listing = result.get("listing") or {}
    fixed, notes = check_listing(listing, preferences, allow_excluded=True)
    for note in notes:
        state.record_violation(SHOP, "contract_violation", note)
    if fixed is None:
        return {"listing_id": listing.get("listing_id"), "missing": True}
    return dict(result, listing=fixed)
