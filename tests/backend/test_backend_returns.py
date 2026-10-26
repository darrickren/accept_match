"""Real FastAPI app + DynamoDB Local: uploads, return evaluation, admin lifecycle, AI failures, persistence."""

import io
import uuid

import pytest
from conftest import env_for, free_port, frontend_checks, login, shoe_photo, start_ddb
from PIL import Image

VIEWS = ["pair_top", "pair_outer", "pair_inner", "pair_soles", "pair_heels", "defect_closeup", "size_label"]
DEFECT_TEXT = ("Return request for demo order {order}, line 1. Type: defect claim. Reason: a defect or damage. "
               "What happened: {what}")


def start_return(client, headers, order="ORD-001", what="There's a scratch on the back of the left heel. I noticed "
                 "it the first time I wore them indoors. They haven't been repaired.", claim="defect claim"):
    sid = client.post("/api/sessions", json={"mode": "return"}, headers=headers).json()["session_id"]
    text = DEFECT_TEXT.format(order=order, what=what).replace("defect claim", claim)
    reply = client.post(f"/api/sessions/{sid}/messages", headers=headers,
                        json={"text": text, "photo_ids": [], "client_request_id": "cr-" + uuid.uuid4().hex[:8]})
    assert reply.status_code == 200, reply.text
    return sid, reply.json()


def upload(client, headers, sid, view, data=None, content_type="image/jpeg"):
    data = data if data is not None else shoe_photo(view, seed=VIEWS.index(view) + 1 if view in VIEWS else 9)
    return client.post("/api/uploads", headers=headers, files={"file": (f"{view}.jpg", data, content_type)},
                       data={"session_id": sid, "declared_view": view, "purpose": "RETURN_EVIDENCE"})


def submit(client, headers, sid, order="ORD-001", claim="DEFECT_CLAIM", key=None):
    response = client.post("/api/returns", headers={**headers, "Idempotency-Key": key or "rk-" + uuid.uuid4().hex[:10]},
                           json={"session_id": sid, "order_id": order, "order_line": "1", "claim_type": claim,
                                 "reason": "defect"})
    assert response.status_code == 200, response.text
    return response.json()


def evaluate(client, headers, return_id, rid=None):
    return client.post(f"/api/returns/{return_id}/evaluate", headers=headers,
                       json={"client_request_id": rid or "ev-" + uuid.uuid4().hex[:8]})


def approved_return(client, headers):
    sid, _ = start_return(client, headers)
    for view in VIEWS:
        assert upload(client, headers, sid, view).status_code == 200
    created = submit(client, headers, sid)
    result = evaluate(client, headers, created["return_id"]).json()
    assert result["status"] == "APPROVED_DEMO", result
    return sid, result


# ----------------------------------------------------------------- conversation and uploads

def test_return_conversation_asks_only_for_missing_facts(backend):
    client, _ = backend
    headers = login(client, "cust-01")
    _, reply = start_return(client, headers)
    assert reply["next_action"] == "UPLOAD_PHOTOS" and [p["view"] for p in reply["required_photos"]] == VIEWS
    _, vague = start_return(client, headers, what="They have a scuff.")
    assert vague["next_action"] == "ASK_QUESTION"
    assert [q["question_id"] for q in vague["questions"]] == ["worn", "repaired"]
    _, missing = start_return(client, headers, order="ORD-999")
    assert "couldn't find demo order ORD-999" in missing["message"]


def test_uploads_are_validated_cleaned_and_owned(backend):
    client, c = backend
    headers = login(client, "cust-01")
    sid, _ = start_return(client, headers)
    good = upload(client, headers, sid, "pair_top")
    body = good.json()
    assert good.status_code == 200 and body["photo_id"].startswith("P-") and body["quality"]["verdict"]["sharpness"] == "PASS"
    stored = c.store.get(f"returns/{sid}/{body['photo_id']}.jpg")
    assert Image.open(io.BytesIO(stored)).format == "JPEG" and not Image.open(io.BytesIO(stored)).getexif()
    assert upload(client, headers, sid, "pair_top", b"<svg onload=alert(1)>", "image/svg+xml").status_code == 422
    assert upload(client, headers, sid, "pair_top", b"\xff\xd8\xff" + b"0" * 100).status_code == 422
    huge = upload(client, headers, sid, "pair_top", b"\xff\xd8\xff" + b"0" * (c.settings.max_image_bytes + 10))
    assert huge.status_code == 413
    assert upload(client, login(client, "cust-02"), sid, "pair_top").status_code == 404
    shop_sid = client.post("/api/sessions", json={"mode": "shopping"}, headers=headers).json()["session_id"]
    assert upload(client, headers, shop_sid, "pair_top").status_code == 422


