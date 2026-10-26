"""APP_MODE=bedrock-local, offline: the real Strands agent loop inside the real FastAPI app + DynamoDB Local, with a
scripted model standing in for Amazon Bedrock.

No AWS request is ever made: the model factory never builds a BedrockModel, and BedrockModel.stream is patched to
fail the test if anything tries. The one test that builds a real BedrockModel only checks its configuration.
"""

import asyncio
import io
import json
import re
import uuid

import pytest
from botocore.exceptions import ClientError, NoCredentialsError, ReadTimeoutError
from conftest import env_for, frontend_checks, login, shoe_photo
from strands.models.model import Model
from strands.types.exceptions import ModelThrottledException

VIEWS = ["pair_top", "pair_outer", "pair_inner", "pair_soles", "pair_heels", "defect_closeup", "size_label"]
BOOTS = ["Ankle boot", "Chelsea boot", "Knee-high boot"]
PREFS = {"shoe_types": ["Ankle boot"], "size_system": "US_WOMEN", "size_value": "7", "max_price_cents": 10000,
         "ok_defect_types": ["scratch"], "rejected_defect_types": ["stain"]}
FULL_FACTS = {"order_id": "ORD-001", "claim_type": "DEFECT_CLAIM", "worn": "INDOORS", "repaired": False,
              "onset": "FIRST_WEAR"}
SITE_TEXT = ("Return request for demo order {order}, line 1. Type: defect claim. Reason: a defect or damage. "
             "What happened: {what}")


# ----------------------------------------------------------------- the scripted model

def use(name, payload):
    return ("tool", name, payload)


def say(text):
    return ("text", text)


def slow(seconds, step):
    return ("slow", seconds, step)


class Scripted(Model):
    """Stands in for Bedrock: every call takes the next scripted step and records what the agent sent."""

    def __init__(self):
        self.config = {"model_id": "scripted"}
        self.script()

    def script(self, *steps):
        self.steps, self.calls, self.built = list(steps), [], []

    def factory(self, model_id, max_tokens, read_timeout_s):
        self.built.append((model_id, max_tokens, read_timeout_s))
        return self

    def update_config(self, **kwargs):
        self.config.update(kwargs)

    def get_config(self):
        return self.config

    async def structured_output(self, *args, **kwargs):
        raise NotImplementedError
        yield  # pragma: no cover

    async def stream(self, messages, tool_specs=None, system_prompt=None, *, tool_choice=None, **kwargs):
        call = {"messages": messages, "tools": [t["name"] for t in tool_specs or []], "system": system_prompt,
                "tool_choice": tool_choice}
        self.calls.append(call)
        assert self.steps, "the agent made more model calls than were scripted"
        step = self.steps.pop(0)
        if callable(step):
            step = step(call)
        if isinstance(step, tuple) and step[0] == "slow":
            await asyncio.sleep(step[1])
            step = step[2]
        if isinstance(step, BaseException):
            raise step
        yield {"messageStart": {"role": "assistant"}}
        if step[0] == "text":
            yield {"contentBlockStart": {"start": {}}}
            yield {"contentBlockDelta": {"delta": {"text": step[1]}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "end_turn"}}
        else:
            yield {"contentBlockStart": {"start": {"toolUse": {"toolUseId": f"tu-{len(self.calls)}",
                                                               "name": step[1]}}}}
            yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(step[2])}}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "tool_use"}}
        yield {"metadata": {"usage": {"inputTokens": 100, "outputTokens": 20, "totalTokens": 120},
                            "metrics": {"latencyMs": 5}}}


def texts(call):
    """Every text the model was sent in one call (prompt text and tool results)."""
    out = []
    for message in call["messages"]:
        for block in message["content"]:
            if "text" in block:
                out.append(block["text"])
            for part in (block.get("toolResult") or {}).get("content") or []:
                out.append(part.get("text", ""))
    return out


def tool_results(call):
    return [block["toolResult"] for message in call["messages"] for block in message["content"]
            if "toolResult" in block]


