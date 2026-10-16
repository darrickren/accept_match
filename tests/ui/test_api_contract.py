"""api mode, pure parts: contract checks, mapping, error mapping, config and status labels.

Fixtures in fixtures/api/ are frontend-authored from the plan's section 6
examples (the backend hasn't published any yet).
"""
import copy
import json
from pathlib import Path

import pytest

from ui.client import api_mapper as mapper
from ui.client.api_schemas import chat_problems, item_problems, return_problems
from ui.client.http_api import map_error, redact
from ui.config import load_settings
from ui.status import DEMO_DISCLAIMER, api_return_status

FIXTURES = Path(__file__).parent / "fixtures" / "api"


def fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


# ----------------------------------------------------------------- valid fixtures

def test_shopping_candidates_map_to_footwear_cards_in_cad():
    body = fixture("shopping-candidates")
    assert chat_problems(body) == []
    chat = mapper.chat_view(body)
    assert chat["next_action"] == "SHOW_ITEMS" and chat["analysis_source"] == "SIMULATED"
    boots, sneakers = chat["items"]
    assert boots["size_label"] == "US women's 7" and boots["price"] == 69.0 and boots["currency"] == "CAD"
    assert boots["grade_label"] == "Grade A" and boots["purchasable"] is True and boots["listing_version"] == 1
    assert boots["defects"][0]["defect_id"] == "D-001" and boots["defects"][0]["location"] == "back heel"
    assert mapper.defect_line(boots["defects"][0]) == "Scratch · back heel (grade A)"
    assert boots["match_status"] == "MATCH"
    assert sneakers["match_status"] == "NEEDS_CONFIRMATION" and sneakers["pending_confirmations"]
    assert chat["notes"] == []


def test_no_match_keeps_blocking_constraints_and_the_relax_question():
    chat = mapper.chat_view(fixture("shopping-no-match"))
    assert chat["items"] == [] and chat["blocking_constraints"] == ["Size EU 36", "Under CA$80", "No stains"]
    question = chat["questions"][0]
    assert question["text"] == "Would you consider up to CA$120?"
    assert [o["label"] for o in question["options"]] == ["Yes, up to CA$120", "No, keep CA$80"]


def test_needs_more_evidence_lists_requested_shoe_views_with_reasons():
    body = fixture("return-needs-more-evidence")
    assert chat_problems(body) == []
    slots = mapper.chat_view(body)["required_photos"]
    assert [s["view"] for s in slots] == ["pair_soles", "size_label"]
    assert slots[0]["label"] == "Soles" and slots[0]["reason"] == "Both soles aren't visible."


def test_approved_return_is_a_demo_result_with_the_backend_address():
    body = fixture("return-approved")
    assert return_problems(body) == []
    view = mapper.return_view(body)
    assert view["demo_address"][0] == "FICTIONAL DEMO ADDRESS — DO NOT SHIP"
    assert view["final_merchant_decision"] is False and view["inventory_label"] == "Waiting for the item to arrive"
    assert api_return_status(view) == "Approved in the demo review · send your item"
    assert "not an actual ALDO refund approval" in DEMO_DISCLAIMER


def test_checkout_fixture_maps_to_a_cad_purchase():
    view = mapper.purchase_view(fixture("checkout-success"))
    assert view["purchase_id"] == "PUR-001" and view["amount"] == 69.0 and view["payment"] == "SIMULATED"


# ----------------------------------------------------------------- malformed payloads fail closed

def test_chat_response_with_missing_or_mistyped_fixed_keys_is_rejected():
    body = fixture("shopping-candidates")
    for key in ("next_action", "items", "request_id", "can_purchase"):
        broken = copy.deepcopy(body)
        del broken[key]
        assert chat_problems(broken), key
    for key, value in (("next_action", "BUY_NOW"), ("items", None), ("can_purchase", "yes"),
                       ("return_status", "REFUNDED"), ("mode", "admin")):
        broken = dict(copy.deepcopy(body), **{key: value})
        assert chat_problems(broken), key
    assert chat_problems(dict(copy.deepcopy(body), next_action="UPLOAD_PHOTOS"))  # needs required_photos
    assert chat_problems("not json object")