# ----------------------------------------------------------------- creation

def test_returns_check_order_ownership_and_create_one_case_per_line(backend):
    client, c = backend
    headers = login(client, "cust-01")
    sid, _ = start_return(client, headers)
    other = client.post("/api/returns", headers=login(client, "cust-02"),
                        json={"session_id": sid, "order_id": "ORD-001", "order_line": "1",
                              "claim_type": "DEFECT_CLAIM", "reason": "defect"})
    assert other.status_code == 404  # not their session, not their order
    first = submit(client, headers, sid, key="same-key-1")
    assert frontend_checks().return_problems(first) == []
    assert submit(client, headers, sid, key="same-key-1")["return_id"] == first["return_id"]
    sid2, _ = start_return(client, headers)
    assert submit(client, headers, sid2)["return_id"] == first["return_id"]  # one return per order line
    reused = client.post("/api/returns", headers={**headers, "Idempotency-Key": "same-key-1"},
                         json={"session_id": sid, "order_id": "ORD-003", "order_line": "1",
                               "claim_type": "STANDARD_RETURN", "reason": "fit"})
    assert reused.status_code == 409 and reused.json()["code"] == "IDEMPOTENCY_KEY_REUSED"


# ----------------------------------------------------------------- evaluation

def test_evaluation_uses_measured_quality_and_names_the_missing_views(backend):
    client, _ = backend
    headers = login(client, "cust-01")
    sid, _ = start_return(client, headers)
    for view in VIEWS[:5]:
        data = shoe_photo(view, blurry=True) if view == "pair_soles" else shoe_photo(view, dark=True) \
            if view == "pair_heels" else None
        upload(client, headers, sid, view, data)
    created = submit(client, headers, sid)
    result = evaluate(client, headers, created["return_id"]).json()
    missing = {m["view"]: m["reason"] for m in result["missing_views"]}
    assert result["status"] == "NEEDS_MORE_EVIDENCE" and frontend_checks().return_problems(result) == []
    assert set(missing) == {"pair_soles", "pair_heels", "defect_closeup", "size_label"}
    assert "blurry" in missing["pair_soles"] and "dark" in missing["pair_heels"]


def test_duplicate_photos_for_different_views_are_rejected(backend):
    client, _ = backend
    headers = login(client, "cust-01")
    sid, _ = start_return(client, headers)
    for view in VIEWS:
        upload(client, headers, sid, view, shoe_photo("pair_top") if view == "pair_outer" else None)
    result = evaluate(client, headers, submit(client, headers, sid)["return_id"]).json()
    assert result["status"] == "NEEDS_MORE_EVIDENCE"
    assert [m["view"] for m in result["missing_views"]] == ["pair_outer"]


def test_approval_registers_exactly_one_unit_awaiting_receipt(backend):
    client, c = backend
    headers = login(client, "cust-01")
    sid, result = approved_return(client, headers)
    assert result["inventory_status"] == "AWAITING_RECEIPT" and result["demo_address"].startswith("FICTIONAL")
    assert result["analysis_source"] == "SIMULATED" and result["model_id"] is None
    assert result["rule_decision"]["final_merchant_decision"] is False
    for _ in range(3):
        again = evaluate(client, headers, result["return_id"]).json()
        assert again["status"] == "APPROVED_DEMO" and again["item_id"] == result["item_id"]
    units = [r for r in c.repo.query("INV", "ITEM#") if r.get("source_return_id") == result["return_id"]]
    assert len(units) == 1 and units[0]["status"] == "AWAITING_RECEIPT" and units[0]["price_cents"] == 7095
    assert client.get(f"/api/items/{result['item_id']}").status_code == 404  # not for sale before inspection


@pytest.mark.parametrize("order,what,claim,status", [
    ("ORD-002", "Scratch on the left heel, noticed the first time I wore them indoors. Not repaired.",
     "defect claim", "HUMAN_REVIEW_REQUIRED"),
    ("ORD-004", "The sole is coming off at the toe. Noticed after a few wears outdoors. Not repaired.",
     "defect claim", "HUMAN_REVIEW_REQUIRED"),
])
def test_cases_outside_the_auto_rules_go_to_a_simulated_review(backend, order, what, claim, status):
    client, c = backend
    headers = login(client, "cust-01")
    sid, _ = start_return(client, headers, order=order, what=what, claim=claim)
    for view in VIEWS:
        upload(client, headers, sid, view)
    result = evaluate(client, headers, submit(client, headers, sid, order=order)["return_id"]).json()
    assert result["status"] == status and result["inventory_status"] is None and result["item_id"] is None
    reviews = client.get("/api/admin/reviews", params={"status": "OPEN"}, headers=login(client, "admin")).json()
    assert any(r["return_id"] == result["return_id"] for r in reviews["reviews"])


