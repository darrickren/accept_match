"""APP_MODE=agentcore, offline: both halves of the AgentCore integration without AWS.

- The deployed agent (backend/agentcore): its real HTTP contract (/ping, /invocations) through the bedrock-agentcore
  SDK app, the real Strands loop with a scripted model, and the snapshot-only search tool.
- The local FastAPI side: the real app on DynamoDB Local with a botocore Stubber in place of InvokeAgentRuntime,
  so the request (ARN, session, sanitized snapshot) and the validation of the answer are checked.
"""

import io
import json
import uuid

import pytest
from botocore.response import StreamingBody
from botocore.stub import Stubber
from conftest import env_for, frontend_checks, login
from test_backend_strands import PREFS, Scripted, plan, use

ARN = "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/acceptmatch_shopping-AbCdE12345"
MODEL = "us.amazon.nova-2-lite-v1:0"


def remote_settings():
    from backend.agentcore.remote import settings_from_env

    return settings_from_env({"BEDROCK_MODEL_ID": MODEL, "BEDROCK_REGION": "us-east-1"})


def snapshot(c):
    from backend.agentcore.remote import snapshot_row

    return [snapshot_row(row) for row in c.inventory.sellable()]


def remote_agent(model):
    from backend.agent.strands_agent import BedrockAgent

    return lambda settings: BedrockAgent(settings, model_factory=model.factory)


@pytest.fixture
def agentcore(ddb, image_root):
    """(TestClient, services, stubber): the real app in APP_MODE=agentcore, InvokeAgentRuntime stubbed."""
    import boto3
    from fastapi.testclient import TestClient

    from backend.agent.agentcore_client import AgentCoreShopping
    from backend.agent.invoker import AgentInvoker
    from backend.config import load_settings
    from backend.main import create_app
    from backend.seed.load import seed

    settings = load_settings(env_for(ddb, "t_" + uuid.uuid4().hex[:10], image_root, APP_MODE="agentcore",
                                     AGENTCORE_RUNTIME_ARN=ARN, AGENTCORE_MODEL_ID=MODEL))
    client = boto3.client("bedrock-agentcore", region_name="us-east-1", aws_access_key_id="test",
                          aws_secret_access_key="test")
    stubber = Stubber(client)
    app = create_app(settings, invoker=AgentInvoker(settings, remote=AgentCoreShopping(settings, client=client)))
    with TestClient(app) as http, stubber:
        seed(app.state.c.repo, app.state.c.store)
        yield http, app.state.c, stubber
        app.state.c.repo.drop_table()


def answer(body):
    data = json.dumps(body).encode()
    return {"response": StreamingBody(io.BytesIO(data), len(data)), "contentType": "application/json",
            "statusCode": 200, "runtimeSessionId": "x" * 40, "ResponseMetadata": {"RequestId": "agentcore-req-1"}}


def test_deployed_agent_answers_over_the_agentcore_http_contract(agentcore):
    """The entrypoint the runtime runs: /ping, then a shopping turn searched over the snapshot only."""
    from starlette.testclient import TestClient

    from backend.agentcore import app as entry
    from backend.agentcore.remote import handle

    _, c, _ = agentcore
    model = Scripted()
    model.script(use("search_inventory", PREFS), plan())
    with TestClient(entry.app) as runtime:
        assert runtime.get("/ping").json()["status"] == "Healthy"
        original = entry.handle
        entry.handle = lambda payload: handle(payload, agent_factory=remote_agent(model), settings=remote_settings())
        try:
            body = runtime.post("/invocations", json={"action": "shopping_turn", "state": {}, "inventory": snapshot(c),
                                                      "message": "Black ankle boots, US women's 7, under $100"}).json()
        finally:
            entry.handle = original
    assert body["ok"] is True and body["turn"]["item_ids"] == ["ITM-001"]
    assert body["turn"]["agent"]["tools_used"] == ["search_inventory"]
    assert body["turn"]["analysis_source"] == "BEDROCK" and body["turn"]["model_id"] == MODEL
    assert model.built == [(MODEL, 1200, 25.0)]
    rejected = handle({"action": "shopping_turn", "state": {}, "message": "hi",
                       "inventory": [{**snapshot(c)[0], "source_return_id": "RET-001"}]})
    assert rejected["ok"] is False and rejected["error"]["code"] == "INVALID_REQUEST"  # only sanitized fields