@pytest.mark.parametrize("change", [
    {"price_cents": 69.0}, {"price_cents": -1}, {"currency": "USD"}, {"grade": "A+"}, {"status": "LISTED"},
    {"listing_version": 0}, {"size_system": None}, {"size_value": ""}, {"defects": "scratch"},
    {"title": ""},
])
def test_invalid_items_are_dropped_with_a_note(change):
    item = dict(copy.deepcopy(fixture("shopping-candidates")["items"][0]), **change)
    assert item_problems(item)
    views, notes = mapper.screen_items([item])
    assert views == [] and notes


def test_invalid_defects_and_duplicate_ids_drop_the_item():
    item = copy.deepcopy(fixture("shopping-candidates")["items"][0])
    bad_type = copy.deepcopy(item)
    bad_type["defects"][0]["type"] = "hole"
    duplicate = copy.deepcopy(item)
    duplicate["defects"].append(copy.deepcopy(duplicate["defects"][0]))
    for broken in (bad_type, duplicate):
        assert mapper.screen_items([broken])[0] == []


def test_grade_c_listed_as_available_is_shown_but_never_purchasable():
    item = dict(copy.deepcopy(fixture("shopping-candidates")["items"][0]), grade="C")
    views, notes = mapper.screen_items([item])
    assert views[0]["purchasable"] is False and "purchase disabled" in notes[0]


def test_a_match_with_pending_confirmations_is_downgraded():
    item = copy.deepcopy(fixture("shopping-candidates")["items"][0])
    item["pending_confirmations"] = ["You haven't said whether scuffs are OK"]
    views, notes = mapper.screen_items([item])
    assert views[0]["match_status"] == "NEEDS_CONFIRMATION" and notes


def test_unknown_photo_views_get_generic_guidance_and_a_note():
    body = copy.deepcopy(fixture("return-needs-more-evidence"))
    body["required_photos"].append({"view": "box_label", "reason": None})
    chat = mapper.chat_view(body)
    assert chat["required_photos"][-1]["known"] is False and chat["notes"]


def test_returns_need_an_id_a_known_status_and_missing_views_when_more_evidence_is_needed():
    assert return_problems({"status": "APPROVED_DEMO"})
    assert return_problems({"return_id": "RET-1", "status": "REFUND_ISSUED"})
    assert return_problems({"return_id": "RET-1", "status": "NEEDS_MORE_EVIDENCE"})
    assert return_problems({"return_id": "RET-1", "status": "APPROVED_DEMO", "inventory_status": "LISTED"})


# ----------------------------------------------------------------- error contract

def error(name):
    return fixture(name)


@pytest.mark.parametrize("status,code,expected,retryable", [
    (409, "STALE_LISTING_VERSION", "stale_listing", False),
    (409, "INCOMPLETE_ACCEPTANCES", "incomplete_acceptances", False),
    (409, "ITEM_UNAVAILABLE", "item_unavailable", False),
    (502, "AI_OUTPUT_INVALID", "ai_output_invalid", True),
    (503, "AI_UNAVAILABLE", "agent_unavailable", True),
    (504, "AGENT_TIMEOUT", "agent_timeout", True),
    (413, "PAYLOAD_TOO_LARGE", "photo_rejected", False),
    (401, "UNAUTHORIZED", "unauthorized", False),
    (403, "FORBIDDEN", "forbidden", False),
    (400, "BAD_INPUT", "invalid_input", False),
])
def test_status_and_backend_codes_map_to_ui_codes(status, code, expected, retryable):
    body = {"request_id": "req-9", "code": code, "message": "x", "detail": None}
    err = map_error(status, body, {})
    assert err.code == expected and err.retryable is retryable and err.request_id == "req-9"
    assert err.backend_code == code and err.status == status


