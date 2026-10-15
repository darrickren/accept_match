"""SYNTHETIC fixtures for the UI-only mock backend.

Everything here is made up for the demo: item IDs, defect records, check
results and prices. It is not derived from the raw datasets and will be
replaced by Zikun's ten sample items and business rules. Money values are
illustrative and loosely follow the proposal's net-recovery table (USD).
"""

PRODUCT = {"style_id": "SYN-STYLE-01", "name": "Structured crossbody bag", "colour": "Black"}

CURRENCY = "USD"
CHECK_COST = 3.0  # extra inspection cost per check (proposal: 4 vs 1)
MARKDOWN_PRICE = 45.0  # uniform-markdown resale price (proposal)
REPEAT_RETURN_SAVING = 3.0  # expected repeat-return cost 6 -> 3 (proposal)

PRICING_POLICY = {"rule_id": "PR-01", "floor": 50.0, "ceiling": 70.0}
AFTER_SALES = {
    "rule_id": "AS-01",
    "text": (
        "Applicable after-sales rights are preserved, and the disclosed defects are part of the "
        "item description. (Placeholder wording until the team's policy text is ready.)"
    ),
}

# The controls come from the proposal; these IDs are placeholders.
CONTROLS = {
    "CTL-01": "Unknown never counts as passed",
    "CTL-02": "Humans verify defects and function",
    "CTL-03": "Photos support disclosure",
    "CTL-04": "Merchant pricing limits are enforced",
    "CTL-05": "Reserve only after buyer confirmation",
    "CTL-06": "Applicable after-sales rights are preserved",
    "CTL-07": "Check only when expected extra recovery covers the cost",
    "SYS-01": "Defects the buyer has not accepted count as not accepted (mock assumption)",
}


def _defect(record_id, area, kind, description):
    return {
        "record_id": record_id,
        "area": area,
        "type": kind,
        "severity": "minor",
        "description": description,
        "verified_by": "human",
    }


def _check(status, source_id=None):
    return {"status": status, "source_id": source_id, "verified_by": "human" if source_id else None}


def _unit(item_id, price, defects, front, zipper, status="available"):
    return {
        "item_id": item_id,
        "status": status,
        "match_price": price,
        "defects": defects,
        "checks": {"front_intact": front, "zipper_function": zipper},
    }


UNITS = [
    _unit("AM-001", 55.0, [_defect("DR-001-01", "Front", "scuff", "Minor scuff on front panel")],
          _check("failed", "CHK-001-01"), _check("passed", "CHK-001-02")),
    _unit("AM-002", 58.0, [_defect("DR-002-01", "Back", "scratch", "Minor scratch on back panel")],
          _check("passed", "CHK-002-01"), _check("failed", "CHK-002-02")),
    _unit("AM-003", 47.0, [_defect("DR-003-01", "Back", "scratch", "Minor scratch on back panel")],
          _check("unknown"), _check("unknown")),
    _unit("AM-004", 58.0, [_defect("DR-004-01", "Back", "scratch", "Minor scratch on back panel")],
          _check("passed", "CHK-004-01"), _check("unknown")),
    _unit("AM-005", 60.0, [_defect("DR-005-01", "Strap", "scratch", "Minor scratch on strap")],
          _check("passed", "CHK-005-01"), _check("passed", "CHK-005-02")),
    _unit("AM-006", 55.0, [_defect("DR-006-01", "Back", "scratch", "Minor scratch on back panel"),
                           _defect("DR-006-02", "Back", "stain", "Minor stain on back panel")],
          _check("passed", "CHK-006-01"), _check("passed", "CHK-006-02")),
    _unit("AM-007", 60.0, [_defect("DR-007-01", "Back", "scratch", "Minor scratch on back panel, about 2 cm")],
          _check("passed", "CHK-007-01"), _check("unknown")),
    _unit("AM-008", 60.0, [_defect("DR-008-01", "Back", "scratch", "Minor scratch on back panel")],
          _check("passed", "CHK-008-01"), _check("passed", "CHK-008-02"), status="reserved"),
    _unit("AM-009", 57.0, [_defect("DR-009-01", "Corners", "scuff", "Minor scuff on bottom corner")],
          _check("passed", "CHK-009-01"), _check("passed", "CHK-009-02")),
    _unit("AM-010", 56.0, [_defect("DR-010-01", "Interior", "mark", "Minor pen mark on lining")],
          _check("passed", "CHK-010-01"), _check("passed", "CHK-010-02")),
]
