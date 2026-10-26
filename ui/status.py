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


# ----------------------------------------------------------------- api mode (FastAPI return statuses)

DEMO_DISCLAIMER = "This is not an actual ALDO refund approval. No refund, shipping label or payment is created."
AI_OBSERVATION = "Photo analysis is an observation that supports the review. It is not a final decision."
API_RETURN_LABELS = {
    "DRAFT": "Collecting details",
    "INFO_COLLECTION": "Collecting details",
    "NEEDS_MORE_EVIDENCE": "Action needed · more photos",
    "APPROVED_DEMO": "Approved in the demo review · send your item",
    "HUMAN_REVIEW_REQUIRED": "With a specialist (simulated review)",
    "REJECTED_AFTER_REVIEW": "Not accepted after review (demo)",
}
API_AFTER_APPROVAL = {
    "AWAITING_RECEIPT": "Approved in the demo review · send your item",
    "INSPECTION_PENDING": "Received · inspection pending",
    "AVAILABLE": "Inspection complete",
    "QUARANTINED": "Inspection complete",
    "SOLD": "Inspection complete",
}


def api_return_status(view: Dict[str, Any]) -> Optional[str]:
    """Customer label for a backend return, or None when the combination can't be trusted.

    The resale inventory record only exists after APPROVED_DEMO, so an inventory
    status on any other return status is treated as untrustworthy.
    """
    status, inventory = view.get("status"), view.get("inventory_status")
    if status not in API_RETURN_LABELS:
        return None
    if status == "APPROVED_DEMO":
        return API_RETURN_LABELS[status] if inventory is None else API_AFTER_APPROVAL.get(inventory)
    return None if inventory is not None else API_RETURN_LABELS[status]


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
