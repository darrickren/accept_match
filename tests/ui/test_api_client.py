"""api mode transport against the fake FastAPI server: auth, retries, timeouts, idempotency, redaction."""
import logging
import socket

import pytest
from fake_api import jpeg

from ui.client.api_agent import ApiAgentClient
from ui.config import load_settings


def client(url, journey="shop", persona="cust-02", password="pw-cust-02", **env):
    values = {"ACCEPTMATCH_AGENT": "api", "ACCEPTMATCH_API_BASE_URL": url, "ACCEPTMATCH_DEMO_PERSONA": persona,
              "ACCEPTMATCH_DEMO_PASSWORD": password}
    values.update(env)
    return ApiAgentClient(journey, load_settings({k: v for k, v in values.items() if v is not None}))


def closed_port_url():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return f"http://127.0.0.1:{port}"


def test_signs_in_once_and_sends_the_bearer_token_to_protected_calls(fake_api):
    backend, url = fake_api
    c = client(url)
    assert c.open_chat("am-shop-1", "shopping", "cr_1")["ok"]
    assert c.send_message("am-shop-1", "shopping", "hello", [], "cr_2")["ok"]
    assert len(backend.calls("POST", "/api/demo-auth/login")) == 1
    assert all(r["authorized"] for r in backend.calls("POST", "/api/sessions"))
    message = backend.calls("POST", "/api/sessions/")[0]
    assert message["idempotency_key"] == "cr_2" and message["x_request_id"] == "cr_2"


def test_expired_token_signs_in_again_once(fake_api):
    backend, url = fake_api
    c = client(url)
    assert c.open_chat("am-shop-1", "shopping", "cr_1")["ok"]
    backend.expire_tokens()
    assert c.send_message("am-shop-1", "shopping", "hello", [], "cr_2")["ok"]
    assert len(backend.calls("POST", "/api/demo-auth/login")) == 2


def test_wrong_password_is_unauthorized_and_never_echoed(fake_api):
    backend, url = fake_api
    response = client(url, password="pw-wrong-123").open_chat("am-shop-1", "shopping", "cr_1")
    assert response["ok"] is False and response["error"]["code"] == "unauthorized"
    assert "pw-wrong-123" not in repr(response)


def test_admin_personas_are_refused_by_the_customer_site(fake_api):
    _, url = fake_api
    response = client(url, persona="admin", password="pw-admin").open_chat("am-shop-1", "shopping", "cr_1")
    assert response["error"]["code"] == "forbidden"


def test_health_failure_stops_before_any_business_call_and_recovers(fake_api):
    backend, url = fake_api
    backend.fault("GET", "/health", status=503, times=2)  # the GET is retried once, so fail both attempts
    c = client(url)
    response = c.open_chat("am-shop-1", "shopping", "cr_1")
    assert response["error"]["code"] == "agent_unavailable" and response["error"]["retryable"]
    assert len(backend.calls("GET", "/health")) == 2 and backend.calls("POST", "/api/sessions") == []
    assert c.open_chat("am-shop-1", "shopping", "cr_2")["ok"]


def test_unsupported_backend_major_version_is_refused(fake_api):
    backend, url = fake_api
    backend.health_version = "2.0.0"
    response = client(url).open_chat("am-shop-1", "shopping", "cr_1")
    assert response["error"]["code"] == "agent_unavailable" and "version" in response["error"]["user_message"]


def test_unreachable_backend_is_unavailable_not_mock():
    response = client(closed_port_url()).open_chat("am-shop-1", "shopping", "cr_1")
    assert response["ok"] is False and response["error"]["code"] == "agent_unavailable"
    assert response["meta"]["source"] == "agent"


def test_timeouts_are_reported_and_never_retried_automatically(fake_api):
    backend, url = fake_api
    c = client(url, ACCEPTMATCH_API_TIMEOUT="0.5")
    assert c.open_chat("am-shop-1", "shopping", "cr_1")["ok"]
    backend.fault("POST", r"/api/sessions/[^/]+/messages", delay=1.5)
    response = c.send_message("am-shop-1", "shopping", "hello", [], "cr_2")
    assert response["error"]["code"] == "agent_timeout" and response["error"]["retryable"]
    assert len(backend.calls("POST", "/api/sessions/")) == 1


def test_safe_gets_retry_once_after_a_503(fake_api):
    backend, url = fake_api
    backend.fault("GET", "/api/items", status=503)
    response = client(url).search_items({}, None, "cr_1")
    assert response["ok"] and len(backend.calls("GET", "/api/items")) == 2


