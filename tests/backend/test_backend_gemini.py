"""APP_MODE=gemini-local, offline: the real FastAPI app, DynamoDB Local, Strands, Strands' GeminiModel and the
google-genai SDK, with only the network replaced (an httpx MockTransport answers as the Gemini API would).

This checks the exact REST requests the backend would send to Google (model, key header, tools, schemas, images,
thought signatures) and how replies and errors are handled. No request leaves the machine and no key is needed.
"""

import base64
import json
import re
import uuid

import httpx
import pytest
from conftest import env_for, frontend_checks, login, shoe_photo

FAKE_KEY = "test-gemini-api-key-for-offline-transport"
MODEL = "gemini-3.1-flash-lite"
VIEWS = ["pair_top", "pair_outer", "pair_inner", "pair_soles", "pair_heels", "defect_closeup", "size_label"]
PREFS = {"shoe_types": ["Ankle boot"], "size_system": "US_WOMEN", "size_value": "7", "max_price_cents": 10000,
         "ok_defect_types": ["scratch"], "rejected_defect_types": ["stain"], "colors": ["black"]}
ALLOWED_SCHEMA_KEYS = {"type", "title", "description", "properties", "required", "additionalProperties", "enum",
                       "format", "minimum", "maximum", "items", "prefixItems", "minItems", "maxItems", "anyOf"}