def test_unworn_standard_return_is_approved_but_not_listed(backend):
    client, c = backend
    headers = login(client, "cust-01")
    sid, reply = start_return(client, headers, order="ORD-003", claim="standard return",
                              what="They don't fit. Never worn, not repaired.")
    assert [p["view"] for p in reply["required_photos"]] == [v for v in VIEWS if v != "defect_closeup"]
    for view in reply["required_photos"]:
        upload(client, headers, sid, view["view"], shoe_photo(view["view"], colour="white", defect=None))
    result = evaluate(client, headers, submit(client, headers, sid, order="ORD-003",
                                              claim="STANDARD_RETURN")["return_id"]).json()
    assert result["status"] == "APPROVED_DEMO" and result["item_id"] is None  # no defect: not on Buy Defective


# ----------------------------------------------------------------- admin lifecycle

def test_inspection_publishes_the_unit_and_a_second_customer_can_buy_it(backend):
    client, c = backend
    cust = login(client, "cust-01")
    _, result = approved_return(client, cust)
    item_id = result["item_id"]
    admin = login(client, "admin")
    assert client.post(f"/api/admin/items/{item_id}/inspect", headers=admin,
                       json={"outcome": "PASS", "simulated": True}).status_code == 409  # not received yet
    assert client.post(f"/api/admin/items/{item_id}/receive", headers=cust, json={"simulated": True}).status_code == 403
    received = client.post(f"/api/admin/items/{item_id}/receive", headers=admin, json={"simulated": True}).json()
    assert received["status"] == "INSPECTION_PENDING"
    inspected = client.post(f"/api/admin/items/{item_id}/inspect", headers=admin,
                            json={"outcome": "PASS", "notes": "ok", "simulated": True}).json()
    assert inspected["status"] == "AVAILABLE" and inspected["inspection"]["simulated"] is True
    assert client.get(f"/api/returns/{result['return_id']}", headers=cust).json()["inventory_status"] == "AVAILABLE"
    buyer = login(client, "cust-02")
    sid = client.post("/api/sessions", json={"mode": "shopping"}, headers=buyer).json()["session_id"]
    reply = client.post(f"/api/sessions/{sid}/messages", headers=buyer,
                        json={"text": "Black ankle boots, US women's 7, under $100, scratches are fine",
                              "photo_ids": [], "client_request_id": "cr-buy-1"}).json()
    assert item_id in [i["item_id"] for i in reply["items"]]
    item = client.get(f"/api/items/{item_id}").json()
    assert item["photos"] and client.get(item["photos"][0]["url"]).status_code == 200
    client.post(f"/api/items/{item_id}/acceptances", headers=buyer, json={
        "listing_version": 1, "acceptances": [{"defect_id": d["defect_id"], "accepted": True} for d in item["defects"]]})
    bought = client.post("/api/demo-checkout", headers={**buyer, "Idempotency-Key": "buy-key-0001"},
                         json={"item_id": item_id, "listing_version": 1, "idempotency_key": "buy-key-0001"})
    assert bought.status_code == 200 and bought.json()["amount_cents"] == 7095


def test_grade_c_is_quarantined_and_ungraded_units_need_a_grade(backend):
    client, _ = backend
    admin = login(client, "admin")
    unknown = client.post("/api/admin/items/ITM-021/inspect", headers=admin, json={"outcome": "PASS", "simulated": True})
    assert unknown.status_code == 422 and unknown.json()["code"] == "GRADE_REQUIRED"
    graded = client.post("/api/admin/items/ITM-020/inspect", headers=admin,
                         json={"outcome": "PASS", "simulated": True}).json()
    assert graded["status"] == "AVAILABLE"  # grade B with a disclosed defect
    no_defect = client.post("/api/admin/items/ITM-021/inspect", headers=admin,
                            json={"outcome": "PASS", "grade": "A", "simulated": True}).json()
    assert no_defect["status"] == "QUARANTINED"  # nothing to disclose: not a Buy Defective listing
    not_simulated = client.post("/api/admin/items/ITM-019/receive", headers=admin, json={"simulated": False})
    assert not_simulated.status_code == 422