def test_backend_retryable_flag_wins_and_retry_after_is_read():
    err = map_error(429, {"request_id": "r", "code": "RATE_LIMITED", "message": "slow down", "retryable": True},
                    {"retry-after": "1.5"})
    assert err.code == "rate_limited" and err.retry_after == 1.5
    assert map_error(503, {"request_id": "r", "code": "DB_DOWN", "message": "x", "retryable": False}, {}).retryable is False


def test_technical_backend_messages_are_never_shown_to_customers():
    err = map_error(503, error("ai-unavailable"), {})
    assert "arn:aws" not in err.message and "Bedrock" not in err.message
    assert err.message.startswith("The AcceptMatch service is unavailable")
    shown = map_error(404, {"request_id": "r", "code": "ORDER_NOT_FOUND", "message": "Order ORD-404 was not found."}, {})
    assert shown.code == "not_found" and shown.message == "Order ORD-404 was not found."
    hidden = map_error(400, {"request_id": "r", "code": "X", "message": "Traceback (most recent call last): ..."}, {})
    assert "Traceback" not in hidden.message


def test_missing_routes_mean_pending_integration_not_missing_records():
    assert map_error(404, {"detail": "Not Found"}, {}).code == "not_implemented"
    assert map_error(405, {"detail": "Method Not Allowed"}, {}).code == "not_implemented"
    assert map_error(404, {"detail": "Return not found"}, {}).code == "not_found"


def test_fastapi_wrapped_errors_are_unwrapped():
    err = map_error(409, {"detail": error("checkout-version-conflict")}, {})
    assert err.code == "stale_listing" and err.request_id == "req-202"


def test_request_validation_errors_keep_field_locations_but_never_input_values():
    body = {"detail": [{"loc": ["body", "password"], "msg": "too short", "type": "value_error",
                        "input": "hunter2-secret"}]}
    err = map_error(422, body, {})
    assert err.code == "contract_violation" and "body.password" in err.note and "hunter2" not in err.note


def test_redirects_are_contract_violations():
    assert map_error(302, None, {"location": "https://evil.example/"}).code == "contract_violation"


def test_redact_removes_tokens_and_known_secrets():
    text = redact("Authorization: Bearer abc.def-123 and eyJhbGciOi.eyJzdWIiOiJ4.c2lnbmF0dXJl pw-cust-02",
                  ("pw-cust-02",))
    assert "abc.def-123" not in text and "eyJzdWIi" not in text and "pw-cust-02" not in text


# ----------------------------------------------------------------- request bodies follow plan section 6

def test_request_bodies_match_the_plan_exactly():
    assert mapper.acceptance_body(1, ["D-001", "D-002"]) == {
        "listing_version": 1, "acceptances": [{"defect_id": "D-001", "accepted": True},
                                              {"defect_id": "D-002", "accepted": True}]}
    assert mapper.checkout_body("ITM-001", 1, "k") == {"item_id": "ITM-001", "listing_version": 1,
                                                       "idempotency_key": "k"}
    assert mapper.message_body("hi", [], "cr_1") == {"text": "hi", "photo_ids": [], "client_request_id": "cr_1"}
    assert set(mapper.return_body("s", "ORD-001", "1", "DEFECT_CLAIM", "defect")) == {
        "session_id", "order_id", "order_line", "claim_type", "reason"}
    assert mapper.evaluate_body("cr_2") == {"client_request_id": "cr_2"}
    assert mapper.items_query({"category": "SHOES", "size_value": "", "max_price_cents": 10000}, "c2") == {
        "category": "SHOES", "max_price_cents": 10000, "cursor": "c2"}


# ----------------------------------------------------------------- labels: real AI is never assumed