def test_uploads_are_never_retried_automatically(fake_api):
    backend, url = fake_api
    c = client(url, journey="return", persona="cust-01", password="pw-cust-01")
    backend.fault("POST", "/api/uploads", status=503)
    response = c.upload_photo("am-retn-1", "pair_top", jpeg((50, 50, 50)), "image/jpeg", "cr_1")
    assert response["error"]["code"] == "agent_unavailable"
    assert len(backend.calls("POST", "/api/uploads")) == 1 and backend.photos == {}


def test_checkout_retries_reuse_the_idempotency_key_so_only_one_purchase_exists(fake_api):
    backend, url = fake_api
    c = client(url)
    assert c.accept_defects("ITM-001", 1, ["D-001"], "cr_1")["ok"]
    backend.fault("POST", "/api/demo-checkout", status=503)
    first = c.checkout("ITM-001", 1, "am-buy-key-1", "cr_2")
    again = c.checkout("ITM-001", 1, "am-buy-key-1", "cr_3")
    assert first["ok"] and again["ok"]
    assert first["data"]["purchase"]["purchase_id"] == again["data"]["purchase"]["purchase_id"]
    keys = {r["idempotency_key"] for r in backend.calls("POST", "/api/demo-checkout")}
    assert keys == {"am-buy-key-1"} and len(backend.purchases) == 1


@pytest.mark.parametrize("raw,content_type", [(b"{not json", "application/json"), (b"<html>oops</html>", "text/html")])
def test_malformed_bodies_are_contract_violations(fake_api, raw, content_type):
    backend, url = fake_api
    c = client(url)
    assert c.open_chat("am-shop-1", "shopping", "cr_1")["ok"]
    backend.fault("POST", r"/api/sessions/[^/]+/messages", status=200, raw=raw, content_type=content_type)
    response = c.send_message("am-shop-1", "shopping", "hello", [], "cr_2")
    assert response["error"]["code"] == "contract_violation"


def test_schema_invalid_chat_responses_are_rejected_with_the_problems_logged(fake_api):
    backend, url = fake_api
    c = client(url)
    assert c.open_chat("am-shop-1", "shopping", "cr_1")["ok"]
    backend.fault("POST", r"/api/sessions/[^/]+/messages", status=200,
                  raw=b'{"session_id": "x", "message": "hi"}')
    response = c.send_message("am-shop-1", "shopping", "hello", [], "cr_2")
    assert response["error"]["code"] == "contract_violation"
    assert any("missing" in note for note in response["meta"]["violations"])


def test_redirects_are_refused_so_the_token_never_leaves_the_api_host(fake_api):
    backend, url = fake_api
    backend.fault("GET", "/api/items", status=302, raw=b"", headers={"Location": "https://evil.example/steal"},
                  times=5)
    response = client(url).search_items({}, None, "cr_1")
    assert response["error"]["code"] == "contract_violation"


def test_missing_endpoints_are_pending_integration(fake_api):
    backend, url = fake_api
    backend.remove_route("POST", "/api/demo-checkout")
    c = client(url)
    assert c.supports("POST", "/api/demo-checkout") is False
    assert c.supports("POST", "/api/items/{item_id}/acceptances") is True
    response = c.checkout("ITM-001", 1, "k", "cr_1")
    assert response["error"]["code"] == "not_implemented" and "pending integration" in response["error"]["user_message"]


def test_images_are_only_fetched_from_the_api_origin(fake_api):
    backend, url = fake_api
    c = client(url)
    assert c.fetch_image("/media/items/ITM-001.jpg")[:2] == b"\xff\xd8"
    assert c.fetch_image("https://evil.example/x.jpg") is None
    assert c.fetch_image("/media/items/ITM-001.jpg") and len(backend.calls("GET", "/media/")) == 1  # cached


def test_no_tokens_or_passwords_in_call_logs_or_debug_logging(fake_api, caplog):
    backend, url = fake_api
    caplog.set_level(logging.DEBUG, logger="acceptmatch.api")
    c = client(url)
    c.open_chat("am-shop-1", "shopping", "cr_1")
    issued = set(backend.tokens)
    backend.expire_tokens()
    c.send_message("am-shop-1", "shopping", "hello", [], "cr_2")  # 401, then a second sign-in
    issued |= set(backend.tokens)
    c.search_items({}, None, "cr_3")
    secrets = sorted(issued) + ["pw-cust-02"]
    logged = repr(c.http.recent_calls()) + caplog.text + repr(c) + repr(c.http) + repr(c.info())
    assert len(issued) == 2 and not any(secret in logged for secret in secrets)
