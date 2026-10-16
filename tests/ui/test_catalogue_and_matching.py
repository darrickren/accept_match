"""Data model, publication policy and matching semantics (Task 2/4)."""
import copy

import pytest

from ui.client import mock_store as store
from ui.client.contracts import empty_preferences, validate_listing
from ui.client.matching import evaluate


def prefs(categories=("crossbody",), areas=("back",), types=("scratch",), severity="minor", working=("zipper",),
          **extra):
    p = empty_preferences()
    p["category"] = list(categories)
    p["tolerated_defects"] = [{"area": a, "types": list(types), "max_severity": severity} for a in areas]
    p["required_working"] = list(working)
    p.update(extra)
    return p


def view(unit_id):
    row = next(r for r in store.listings() if r["unit_id"] == unit_id)
    return store.listing_view(row)


def test_catalogue_satisfies_model_constraints():
    assert store.integrity_problems() == []


def test_photo_must_have_exactly_one_owner_and_evidence_one_target():
    data = store.catalogue()
    data["photos"][0]["product_id"] = "SYN-CB-01"  # now two owners
    data["evidence"][0]["component"] = "zipper"   # now both area and component
    problems = store.integrity_problems(data)
    assert any("exactly one owner" in p for p in problems)
    assert any("exactly one of area or component" in p for p in problems)


def test_second_open_listing_for_a_unit_is_rejected():
    data = store.catalogue()
    data["listings"].append(dict(data["listings"][0], listing_id="LS-DUP"))
    assert any("more than one open listing" in p for p in store.integrity_problems(data))


def test_verification_is_derived_from_the_latest_completed_inspection():
    assert store.verification_status("AM-007") == "verified"
    assert store.verification_status("AM-009") == "pending"  # inspection still pending
    report = store.condition("AM-009")
    assert {a["status"] for a in report["areas"]} == {"unknown"}


def test_unit_in_intake_is_not_published():
    row = next(r for r in store.listings() if r["unit_id"] == "AM-009")
    assert row["publication_status"] == "draft"


def test_publication_policy_is_separate_from_matching():
    """A clean, verified unit fails the *publication* policy, but matching still accepts it."""
    clean = view("AM-008")
    for area in clean["condition"]["areas"]:
        area.update(status="verified_ok", defects=[])
    assert evaluate(clean, prefs())["level"] == "verified"
    data_row = next(r for r in store.listings() if r["unit_id"] == "AM-008")
    assert store.publication_problems(data_row) == []  # the real AM-008 has a verified scratch


def test_listing_views_carry_a_condition_report_and_valid_images():
    for row in store.listings():
        if row["publication_status"] == "published":
            v = store.listing_view(row)
            v["match"] = {"level": "verified", "reasons": [], "missing_evidence": []}
            assert validate_listing(v) == [], row["listing_id"]


def test_cleaner_published_piece_meets_a_more_tolerant_request():
    tolerant = prefs(types=("scratch", "scuff"), severity="moderate")
    assert evaluate(view("AM-008"), tolerant)["level"] == "verified"


def test_unknown_zipper_is_only_a_possible_match():
    result = evaluate(view("AM-007"), prefs())
    assert result["level"] == "possible"
    assert result["missing_evidence"] == ["Zipper hasn't been checked yet"]


def test_failed_required_check_excludes():
    assert evaluate(view("AM-002"), prefs())["level"] == "excluded"


def test_defect_beyond_maximum_severity_excludes():
    assert evaluate(view("AM-004"), prefs(severity="minor"))["level"] == "excluded"
    assert evaluate(view("AM-004"), prefs(severity="moderate"))["level"] != "excluded"


def test_extra_unaccepted_defect_excludes():
    result = evaluate(view("AM-010"), prefs())
    assert result["level"] == "excluded"
    assert any("stain" in e for e in result["exclusions"])


def test_defect_on_an_area_not_accepted_excludes():
    assert evaluate(view("AM-001"), prefs())["level"] == "excluded"  # scuff on the front


def test_unrequired_component_does_not_affect_level():
    v = view("AM-007")
    assert evaluate(v, prefs(working=()))["level"] == "verified"


def test_unknown_area_is_never_verified():
    v = view("AM-008")
    v["condition"]["areas"][0].update(status="unknown", defects=[])
    assert evaluate(v, prefs())["level"] == "possible"


def test_seed_is_not_mutated_by_readers():
    before = store.fingerprint()
    v = view("AM-007")
    v["condition"]["areas"].clear()
    store.catalogue()["units"].clear()
    assert store.fingerprint() == before