def call(name, args, signature=None, usage=(900, 40)):
    part = {"functionCall": {"name": name, "args": args}}
    if signature:
        part["thoughtSignature"] = base64.b64encode(signature).decode()
    return {"candidates": [{"content": {"role": "model", "parts": [part]}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": usage[0], "candidatesTokenCount": usage[1],
                              "totalTokenCount": sum(usage)}}


def error(code, status, message, reason=None):
    body = {"error": {"code": code, "status": status, "message": message}}
    if reason:
        body["error"]["details"] = [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": reason}]
    return httpx.Response(code, json=body)


class FakeGemini:
    """Answers generativelanguage.googleapis.com requests from a script and records every request."""

    def __init__(self):
        self.steps, self.requests = [], []
        self.transport = httpx.MockTransport(self.handle)

    def script(self, *steps):
        self.steps, self.requests = list(steps), []

    def handle(self, request):
        body = json.loads(request.content)
        self.requests.append({"url": str(request.url), "headers": dict(request.headers), "body": body})
        assert self.steps, "the backend made more Gemini calls than were scripted"
        step = self.steps.pop(0)
        if callable(step):
            step = step(body)
        if isinstance(step, Exception):
            raise step
        if isinstance(step, httpx.Response):
            return step
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=("data: " + json.dumps(step) + "\r\n\r\n").encode())


def keywords(schema, found):
    if isinstance(schema, dict):
        for key, value in schema.items():
            if key == "properties":
                for sub in value.values():
                    keywords(sub, found)
            else:
                found.add(key)
                keywords(value, found)
    elif isinstance(schema, list):
        for value in schema:
            keywords(value, found)
    return found


def make_app(ddb, image_root, fake, **extra):
    from backend.agent.invoker import AgentInvoker
    from backend.agent.strands_agent import GeminiAgent
    from backend.config import load_settings
    from backend.main import create_app

    settings = load_settings(env_for(ddb, "t_" + uuid.uuid4().hex[:10], image_root, APP_MODE="gemini-local",
                                     GEMINI_API_KEY=FAKE_KEY, **extra))
    return create_app(settings, invoker=AgentInvoker(settings, real=GeminiAgent(settings, transport=fake.transport)))


@pytest.fixture
def gemini(ddb, image_root, monkeypatch):
    """(TestClient, services, FakeGemini) in APP_MODE=gemini-local on a fresh, seeded table. The mock is fenced off."""
    from fastapi.testclient import TestClient

    from backend.agent import mock
    from backend.seed.load import seed

    def no_fallback(*args, **kwargs):
        raise AssertionError("gemini-local must never fall back to the mock agent")

    for name in ("shopping_turn", "return_turn", "analyze"):
        monkeypatch.setattr(mock, name, no_fallback)
    fake = FakeGemini()
    app = make_app(ddb, image_root, fake)
    with TestClient(app) as client:
        seed(app.state.c.repo, app.state.c.store)
        yield client, app.state.c, fake
        app.state.c.repo.drop_table()


def chat(client, headers, sid, text):
    return client.post(f"/api/sessions/{sid}/messages", headers=headers,
                       json={"text": text, "photo_ids": [], "client_request_id": "cr-" + uuid.uuid4().hex[:8]})


def new_session(client, headers, mode):
    return client.post("/api/sessions", json={"mode": mode}, headers=headers).json()["session_id"]


# ----------------------------------------------------------------- shopping over the wire

def test_shopping_turn_sends_a_valid_gemini_request_and_shows_database_items(gemini):
    client, c, fake = gemini
    headers = login(client, "cust-02")
    sid = new_session(client, headers, "shopping")
    fake.script(call("search_inventory", PREFS, signature=b"thought-1"),
                call("ShoppingPlan", {"preferences": PREFS, "reply": "One pair fits and two need a quick check.",
                                      "questions": [], "show_items": True}, usage=(1400, 60)))
    response = chat(client, headers, sid, "I'm looking for black women's ankle boots, US size 7, under CAD 100. "
                                          "A small scratch on the back is okay, but I don't want stains.")
    body = response.json()
    assert response.status_code == 200 and frontend_checks().chat_problems(body) == []
    assert body["analysis_source"] == "GEMINI" and body["model_id"] == MODEL
    assert body["next_action"] == "SHOW_ITEMS" and [i["item_id"] for i in body["items"]] == ["ITM-001"]
    expected = {"provider": "gemini", "model_id": MODEL, "step": "shopping", "model_calls": 2,
                "tools_used": ["search_inventory"], "structured_output": "ShoppingPlan", "input_tokens": 2300,
                "output_tokens": 100, "images_sent": None, "runtime": None}
    assert {k: body["agent"][k] for k in expected} == expected and body["agent"]["latency_ms"] >= 0
    rows = {(r["title"], r["value"]) for r in body["preferences"]}
    assert {("Size", "US women's 7"), ("Budget", "Under CA$100"), ("Not OK", "Stains")} <= rows
    assert body["progress"] == {"answered": 4, "estimated_total": 4} and body["match_count"] == 1

    first, second = fake.requests
    assert first["url"].endswith(f"/models/{MODEL}:streamGenerateContent?alt=sse")
    assert first["headers"]["x-goog-api-key"] == FAKE_KEY and FAKE_KEY not in first["url"]
    config = first["body"]["generationConfig"]
    assert config["maxOutputTokens"] == 2048 and config["thinkingConfig"] == {"thinking_level": "LOW"}
    assert "temperature" not in config  # Gemini 3 default, as Google recommends
    declarations = first["body"]["tools"][0]["functionDeclarations"]
    assert [d["name"] for d in declarations] == ["search_inventory", "get_item_details", "ShoppingPlan"]
    for declaration in declarations:
        assert keywords(declaration["parameters_json_schema"], set()) <= ALLOWED_SCHEMA_KEYS, declaration["name"]
    assert "stains" in json.dumps(first["body"]["contents"][0])
    model_turn, tool_turn = second["body"]["contents"][1], second["body"]["contents"][2]
    assert model_turn["parts"][0]["thoughtSignature"] == base64.b64encode(b"thought-1").decode()
    result = tool_turn["parts"][0]["functionResponse"]
    assert result["name"] == "search_inventory" and "ITM-001" in json.dumps(result)
    state = c.repo.get(("SESSION#" + sid, "META"))["state"]["preferences"]
    assert state["accepted_defect_types"] == {"scratch": "YES", "stain": "NO"}


# ----------------------------------------------------------------- multimodal return over the wire

def test_photo_review_sends_the_stored_image_bytes_and_the_backend_decides(gemini):
    client, c, fake = gemini
    headers = login(client, "cust-01")
    sid = new_session(client, headers, "return")
    facts = {"order_id": "ORD-001", "claim_type": "DEFECT_CLAIM", "worn": "INDOORS", "repaired": False,
             "onset": "FIRST_WEAR"}
    fake.script(call("lookup_demo_order", {"order_id": "ORD-001"}),
                call("ReturnPlan", {"facts": facts, "reply": "Thanks. Please add the photos.", "questions": [],
                                    "ready_for_photos": True}))
    reply = chat(client, headers, sid, "Return request for demo order ORD-001, line 1. Type: defect claim. What "
                                       "happened: Scratch on the back of the left heel, noticed the first time I wore "
                                       "them indoors. Not repaired.").json()
    assert reply["next_action"] == "UPLOAD_PHOTOS" and reply["agent"]["tools_used"] == ["lookup_demo_order"]
    for index, view in enumerate(VIEWS):
        data = shoe_photo(view, seed=index + 1)
        assert client.post("/api/uploads", headers=headers, files={"file": (f"{view}.jpg", data, "image/jpeg")},
                           data={"session_id": sid, "declared_view": view, "purpose": "RETURN_EVIDENCE"}
                           ).status_code == 200
    created = client.post("/api/returns", headers={**headers, "Idempotency-Key": "rk-" + uuid.uuid4().hex[:10]},
                          json={"session_id": sid, "order_id": "ORD-001", "order_line": "1",
                                "claim_type": "DEFECT_CLAIM", "reason": "defect"}).json()

    def look(body):
        parts = body["contents"][0]["parts"]
        labels = [re.match(r"Photo (P-\d+) \(the customer says it shows: (\w+)\)", p.get("text", "")) for p in parts]
        sent = {m.group(1): m.group(2) for m in labels if m}
        images = [p["inlineData"] for p in parts if "inlineData" in p]
        assert sorted(sent.values()) == sorted(VIEWS) and len(images) == 7
        assert all(i["mime_type"] == "image/jpeg" for i in images)
        assert all(base64.urlsafe_b64decode(i["data"])[:2] == b"\xff\xd8" for i in images)  # real JPEG bytes
        assert body["tools"][0]["functionDeclarations"][0]["name"] == "VisionObservation"
        closeup = next(pid for pid, view in sent.items() if view == "defect_closeup")
        return call("VisionObservation", {
            "photos": [{"photo_id": pid, "detected_view": view, "looks_like_footwear": True, "occlusion": "LOW",
                        "sharpness": "PASS", "exposure": "PASS"} for pid, view in sent.items()],
            "defects": [{"type": "scratch", "location": "back of the left heel", "severity": "A",
                         "description": "Short light scratch on the heel counter", "photo_ids": [closeup],
                         "uncertainty": "low"}],
            "possible_structural_or_safety_issue": False, "text_photo_conflicts": [],
            "cannot_verify": ["cause of the scratch"]}, usage=(9000, 300))

    fake.script(look)
    result = client.post(f"/api/returns/{created['return_id']}/evaluate", headers=headers,
                         json={"client_request_id": "ev-" + uuid.uuid4().hex[:8]})
    body = result.json()
    assert result.status_code == 200 and frontend_checks().return_problems(body) == []
    assert body["status"] == "APPROVED_DEMO" and body["inventory_status"] == "AWAITING_RECEIPT"
    assert body["analysis_source"] == "GEMINI" and body["model_id"] == MODEL
    assert body["agent"]["images_sent"] == 7 and body["agent"]["step"] == "vision"
    unit = c.repo.get(("INV", "ITEM#" + body["item_id"]))
    assert unit["defects"][0]["verified_by"] == "AI_OBSERVATION" and unit["status"] == "AWAITING_RECEIPT"


# ----------------------------------------------------------------- failures, limits and secrets

@pytest.mark.parametrize("answer,status,code,cause", [
    (error(429, "RESOURCE_EXHAUSTED", "You exceeded your current quota."), 429, "RATE_LIMITED", "free-tier quota"),
    (error(400, "INVALID_ARGUMENT", "API key not valid. Please pass a valid API key.", "API_KEY_INVALID"), 503,
     "AI_UNAVAILABLE", "API key was rejected"),
    (error(404, "NOT_FOUND", f"models/{MODEL} is not found"), 503, "AI_UNAVAILABLE", "wasn't found"),
    (error(503, "UNAVAILABLE", "The model is overloaded."), 503, "AI_UNAVAILABLE", "temporary problem"),
    (httpx.ReadTimeout("timed out"), 504, "AGENT_TIMEOUT", None),
])
def test_gemini_failures_map_to_the_contract_without_fallback(gemini, answer, status, code, cause):
    client, c, fake = gemini
    headers = login(client, "cust-02")
    sid = new_session(client, headers, "shopping")
    fake.script(answer)
    response = chat(client, headers, sid, "ankle boots")
    body = response.json()
    assert response.status_code == status and body["code"] == code and frontend_checks().error_problems(body) == []
    if cause:
        assert cause in body["detail"]["cause"]
    assert FAKE_KEY not in response.text and len(fake.requests) == 1  # no SDK retry, no second attempt
    assert c.repo.get(("SESSION#" + sid, "META"))["turns"] == 0  # nothing was saved


def test_call_limit_and_session_budget_stop_before_calling_gemini(ddb, image_root):
    from fastapi.testclient import TestClient

    from backend.seed.load import seed

    fake = FakeGemini()
    app = make_app(ddb, image_root, fake, AGENT_MAX_CALLS_PER_MINUTE="2", AGENT_MAX_TURNS_PER_SESSION="1")
    with TestClient(app) as client:
        seed(app.state.c.repo, app.state.c.store)
        headers = login(client, "cust-02")
        first = new_session(client, headers, "shopping")
        fake.script(call("search_inventory", PREFS), call("ShoppingPlan", {"preferences": PREFS, "reply": "Here.",
                                                                           "questions": [], "show_items": True}))
        assert chat(client, headers, first, "ankle boots, size 7").status_code == 200
        again = chat(client, headers, first, "and in brown?")
        assert again.status_code == 429 and again.json()["code"] == "SESSION_LIMIT_REACHED"
        busy = chat(client, headers, new_session(client, headers, "shopping"), "sneakers")
        assert busy.status_code == 429 and "per minute" in busy.json()["detail"]["cause"]
        assert len(fake.requests) == 2  # neither refusal reached Google
        app.state.c.repo.drop_table()


def test_gemini_mode_needs_a_server_side_key_and_never_shows_it(ddb, image_root, caplog):
    from fastapi.testclient import TestClient

    from backend.config import ConfigError, load_settings

    with pytest.raises(ConfigError, match="GEMINI_API_KEY"):
        load_settings(env_for(ddb, "t_nokey", image_root, APP_MODE="gemini-local"))
    fake = FakeGemini()
    app = make_app(ddb, image_root, fake)
    assert FAKE_KEY not in repr(app.state.c.settings)
    with TestClient(app) as client:
        health = client.get("/health")
        assert health.json()["agent"] == {"configured": True, "provider": "gemini", "model_id": MODEL,
                                          "vision_model_id": MODEL}
        assert FAKE_KEY not in health.text and FAKE_KEY not in client.get("/openapi.json").text
        from backend.seed.load import seed

        seed(app.state.c.repo, app.state.c.store)
        headers = login(client, "cust-02")
        fake.script(error(400, "INVALID_ARGUMENT", f"API key not valid: {FAKE_KEY}", "API_KEY_INVALID"))
        with caplog.at_level("INFO", logger="acceptmatch"):
            chat(client, headers, new_session(client, headers, "shopping"), "boots")
        assert "ai_call" in caplog.text and FAKE_KEY not in caplog.text  # redacted even if Google echoed it
        app.state.c.repo.drop_table()
