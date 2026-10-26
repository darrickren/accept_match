"""Real FastAPI app + DynamoDB Local: auth, inventory, shopping chat, acceptances and checkout."""

import threading

from conftest import frontend_checks, login

EXAMPLE = "Black ankle boots in US women's 7, under CA$100. Scratches are fine, but no stains."


def chat(client, headers, session_id, text, rid):
    response = client.post(f"/api/sessions/{session_id}/messages", headers=headers,
                           json={"text": text, "photo_ids": [], "client_request_id": rid})
    assert response.status_code == 200, response.text
    return response.json()


# ----------------------------------------------------------------- system and auth

def test_health_reports_both_mode_layers_and_the_database(backend):
    client, _ = backend
    body = client.get("/health").json()
    assert body["mode"] == "mock" and body["database"]["ok"] is True and body["version"] == "0.1.0"
    assert body["agent"] == {"configured": True, "provider": "mock", "model_id": None, "vision_model_id": None}


def test_health_is_503_when_the_database_is_down(image_root):
    from fastapi.testclient import TestClient

    from backend.config import load_settings
    from backend.main import create_app
    from conftest import env_for, free_port

    app = create_app(load_settings(env_for(f"http://127.0.0.1:{free_port()}", "t_down", image_root)))
    with TestClient(app) as client:
        response = client.get("/health")
        assert response.status_code == 503 and response.json()["database"]["ok"] is False
        assert client.get("/api/items").status_code == 503


def test_openapi_lists_the_endpoints_the_frontend_checks(backend):
    client, _ = backend
    paths = client.get("/openapi.json").json()["paths"]
    assert "post" in paths["/api/items/{item_id}/acceptances"] and "post" in paths["/api/demo-checkout"]
    assert client.get("/api/nope").json() == {"detail": "Not Found"}  # unknown routes keep FastAPI's body


def test_login_tokens_and_roles(backend):
    client, c = backend
    bad = client.post("/api/demo-auth/login", json={"persona": "cust-01", "password": "wrong-password"})
    assert bad.status_code == 401 and bad.json()["code"] == "INVALID_CREDENTIALS"
    assert "wrong-password" not in bad.text
    customer = login(client, "cust-01")
    assert client.post("/api/sessions", json={"mode": "shopping"}, headers=customer).status_code == 200
    tampered = {"Authorization": customer["Authorization"][:-3] + "abc"}
    assert client.post("/api/sessions", json={"mode": "shopping"}, headers=tampered).status_code == 401
    assert client.get("/api/admin/reviews", headers=customer).status_code == 403
    admin = login(client, "admin")
    assert client.post("/api/sessions", json={"mode": "shopping"}, headers=admin).status_code == 403
    from backend.auth import issue_token

    expired, _ = issue_token(c.settings, "cust-01", "customer", now=0)
    response = client.post("/api/sessions", json={"mode": "shopping"}, headers={"Authorization": f"Bearer {expired}"})
    assert response.status_code == 401 and response.json()["code"] == "TOKEN_EXPIRED"


def test_validation_errors_follow_the_contract_and_never_echo_values(backend):
    client, _ = backend
    response = client.post("/api/demo-auth/login", json={"persona": "cust-01", "password": "x" * 300})
    body = response.json()
    assert response.status_code == 422 and body["code"] == "VALIDATION_ERROR" and set(body) == {
        "request_id", "code", "message", "retryable", "detail"}
    assert "xxxx" not in response.text


# ----------------------------------------------------------------- inventory

def test_listing_only_shows_sellable_units_in_cad(backend):
    client, _ = backend
    page = client.get("/api/items", params={"limit": 50}).json()
    ids = {i["item_id"] for i in page["items"]}
    assert len(ids) == 15 and "ITM-003" not in ids and "ITM-022" not in ids and "ITM-018" not in ids
    checks = frontend_checks()
    assert all(checks.item_problems(i) == [] for i in page["items"])
    assert all(i["status"] == "AVAILABLE" and i["grade"] in ("A", "B") for i in page["items"])