def test_analysis_labels_only_claim_ai_with_a_real_source_and_a_model():
    assert mapper.analysis_label(None, None) == ("AcceptMatch service", "demo")
    assert mapper.analysis_label("SIMULATED", "model-x")[0] == "Live service · simulated AI"
    assert mapper.analysis_label("BEDROCK", None)[0] == "Live service · AI not confirmed"
    label, tone = mapper.analysis_label("BEDROCK", "us.amazon.nova-2-lite-v1:0")
    assert label == "AI-assisted · us.amazon.nova-2-lite-v1" and tone == "verified"


@pytest.mark.parametrize("status,inventory,label", [
    ("NEEDS_MORE_EVIDENCE", None, "Action needed · more photos"),
    ("HUMAN_REVIEW_REQUIRED", None, "With a specialist (simulated review)"),
    ("REJECTED_AFTER_REVIEW", None, "Not accepted after review (demo)"),
    ("APPROVED_DEMO", "INSPECTION_PENDING", "Received · inspection pending"),
    ("APPROVED_DEMO", "QUARANTINED", "Inspection complete"),
    ("REJECTED_AFTER_REVIEW", "AVAILABLE", None),  # inventory only exists after approval
    ("REFUNDED", None, None),
])
def test_return_status_labels(status, inventory, label):
    assert api_return_status({"status": status, "inventory_status": inventory}) == label


# ----------------------------------------------------------------- configuration

@pytest.mark.parametrize("url,expected", [
    ("https://api.example.com/", "https://api.example.com"),
    ("https://api.example.com/acceptmatch", "https://api.example.com/acceptmatch"),
    ("http://localhost:8000", "http://localhost:8000"),
    ("http://127.0.0.1:8000/", "http://127.0.0.1:8000"),
    ("http://api.example.com", None),
    ("https://user:pass@api.example.com", None),
    ("https://api.example.com/?token=x", None),
    ("ftp://api.example.com", None),
])
def test_api_base_url_validation(url, expected):
    settings = load_settings({"ACCEPTMATCH_API_BASE_URL": url})
    assert settings.api_base_url == expected
    if expected is None:
        assert settings.warnings and url not in " ".join(settings.warnings)


def test_secrets_never_appear_in_settings_repr_and_personas_resolve_per_journey():
    settings = load_settings({"ACCEPTMATCH_API_TOKEN": "tok-secret-123", "ACCEPTMATCH_DEMO_PASSWORD": "pw-shared",
                              "ACCEPTMATCH_DEMO_PERSONA": "cust-01", "ACCEPTMATCH_DEMO_PERSONA_SHOP": "cust-02",
                              "ACCEPTMATCH_DEMO_PASSWORD_SHOP": "pw-cust-02", "ACCEPTMATCH_API_TIMEOUT": "12",
                              "ACCEPTMATCH_API_MAX_RETRIES": "0", "ACCEPTMATCH_IMAGE_HOSTS": "img.example.com, bad host"})
    text = repr(settings)
    assert "tok-secret-123" not in text and "pw-shared" not in text and "pw-cust-02" not in text
    assert settings.persona_for("shop") == "cust-02" and settings.password_for("shop") == "pw-cust-02"
    assert settings.persona_for("return") == "cust-01" and settings.password_for("return") == "pw-shared"
    assert settings.api_timeout_s == 12.0 and settings.api_max_retries == 0
    assert settings.image_hosts == ("img.example.com",) and settings.warnings


def test_backend_text_in_widget_labels_cannot_render_links_images_or_latex():
    from ui.kit import md_safe

    assert md_safe("[win](https://evil.example)") == r"\[win\]\(https://evil.example\)"
    assert md_safe("![x](https://tracker.example/p.gif)").startswith(r"\!\[x\]")
    assert md_safe("Under CA$100 or $200") == r"Under CA\$100 or \$200"
    assert md_safe("1. first") == r"1\. first" and md_safe("- item") == r"\- item"
    assert md_safe("Scratch · back heel (grade A)") == r"Scratch · back heel \(grade A\)"