def test_review_decisions_are_recorded_once(backend):
    client, c = backend
    admin = login(client, "admin")
    reject = client.post("/api/admin/reviews/REV-RET-008-CLAIM/decision", headers=admin,
                         json={"conclusion": "REJECT", "reason": "Repair voids the claim (demo)", "simulated": True})
    assert reject.status_code == 200 and reject.json()["return_status"] == "REJECTED_AFTER_REVIEW"
    twice = client.post("/api/admin/reviews/REV-RET-008-CLAIM/decision", headers=admin,
                        json={"conclusion": "APPROVE", "reason": "changed my mind", "simulated": True})
    assert twice.status_code == 409
    approve = client.post("/api/admin/reviews/REV-RET-010-CLAIM/decision", headers=admin, json={
        "conclusion": "APPROVE", "reason": "Stain is minor on inspection", "simulated": True,
        "corrected_labels": [{"type": "stain", "severity": "B", "location": "left side", "description": "Water mark"}]})
    body = approve.json()
    assert body["return_status"] == "APPROVED_DEMO" and body["item_id"] == "ITM-RET-010"
    assert c.repo.get(("INV", "ITEM#ITM-RET-010"))["status"] == "AWAITING_RECEIPT"


# ----------------------------------------------------------------- AI failures never fall back

class Failing:
    def __init__(self, error):
        self.error = error

    def __call__(self, *args, **kwargs):
        raise self.error


@pytest.mark.parametrize("make,status,code", [
    (lambda b: b.ai_unavailable("ThrottlingException"), 503, "AI_UNAVAILABLE"),
    (lambda b: b.agent_timeout(), 504, "AGENT_TIMEOUT"),
    (lambda b: b.ai_output_invalid(["covered_views: bad value"]), 502, "AI_OUTPUT_INVALID"),
])
def test_ai_failures_are_reported_and_never_replaced_by_the_mock(backend, monkeypatch, make, status, code):
    from backend.agent import base

    client, c = backend
    headers = login(client, "cust-01")
    sid, _ = start_return(client, headers)
    for view in VIEWS:
        upload(client, headers, sid, view)
    created = submit(client, headers, sid)
    monkeypatch.setattr(c.invoker, "analyze_photos", Failing(make(base)))
    response = evaluate(client, headers, created["return_id"])
    assert response.status_code == status and response.json()["code"] == code
    after = client.get(f"/api/returns/{created['return_id']}", headers=headers).json()
    if code == "AI_OUTPUT_INVALID":
        assert after["status"] == "HUMAN_REVIEW_REQUIRED"  # plan: a person takes over, no normal decision
    else:
        assert after["status"] == "INFO_COLLECTION"  # nothing decided; retry is safe
    assert not [r for r in c.repo.query("INV", "ITEM#") if r.get("source_return_id") == created["return_id"]]


# ----------------------------------------------------------------- persistence

def test_data_survives_restarting_dynamodb_and_the_api(tmp_path, image_root):
    from fastapi.testclient import TestClient

    from backend.config import load_settings
    from backend.main import create_app
    from backend.seed.load import seed

    port = free_port()
    data_dir = tmp_path / "ddb"
    data_dir.mkdir()
    env = env_for(f"http://127.0.0.1:{port}", "persist_test", image_root)
    proc = start_ddb(port, data_dir)
    try:
        app = create_app(load_settings(env))
        with TestClient(app) as client:
            seed(app.state.c.repo, app.state.c.store)
            buyer = login(client, "cust-02")
            client.post("/api/items/ITM-001/acceptances", headers=buyer, json={
                "listing_version": 1, "acceptances": [{"defect_id": "D-001", "accepted": True}]})
            first = client.post("/api/demo-checkout", headers={**buyer, "Idempotency-Key": "persist-key-1"},
                                json={"item_id": "ITM-001", "listing_version": 1, "idempotency_key": "persist-key-1"})
            assert first.status_code == 200
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    proc = start_ddb(port, data_dir)
    try:
        app = create_app(load_settings(env))
        with TestClient(app) as client:
            assert client.get("/api/items/ITM-001").json()["status"] == "SOLD"
            buyer = login(client, "cust-02")
            again = client.post("/api/demo-checkout", headers={**buyer, "Idempotency-Key": "persist-key-1"},
                                json={"item_id": "ITM-001", "listing_version": 1, "idempotency_key": "persist-key-1"})
            assert again.json()["purchase_id"] == first.json()["purchase_id"]
            assert seed(app.state.c.repo, app.state.c.store)["items"] == 0  # re-seeding never duplicates or resets
            assert client.get("/api/items/ITM-001").json()["status"] == "SOLD"
    finally:
        proc.terminate()
        proc.wait(timeout=10)