def test_filters_pagination_details_and_photos(backend):
    client, _ = backend
    first = client.get("/api/items", params={"size_system": "US_WOMEN", "size_value": "7", "limit": 2}).json()
    second = client.get("/api/items", params={"size_system": "US_WOMEN", "size_value": "7", "limit": 2,
                                              "cursor": first["next_cursor"]}).json()
    assert len(first["items"]) == 2 and first["next_cursor"] and not ({i["item_id"] for i in first["items"]} &
                                                                       {i["item_id"] for i in second["items"]})
    item = client.get("/api/items/ITM-001").json()
    assert item["defects"][0]["defect_id"] == "D-001" and "source_return_id" not in item
    photo = client.get(item["photos"][0]["url"])
    assert photo.status_code == 200 and photo.headers["content-type"] == "image/jpeg"
    assert client.get("/api/items/ITM-018").status_code == 404  # awaiting receipt: not public
    assert client.get("/api/items/ITM-018/photos/P-018A").status_code == 404
    assert client.get("/api/items/ITM-003").json()["status"] == "SOLD"  # sold stays readable


# ----------------------------------------------------------------- shopping chat

def test_shopping_chat_uses_real_inventory_and_honest_matching(backend):
    client, _ = backend
    headers = login(client, "cust-02")
    opening = client.post("/api/sessions", json={"mode": "shopping"}, headers=headers).json()
    checks = frontend_checks()
    assert checks.chat_problems(opening) == [] and opening["analysis_source"] == "SIMULATED"
    reply = chat(client, headers, opening["session_id"], EXAMPLE, "cr-1")
    assert checks.chat_problems(reply) == [] and reply["next_action"] == "SHOW_ITEMS"
    statuses = {i["item_id"]: i["match_status"] for i in reply["items"]}
    assert statuses == {"ITM-001": "MATCH"}  # ITM-004 has a stain (NO), ITM-003 is sold, others aren't ankle boots
    assert all(checks.item_problems(i) == [] for i in reply["items"])


def test_unknown_preferences_are_never_a_full_match(backend):
    client, _ = backend
    headers = login(client, "cust-02")
    sid = client.post("/api/sessions", json={"mode": "shopping"}, headers=headers).json()["session_id"]
    reply = chat(client, headers, sid, "Sneakers in US women's 7 under $100, no stains", "cr-1")
    item = next(i for i in reply["items"] if i["item_id"] == "ITM-005")
    assert item["match_status"] == "NEEDS_CONFIRMATION" and item["pending_confirmations"]


def test_questions_are_dynamic_and_never_repeated(backend):
    client, _ = backend
    headers = login(client, "cust-02")
    sid = client.post("/api/sessions", json={"mode": "shopping"}, headers=headers).json()["session_id"]
    first = chat(client, headers, sid, "Ankle boots", "cr-1")
    asked = [q["question_id"] for q in first["questions"]]
    assert asked == ["size", "budget"]
    size_labels = [o["label"] for o in first["questions"][0]["options"]]
    assert "US women's 7" in size_labels and "EU 37" not in size_labels  # only sizes of ankle boots in stock
    second = chat(client, headers, sid, "US women's 7", "cr-2")
    assert not {q["question_id"] for q in second["questions"]} & set(asked)


def test_no_match_returns_blocking_constraints_without_changing_preferences(backend):
    client, _ = backend
    headers = login(client, "cust-02")
    sid = client.post("/api/sessions", json={"mode": "shopping"}, headers=headers).json()["session_id"]
    reply = chat(client, headers, sid, "Loafers in US men's 9 under $50, no scuffs", "cr-1")
    assert reply["items"] == [] and "Under CA$50" in reply["blocking_constraints"]
    assert reply["questions"][0]["question_id"] == "relax"


def test_retried_messages_replay_the_same_turn(backend):
    client, _ = backend
    headers = login(client, "cust-02")
    sid = client.post("/api/sessions", json={"mode": "shopping"}, headers=headers).json()["session_id"]
    first = chat(client, headers, sid, "Ankle boots", "cr-same")
    again = chat(client, headers, sid, "Ankle boots", "cr-same")
    assert first == again  # same request id, same questions: the turn ran once
    other = login(client, "cust-01")
    response = client.post(f"/api/sessions/{sid}/messages", headers=other,
                           json={"text": "hi", "photo_ids": [], "client_request_id": "cr-x"})
    assert response.status_code == 404  # someone else's session


# ----------------------------------------------------------------- acceptances and checkout