def images(call):
    return [block["image"]["source"]["bytes"] for message in call["messages"] for block in message["content"]
            if "image" in block]


def customer_prompt(call):
    return json.loads(call["messages"][0]["content"][0]["text"])


def plan(prefs=None, reply="Here are the pairs that fit.", questions=(), show=True, **extra):
    return use("ShoppingPlan", {"preferences": PREFS if prefs is None else prefs, "reply": reply,
                                "questions": list(questions), "show_items": show, **extra})


def return_plan(facts, ready=True, reply="Thanks. Please add the photos.", questions=()):
    return use("ReturnPlan", {"facts": facts, "reply": reply, "questions": list(questions), "ready_for_photos": ready})


# ----------------------------------------------------------------- fixtures and helpers

@pytest.fixture(autouse=True)
def never_call_bedrock(monkeypatch):
    from strands.models.bedrock import BedrockModel

    def refuse(*args, **kwargs):
        raise AssertionError("tests must never call Amazon Bedrock")

    monkeypatch.setattr(BedrockModel, "stream", refuse)


def bedrock_env(ddb, image_root, **extra):
    return env_for(ddb, "t_" + uuid.uuid4().hex[:10], image_root, APP_MODE="bedrock-local",
                   BEDROCK_MODEL_ID="test-model", BEDROCK_VISION_MODEL_ID="test-vision-model", **extra)


@pytest.fixture
def bedrock(ddb, image_root, monkeypatch):
    """(TestClient, services, scripted model): APP_MODE=bedrock-local on a fresh, seeded table."""
    from fastapi.testclient import TestClient

    from backend.agent import mock
    from backend.agent.invoker import AgentInvoker
    from backend.agent.strands_agent import BedrockAgent
    from backend.config import load_settings
    from backend.main import create_app
    from backend.seed.load import seed

    def no_fallback(*args, **kwargs):
        raise AssertionError("bedrock-local must never fall back to the mock agent")

    for name in ("shopping_turn", "return_turn", "analyze"):
        monkeypatch.setattr(mock, name, no_fallback)
    model = Scripted()
    settings = load_settings(bedrock_env(ddb, image_root))
    app = create_app(settings, invoker=AgentInvoker(settings, real=BedrockAgent(settings, model_factory=model.factory)))
    with TestClient(app) as client:
        seed(app.state.c.repo, app.state.c.store)
        yield client, app.state.c, model
        app.state.c.repo.drop_table()