def test_shopping_turn_goes_to_agentcore_and_items_are_checked_against_dynamodb(agentcore):
    http, c, stubber = agentcore
    from backend.agentcore.remote import handle

    model = Scripted()
    model.script(use("search_inventory", PREFS), plan())
    headers = login(http, "cust-02")
    sid = http.post("/api/sessions", json={"mode": "shopping"}, headers=headers).json()["session_id"]
    sent = {}

    def run_remote(params):  # what the deployed agent would answer to exactly this request
        sent.update(params)
        return handle(json.loads(params["payload"]), agent_factory=remote_agent(model), settings=remote_settings())

    expected = {"agentRuntimeArn": ARN, "qualifier": "DEFAULT", "contentType": "application/json",
                "accept": "application/json", "runtimeSessionId": sid,
                "payload": __import__("botocore.stub", fromlist=["ANY"]).ANY}
    original = stubber.client.invoke_agent_runtime

    def invoke(**params):
        stubber.add_response("invoke_agent_runtime", answer(run_remote(params)), expected)
        return original(**params)

    stubber.client.invoke_agent_runtime = invoke
    response = http.post(f"/api/sessions/{sid}/messages", headers=headers, json={
        "text": "Black ankle boots, US women's 7, under $100. Scratches fine, no stains.", "photo_ids": [],
        "client_request_id": "cr-ac-1"})
    body = response.json()
    assert response.status_code == 200 and frontend_checks().chat_problems(body) == []
    assert body["analysis_source"] == "AGENTCORE" and body["model_id"] == MODEL
    assert [i["item_id"] for i in body["items"]] == ["ITM-001"] and body["items"][0]["price_cents"] == 6900
    assert body["agent"]["runtime"] == "bedrock-agentcore"
    assert body["agent"]["agentcore_request_id"] == "agentcore-req-1"
    assert body["agent"]["tools_used"] == ["search_inventory"] and body["agent"]["verified_item_ids"] == ["ITM-001"]
    payload = json.loads(sent["payload"])
    assert payload["action"] == "shopping_turn" and len(payload["inventory"]) == len(c.inventory.sellable())
    leaked = {k for item in payload["inventory"] for k in item} - {
        "item_id", "title", "category", "shoe_type", "size_system", "size_value", "color", "price_cents", "currency",
        "grade", "status", "listing_version", "resale_eligible", "defects", "verified_features"}
    assert not leaked and all("evidence_photo_ids" not in d for i in payload["inventory"] for d in i["defects"])
    assert http.get("/health").json()["agent"]["provider"] == "agentcore"


def test_invented_item_ids_and_aws_errors_never_reach_the_site(agentcore):
    http, c, stubber = agentcore
    headers = login(http, "cust-02")
    sid = http.post("/api/sessions", json={"mode": "shopping"}, headers=headers).json()["session_id"]
    turn = {"message": "Here you go.", "next_action": "SHOW_ITEMS", "item_ids": ["ITM-999", "ITM-022"],
            "state": {"preferences": {"size": {"system": "US_WOMEN", "value": "7"}}, "asked": [], "offered": [],
                      "history": []}, "questions": [], "model_id": MODEL,
            "agent": {"provider": "bedrock", "tools_used": ["search_inventory"], "model_calls": 2}}
    stubber.add_response("invoke_agent_runtime", answer({"ok": True, "turn": turn}))
    body = http.post(f"/api/sessions/{sid}/messages", headers=headers, json={
        "text": "size 7", "photo_ids": [], "client_request_id": "cr-ac-2"}).json()
    assert body["items"] == [] and body["next_action"] == "ASK_QUESTION"  # not in DynamoDB / not for sale
    assert body["agent"]["unverified_item_ids"] == ["ITM-999", "ITM-022"]
    stubber.add_client_error("invoke_agent_runtime", "AccessDeniedException", "User is not authorized", 403)
    denied = http.post(f"/api/sessions/{sid}/messages", headers=headers, json={
        "text": "boots", "photo_ids": [], "client_request_id": "cr-ac-3"})
    assert denied.status_code == 503 and denied.json()["code"] == "AI_UNAVAILABLE"
    assert denied.json()["retryable"] is False and "123456789012" not in denied.text
    stubber.add_response("invoke_agent_runtime", answer({"ok": False, "error": {"code": "RATE_LIMITED", "status": 429,
                                                                                "retryable": True, "cause": "x"}}))
    busy = http.post(f"/api/sessions/{sid}/messages", headers=headers, json={
        "text": "boots", "photo_ids": [], "client_request_id": "cr-ac-4"})
    assert busy.status_code == 429 and busy.json()["code"] == "RATE_LIMITED"
    returns = http.post("/api/sessions", json={"mode": "return"}, headers=login(http, "cust-01")).json()
    assert returns["analysis_source"] == "SIMULATED"  # Return stays on the labelled mock in this mode
