"""Customer-facing return status, derived from the backend's separate status fields.

Pure functions. Any combination not in the table is treated as untrustworthy:
the page shows a neutral message and records a contract violation.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

NEUTRAL = "This status isn't available right now. Please check back later or contact support."
NEXT_UPDATE = "Inspection complete · check this status page for the next update."

TABLE = {
    ("pending", "not_started", None): "Reviewing your request",
    ("needs_more_info", "not_started", None): "Action needed",
    ("manual_review", "not_started", None): "Under review",
    ("rejected", "not_started", None): "Not accepted",
    ("approved", "awaiting_receipt", None): "Approved · ship your item",
    ("approved", "received", None): "Received · inspecting",
    ("approved", "received", "pending"): "Received · inspecting",
    ("approved", "received", "verified"): "Inspection complete",
    ("approved", "received", "mismatch"): NEXT_UPDATE,
    ("approved", "received", "inconclusive"): NEXT_UPDATE,
    ("approved", "expired", None): "Return window closed",
}


def customer_status(case: Dict[str, Any]) -> Optional[str]:
    """Customer label, or None if the combination (or the shipping data) can't be trusted."""
    key = (case.get("assessment_status"), case.get("logistics_status"), case.get("verification_result"))
    label = TABLE.get(key)
    if label is None:
        return None
    if case.get("shipping") and case.get("assessment_status") != "approved":
        return None  # shipping instructions only ever exist for approved returns
    if case.get("assessment_status") == "approved" and case.get("logistics_status") == "awaiting_receipt" \
            and not case.get("shipping"):
        return None
    return label