def _checkout(client, headers, item_id, version, key):
    return client.post("/api/demo-checkout", headers={**headers, "Idempotency-Key": key},
                       json={"item_id": item_id, "listing_version": version, "idempotency_key": key})


def test_checkout_needs_every_defect_accepted_for_the_current_version(backend):
    client, c = backend
    headers = login(client, "cust-02")
    assert _checkout(client, headers, "ITM-005", 1, "key-00000001").json()["code"] == "INCOMPLETE_ACCEPTANCES"
    partial = client.post("/api/items/ITM-005/acceptances", headers=headers,
                          json={"listing_version": 1, "acceptances": [{"defect_id": "D-006", "accepted": True}]})
    assert partial.json()["complete"] is False
    assert _checkout(client, headers, "ITM-005", 1, "key-00000001").json()["code"] == "INCOMPLETE_ACCEPTANCES"
    refused = client.post("/api/items/ITM-005/acceptances", headers=headers,
                          json={"listing_version": 1, "acceptances": [{"defect_id": "D-007", "accepted": False}]})
    assert refused.status_code == 422
    stale = client.post("/api/items/ITM-005/acceptances", headers=headers,
                        json={"listing_version": 2, "acceptances": [{"defect_id": "D-007", "accepted": True}]})
    assert stale.status_code == 409 and stale.json()["code"] == "STALE_LISTING_VERSION"
    mismatch = client.post("/api/demo-checkout", headers={**headers, "Idempotency-Key": "key-a-0001"},
                           json={"item_id": "ITM-005", "listing_version": 1, "idempotency_key": "key-b-0001"})
    assert mismatch.status_code == 400


def test_checkout_once_and_retries_return_the_same_purchase(backend):
    client, c = backend
    headers = login(client, "cust-02")
    client.post("/api/items/ITM-001/acceptances", headers=headers,
                json={"listing_version": 1, "acceptances": [{"defect_id": "D-001", "accepted": True}]})
    first = _checkout(client, headers, "ITM-001", 1, "key-00000002")
    again = _checkout(client, headers, "ITM-001", 1, "key-00000002")
    assert first.status_code == 200 and again.json()["purchase_id"] == first.json()["purchase_id"]
    assert first.json()["amount_cents"] == 6900 and first.json()["payment"] == "SIMULATED"
    other = _checkout(client, headers, "ITM-001", 1, "key-00000003")
    assert other.status_code == 409 and other.json()["code"] == "ITEM_UNAVAILABLE"
    assert client.get("/api/items/ITM-001").json()["status"] == "SOLD"
    purchases = [r for r in c.repo.scan_keys() if r[0].startswith("PURCHASE#")]
    assert len(purchases) == 1


def test_two_customers_racing_for_one_pair_produce_one_sale(backend):
    client, c = backend
    buyers = {p: login(client, p) for p in ("cust-02", "cust-03")}
    for headers in buyers.values():
        client.post("/api/items/ITM-008/acceptances", headers=headers,
                    json={"listing_version": 1, "acceptances": [{"defect_id": "D-011", "accepted": True}]})
    results = {}
    barrier = threading.Barrier(2)

    def buy(persona):
        barrier.wait()
        results[persona] = _checkout(client, buyers[persona], "ITM-008", 1, f"race-key-{persona}").status_code

    threads = [threading.Thread(target=buy, args=(p,)) for p in buyers]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results.values()) == [200, 409]
    assert len([k for k in c.repo.scan_keys() if k[0].startswith("PURCHASE#")]) == 1


def test_a_listing_change_invalidates_earlier_acceptances(backend):
    client, c = backend
    headers = login(client, "cust-02")
    client.post("/api/items/ITM-002/acceptances", headers=headers,
                json={"listing_version": 1, "acceptances": [{"defect_id": "D-002", "accepted": True}]})
    c.repo.update(("INV", "ITEM#ITM-002"), {"listing_version": 2})  # e.g. a new disclosure photo
    stale = _checkout(client, headers, "ITM-002", 1, "key-00000004")
    assert stale.status_code == 409 and stale.json()["code"] == "STALE_LISTING_VERSION"
    assert _checkout(client, headers, "ITM-002", 2, "key-00000005").json()["code"] == "INCOMPLETE_ACCEPTANCES"