def new_session(client, headers, mode):
    response = client.post("/api/sessions", json={"mode": mode}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["session_id"]


def chat(client, headers, sid, text):
    return client.post(f"/api/sessions/{sid}/messages", headers=headers,
                       json={"text": text, "photo_ids": [], "client_request_id": "cr-" + uuid.uuid4().hex[:8]})


def stored_session(c, sid):
    return c.repo.get(("SESSION#" + sid, "META"))


def upload(client, headers, sid, view, data=None):
    data = data if data is not None else shoe_photo(view, seed=VIEWS.index(view) + 1)
    response = client.post("/api/uploads", headers=headers, files={"file": (f"{view}.jpg", data, "image/jpeg")},
                           data={"session_id": sid, "declared_view": view, "purpose": "RETURN_EVIDENCE"})
    assert response.status_code == 200, response.text
    return response.json()["photo_id"]


def ready_return(client, headers, model, order="ORD-001", photos=None):
    """A bedrock-mode return conversation that reached UPLOAD_PHOTOS, with photos uploaded and the return created."""
    sid = new_session(client, headers, "return")
    model.script(return_plan({**FULL_FACTS, "order_id": order}))
    what = "Scratch on the back of the left heel, noticed the first time I wore them indoors. Not repaired."
    reply = chat(client, headers, sid, SITE_TEXT.format(order=order, what=what))
    assert reply.status_code == 200 and reply.json()["next_action"] == "UPLOAD_PHOTOS", reply.text
    ids = {view: upload(client, headers, sid, view, (photos or {}).get(view)) for view in VIEWS}
    created = client.post("/api/returns", headers={**headers, "Idempotency-Key": "rk-" + uuid.uuid4().hex[:10]},
                          json={"session_id": sid, "order_id": order, "order_line": "1", "claim_type": "DEFECT_CLAIM",
                                "reason": "defect"})
    assert created.status_code == 200, created.text
    return sid, ids, created.json()["return_id"]


def evaluate(client, headers, return_id):
    return client.post(f"/api/returns/{return_id}/evaluate", headers=headers,
                       json={"client_request_id": "ev-" + uuid.uuid4().hex[:8]})


def sent_photos(call):
    """Photo id -> declared view, read from the text blocks the agent put before each image."""
    found = {}
    for text in texts(call):
        match = re.match(r"Photo (P-\d+) \(the customer says it shows: (\w+)\)", text)
        if match:
            found[match.group(1)] = match.group(2)
    return found


def observation(views_by_id, detected=None, defects=None, **overrides):
    detected = detected or {}
    return {"photos": [{"photo_id": pid, "detected_view": detected.get(pid, view), "looks_like_footwear": True,
                        "occlusion": "LOW", "sharpness": "PASS", "exposure": "PASS"}
                       for pid, view in views_by_id.items()],
            "defects": defects if defects is not None else [],
            "possible_structural_or_safety_issue": False, "text_photo_conflicts": [],
            "cannot_verify": ["cause of the scratch"], **overrides}


# ----------------------------------------------------------------- shopping

def test_shopping_preferences_are_validated_and_pairs_come_from_matching(bedrock):
    from backend.agent import prompts
    from backend.agent.preferences import Preferences

    client, c, model = bedrock
    headers = login(client, "cust-02")
    sid = new_session(client, headers, "shopping")
    model.script(use("search_inventory", PREFS), plan())
    response = chat(client, headers, sid, "Black ankle boots, US women's 7, under $100. Scratches fine, no stains.")
    body = response.json()
    assert response.status_code == 200 and frontend_checks().chat_problems(body) == []
    expected = [row["item_id"] for row, _ in c.inventory.find_for(Preferences(
        shoe_types=["Ankle boot"], size={"system": "US_WOMEN", "value": "7"}, max_price_cents=10000,
        accepted_defect_types={"scratch": "YES", "stain": "NO"}))]
    assert body["next_action"] == "SHOW_ITEMS" and [i["item_id"] for i in body["items"]] == expected == ["ITM-001"]
    assert body["analysis_source"] == "BEDROCK" and body["model_id"] == "test-model"
    assert body["items"][0]["match_status"] == "MATCH"
    first, second = model.calls
    assert first["system"] == prompts.SHOPPING
    assert first["tools"] == ["search_inventory", "get_item_details", "ShoppingPlan"]
    assert body["agent"]["tools_used"] == ["search_inventory"] and body["agent"]["model_calls"] == 2
    assert customer_prompt(first)["customer_message"].startswith("Black ankle boots")
    searched = json.loads(tool_results(second)[0]["content"][0]["text"])
    assert searched["count"] == 1 and searched["matching"][0]["item_id"] == "ITM-001"
    assert model.built == [("test-model", 1200, 25.0)]
    state = stored_session(c, sid)["state"]
    assert state["preferences"]["accepted_defect_types"] == {"scratch": "YES", "stain": "NO"}
    assert state["preferences"]["size"] == {"system": "US_WOMEN", "value": "7"}


def test_questions_are_dynamic_but_options_come_from_stock(bedrock):
    from backend.agent.preferences import Preferences, size_label

    client, c, model = bedrock
    headers = login(client, "cust-02")
    sid = new_session(client, headers, "shopping")
    model.script(use("search_inventory", {"shoe_types": BOOTS}), plan(
        {"shoe_types": BOOTS}, "A couple of quick questions.", show=False, questions=[
            {"question_id": "size", "text": "Which size do you wear?", "options": ["Size 99"]},
            {"question_id": "budget", "text": "What's your budget?", "options": ["Under CA$90", "Any price"]}]))
    body = chat(client, headers, sid, "I'd like some boots").json()
    assert body["next_action"] == "ASK_QUESTION" and [q["question_id"] for q in body["questions"]] == ["size", "budget"]
    in_stock = [size_label(s, v) for s, v in c.inventory.catalogue_facts(Preferences(shoe_types=BOOTS))["sizes"]]
    assert [o["label"] for o in body["questions"][0]["options"]] == in_stock and "Size 99" not in in_stock
    assert [o["label"] for o in body["questions"][1]["options"]] == ["Under CA$90", "Any price"]

    second = {"shoe_types": BOOTS, "size_system": "US_WOMEN", "size_value": "7", "any_price": True}
    model.script(use("search_inventory", second), plan(second, "Which imperfections are fine?", show=False, questions=[
        {"question_id": "size", "text": "Which size?"},  # already answered: dropped
        {"question_id": "defects", "text": "Which imperfections are OK for you?", "options": ["Holes"],
         "multi": True}]))
    body = chat(client, headers, sid, "US women's 7, any price").json()
    prompt = customer_prompt(model.calls[0])
    assert prompt["asked_questions"] == ["style", "size", "budget"]
    assert [q["question_id"] for q in prompt["questions_just_asked"]] == ["size", "budget"]
    assert prompt["current_preferences"]["shoe_types"] == BOOTS
    assert [q["question_id"] for q in body["questions"]] == ["defects"]
    assert body["questions"][0]["type"] == "multi_choice"
    assert [o["label"] for o in body["questions"][0]["options"]] == ["Scratches", "Scuffs", "Stains", "None of these"]


def test_nothing_in_stock_offers_to_relax_without_changing_preferences(bedrock):
    client, c, model = bedrock
    headers = login(client, "cust-02")
    sid = new_session(client, headers, "shopping")
    prefs = {**PREFS, "max_price_cents": 1000}
    model.script(use("search_inventory", prefs), plan(prefs, "Here you go!"))
    body = chat(client, headers, sid, "Ankle boots in 7 under $10, scratches ok, no stains").json()
    assert body["next_action"] == "ASK_QUESTION" and body["items"] == []
    assert body["message"].startswith("Nothing in stock fits")  # not the model's "Here you go!"
    assert body["questions"][0]["question_id"] == "relax" and "Under CA$10" in body["blocking_constraints"]
    assert stored_session(c, sid)["state"]["preferences"]["max_price_cents"] == 1000


def test_invalid_output_gets_exactly_one_repair_then_502(bedrock):
    client, c, model = bedrock
    headers = login(client, "cust-02")
    sid = new_session(client, headers, "shopping")
    model.script(use("search_inventory", PREFS), plan({"shoe_types": ["Boot"]}), plan())
    repaired = chat(client, headers, sid, "ankle boots")
    assert repaired.status_code == 200 and repaired.json()["items"][0]["item_id"] == "ITM-001"
    feedback = tool_results(model.calls[2])[-1]
    assert feedback["status"] == "error" and "Validation failed for ShoppingPlan" in feedback["content"][0]["text"]

    turns = stored_session(c, sid)["turns"]
    model.script(use("search_inventory", PREFS), plan({"shoe_types": ["Boot"]}), plan(items=["ITM-022"]),
                 plan())  # unknown style, then an injected field
    failed = chat(client, headers, sid, "ankle boots")
    assert failed.status_code == 502 and failed.json()["code"] == "AI_OUTPUT_INVALID"
    assert len(model.calls) == 3 and len(model.steps) == 1  # search, attempt, one repair; no third attempt
    assert stored_session(c, sid)["turns"] == turns  # nothing was saved


def test_a_plan_without_a_search_is_turned_back_once(bedrock):
    client, _, model = bedrock
    headers = login(client, "cust-02")
    sid = new_session(client, headers, "shopping")
    model.script(plan(), use("search_inventory", PREFS), plan())
    body = chat(client, headers, sid, "Black ankle boots, US women's 7, under $100").json()
    assert body["next_action"] == "SHOW_ITEMS" and [i["item_id"] for i in body["items"]] == ["ITM-001"]
    note = tool_results(model.calls[1])[-1]
    assert note["status"] == "error" and "Call search_inventory" in note["content"][0]["text"]
    assert body["agent"]["tools_used"] == ["search_inventory"] and len(model.calls) == 3


def test_nova_gets_the_documented_schema_subset():
    from strands.tools.structured_output import convert_pydantic_to_tool_spec

    from backend.agent.providers import NOVA_SCHEMA_KEYS, nova_schema
    from backend.agent.strands_agent import ShoppingPlan, observation_model

    def keys(node, found):  # every schema keyword used below the top level (property names excluded)
        for key, value in node.items():
            found.add(key)
            for sub in (value.values() if key == "properties" else [value] if key == "items" else []):
                keys(sub, found)
        return found

    for model in (ShoppingPlan, observation_model(["P-1", "P-2"])):
        reduced = nova_schema(convert_pydantic_to_tool_spec(model)["inputSchema"]["json"])
        assert set(reduced) <= {"type", "properties", "required"} and reduced["type"] == "object"
        nested = set().union(*(keys(sub, set()) for sub in reduced["properties"].values()))
        assert nested <= NOVA_SCHEMA_KEYS, nested - NOVA_SCHEMA_KEYS


@pytest.mark.parametrize("error,status,code,cause", [
    (ModelThrottledException("Too many requests"), 429, "RATE_LIMITED", None),
    (NoCredentialsError(), 503, "AI_UNAVAILABLE", "no usable AWS credentials"),
    (ClientError({"Error": {"Code": "AccessDeniedException", "Message": "User: arn:aws:iam::123456789012:user/demo "
                            "is not authorized to perform: bedrock:InvokeModel"}}, "Converse"),
     503, "AI_UNAVAILABLE", "access to the Bedrock model was denied"),
    (ClientError({"Error": {"Code": "ValidationException", "Message": "The provided model identifier is invalid."}},
                 "Converse"), 503, "AI_UNAVAILABLE", "check BEDROCK_MODEL_ID"),
    (ReadTimeoutError(endpoint_url="https://bedrock-runtime.ca-central-1.amazonaws.com"), 504, "AGENT_TIMEOUT", None),
])
def test_model_failures_map_to_the_contract_and_never_reach_the_mock(bedrock, error, status, code, cause):
    client, c, model = bedrock
    headers = login(client, "cust-02")
    sid = new_session(client, headers, "shopping")
    model.script(error)
    response = chat(client, headers, sid, "ankle boots")
    body = response.json()
    assert response.status_code == status and body["code"] == code and frontend_checks().error_problems(body) == []
    if cause:
        assert cause in body["detail"]["cause"]
    assert "123456789012" not in response.text  # AWS messages (account ids, ARNs) stay in the server log
    assert len(model.calls) == 1 and stored_session(c, sid)["turns"] == 0


def test_model_calls_per_message_are_capped(bedrock):
    from backend.agent.strands_agent import MAX_TOOL_CALLS, TURNS

    client, _, model = bedrock
    headers = login(client, "cust-02")
    sid = new_session(client, headers, "shopping")
    model.script(*[use("search_inventory", {"shoe_types": ["Sneaker"]})] * 10)
    response = chat(client, headers, sid, "sneakers")
    assert response.status_code == 502 and response.json()["code"] == "AI_OUTPUT_INVALID"
    assert len(model.calls) == TURNS["shopping"]
    results = tool_results(model.calls[-1])
    assert [r["status"] for r in results] == ["success"] * MAX_TOOL_CALLS + ["error"] * (len(results) - MAX_TOOL_CALLS)


def test_a_slow_model_hits_the_turn_deadline(ddb, image_root):
    from backend.agent.base import AgentFailure
    from backend.agent.strands_agent import BedrockAgent
    from backend.config import load_settings

    model = Scripted()
    agent = BedrockAgent(load_settings(bedrock_env(ddb, image_root, AGENT_TURN_TIMEOUT_S="0.2")), model.factory)
    model.script(slow(0.6, plan()))
    with pytest.raises(AgentFailure) as failure:
        agent.shopping_turn({}, "ankle boots", inventory=None)
    assert failure.value.status == 504 and failure.value.code == "AGENT_TIMEOUT"
    assert model.built[0][2] == 0.2  # the read timeout never exceeds the deadline


# ----------------------------------------------------------------- returns

def test_return_order_ids_are_verified_by_the_backend(bedrock):
    from backend.agent import prompts

    client, c, model = bedrock
    owner, other = login(client, "cust-01"), login(client, "cust-02")
    sid = new_session(client, owner, "return")
    model.script(return_plan(FULL_FACTS))  # the model "found" an order the customer never typed
    body = chat(client, owner, sid, "My boots are falling apart. I wore them indoors once, never repaired.").json()
    assert body["next_action"] == "ASK_QUESTION" and [q["question_id"] for q in body["questions"]] == ["order"]
    assert body["return_status"] == "INFO_COLLECTION" and "order_id" not in stored_session(c, sid)["state"]["facts"]
    assert model.calls[0]["system"] == prompts.RETURN
    assert model.calls[0]["tools"] == ["lookup_demo_order", "get_return_policy", "ReturnPlan"]

    model.script(use("lookup_demo_order", {"order_id": "ORD-001"}), return_plan(FULL_FACTS))
    body = chat(client, owner, sid, "Sorry, it's ORD-001").json()
    assert body["next_action"] == "UPLOAD_PHOTOS" and [p["view"] for p in body["required_photos"]] == VIEWS
    looked_up = json.loads(tool_results(model.calls[1])[0]["content"][0]["text"])
    assert looked_up["found"] is True and looked_up["order"]["lines"][0]["product_name"] == "Black Ankle Boots"
    assert frontend_checks().chat_problems(body) == []

    sid2 = new_session(client, other, "return")
    model.script(use("lookup_demo_order", {"order_id": "ORD-001"}), return_plan(FULL_FACTS))
    body = chat(client, other, sid2, "It's ORD-001, worn indoors, first wear, not repaired").json()
    assert body["next_action"] == "ASK_QUESTION" and "couldn't find demo order ORD-001" in body["message"]
    assert json.loads(tool_results(model.calls[1])[0]["content"][0]["text"])["found"] is False  # owner-checked


def test_return_questions_cover_only_missing_facts_and_keep_the_customers_words(bedrock):
    client, c, model = bedrock
    headers = login(client, "cust-01")
    sid = new_session(client, headers, "return")
    model.script(return_plan({"order_id": "ORD-002", "claim_type": "DEFECT_CLAIM", "description": "Heel damage."},
                             ready=True, reply="When did this start and were they worn?", questions=[
                                 {"question_id": "worn", "text": "Have you worn them outside?",
                                  "options": ["Not worn", "Indoors", "Outdoors"]}]))
    body = chat(client, headers, sid, SITE_TEXT.format(order="ORD-002", what="The heel tip is scratched.")).json()
    assert body["next_action"] == "ASK_QUESTION"  # not ready: worn, repaired and onset are unknown
    assert [(q["question_id"], q["text"]) for q in body["questions"]] == [("worn", "Have you worn them outside?")]
    facts = stored_session(c, sid)["state"]["facts"]
    assert facts["description"] == "The heel tip is scratched." and facts["order_id"] == "ORD-002"

    model.script(return_plan({"worn": "OUTDOORS"}, ready=False, reply="Got it.", questions=[
        {"question_id": "other", "text": "Anything else?"}]))
    body = chat(client, headers, sid, "Outdoors").json()
    assert [q["question_id"] for q in body["questions"]] == ["repaired", "onset"]  # the backend asks what's missing
    assert stored_session(c, sid)["state"]["facts"]["worn"] == "OUTDOORS"


# ----------------------------------------------------------------- photos (multimodal)

def test_bedrock_photo_review_approves_and_registers_one_unit(bedrock):
    from backend.agent import prompts

    client, c, model = bedrock
    headers = login(client, "cust-01")
    _, ids, return_id = ready_return(client, headers, model)

    def look(call):
        sent = sent_photos(call)
        assert sent == {pid: view for view, pid in ids.items()}
        assert len(images(call)) == 7 and all(img[:2] == b"\xff\xd8" for img in images(call))
        assert call["system"] == prompts.VISION and call["tools"] == ["VisionObservation"]
        return use("VisionObservation", observation(sent, defects=[{
            "type": "scratch", "location": "back of the left heel", "severity": "A",
            "description": "Short light scratch on the heel counter", "photo_ids": [ids["defect_closeup"]],
            "uncertainty": "low"}]))

    model.script(look)
    response = evaluate(client, headers, return_id)
    body = response.json()
    assert response.status_code == 200 and body["status"] == "APPROVED_DEMO", body
    assert body["analysis_source"] == "BEDROCK" and body["model_id"] == "test-vision-model"
    assert body["inventory_status"] == "AWAITING_RECEIPT" and frontend_checks().return_problems(body) == []
    assert model.built == [("test-vision-model", 2000, 50.0)]
    unit = c.repo.get(("INV", "ITEM#" + body["item_id"]))
    assert unit["defects"][0]["verified_by"] == "AI_OBSERVATION" and unit["defects"][0]["severity"] == "A"
    assert evaluate(client, headers, return_id).json()["item_id"] == body["item_id"]  # decided once: no new call
    assert len(model.calls) == 1


def test_coverage_comes_from_what_the_model_saw_and_it_cannot_pass_a_blurry_photo(bedrock):
    client, _, model = bedrock
    headers = login(client, "cust-01")
    _, ids, return_id = ready_return(client, headers, model, photos={"pair_soles": shoe_photo("pair_soles",
                                                                                              blurry=True)})
    model.script(lambda call: use("VisionObservation", observation(
        sent_photos(call), detected={ids["pair_outer"]: "pair_top"})))  # and it calls the blurry photo sharp
    body = evaluate(client, headers, return_id).json()
    missing = {m["view"]: m["reason"] for m in body["missing_views"]}
    assert body["status"] == "NEEDS_MORE_EVIDENCE" and set(missing) == {"pair_outer", "pair_soles"}
    assert "blurry" in missing["pair_soles"]  # our measurement wins over the model's PASS


@pytest.mark.parametrize("bad", ["unknown_photo", "missing_field"])
def test_invalid_vision_output_goes_to_human_review(bedrock, bad):
    client, c, model = bedrock
    headers = login(client, "cust-01")
    _, ids, return_id = ready_return(client, headers, model)

    def broken(call):
        sent = sent_photos(call)
        if bad == "unknown_photo":
            return use("VisionObservation", observation({**sent, "P-99999": "pair_top"}))
        payload = observation(sent)
        payload.pop("possible_structural_or_safety_issue")
        return use("VisionObservation", payload)

    model.script(broken, broken)
    response = evaluate(client, headers, return_id)
    assert response.status_code == 502 and response.json()["code"] == "AI_OUTPUT_INVALID"
    assert len(model.calls) == 2
    after = client.get(f"/api/returns/{return_id}", headers=headers).json()
    assert after["status"] == "HUMAN_REVIEW_REQUIRED"
    assert not [r for r in c.repo.query("INV", "ITEM#") if r.get("source_return_id") == return_id]


def test_vision_failures_leave_the_return_unchanged(bedrock):
    client, _, model = bedrock
    headers = login(client, "cust-01")
    _, _, return_id = ready_return(client, headers, model)
    model.script(NoCredentialsError())
    response = evaluate(client, headers, return_id)
    assert response.status_code == 503 and response.json()["code"] == "AI_UNAVAILABLE"
    assert client.get(f"/api/returns/{return_id}", headers=headers).json()["status"] == "INFO_COLLECTION"


def test_merge_rejects_unknown_photos_and_flags_duplicates():
    from backend.agent.base import AgentFailure
    from backend.agent.strands_agent import VisionObservation, merge, observation_model

    ok = {"sharpness": "PASS", "exposure": "PASS", "resolution": "PASS"}
    photos = [{"photo_id": "P-1", "declared_view": "pair_top", "dhash": "f" * 64, "verdict": ok},
              {"photo_id": "P-2", "declared_view": "pair_outer", "dhash": "f" * 63 + "e", "verdict": ok}]
    seen = VisionObservation(**observation({"P-1": "pair_top", "P-2": "pair_outer"}))
    result = merge(seen, photos, ["pair_top", "pair_outer"], {"quality": {"duplicate_max_distance": 12}})
    assert result.covered_views == ["pair_top"] and result.photo_quality[1].duplicate_of == "P-1"
    with pytest.raises(AgentFailure):
        merge(VisionObservation(**observation({"P-9": "pair_top"})), photos, ["pair_top"], {})
    schema = observation_model(["P-1", "P-2"])
    with pytest.raises(ValueError):
        schema(**observation({"P-1": "pair_top"}))  # every photo must be reported
    with pytest.raises(ValueError):
        schema(**observation({"P-1": "pair_top", "P-2": "pair_outer"}, defects=[{
            "type": "scuff", "location": "toe", "severity": "A", "description": "x", "photo_ids": ["P-7"],
            "uncertainty": "low"}]))


# ----------------------------------------------------------------- configuration

def test_health_reports_the_bedrock_provider(bedrock):
    client, _, _ = bedrock
    body = client.get("/health").json()
    assert body["mode"] == "bedrock-local"
    assert body["agent"] == {"configured": True, "provider": "bedrock", "model_id": "test-model",
                             "vision_model_id": "test-vision-model"}


def test_real_bedrock_model_is_built_for_low_cost_and_no_retries(ddb, image_root):
    from backend.agent.strands_agent import BedrockAgent
    from backend.config import load_settings

    settings = load_settings(bedrock_env(ddb, image_root, BEDROCK_REGION="us-east-1"))
    built = BedrockAgent(settings)._bedrock_model("us.amazon.nova-2-lite-v1:0", 900, 20.0)  # no request is made
    assert built.config["model_id"] == "us.amazon.nova-2-lite-v1:0" and built.config["temperature"] == 0.0
    assert built.config["max_tokens"] == 900 and built.config["streaming"] is False
    meta = built.client.meta
    assert meta.region_name == "us-east-1" and meta.config.read_timeout == 20.0
    assert meta.config.retries == {"total_max_attempts": 1, "mode": "standard"}


def test_connectivity_check_uses_the_production_path(ddb, image_root):
    from backend.agent.strands_agent import BedrockAgent
    from backend.config import load_settings

    model = Scripted()
    agent = BedrockAgent(load_settings(bedrock_env(ddb, image_root)), model.factory)
    model.script(say("OK!"), use("ConnectivityCheck", {"ok": True, "shoes_visible": True}))  # text first: forced
    assert agent.check(with_image=True).model_dump() == {"ok": True, "shoes_visible": True}
    assert model.built == [("test-vision-model", 100, 25.0)] and len(images(model.calls[0])) == 1
    assert model.calls[1]["tool_choice"] == {"tool": {"name": "ConnectivityCheck"}}


def test_jpeg_sent_to_the_model_is_downscaled():
    from PIL import Image

    from backend.agent.strands_agent import IMAGE_SIDE, _jpeg_for_model

    big = io.BytesIO()
    Image.new("RGB", (3000, 2000), "white").save(big, "PNG")
    with Image.open(io.BytesIO(_jpeg_for_model(big.getvalue()))) as out:
        assert out.format == "JPEG" and max(out.size) == IMAGE_SIDE
